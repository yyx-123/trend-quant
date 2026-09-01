"""MCP server for trend-quant.

Exposes 8 tools to external agents via MCP SSE transport:

1. **trend_dashboard** -- 标的看板: multi-symbol trend dashboard grouped by
   three-level category hierarchy (EOD daily bars).
2. **intraday_dashboard** -- 实时看板: same structure as trend_dashboard but
   computed from real-time quotes (trading days 9:30-15:00, lunch break
   included).
3. **symbol_detail** -- 标的查看: historical OHLCV + full indicator suite for
   a single symbol, with an optional real-time intraday overlay.
4. **calc_stop_loss** -- 辅助计算: hard-stop and chandelier-stop prices for
   a given buy entry.
5. **calc_stop_loss_batch** -- 批量止损试算: same computation as
   calc_stop_loss for a list of entries, with bulk quotes/K-lines/ATR IO.
6. **list_instruments** -- 标的列表: searchable / filterable instrument
   catalogue.
7. **add_trade** -- 手工交易录入: record a buy trade for the token-mapped
   user (Bearer token channel auth, same path as the web UI).
8. **open_positions** -- 持仓概览: realtime overview of the token-mapped
   user's open (not yet closed) positions.

通道鉴权：/mcp 由 McpBearerMiddleware（app/mcp_auth.py）做 Bearer token
校验，token→用户映射见 TREND_MCP_TOKENS；工具不再接收 username/password。
"""

from __future__ import annotations

import threading
import time

import pandas as pd
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.mcp_auth import load_mcp_allowed_hosts
from audit.app_logger import get_logger
from core import env
from core.calendar import is_past_market_open, is_realtime_available, is_trading_day, market_now
from core.display import category_path as _category_path
from core.display import filter_fully_classified, format_symbol_display
from core.display import load_instrument_name_map as _config_name_map
from core.symbols import normalize_symbol as _normalize_symbol
from data.intraday_service import build_intraday_dashboard, build_intraday_overlay
from data.service import get_data_service
from data.storage.db import get_db
from services import trade_records as tr
from services.dashboard import build_subject_dashboard_payload, dashboard_revision_cache
from services.market_indicators import compute_market_indicators
from services.market_indicators import trend_config as _trend_config
from services.stop_loss import StopLossError, compute_stop_loss, compute_stop_loss_batch

# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------

logger = get_logger(__name__)

# DNS rebinding 保护：配置 TREND_MCP_ALLOWED_HOSTS（frp 域名，可带端口或
# :* 通配）后开启；空配置保持关闭——上线顺序为先验证 Bearer token 中间件，
# 再配置域名开启保护（配错 allowed_hosts 会导致所有 MCP 请求 421）。
_mcp_allowed_hosts = load_mcp_allowed_hosts()

mcp = FastMCP(
    "trend-quant",
    transport_security=(
        TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=_mcp_allowed_hosts,
        )
        if _mcp_allowed_hosts
        else TransportSecuritySettings(enable_dns_rebinding_protection=False)
    ),
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_instruments_raw() -> list[dict]:
    import sqlite3

    try:
        return [dict(item) for item in get_db().list_instrument_metadata()]
    except (RuntimeError, sqlite3.Error) as exc:
        logger.warning("Instrument metadata unavailable: %s", exc)
        return []

def _instrument_metadata_map(instruments: list[dict]) -> dict[str, dict]:
    return {
        str(item.get("symbol", "")).strip().upper(): item
        for item in instruments
        if str(item.get("symbol", "")).strip().upper()
    }

def _token_user(ctx: Context) -> dict:
    """从 Bearer token 映射取用户身份（P0-2：工具不再接收 username/password）。

    McpBearerMiddleware 校验通过后把用户名写入 ``scope["state"]["mcp_user"]``；
    SSE transport 将 Starlette Request 塞进 ``ServerMessageMetadata.request_context``，
    工具经 ``ctx.request_context.request.scope`` 取回。
    """
    username: str | None = None
    try:
        request = ctx.request_context.request
    except (ValueError, AttributeError):
        request = None
    if request is not None:
        state = request.scope.get("state") or {}
        username = state.get("mcp_user")
    if not username:
        raise tr.TradeAuthError(
            "MCP 通道缺少 token 用户映射（请求未经过 McpBearerMiddleware 或 token 无效）"
        )
    user = get_db().get_user_by_username(username)
    if user is None:
        raise tr.TradeAuthError(
            f"token 映射的用户「{username}」在 users 表中不存在，请检查 TREND_MCP_TOKENS 配置"
        )
    return {"id": user["id"], "username": user["username"], "is_admin": user["is_admin"]}

