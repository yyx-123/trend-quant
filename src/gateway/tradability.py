"""可交易性标注（详设 §3.2）：停牌 / 涨停 / 跌停 逐日逐标的推导。

推导规则（不需要额外数据源）：

- 停牌：当日是交易日但无 bar → suspended=True；
- 涨跌停价：**除权基准修正后**的前收盘价 × 板块幅度——
  基准价 = raw close(t-1)；若 t 是**除权除息日**（= ex_factors 存储日 E
  之后的**第一根该标的 bar**），基准价 = raw close(t-1) / f_t。
  口径依据（loop-review-ds4f R1-P1-1 实证修正）：本项目因子语义为
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
  - 价格按分（0.01）四舍五入（ROUND_HALF_UP，交易所口径）；
- 一字板近似：is_limit_up 用收盘价判定（尾盘口径下，收盘封板 ≈ 尾盘不可买，
  与决策 1 口径自洽）。
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import numpy as np
import pandas as pd

from core.calendar import is_trading_day
from core.symbols import symbol_suffix, symbol_to_code

# 板块幅度规则（详设 §3.2）
_LIMIT_MAIN = 0.10
_LIMIT_CHINEXT_STAR = 0.20
_IPO_NO_LIMIT_DAYS = 5  # 注册制新股上市初期无涨跌幅限制的交易日数（近似）


def board_limit_pct(symbol: str, *, asset_type: str | None = None,
                    name: str | None = None) -> float:
    """按代码板块归属返回涨跌停幅度（不含 ST 修正）。

    GLM53F-P1-1：ETF 的涨跌幅跟随其**标的板块**，不能一律按主板 ±10%——
    科创板 ETF（沪 588xxx）±20%；深市跟踪创业板/科创板指数的 ETF（名称含
    "创业板"/"科创"，如 159915 创业板ETF）±20%；其余 ETF ±10%。
    asset_type 缺省时按股票代码前缀规则（历史口径）。
    """
    suffix = symbol_suffix(symbol)
    code = symbol_to_code(symbol)
    if asset_type == "etf":
        if suffix == "SS" and code.startswith("588"):
            return _LIMIT_CHINEXT_STAR  # 科创板 ETF
        if name and ("创业板" in name or "科创" in name):
            return _LIMIT_CHINEXT_STAR  # 跟踪创业板/科创板指数的 ETF
        return _LIMIT_MAIN
    if suffix == "SS" and code.startswith("68"):
        return _LIMIT_CHINEXT_STAR  # 科创板
    if suffix == "SZ" and code.startswith("30"):
        return _LIMIT_CHINEXT_STAR  # 创业板
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


def _round_fen(price: float) -> float:
    return float(Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


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
    R1-P2-10：14:00 实盘清单的所有标的都被标成"停牌"、涨跌停标记永不置位）。
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
                # **只在库里确实没有该日 bar 时**才并入（V1 复核实证：旧写法
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

    def _round_fen_vec(x: np.ndarray) -> np.ndarray:
        # 分位四舍五入（ROUND_HALF_UP 语义；eps 抵消二进制浮点误差）
        return np.floor(x * 100.0 + 0.5 + 1e-9) / 100.0

    rows: list[dict] = []
    for symbol in symbols:
        closes = raw_closes.get(symbol)
        info = (asset_info or {}).get(symbol) or {}
        asset_type = str(info.get("asset_type") or "").strip() or None
        limit_pct = board_limit_pct(symbol, asset_type=asset_type, name=info.get("name"))
        listing = listing_dates.get(symbol)
        listing_day = pd.Timestamp(listing).date() if listing else None
        # R4A-P2-2（Round 4 复核，P2）：上市日来源问题**未在本轮改行为**——
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

        # 除权基准修正（loop-review-ds4f R1-P1-1）：因子存储日 E 是**除权前**
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
                    if not np.isfinite(f) or f <= 0:
                        continue
                    # 第一根「日期严格晚于 E 且有 bar」的轴日 = 除权除息日。
                    # **累乘**而非覆盖（V1 复核残留）：停牌跨越两个除权日时
                    # 两个因子会落到同一根 bar 上，只取后者会让该日基准价
                    # 偏高、产出假跌停（002129.SZ 真实库有 1 例，差异 0.24%）。
                    k = int(np.searchsorted(bar_ord, ex_ord, side="right"))
                    if k < bar_pos.size:
                        f_ax[int(bar_pos[k])] *= f
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
            limit_up = np.where(valid, _round_fen_vec(base * (1.0 + limit_pct)), np.nan)
            limit_down = np.where(valid, _round_fen_vec(base * (1.0 - limit_pct)), np.nan)
            close_rounded = _round_fen_vec(close_v)
            is_limit_up = has_bar & valid & (close_rounded >= limit_up)
            is_limit_down = has_bar & valid & (close_rounded <= limit_down)

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
                    # R4A-P2-2：上市日未知时"新股无涨跌幅限制"这条不生效——如实标注
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
        ]
    )
