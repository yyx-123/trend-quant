"""可交易性标注（详设 §3.2）：停牌 / 涨停 / 跌停 逐日逐标的推导。

推导规则（不需要额外数据源）：

- 停牌：当日是交易日但无 bar → suspended=True；
- 涨跌停价：**除权基准修正后**的前收盘价 × 板块幅度——
  基准价 = raw close(t-1)；若 t 是**除权除息日**（= ex_factors 存储日 E
  之后的**第一根该标的 bar**），基准价 = raw close(t-1) / f_t。
  口径依据（实证修正）：本项目因子语义为
  qfq(t) = raw(t) / Π_{ex_date≥t} f（core/adjustment.py），该语义要求
  「存储日 E 的前收 ÷ f = E+1 的价格」才是无断裂的复权序列——即 E 是
  权益登记日（除权前最后一根 bar），价格实际在 E+1 跳水。真实库
  107/107 无歧义样本的跌幅落在 E+1，落在 E 的为 0；
  - 主板（.SS 60xxxx、.SZ 00xxxx）±10%；
  - 科创板（.SS 68xxxx）、创业板（.SZ 30xxxx）±20%；
  - ETF 跟随其**标的板块**（GLM53F-P1-1）：沪 588xxx、及名称含"创业板/
    科创"的深市 ETF（如 159915）±20%，其余 ETF ±10%；
  - ST ±5%（阶段 7 前无 ST 状态历史 → st_status=unknown 按主板规则处理，
    报告注明）；
  - 新股上市初期无涨跌幅限制的天数分板块/分时代（GLM53F-P2-17）：注册制
    （科创全程/创业板 2020-08-24 起/主板 2023-04-10 起）前 5 个交易日；
    旧规仅首日（首日 44% 上限按 no_limit 近似）；ETF 上市首日即有限制；
  - 价格按最小变动单位四舍五入（ROUND_HALF_UP，交易所口径）：**ETF 0.001**、
    股票 0.01（见 `_TICK_ETF`；按 0.01 舍入 ETF 会产出假涨停/假跌停）；
- 一字板近似：is_limit_up 用收盘价判定（尾盘口径下，收盘封板 ≈ 尾盘不可买，
  与决策 1 口径自洽）。
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd

from core.calendar import is_trading_day
from core.symbols import symbol_suffix, symbol_to_code

# 板块幅度规则（详设 §3.2）
_LIMIT_MAIN = 0.10
_LIMIT_CHINEXT_STAR = 0.20
_IPO_NO_LIMIT_DAYS = 5  # 注册制新股上市初期无涨跌幅限制的交易日数（近似）
# 最小变动单位：沪深 ETF 报价到 0.001（价格第 3 位小数），股票/主板到 0.01。
# 生产库实证：2024-01 起 ETF 的 close 第 3 位小数 0~9 均匀分布（各约 1.15 万条），
# 同期股票 close 第 3 位恒为 0。按 0.01 舍入 ETF 限价会把涨停价压低、
# 跌停价抬高 → 假涨停（买不进）/假跌停（卖不掉、盘中止损被阻塞）。
_TICK_ETF = 0.001
_TICK_STOCK = 0.01
# 创业板注册制改革：该日（含）起涨跌停幅度 ±20%，此前 ±10%（R15B-P1-1）
_CHINEXT_20PCT_SINCE = date(2020, 8, 24)


def board_limit_pct(symbol: str, *, asset_type: str | None = None,
                    name: str | None = None, as_of: date | None = None) -> float:
    """按代码板块归属返回涨跌停幅度（不含 ST 修正）。

    GLM53F-P1-1：ETF 的涨跌幅跟随其**标的板块**，不能一律按主板 ±10%——
    科创板 ETF（沪 588xxx）±20%；深市跟踪创业板/科创板指数的 ETF（名称含
    "创业板"/"创业"/"科创"，如 159915 创业板ETF、159814 创业大盘ETF）±20%；
    其余 ETF ±10%。名称关键词含"创业"（而非只有"创业板"）：生产库 enabled 池里
    名称含"创业"的 10 只 ETF 跟踪的都是创业板系指数，实测日内幅度上限 0.2005
    （159814.SZ 2024-09-30：前收 0.364 → 涨停 0.437），按 ±10% 会产出 4 天假信号。
    asset_type 缺省时按股票代码前缀规则（历史口径）。

    **日期维度（R15B-P1-1）**：创业板的 ±20% 是 2020-08-24 注册制改革才有的——
    此前创业板（30xxxx 股票与其跟踪 ETF）是 ±10%。旧实现把 30xxxx 无条件当
    ±20%，导致 2015-01-01~2020-08-21 期间**真实涨停/跌停日被判为可成交**
    （生产库实测 13 笔不可达成交：4 笔买在真涨停收盘、9 笔卖在真跌停收盘；
    1444 个 symbol-day 受影响）。`as_of=None` 表示**当前口径**（=20%），
    历史回放必须逐日传当日日期。科创板（688/588）自 2019-07-22 开板即 20%，
    无此分界。
    """
    suffix = symbol_suffix(symbol)
    code = symbol_to_code(symbol)
    # 创业板注册制改革日：该日（含）起 ±20%，之前 ±10%
    chin_next_20 = as_of is None or as_of >= _CHINEXT_20PCT_SINCE

    def _chin_next() -> float:
        return _LIMIT_CHINEXT_STAR if chin_next_20 else _LIMIT_MAIN

    if asset_type == "etf":
        if suffix == "SS" and code.startswith("588"):
            return _LIMIT_CHINEXT_STAR  # 科创板 ETF（开板即 20%）
        if name and "科创" in name:
            return _LIMIT_CHINEXT_STAR  # 科创板系 ETF
        if name and ("创业板" in name or "创业" in name):
            return _chin_next()  # 创业板系 ETF：随改革日切换
        return _LIMIT_MAIN
    if suffix == "SS" and code.startswith("68"):
        return _LIMIT_CHINEXT_STAR  # 科创板
    if suffix == "SZ" and code.startswith("30"):
        return _chin_next()  # 创业板：2020-08-24 起 20%
    return _LIMIT_MAIN


def _ipo_no_limit_days(symbol: str, asset_type: str | None, listing_day: date) -> int:
    """新股上市初期无涨跌幅限制的交易日数（GLM53F-P2-17 分板块/分时代口径）。

    - ETF：上市首日即有涨跌幅限制（以前收为基准）→ 0；
    - 科创板（68xxxx）：注册制自始至终 → 前 5 个交易日；
    - 创业板（30xxxx）：2020-08-24 注册制起前 5 日；此前旧规仅首日
      （首日 44% 上限无法用 ±pct 框架表达，按首日 no_limit 近似）；
    - 主板：2023-04-10 全面注册制起前 5 日；此前仅首日（同上近似）。
    """
    if asset_type == "etf":
        return 0
    suffix = symbol_suffix(symbol)
    code = symbol_to_code(symbol)
    if suffix == "SS" and code.startswith("68"):
        return _IPO_NO_LIMIT_DAYS
    if suffix == "SZ" and code.startswith("30"):
        return _IPO_NO_LIMIT_DAYS if listing_day >= date(2020, 8, 24) else 1
    return _IPO_NO_LIMIT_DAYS if listing_day >= date(2023, 4, 10) else 1


def _tick_for(asset_type: str | None) -> float:
    """最小变动单位：ETF 0.001，其余 0.01（见 _TICK_ETF 注释）。"""
    return _TICK_ETF if asset_type == "etf" else _TICK_STOCK


def compute_tradability(
    db,
    *,
    symbols: list[str],
    dates: list[date],
    raw_closes: dict[str, pd.Series] | None = None,
    ex_factors: dict[str, list[tuple]] | None = None,
    listing_dates: dict[str, str] | None = None,
    asset_info: dict[str, dict] | None = None,
    live_bars: dict[str, float] | None = None,
    live_bar_day: date | None = None,
) -> pd.DataFrame:
    """逐 (date, symbol) 推导可交易性。返回 DataFrame：

    列：date, symbol, suspended, limit_up_price, limit_down_price,
        is_limit_up, is_limit_down, st_status, no_limit
    （无 bar 的非交易日不出现在结果中——回测日循环本就不会走到非交易日。）

    raw_closes: {symbol: Series(index=date, raw close)}——缺省时从 L1 现取；
    ex_factors: {symbol: [(day_str, factor)]}——缺省时从 L1 现取；
    listing_dates: {symbol: 'YYYY-MM-DD'}——缺省时读 instrument_metadata；
    asset_info: {symbol: {asset_type, name}}——缺省时读 instrument_metadata
    （ETF 幅度与新股豁免天数依赖它，GLM53F-P1-1/P2-17）。
    live_bars: {symbol: as_of 当日盘中价}——**仅**在 ``live_bar_day``（须落在
    请求的 dates 内）上并入，用于 live 模式：当日 EOD bar 尚未落库
    （16:30 才写），不并入会让当日 ``suspended=True``（loop-review-ds4f
    14:00 实盘清单的所有标的都被标成"停牌"、涨跌停标记永不置位）。
    该参数只对 ``live_bar_day`` 生效，无法用于注入历史/未来任意日行情。
    """
    symbols = [str(s or "").strip().upper() for s in symbols if str(s or "").strip()]
    symbols = list(dict.fromkeys(symbols))
    dates = sorted({pd.Timestamp(d).date() for d in dates})
    if not symbols or not dates:
        return _empty_frame()

    if raw_closes is None:
        # 垫片 250 自然日（GLM53F-P2-16）：窗口起点前长期停牌的标的，复牌日
        # 前收必须能从垫片期算出——30 日垫片对停牌 >30 天的标的 fail-open
        pad = pd.Timestamp(min(dates)) - pd.Timedelta(days=250)
        raw_closes = _load_raw_closes(db, symbols, pad.date(), max(dates))
    # live 模式并入盘中合成 bar（见 docstring；只在 live_bar_day 生效）
    if live_bars and live_bar_day is not None:
        _live_day = pd.Timestamp(live_bar_day).date()
        if _live_day in set(dates):
            raw_closes = dict(raw_closes)
            for _sym, _close in live_bars.items():
                try:
                    value = float(_close)
                except (TypeError, ValueError):
                    continue
                if not np.isfinite(value) or value <= 0:
                    continue
                key = str(_sym or "").strip().upper()
                ser = raw_closes.get(key)
                # **只在库里确实没有该日 bar 时**才并入（实证：旧写法
                # 会覆盖真实 bar——注入 10.01 即可把真实的 is_limit_up=True
                # 翻成 False）。live 的用途本就是"EOD bar 尚未落库"，一旦
                # 落库就该以库为准。
                if ser is not None and _live_day in {
                    pd.Timestamp(d).date() for d in ser.index
                }:
                    continue
                if ser is None:
                    raw_closes[key] = pd.Series({_live_day: value})
                else:
                    ser = ser.copy()
                    ser.loc[_live_day] = value
                    raw_closes[key] = ser
    if ex_factors is None:
        ex_factors = {s: db.load_ex_factors(s) for s in symbols}
    if listing_dates is None or asset_info is None:
        if db is None:
            # 纯内存调用形态（测试/探针）：缺什么补空，不触库
            listing_dates = listing_dates if listing_dates is not None else {}
            asset_info = asset_info if asset_info is not None else {}
        else:
            meta = db.get_instrument_metadata_map()
            if listing_dates is None:
                listing_dates = {
                    s: (meta.get(s) or {}).get("start_date") for s in symbols
                }
            if asset_info is None:
                asset_info = {
                    s: {"asset_type": (meta.get(s) or {}).get("asset_type"),
                        "name": (meta.get(s) or {}).get("name")}
                    for s in symbols
                }

    # 交易日判定按日期一次算好；逐标的向量化推导（numpy/pandas，无逐日 Python 循环）。
    trading_day_cache = {day: is_trading_day(day) for day in dates}

    # 新股"上市起 N 个交易日"按**交易日历**（而非传入窗口）计算：
    # 上市日早于窗口起点时，窗口内 searchsorted 退化为"窗口内第几天"，
    # 每次 run 前 4 个交易日的涨跌停卡控会失效（评审 K3-P2-1/DS-P1-1 实证）。
    _listing_days = [
        pd.Timestamp(v).date() for v in listing_dates.values() if v
    ]
    cal_start = min([min(dates), *_listing_days]) if _listing_days else min(dates)
    cal_ordinals = np.array(
        [d.toordinal() for d in pd.date_range(cal_start, max(dates)).date
         if is_trading_day(d)],
        dtype=np.int64,
    )
    is_trading_arr = np.array([trading_day_cache[d] for d in dates])
    day_ordinals = np.array([d.toordinal() for d in dates], dtype=np.int64)

    def _round_tick_vec(x: np.ndarray, tick: float) -> np.ndarray:
        # 按最小变动单位四舍五入（ROUND_HALF_UP 语义；eps 抵消二进制浮点误差）
        scale = 1.0 / tick
        return np.floor(x * scale + 0.5 + 1e-9) / scale

    rows: list[dict] = []
    for symbol in symbols:
        closes = raw_closes.get(symbol)
        info = (asset_info or {}).get(symbol) or {}
        asset_type = str(info.get("asset_type") or "").strip() or None
        limit_pct = board_limit_pct(symbol, asset_type=asset_type, name=info.get("name"))
        # 幅度分时代（R15B-P1-1）：创业板系在 2020-08-24 前是 ±10%。逐日选口径——
        # 其它板块两值相同（与旧行为逐位一致）。
        _limit_pct_early = board_limit_pct(
            symbol, asset_type=asset_type, name=info.get("name"),
            as_of=date(2020, 8, 23),
        )
        limit_pct_arr = np.full(len(dates), limit_pct, dtype=float)
        if _limit_pct_early != limit_pct:
            limit_pct_arr[day_ordinals < _CHINEXT_20PCT_SINCE.toordinal()] = _limit_pct_early
        listing = listing_dates.get(symbol)
        listing_day = pd.Timestamp(listing).date() if listing else None
        # 上市日来源问题**未在本轮改行为**——
        # `instrument_metadata.start_date` 是用户/导入写入的字段，其语义
        # （上市日 vs 回填起点）无法从代码判定，且生产库 874/874 行为 NULL
        # （= 新股无涨跌幅限制这条口径在生产库上当前**完全不生效**，属数据缺口
        # 而非代码 bug）；若把它当回填起点则会把"新股前 N 日无限制"整段关掉、
        # 若当上市日就是现在这样。两种口径的取舍需数据侧裁决（见 R4-D-2）。
        # 本轮只做**可见化**：把"上市日未知"如实标在结果里，不再静默。
        listing_known = listing_day is not None

        # 对齐轴 = closes 日期 ∪ 运行日期——运行窗口首日前收必须能从垫片期
        # bar 算出（ffill+shift 在扩展轴上做，再取运行日子集）。
        ser = None
        if closes is not None and len(closes):
            ser = closes.copy()
            ser.index = [pd.Timestamp(d).date() for d in ser.index]
            ser = ser[~ser.index.duplicated(keep="last")].sort_index()
        if ser is not None:
            axis_days = sorted(set(ser.index) | set(dates))
        else:
            axis_days = list(dates)
        ax_i = {d: i for i, d in enumerate(axis_days)}
        run_idx = np.array([ax_i[d] for d in dates])

        aligned = pd.Series(np.nan, index=pd.Index(axis_days), dtype=float)
        if ser is not None:
            aligned.update(ser)
        close_ax = aligned.to_numpy(dtype=float)

        # 前一交易日收盘 = 最近可得 bar（ffill 后 shift 一日；停牌日沿用前收）
        filled_ax = pd.Series(close_ax).ffill().to_numpy()
        prev_ax = np.empty(len(axis_days), dtype=float)
        prev_ax[0] = np.nan
        prev_ax[1:] = filled_ax[:-1]
        prev = prev_ax[run_idx]
        close_v = close_ax[run_idx]
        has_bar = np.isfinite(close_v)
        suspended = is_trading_arr & ~has_bar

        # 除权基准修正：因子存储日 E 是**除权前**
        # 的最后一根 bar（权益登记日口径，与 core/adjustment.py 的
        # qfq(t)=raw(t)/Π_{ex_date≥t}f 语义自洽）——raw 价格实际在 E 之后的
        # **第一根该标的 bar**（除权除息日）跳水。交易所参考前收只在那一根
        # bar 上做 ÷f 修正；其余 bar 的基准价就是最近可得前收。
        # 真实库实证：factor ≥ 1.15 的 107 个无歧义样本，跌幅 107/107 落在
        # E 的后一根 bar（落在 E 的 0 个）。此前的"按 E 当日生效"会在 E 日
        # 产出**假涨停**（f ≥ 1.1 时收盘 ≥ 被压低的涨停价）→ 买不进；在
        # E+1 日产出**假跌停**（f ≥ 1/0.9 时收盘 ≤ 被抬高的跌停价）→ 卖不掉
        # 且盘中止损被阻塞。enabled 池 274 只标的 / 932 个交易日受影响。
        f_ax = np.ones(len(axis_days), dtype=float)
        tick = _tick_for(asset_type)
        if ser is not None:
            bar_pos = np.flatnonzero(np.isfinite(close_ax))
            if bar_pos.size:
                bar_ord = np.array(
                    [axis_days[int(j)].toordinal() for j in bar_pos], dtype=np.int64
                )
                for day_raw, factor in (ex_factors.get(symbol) or []):
                    try:
                        ex_ord = pd.Timestamp(str(day_raw)[:10]).date().toordinal()
                        f = float(factor)
                    except (TypeError, ValueError):
                        continue
                    # 脏因子守卫：非有限/≤0 直接跳过（否则 limit_up=0.0 且
                    # is_limit_up=True 这类"像真值的坏数字"）。
                    if not np.isfinite(f) or f <= 0:
                        continue
                    # 第一根「日期严格晚于 E 且有 bar」的轴日 = 除权除息日。
                    # **累乘**而非覆盖（残留）：停牌跨越两个除权日时
                    # 两个因子会落到同一根 bar 上，只取后者会让该日基准价
                    # 偏高、产出假跌停（002129.SZ 真实库有 1 例，差异 0.24%）。
                    k = int(np.searchsorted(bar_ord, ex_ord, side="right"))
                    if k < bar_pos.size:
                        pos = int(bar_pos[k])
                        # **与观测跳变互证**（两层判据）：
                        # ① 经验合法带之外（[0.05, 20]，生产库实测 f∈[0.2, 9.97]）
                        #    直接视为脏因子——f=1e3 会产出 limit_up=0.001 且
                        #    is_limit_up=True，f=1e12 同形（R12A-F2 实证）；
                        # ② 带内但跳变**严重**不一致（|log cum − log implied| > 0.6，
                        #    因子与价格序列差 1.8 倍以上）跳过——基准价被压低同样会
                        #    伪造涨停。implied = raw(上一根有 bar)/raw(该根)；
                        #    **cum 是累乘后**的因子（停牌跨越多个除权日时两个因子落到
                        #    同一根 bar，观测跳变只与乘积对应，逐因子比对会误杀第二个）。
                        # 容差刻意放很松：生产库 10,177 条真实因子里只有 2 条
                        # （2005/2008 年的两只股票，其 raw 序列本身自相矛盾）会被拦下，
                        # 其余除权/折算全部照常生效；小额分红在分位舍入下不可测，
                        # 故不要求精确匹配。
                        if not (0.05 <= f <= 20):
                            continue
                        cum = float(f_ax[pos]) * f
                        prev_pos = int(bar_pos[k - 1]) if k > 0 else None
                        if prev_pos is not None:
                            raw_prev = float(close_ax[prev_pos])
                            raw_bar = float(close_ax[pos])
                            if raw_prev > 0 and raw_bar > 0:
                                implied = raw_prev / raw_bar
                                if abs(math.log(cum) - math.log(implied)) > max(
                                    0.6, 3.0 * tick / raw_prev
                                ):
                                    continue  # 与价格序列严重不一致 → 不动基准价
                        f_ax[pos] *= f
        f_t = f_ax[run_idx]

        # 新股上市初期无涨跌幅限制（天数分板块/分时代，GLM53F-P2-17）
        no_limit = np.zeros(len(dates), dtype=bool)
        if listing_day is not None:
            n_free = _ipo_no_limit_days(symbol, asset_type, listing_day)
            if n_free > 0:
                elapsed = np.searchsorted(
                    cal_ordinals, day_ordinals, side="right"
                ) - np.searchsorted(cal_ordinals, listing_day.toordinal(), side="right")
                no_limit = (day_ordinals >= listing_day.toordinal()) & (elapsed < n_free)

        with np.errstate(all="ignore"):
            base = prev / f_t
            valid = np.isfinite(base) & (base > 0) & ~no_limit
            limit_up = np.where(valid, _round_tick_vec(base * (1.0 + limit_pct_arr), tick), np.nan)
            limit_down = np.where(valid, _round_tick_vec(base * (1.0 - limit_pct_arr), tick), np.nan)
            close_rounded = _round_tick_vec(close_v, tick)
            # 价格必须为正才参与比较：close<=0 是坏数据，不得据此产出
            # is_limit_down=True（0 <= 跌停价恒真）这类"像真值的坏数字"。
            close_ok = has_bar & (close_v > 0)
            is_limit_up = close_ok & valid & (close_rounded >= limit_up)
            is_limit_down = close_ok & valid & (close_rounded <= limit_down)

        for i, day in enumerate(dates):
            if not is_trading_arr[i]:
                continue
            rows.append(
                {
                    "date": day,
                    "symbol": symbol,
                    "suspended": bool(suspended[i]),
                    "limit_up_price": float(limit_up[i]) if np.isfinite(limit_up[i]) else None,
                    "limit_down_price": float(limit_down[i]) if np.isfinite(limit_down[i]) else None,
                    "is_limit_up": bool(is_limit_up[i]),
                    "is_limit_down": bool(is_limit_down[i]),
                    # 上市日未知时"新股无涨跌幅限制"这条不生效——如实标注
                    "listing_known": bool(listing_known),
                    "st_status": "unknown",  # 阶段 7 前无 ST 状态历史
                    "no_limit": bool(no_limit[i]),
                }
            )

    return pd.DataFrame(rows)


def _load_raw_closes(db, symbols: list[str], start=None, end=None) -> dict[str, pd.Series]:
    frames = db.load_market_data_window_many(symbols, start, end, price_mode="raw", period="1d")
    out: dict[str, pd.Series] = {}
    for symbol, df in frames.items():
        if df is None or df.empty:
            continue
        days = pd.to_datetime(df["time"], errors="coerce").dt.date
        close = pd.to_numeric(df["close"], errors="coerce")
        out[symbol] = pd.Series(close.to_numpy(dtype=float), index=days)
    return out


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "date", "symbol", "suspended", "limit_up_price", "limit_down_price",
            "is_limit_up", "is_limit_down", "st_status", "no_limit",
            # 空帧的列集必须与非空帧一致（否则消费方按列取用会 KeyError）
            "listing_known",
        ]
    )