# Dashboard cache：services.dashboard 的模块级 RevisionCache 单例（P2-10），
# 与 Web 标的看板共用同一份缓存。


class _TtlPayloadCache:
    """短 TTL get-or-compute 缓存 + 单飞合并（intraday_dashboard 专用）。

    全量盘中看板一次计算要秒级~分钟级（全市场批量报价受 vendor 限流），
    而调用方（如每日报告技能）常在同一分钟内由多个脚本各拉一次。TTL 内
    重复请求直接复用；并发miss 时持锁计算，后到的请求等待后读同一份，
    不重复触发全量构建。TTL 默认与报价缓存窗口对齐（30s，见
    ``env.mcp_intraday_cache_ttl_seconds``）。
    """

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = max(0.0, float(ttl_seconds))
        self._cached: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def get_or_compute(self, key: str, compute) -> dict:
        if self._ttl <= 0:
            return compute()
        now = time.monotonic()
        hit = self._cached.get(key)
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1]
        with self._lock:
            now = time.monotonic()
            hit = self._cached.get(key)
            if hit is not None and now - hit[0] < self._ttl:
                return hit[1]
            payload = compute()
            self._cached[key] = (time.monotonic(), payload)
            return payload


_intraday_payload_cache = _TtlPayloadCache(env.mcp_intraday_cache_ttl_seconds(30.0))

# detail="lite" 瘦身规则：序列字段只留末尾 N 个 / 整键删除 / 浮点 6 位。
# 扫描类消费方只用各序列的最新值（金叉缺口用最后两个 DIF/DEA），
# mini K线/MACD 全序列与 61 日 trend_history 是全量响应 10MB 的大头。
_LITE_TAIL_KEYS = {"kline": 2, "macd_dif": 2, "macd_dea": 2, "macd_dates": 2}
_LITE_DROP_KEYS = {"kline_ma5", "macd_hist", "trend_history", "trend_dates"}


def _dashboard_lite(node):
    """看板 payload 的 lite 变换：构建新结构，绝不改动共享缓存里的原对象。"""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key in _LITE_DROP_KEYS:
                continue
            if key in _LITE_TAIL_KEYS and isinstance(value, list):
                out[key] = [_dashboard_lite(v) for v in value[-_LITE_TAIL_KEYS[key]:]]
            else:
                out[key] = _dashboard_lite(value)
        return out
    if isinstance(node, list):
        return [_dashboard_lite(v) for v in node]
    if isinstance(node, float):
        return round(node, 6)
    return node

# ---------------------------------------------------------------------------
# Tool 1 -- trend_dashboard
# ---------------------------------------------------------------------------

@mcp.tool()
def trend_dashboard(detail: str = "full") -> dict:
    """获取标的看板数据（基于日K线，不含当天盘中实时数据）。

    Returns all ETF instruments grouped by a three-level category
    hierarchy (L1/L2/L3), each with:
    - 最新趋势值 (trend_score)
    - 趋势值 MA5 (trend_ma5, the primary ranking metric)
    - 同级强度百分位 (strength)
    - 日涨跌幅 / 5日 / 20日 / 60日涨跌幅
    - 趋势相位检测 (上升 / 下降 / 震荡)
    - 历史趋势值 MA5 序列 (trend_history)

    数据来自本地日K库，最新一根K线通常是上一个交易日。
    如需当天实时数据请使用 intraday_dashboard。

    Args:
        detail: "full"（默认，完整序列）/ "lite"（每标的只保留最新
            K线/MACD 值，删除 trend_history 等长序列，响应体积约 1/10；
            只需要各指标最新值的扫描类场景请用 lite）。
    """
    db = get_db()
    revision = db.get_market_dashboard_revision()
    payload = dashboard_revision_cache.get_or_compute(
        revision, lambda: build_subject_dashboard_payload(db)
    )
    if detail == "lite":
        lite = _dashboard_lite(payload)
        lite["detail"] = "lite"
        return lite
    return payload

