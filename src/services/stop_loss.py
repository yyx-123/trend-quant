"""止损价计算 — 硬止损 / 吊灯止损的单一实现来源。

消费方：
- MCP 工具 ``trend_mcp/server.py`` 的 ``calc_stop_loss``
- 手工交易聚合服务 ``services/manual_trade.py``

止损公式与回测引擎一致（见 ``rule_backtest/state_values.py``）：
- 硬止损 = 买入价 − ATR(20, 买入日) × hard_stop_atr_mul（默认 1.5，
  支持 per-instrument ``stop_atr_mul`` 覆盖）
- 吊灯止损 = 买入以来最高价 − ATR(20, 最新) × chandelier_stop_atr_mul（默认 2.5）
- 棘轮吊灯止损 = 同公式逐日候选值的历史最大值（只上移不下移）

盘中/盘后实时叠加（``intraday=True`` 且当日已过 9:30 开盘，``is_past_market_open``）：
- 用实时报价合成当日K线，计入「买入以来最高价」「最新价」「止损触发判断」，
  因此吊灯止损价在盘中会随新高实时上移；收盘后到日K补库任务（默认 16:30）
  落库前的窗口内，实时报价即为当日收盘快照，同样合成当日K线，避免现价滞后一天；
- ATR 沿用历史完整K线的值（与实时看板 ``compute_intraday_trend_score``
  同一口径），避免当日不完整K线污染 ATR；
- 非交易时段或报价获取失败时静默回退为纯日K（EOD）结果。
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from audit.app_logger import get_logger
from core.calendar import is_past_market_open
from core.strategy_config import get_strategy_config
from core.symbols import normalize_symbol
from core.trend import safe_float
from data import indicator_store
from data.indicator_store import get_series
from data.intraday_service import build_synthetic_bar, has_persisted_today_bar, is_quote_fresh
from data.service import get_data_service
from data.storage.db import get_db

logger = get_logger(__name__)

#: 哨兵值：调用方未提供预取的盘中合成K线 —— 按默认路径实时拉取。
#: 显式传 None 表示「已确认无盘中数据，直接用 EOD」（交易记录列表的同 symbol 去重用）。
UNSET_INTRADAY_BAR: object = object()


def stop_mode_toggle_title() -> str:
    """止损松紧档位开关的统一悬停文案（P2-28：market_view 与 manual_trade
    原两处硬编码且措辞已不一致，后端按当前配置下发唯一口径）。"""
    cfg = get_strategy_config()
    loose_hard = cfg.get("hard_stop_atr_mul_default", 1.5)
    loose_chand = cfg.get("chandelier_stop_atr_mul", 2.5)
    return (
        f"紧止损：硬止损 1×ATR、吊灯止损 2×ATR；"
        f"松止损：硬止损 {loose_hard}×ATR、吊灯止损 {loose_chand}×ATR"
    )


class StopLossError(ValueError):
    """止损计算中的业务错误（无效输入 / 数据不足）。"""


def _synthesize_intraday_bar(symbol: str, df: pd.DataFrame, quote: dict | None) -> dict | None:
    """由实时报价合成当日K线（单标的/批量两条路径共用）。报价无效/陈旧返回 None。"""
    if not quote or quote.get("price") is None:
        return None
    if not is_quote_fresh(quote):
        # 停牌/陈旧报价：trade date 不是今天，绝不能合成“当日”K线。
        return None
    volumes = pd.to_numeric(df["volume"], errors="coerce") if len(df) else pd.Series(dtype=float)
    prev_vol = safe_float(volumes.iloc[-1], 0.0) if len(volumes) else 0.0
    return build_synthetic_bar(quote, prev_vol)


def _fetch_intraday_bar(symbol: str, df: pd.DataFrame) -> dict | None:
    """当日已过开盘（含收盘后日K未落库的窗口）用实时报价合成当日K线；否则/失败返回 None。

    与 ``symbol_detail`` 的 intraday overlay 同一路径：
    ``is_past_market_open`` 门控 + 当日K线已落库时 DB 优先
    （共享 ``has_persisted_today_bar``）+ ``DataService.fetch_latest_quote``
    + ``build_synthetic_bar``，任何失败都静默回退 EOD。
    """
    if not is_past_market_open():
        return None
    if has_persisted_today_bar(df):
        # 当日K线已由盘后任务落库 —— 与图表 overlay 一致，DB 数据优先。
        return None
    try:
        quote = get_data_service().fetch_latest_quote(symbol)
        return _synthesize_intraday_bar(symbol, df, quote)
    except Exception as exc:
        logger.warning("Intraday quote failed for %s; falling back to EOD: %s", symbol, exc)
        return None


def fetch_intraday_bars(dfs: dict[str, pd.DataFrame]) -> dict[str, dict | None]:
    """批量盘中K线预取：一次批量报价请求，逐标的本地合成当日K线。

    交易记录列表一次需要全部持仓标的的盘中数据；逐标的单调实时行情会打满
    tickflow 的 10/min 限流（每个持仓一次请求叠加 429 重试，9 个持仓曾导致
    列表接口耗时 115 秒）。这里改为一次批量请求。任一标的失败静默回退 EOD
    （对应值为 None）。
    """
    result: dict[str, dict | None] = dict.fromkeys(dfs, None)
    if not dfs or not is_past_market_open():
        return result
    # 当日K线已落库的标的直接 DB 优先（与图表 overlay 口径一致），
    # 无需再为其拉取报价；也避免用报价覆盖已确认的盘后数据。
    pending = {s: df for s, df in dfs.items() if not has_persisted_today_bar(df)}
    if not pending:
        return result
    try:
        quotes = get_data_service().fetch_latest_quotes(list(pending))
    except Exception as exc:
        logger.warning("Batch intraday quotes failed; falling back to EOD: %s", exc)
        return result
    for symbol, df in pending.items():
        quote = quotes.get(symbol) or {}
        if quote.get("error"):
            continue
        try:
            result[symbol] = _synthesize_intraday_bar(symbol, df, quote)
        except Exception as exc:
            logger.warning("Intraday bar build failed for %s; falling back to EOD: %s", symbol, exc)
    return result


def compute_stop_loss(
    symbol: str,
    buy_date: str,
    buy_price: float,
    db=None,
    intraday: bool = True,
    end_date: str | None = None,
    intraday_bar: dict | None | object = UNSET_INTRADAY_BAR,
    stop_mode: str | None = None,
    df: pd.DataFrame | None = None,
    atr_series: pd.Series | None = None,
    metadata_map: dict | None = None,
) -> dict:
    """计算给定买入的硬止损价和吊灯止损价。

    硬止损公式: 买入价 − 买入当日 ATR(20) × hard_stop_atr_mul (默认 1.5)。
    注意以买入价而非买入当日收盘价为基准 —— 手工输入的买入价通常不是收盘价。
    吊灯止损公式: 买入以来最高价 − 最新 ATR(20) × chandelier_stop_atr_mul (默认 2.5)。

    ``stop_mode`` 为止损松紧档位：``"tight"``（紧止损）固定硬止损 1×ATR、
    吊灯止损 2×ATR，且忽略标的级 stop_atr_mul 覆盖；``"loose"`` 或 None
    （松止损，默认）沿用既有口径（配置默认值 + 标的级覆盖）。

    ``intraday=True``（默认）时，交易时段内会把实时报价合成的当日K线计入
    最高价 / 最新价 / 止损触发判断；ATR 仍为历史完整K线口径（见模块 docstring）。
    ``intraday_bar`` 显式传入（含 None）时跳过实时拉取，直接使用该值。

    ``end_date`` 用于已清仓交易：数据与 ATR 均截断到该日（含），
    "最新价 / 买入以来最高价" 都按截止日口径，且强制关闭 intraday。
    ``df`` 预加载的日K（P2-18：持仓列表同一标的只读一次全量行情，
    调用方传入后不再重复 ``db.load_market_data``）。
    ``atr_series`` / ``metadata_map`` 为批量路径（compute_stop_loss_batch）
    的预取注入：传入后跳过 ``get_series`` 与逐标的 metadata 查询。

    Raises:
        StopLossError: 标的无效、无数据或 ATR 异常。
    """
    symbol = normalize_symbol(symbol)
    if not symbol:
        raise StopLossError("无效的标的代码")
    try:
        buy_ts = pd.Timestamp(buy_date)
    except (ValueError, TypeError) as exc:
        raise StopLossError(f"无效的买入日期: {buy_date}") from exc
    if buy_price <= 0:
        raise StopLossError("买入价格必须大于 0")

    end_ts: pd.Timestamp | None = None
    if end_date is not None:
        try:
            end_ts = pd.Timestamp(end_date)
        except (ValueError, TypeError) as exc:
            raise StopLossError(f"无效的截止日期: {end_date}") from exc
        if end_ts < buy_ts:
            raise StopLossError(f"截止日期 {end_date} 早于买入日期 {buy_date}")
        intraday = False  # 历史截断口径，不叠加盘中

    db = db or get_db()
    if df is None:
        df = db.load_market_data(symbol)
    if df.empty:
        raise StopLossError(f"未找到 {symbol} 的数据")

    if stop_mode == "tight":
        # 紧止损：固定 1×ATR / 2×ATR，忽略标的级覆盖
        hard_stop_mul = 1.0
        chandelier_mul = 2.0
    else:
        strategy_cfg = get_strategy_config()
        hard_stop_mul = float(strategy_cfg.get("hard_stop_atr_mul_default", 1.5))
        chandelier_mul = float(strategy_cfg.get("chandelier_stop_atr_mul", 2.5))

        # Per-instrument stop_atr_mul override（主键查询；DB 行可能为 NULL）
        item = None
        if metadata_map is not None:
            # 批量路径：调用方已一次取出全量 metadata map，免去逐标的查询。
            item = metadata_map.get(symbol)
        else:
            try:
                item = db.get_instrument_metadata(symbol)
            except (RuntimeError, sqlite3.Error) as exc:
                logger.warning("Instrument metadata unavailable: %s", exc)
        if item and item.get("stop_atr_mul") is not None:
            hard_stop_mul = float(item["stop_atr_mul"])

    # ATR from the precomputed cache (single source, D11); the store falls
    # back to a live full-history compute when the cache is stale/missing.
    if atr_series is None:
        atr_series = get_series(symbol, "atr", db=db)
    if end_ts is not None:
        atr_series = atr_series[atr_series.index <= end_ts]
    if atr_series.empty:
        raise StopLossError("数据不足，无法计算 ATR")

    current_atr = safe_float(atr_series.iloc[-1], 0.0)
    if current_atr <= 0:
        raise StopLossError("ATR 值为 0，数据异常")

    df = df.copy()
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    if end_ts is not None:
        df = df[df["time"] <= end_ts]
        if df.empty:
            raise StopLossError(f"{symbol} 在 {end_date}（含）之前无数据")

    # 盘中实时叠加：合成当日K线计入最高价/最新价。
    # ATR 刻意不叠加当日不完整K线（与实时看板同一口径），故无需重算。
    if intraday:
        synth = (
            _fetch_intraday_bar(symbol, df)
            if intraday_bar is UNSET_INTRADAY_BAR
            else intraday_bar
        )
    else:
        synth = None
    if synth is not None:
        today = pd.Timestamp(synth["time"]).normalize()
        df = df[df["time"] < today]  # 防御：剔除可能已存在的当日行
        df = pd.concat([df, pd.DataFrame([synth])], ignore_index=True)

    # 买入价合理性校验：必须落在买入日当根K线的最高/最低价之间。
    # 买入日为非交易日（无当根K线）时跳过 —— 历史行为允许非交易日买入。
    day_bars = df[df["time"].dt.normalize() == buy_ts]
    if not day_bars.empty:
        day_low = safe_float(pd.to_numeric(day_bars["low"], errors="coerce").iloc[0], 0.0)
        day_high = safe_float(pd.to_numeric(day_bars["high"], errors="coerce").iloc[0], 0.0)
        eps = max(1e-4, abs(day_high) * 1e-6)
        if day_low > 0 and day_high > 0 and not (day_low - eps <= buy_price <= day_high + eps):
            raise StopLossError(
                f"买入价格 {buy_price} 超出 {buy_date} 当日价格区间 "
                f"[{round(day_low, 4)}, {round(day_high, 4)}]"
            )

    # ATR at buy date (look back up to and including buy_date)
    atr_at_buy = current_atr
    subset = atr_series[atr_series.index <= buy_ts]
    if not subset.empty and pd.notna(subset.iloc[-1]):
        atr_at_buy = safe_float(subset.iloc[-1], current_atr)

    # Highest price since buy date (inclusive)
    highs = pd.to_numeric(df["high"], errors="coerce")
    latest_price = safe_float(pd.to_numeric(df["close"], errors="coerce").iloc[-1], 0.0)
    highest_since_buy = latest_price
    highest_since_buy_date: str | None = str(df["time"].iloc[-1].date())
    mask_since = df["time"] >= buy_ts
    if mask_since.any():
        since_highs = highs[mask_since]
        if not since_highs.empty and since_highs.notna().any():
            highest_since_buy = safe_float(since_highs.max(), latest_price)
            # 最高价出现日期（首次触及；供前端悬停展示最大浮盈/吊灯止损的计算过程）
            peak_idx = since_highs.fillna(float("-inf")).to_numpy(dtype=float).argmax()
            highest_since_buy_date = str(df.loc[mask_since, "time"].iloc[peak_idx].date())

    # Calculate stop prices
    hard_stop_price = round(buy_price - hard_stop_mul * atr_at_buy, 4)
    chandelier_stop_price = round(highest_since_buy - chandelier_mul * current_atr, 4)

    # 棘轮版吊灯止损：与原版同公式，但只上移不下移 —— 无状态逐日回放，
    # 候选值 = 截至当日最高价 − mul × 当日 ATR，取买入以来历史最大值
    # （等价于逐日 max(前一日棘轮价, 当日候选值)）。
    # 盘中当日无完整K线 ATR，按本模块既有约定沿用最近历史 ATR（ffill）。
    chandelier_stop_ratchet_price = chandelier_stop_price
    chandelier_first_trigger_date: str | None = None
    chandelier_first_trigger_close: float | None = None
    chandelier_first_trigger_stop: float | None = None
    if mask_since.any():
        since_dates = pd.DatetimeIndex(df.loc[mask_since, "time"].dt.normalize())
        atr_daily = atr_series.copy()
        atr_daily.index = pd.DatetimeIndex(pd.to_datetime(atr_daily.index))
        atr_aligned = atr_daily.reindex(since_dates).ffill()
        running_high = highs[mask_since].cummax()
        candidates = running_high.to_numpy(dtype=float) - chandelier_mul * atr_aligned.to_numpy(dtype=float)
        candidates = candidates[pd.notna(candidates)]
        if len(candidates):
            chandelier_stop_ratchet_price = round(float(candidates.max()), 4)

        # 吊灯止损历史首次跌破：逐日 吊灯_t = 截至当日最高价 − mul×当日 ATR，
        # 首个 收盘价 ≤ 吊灯_t 的交易日（供前端区分"曾跌破/已跌破"并展示悬停历史；
        # NaN 参与比较恒为 False，ATR 缺失的早期日期自然跳过）。
        # 买入当天不参与判定：日K 粒度无法区分当天收盘前的价格行为发生在买入
        # 前后，不可能跌破自己尚未持有的持仓的止损（与 manual_trade 硬止损
        # 「曾跌穿」排除买入日同一口径，2026-09）。
        daily_chandelier = (
            running_high.to_numpy(dtype=float) - chandelier_mul * atr_aligned.to_numpy(dtype=float)
        )
        closes_arr = pd.to_numeric(df.loc[mask_since, "close"], errors="coerce").to_numpy(dtype=float)
        below = closes_arr <= daily_chandelier
        if len(below):
            below[since_dates <= buy_ts] = False
        if below.any():
            first_idx = int(below.argmax())
            chandelier_first_trigger_date = str(since_dates[first_idx].date())
            chandelier_first_trigger_close = round(float(closes_arr[first_idx]), 4)
            chandelier_first_trigger_stop = round(float(daily_chandelier[first_idx]), 4)

    hard_stop_pct = round((hard_stop_price / buy_price - 1) * 100, 2)
    chandelier_pct = (
        round((chandelier_stop_price / highest_since_buy - 1) * 100, 2)
        if highest_since_buy > 0
        else 0.0
    )

    payload = {
        "symbol": symbol,
        "buy_price": buy_price,
        "buy_date": buy_date,
        "hard_stop_price": hard_stop_price,
        "hard_stop_pct": hard_stop_pct,
        "hard_stop_atr_mul": hard_stop_mul,
        "chandelier_stop_price": chandelier_stop_price,
        "chandelier_stop_pct_from_high": chandelier_pct,
        "chandelier_stop_atr_mul": chandelier_mul,
        "chandelier_stop_ratchet_price": chandelier_stop_ratchet_price,
        "chandelier_first_trigger_date": chandelier_first_trigger_date,
        "chandelier_first_trigger_close": chandelier_first_trigger_close,
        "chandelier_first_trigger_stop": chandelier_first_trigger_stop,
        "atr_at_buy": round(atr_at_buy, 4),
        "current_atr": round(current_atr, 4),
        "highest_since_buy": round(highest_since_buy, 4),
        "highest_since_buy_date": highest_since_buy_date,
        "latest_price": round(latest_price, 4),
        "is_intraday": synth is not None,
        "stop_mode": "tight" if stop_mode == "tight" else "loose",
    }
    if synth is not None:
        payload["intraday_ts"] = pd.Timestamp(synth["time"]).isoformat()
        payload["intraday_bar"] = {
            "date": str(pd.Timestamp(synth["time"]).date()),
            "open": round(float(synth["open"]), 4),
            "high": round(float(synth["high"]), 4),
            "low": round(float(synth["low"]), 4),
            "close": round(float(synth["close"]), 4),
        }
    return payload


def compute_stop_loss_batch(
    items: list[dict],
    db=None,
    intraday: bool = True,
    stop_mode: str | None = None,
    max_workers: int = 4,
) -> list[dict]:
    """批量止损试算：N 个标的的 IO 与 N 解耦，逐项结果与输入顺序对齐。

    与逐次 ``compute_stop_loss`` 的差别全部在 IO 层，计算口径完全一致：
    - 日K：``db.load_market_data_many`` 单连接一次批量读取（替代 N 次逐标的全量读）；
    - 盘中K线：``fetch_intraday_bars`` 一次批量报价请求合成全部标的
      （替代逐标的单调实时行情 —— 后者会打满 tickflow 限流，N=100+ 时
      光行情请求就要分钟级）；
    - ATR：``indicator_store.get_series_bulk`` 常数条批量 SQL（替代逐标的
      3 次新鲜度检查 + 1 次全量指标读），过期标的回退 live 重算并复用
      已加载的日K；
    - metadata：一次 ``get_instrument_metadata_map`` 全量 map（替代逐标的
      主键查询）。

    逐项计算注入预取数据后为纯 CPU，用小型线程池并行（pandas/numpy 会
    释放 GIL）。任一项失败不影响其他项：对应位置返回
    ``{"ok": False, "symbol", "error"}``。

    ``stop_mode`` 为批次级默认档位，单项里的 ``stop_mode`` 字段优先。
    """
    db = db or get_db()
    items = list(items or [])
    if not items:
        return []

    symbols = [
        normalize_symbol(str((item or {}).get("symbol", "") or "")) for item in items
    ]
    unique = list(dict.fromkeys(s for s in symbols if s))

    dfs = db.load_market_data_many(unique) if unique else {}
    bars = (
        fetch_intraday_bars(dfs)
        if intraday
        else {s: None for s in dfs}
    )
    atr_map = (
        indicator_store.get_series_bulk(unique, "atr", db=db, bars_map=dfs)
        if unique
        else {}
    )
    metadata_map: dict | None = None
    try:
        metadata_map = db.get_instrument_metadata_map()
    except (RuntimeError, sqlite3.Error, AttributeError) as exc:
        logger.warning("Instrument metadata map unavailable: %s", exc)

    def _one(idx: int) -> dict:
        item = items[idx] or {}
        symbol = symbols[idx]
        try:
            payload = compute_stop_loss(
                symbol,
                item.get("buy_date"),
                float(item.get("buy_price") or 0),
                db=db,
                intraday=intraday,
                intraday_bar=bars.get(symbol),
                stop_mode=item.get("stop_mode") or stop_mode,
                df=dfs.get(symbol),
                atr_series=atr_map.get(symbol),
                metadata_map=metadata_map,
            )
            return {"ok": True, **payload}
        except Exception as exc:  # 单项失败不拖垮整批
            return {"ok": False, "symbol": symbol or str(item.get("symbol", "")), "error": str(exc)}

    if len(items) == 1:
        return [_one(0)]
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(items)))) as ex:
        return list(ex.map(_one, range(len(items))))