# ---------------------------------------------------------------------------
# Tool 2 -- intraday_dashboard (real-time)
# ---------------------------------------------------------------------------

@mcp.tool()
def intraday_dashboard(category: str = "", detail: str = "full") -> dict:
    """获取实时标的看板（基于当天实时报价，含盘中趋势值）。

    交易日 9:30 开盘后（含午间休盘 11:30-13:00 与收盘后）可用；收盘后
    返回的是基于收盘报价的当日快照（日K补库任务落库前）。非交易日或
    开盘前请使用 trend_dashboard 获取日K看板。

    全量结果带短 TTL 缓存（默认 30s，TREND_MCP_INTRADAY_CACHE_TTL_SECONDS
    可调，与报价缓存窗口对齐）：TTL 内重复/并发请求复用同一份计算结果，
    不会重复触发全市场计算。

    Args:
        category: 可选，按分类筛选（匹配 L1/L2/L3），如 "ETF"、"宽基"、
            "跨境"、"股票"。不传则计算全部标的（600+，首次计算可能需要
            1 分钟，TTL 内再次请求秒回）。
        detail: "full"（默认，完整序列）/ "lite"（每标的只保留最新
            K线/MACD 值，删除 trend_history 等长序列，响应体积约 1/10；
            只需要各指标最新值的扫描类场景请用 lite）。

    Returns:
        与 trend_dashboard 相同的三级分类结构，另含:
        - is_intraday: True
        - post_close: True 表示收盘后快照（非盘中实时）
        - intraday_ts: 计算时间戳
        - 每个标的的 daily_change_pct 为实时涨跌幅
    """
    now = market_now()
    if not is_trading_day(now.date()):
        return {
            "ok": False,
            "error": "今日非交易日，无实时数据；请使用 trend_dashboard 获取日K看板",
        }
    if not is_past_market_open(now):
        return {
            "ok": False,
            "error": "今日尚未开盘（需 9:30 之后）；请使用 trend_dashboard 获取日K看板",
        }

    def _compute() -> dict:
        db = get_db()
        symbols = db.list_market_symbols(price_mode="qfq")
        if not symbols:
            return {"ok": False, "error": "本地无日K数据"}

        # Filter to fully classified instruments (same rule as the web intraday job).
        metadata_map = db.get_instrument_metadata_map()
        classified = filter_fully_classified(symbols, metadata_map)

        # Optional category filter (match any level, case-insensitive).
        if category.strip():
            kw = category.strip().lower()
            classified = [
                s for s in classified
                if kw in str(metadata_map[s].get("category_l1", "")).lower()
                or kw in str(metadata_map[s].get("category_l2", "")).lower()
                or kw in str(metadata_map[s].get("category_l3", "")).lower()
            ]

        if not classified:
            return {"ok": False, "error": "无符合条件的标的（需完整三级分类）"}

        built = build_intraday_dashboard(
            classified, db, get_data_service(), _trend_config()
        )
        built["ok"] = True
        built["post_close"] = not is_realtime_available(market_now())
        built["requested_category"] = category.strip()
        return built

    payload = _intraday_payload_cache.get_or_compute(category.strip().lower(), _compute)
    if detail == "lite":
        lite = _dashboard_lite(payload)
        lite["detail"] = "lite"
        return lite
    return payload

# ---------------------------------------------------------------------------
# Tool 3 -- symbol_detail
# ---------------------------------------------------------------------------

@mcp.tool()
def symbol_detail(symbol: str, days: int = 60, rsi_period: int = 14, intraday: bool = False) -> dict:
    """获取指定标的的历史日K线、趋势指标和全套技术指标。

    Args:
        symbol: 标的代码，如 510300.SS 或 510300
        days: 返回最近多少天的数据，默认 60
        rsi_period: RSI 计算周期，默认 14
        intraday: 是否叠加当天实时数据，默认 False。
            交易日 9:30 之后生效（含午间休盘及收盘后）：若当日K线尚未
            写入本地库，则追加一根由实时报价合成的当日K线，并在
            indicators.trend_intraday 中返回盘中趋势值快照；当日K线已
            入库、非交易时段或实时行情获取失败时静默回退为日K数据。

    Returns:
        包含 dates、candles(OHLC)、volumes、indicators 的完整数据。
        indicators 包含: trend(score/ma/price_direction/confidence),
        ma, atr, bias, boll, macd, rsi。
        meta.is_intraday 标记是否包含实时数据。
    """
    symbol = _normalize_symbol(symbol)
    if not symbol:
        return {"ok": False, "error": "无效的标的代码"}

    db = get_db()
    df = db.load_market_data(symbol)
    if df.empty:
        return {"ok": False, "error": f"未找到 {symbol} 的数据，请确认代码正确且数据已入库"}

    # Compute indicators over FULL history (EMA-family indicators have
    # infinite memory; truncating before computing made values depend on
    # the requested window — the old window-truncation bug). Output arrays
    # are tailed afterwards to the requested number of days.
    requested = max(int(days), 1)

    name_map = _config_name_map()
    instruments = _load_instruments_raw()
    metadata_map = _instrument_metadata_map(instruments)
    name = name_map.get(symbol, "")
    metadata = metadata_map.get(symbol)

    rsi_period = max(2, int(rsi_period or 14))
    trend_cfg = _trend_config()
    indicators = compute_market_indicators(df, trend_cfg=trend_cfg, rsi_period=rsi_period)

    # Tail output arrays to the requested number of days
    def _tail(values: list, n: int) -> list:
        return values[-n:] if len(values) > n else values

    def _float_list(series_like) -> list:
        return [round(float(v), 4) if pd.notna(v) else None for v in series_like]

    n = min(requested, len(df))
    full_df = df  # keep full history for the intraday trend computation
    df = df.tail(n).copy()
    dates_out = [str(d.date()) for d in df["time"]]

    payload = {
        "ok": True,
        "symbol": symbol,
        "name": name,
        "display_name": format_symbol_display(symbol, name),
        "category": _category_path(metadata),
        "category_l1": str((metadata or {}).get("category_l1") or ""),
        "category_l2": str((metadata or {}).get("category_l2") or ""),
        "category_l3": str((metadata or {}).get("category_l3") or ""),
        "meta": db.get_market_data_summary(symbol),
        "dates": dates_out,
        "candles": {
            "open": _tail(_float_list(df["open"]), n),
            "high": _tail(_float_list(df["high"]), n),
            "low": _tail(_float_list(df["low"]), n),
            "close": _tail(_float_list(df["close"]), n),
        },
        "volumes": _tail(
            [int(v) if pd.notna(v) else None for v in df.get("volume", pd.Series())], n
        ),
        "indicators": indicators,
    }
    payload["meta"]["is_intraday"] = False

    # --- Intraday overlay (synthetic bar from live quotes) ----------------
    # Shared implementation (data.intraday_service.build_intraday_overlay) —
    # same code path as the web daily-K endpoint. Intraday trend uses FULL
    # history (same ruler as EOD), not the display-truncated window.
    if intraday:
        overlay = build_intraday_overlay(symbol, full_df, trend_cfg)
        if overlay:
            bar = overlay["bar"]
            payload["dates"].append(overlay["date"])
            payload["candles"]["open"].append(round(float(bar["open"]), 4))
            payload["candles"]["high"].append(round(float(bar["high"]), 4))
            payload["candles"]["low"].append(round(float(bar["low"]), 4))
            payload["candles"]["close"].append(round(float(bar["close"]), 4))
            payload["volumes"].append(int(bar["volume"]))
            payload["indicators"]["trend_intraday"] = overlay["trend"]
            payload["meta"]["is_intraday"] = True
            payload["meta"]["intraday_ts"] = overlay["ts"]
            payload["meta"]["post_close"] = bool(overlay.get("post_close"))
            # dates 已含当日合成K线：meta.end 必须与载荷一致，并标注其为
            # 合成（未落库）数据，避免消费方拿 meta.end 误判数据新鲜度。
            payload["meta"]["end"] = overlay["date"]
            payload["meta"]["end_is_synthetic"] = True

    return payload

# ---------------------------------------------------------------------------
# Tool 4 -- calc_stop_loss
# ---------------------------------------------------------------------------

@mcp.tool()
def calc_stop_loss(
    symbol: str,
    buy_date: str,
    buy_price: float,
    stop_mode: str | None = None,
) -> dict:
    """计算给定买入的硬止损价和吊灯止损价。

    硬止损公式: 买入价 − 买入当日 ATR(20) × hard_stop_atr_mul
    吊灯止损公式: 买入以来最高价 − 最新 ATR(20) × chandelier_stop_atr_mul
    棘轮吊灯止损: 同公式但只上移不下移（chandelier_stop_ratchet_price）

    stop_mode 松紧档位（与网页端手工交易同一口径）:
      - "tight" 紧止损: 固定 1×ATR / 2×ATR，忽略标的级 stop_atr_mul 覆盖
      - "loose" 或不传（默认）: 1.5×ATR / 2.5×ATR，可被标的级覆盖调整
    返回的 stop_mode 字段标识本次计算所用口径。

    交易时段（9:30-15:00，含午间休盘）内自动叠加实时报价合成的当日K线：
    最高价 / 最新价 / 止损触发判断均含今日盘中数据（is_intraday=True 标记）；
    非交易时段或报价失败时回退为纯日K结果。

    Args:
        symbol: 标的代码，如 510300.SS
        buy_date: 买入日期，格式 YYYY-MM-DD
        buy_price: 买入均价
        stop_mode: 止损松紧档位 "tight"（紧）/ "loose"（松，默认）

    Returns:
        硬止损价、吊灯止损价、ATR 参数、距买入价的百分比等。
    """
    try:
        payload = compute_stop_loss(symbol, buy_date, buy_price, stop_mode=stop_mode)
    except StopLossError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **payload}

# ---------------------------------------------------------------------------
# Tool 4b -- calc_stop_loss_batch (bulk trial)
# ---------------------------------------------------------------------------

#: 单次批量试算的标的数上限。约束不在行情限流（2000 只 = 40 个 50 标的
#: chunk，仍在 60 req/min 预算内），而在服务端内存（全量日K批量读出）
#: 与响应体积——2000 已覆盖整个市场（约 900 只）并留有充足余量。
_BATCH_STOP_LOSS_MAX_ITEMS = 2000


@mcp.tool()
def calc_stop_loss_batch(items: list[dict], stop_mode: str | None = None) -> dict:
    """批量计算硬止损价和吊灯止损价（与 calc_stop_loss 同一计算口径）。

    专为扫描类场景设计：一次调用完成全部候选标的的试算。服务端内部
    统一做一次批量实时报价、一次批量日K读取、一次批量 ATR 读取，
    IO 次数与标的数解耦——N=100+ 时比逐次调用 calc_stop_loss 快
    一个数量级以上（逐次调用在交易时段每标的一次行情请求，会打满
    行情限流）。

    Args:
        items: 试算条目列表，每项 {"symbol", "buy_date", "buy_price",
            "stop_mode"?}，字段含义同 calc_stop_loss；单次最多 2000 条
            （足以覆盖全市场扫描）。
        stop_mode: 批次级默认止损档位 "tight" / "loose"（默认），
            单项里的 stop_mode 字段优先。

    Returns:
        - results: 与输入顺序对齐的结果列表，每项为 calc_stop_loss 的
          完整字段（ok=True）或 {"ok": False, "symbol", "error"}——
          单项失败不影响其他项
        - succeeded / failed: 成功/失败计数
        - is_intraday: True 表示结果含盘中实时数据
    """
    items = list(items or [])
    if not items:
        return {"ok": False, "error": "items 为空"}
    if len(items) > _BATCH_STOP_LOSS_MAX_ITEMS:
        return {
            "ok": False,
            "error": f"单次最多 {_BATCH_STOP_LOSS_MAX_ITEMS} 条（收到 {len(items)} 条），请分批调用",
        }
    results = compute_stop_loss_batch(items, stop_mode=stop_mode)
    succeeded = sum(1 for r in results if r.get("ok"))
    return {
        "ok": True,
        "count": len(results),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
        "is_intraday": any(r.get("is_intraday") for r in results if r.get("ok")),
        "results": results,
    }

# ---------------------------------------------------------------------------
# Tool 5 -- list_instruments
# ---------------------------------------------------------------------------

@mcp.tool()
def list_instruments(
    category: str = "",
    keyword: str = "",
    enabled_only: bool = True,
) -> dict:
    """列出所有可用的 ETF 标的，支持按分类和关键词筛选。

    Args:
        category: 按分类筛选（匹配 L1/L2/L3），如 "ETF"、"宽基"、"跨境"、"股票"
        keyword: 按代码或名称模糊搜索
        enabled_only: 是否仅返回启用的标的，默认 True

    Returns:
        标的列表，包含代码、名称、三级分类、数据范围、启用状态。
    """
    instruments = _load_instruments_raw()
    db = get_db()

    result: list[dict] = []
    for item in instruments:
        symbol = str(item.get("symbol", "")).strip().upper()
        if not symbol:
            continue

        if enabled_only and not item.get("enabled", True):
            continue

        cat_l1 = str(item.get("category_l1") or "")
        cat_l2 = str(item.get("category_l2") or "")
        cat_l3 = str(item.get("category_l3") or "")
        name = str(item.get("name") or "")

        # Filter by category keyword (match any level)
        if category:
            kw = category.strip().lower()
            if not (kw in cat_l1.lower() or kw in cat_l2.lower() or kw in cat_l3.lower()):
                continue

        # Filter by symbol / name keyword
        if keyword:
            kw = keyword.strip().lower()
            if not (kw in symbol.lower() or kw in name.lower()):
                continue

        db_summary = db.get_market_data_summary(symbol)

        result.append(
            {
                "symbol": symbol,
                "name": name,
                "category_l1": cat_l1,
                "category_l2": cat_l2,
                "category_l3": cat_l3,
                "enabled": bool(item.get("enabled", True)),
                "data_rows": db_summary.get("rows", 0),
                "data_start": str(db_summary.get("start", ""))
                if db_summary.get("start")
                else None,
                "data_end": str(db_summary.get("end", ""))
                if db_summary.get("end")
                else None,
            }
        )

    return {"ok": True, "count": len(result), "instruments": result}

# ---------------------------------------------------------------------------
# Tool 6 -- add_trade (manual trade entry)
# ---------------------------------------------------------------------------

@mcp.tool()
def add_trade(
    symbol: str,
    buy_date: str,
    buy_price: float,
    shares: float,
    ctx: Context,
) -> dict:
    """录入一笔手工交易（买入），与网页端手工交易共用同一条录入链路。

    用户身份来自 Bearer token 映射（TREND_MCP_TOKENS），无需再传账号密码。

    Args:
        symbol: 标的代码，如 510300.SS 或 510300
        buy_date: 买入日期，格式 YYYY-MM-DD（须不晚于最新数据日期）
        buy_price: 买入均价，必须大于 0，且落在买入当日K线 [low, high] 区间内
        shares: 买入份数，必须大于 0

    Returns:
        成功时 ok=True 并返回落库后的完整交易记录（含 id、status=open）；
        失败时 ok=False 并附 error（token 用户映射缺失 / 标的不存在 /
        价格超出当日区间 / 参数非法等）。
    """
    try:
        user = _token_user(ctx)
        trade = tr.create_trade(
            user,
            symbol=symbol,
            buy_date=buy_date,
            buy_price=buy_price,
            shares=shares,
        )
    except (tr.TradeAuthError, tr.TradePermissionError, tr.TradeRecordError, StopLossError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "trade": trade}

# ---------------------------------------------------------------------------
# Tool 7 -- open_positions (realtime overview)
# ---------------------------------------------------------------------------

@mcp.tool()
def open_positions(stop_mode: str | None = None, ctx: Context = None) -> dict:
    """返回当前 token 映射用户当前持仓（未清仓交易）的实时概览。

    与网页端手工交易同一口径：交易时段（9:30-15:00，含午间休盘）内
    最新价 / 浮盈 / 吊灯止损价等均含盘中实时报价（is_intraday=True），
    非交易时段回退为最新日K收盘口径。

    用户身份来自 Bearer token 映射（TREND_MCP_TOKENS），无需再传账号密码。

    Args:
        stop_mode: 止损松紧档位 "tight"（紧：1×ATR/2×ATR）/
            "loose"（松：1.5×ATR/2.5×ATR，默认）

    Returns:
        - positions: 每笔持仓的概览（按持仓金额降序），含买入信息、
          最新价、持仓金额、浮盈金额/比例、最大浮盈、最大回撤、持有
          交易日数、硬止损价、吊灯止损价及各自是否已触发
        - summary: 合计持仓数、总持仓金额、总浮盈金额、整体浮盈比例
        - is_intraday / intraday_ts: 数据口径标记
    """
    try:
        user = _token_user(ctx)
        payload = tr.list_trades(user, intraday=True, stop_mode=stop_mode)
    except (tr.TradeAuthError, tr.TradePermissionError, tr.TradeRecordError) as exc:
        return {"ok": False, "error": str(exc)}

    positions: list[dict] = []
    total_value = 0.0
    total_pnl = 0.0
    total_cost = 0.0
    is_intraday = False
    intraday_ts: str | None = None

    for t in payload["trades"]:
        if t["status"] != "open":
            continue
        if t.get("error"):
            positions.append(
                {
                    "trade_id": t["id"],
                    "symbol": t["symbol"],
                    "name": t["name"],
                    "buy_date": t["buy_date"],
                    "buy_price": t["buy_price"],
                    "shares": t["shares"],
                    "error": t["error"],
                }
            )
            continue
        stops = t.get("stops") or {}
        holding = t.get("holding") or {}
        positions.append(
            {
                "trade_id": t["id"],
                "symbol": t["symbol"],
                "name": t["name"],
                "buy_date": t["buy_date"],
                "buy_price": t["buy_price"],
                "shares": t["shares"],
                "latest_price": t.get("latest_price"),
                "prev_close": t.get("prev_close"),
                "daily_change_pct": t.get("daily_change_pct"),
                "position_value": t.get("position_value"),
                "pnl_amount": t.get("pnl_amount"),
                "pnl_pct": holding.get("pnl_pct"),
                "max_gain_pct": holding.get("max_gain_pct"),
                "max_drawdown": holding.get("max_drawdown"),
                "hold_days": holding.get("hold_days"),
                "hard_stop_price": stops.get("hard_stop_price"),
                "hard_stop_triggered": stops.get("hard_stop_triggered"),
                "chandelier_stop_price": stops.get("chandelier_stop_price"),
                "chandelier_stop_triggered": stops.get("chandelier_stop_triggered"),
                "chandelier_stop_ratchet_price": stops.get("chandelier_stop_ratchet_price"),
                "chandelier_stop_ratchet_triggered": stops.get("chandelier_stop_ratchet_triggered"),
                "stop_mode": stops.get("stop_mode"),
            }
        )
        total_value += float(t.get("position_value") or 0.0)
        total_pnl += float(t.get("pnl_amount") or 0.0)
        total_cost += float(t["buy_price"]) * float(t["shares"])
        is_intraday = is_intraday or bool(t.get("is_intraday"))
        intraday_ts = intraday_ts or t.get("intraday_ts")

    return {
        "ok": True,
        "user": payload["user"]["username"],
        "is_intraday": is_intraday,
        "intraday_ts": intraday_ts,
        "summary": {
            "count": len(positions),
            "total_position_value": round(total_value, 2),
            "total_pnl_amount": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl / total_cost * 100, 2) if total_cost > 0 else 0.0,
        },
        "positions": positions,
    }
