"""MCP server for trend-quant —— 纯接口层。

本模块只做三件事：声明工具、从 Bearer token 解析用户身份、把异常翻译成
``{"ok": False, "error": ...}``。所有取数/计算/聚合/缓存逻辑都在 services
层（与 Web 端共用同一份实现），此处不重复实现任何业务逻辑：

1. **dashboard** -- 标的看板: ``services.dashboard_snapshot.dashboard_payload``，
   交易时段自动取每 5 分钟一轮的定时快照（与 Web 看板同一份数据），
   非交易时段/日K落库后取 EOD 日K口径，响应带 data_mode 标记。
2. **symbol_detail** -- 标的查看: ``services.symbol_detail.symbol_detail_payload``。
3. **calc_stop_loss** -- 辅助计算: ``services.stop_loss.compute_stop_loss``。
4. **calc_stop_loss_batch** -- 批量止损试算:
   ``services.stop_loss.compute_stop_loss_batch`` + ``summarize_stop_loss_batch``。
5. **list_instruments** -- 标的列表: ``services.instrument_catalog.search_instruments``。
6. **add_trade** -- 手工交易录入: ``services.trade_records.create_trade``
   （与网页端同一录入链路）。
7. **open_positions** -- 持仓概览: ``services.trade_records.open_positions_overview``。

通道鉴权：/mcp 由 McpBearerMiddleware（app/mcp_auth.py）做 Bearer token
校验，token→用户映射见 TREND_MCP_TOKENS；工具不再接收 username/password。
"""

from __future__ import annotations

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.mcp_auth import load_mcp_allowed_hosts
from audit.app_logger import get_logger
from services import trade_records as tr
from services.dashboard_snapshot import dashboard_payload
from services.instrument_catalog import search_instruments
from services.stop_loss import (
    StopLossError,
    compute_stop_loss,
    compute_stop_loss_batch,
    summarize_stop_loss_batch,
)
from services.symbol_detail import symbol_detail_payload

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
# 通道身份（MCP 通道特有，不属于业务逻辑）
# ---------------------------------------------------------------------------

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
    return tr.user_by_username(username)

# ---------------------------------------------------------------------------
# Tool 1 -- dashboard (auto-routed)
# ---------------------------------------------------------------------------

@mcp.tool()
def dashboard(category: str = "", detail: str = "full", mode: str = "auto") -> dict:
    """获取标的看板：按三级分类（L1/L2/L3）组织的全市场趋势数据，自动选择数据口径。

    口径路由（mode="auto"，默认）：
    - 交易时段（9:30 后，今日日K未落库）：取服务端每 5 分钟一轮的定时
      快照（与网页端看板同一份数据，最旧约 5 分钟），含当日盘中实时
      涨跌幅与盘中趋势值——盘中金叉/死叉当天即可见；
    - 非交易日 / 开盘前 / 今日日K已落库（约 16:30 后）：取日K（EOD）
      口径，最新一根K线为已落库的最近交易日；
    - 快照暂不可得（如 9:30-9:35 首轮未生成）时自动回退 EOD 口径。
    响应中的 data_mode 字段（"intraday_snapshot" / "eod"）标识本次口径，
    快照口径另带 snapshot_ts（快照计算时间）与 post_close 标记。

    每个标的含：最新趋势值 (trend_score)、趋势值 MA5 (trend_ma5，主排序
    指标)、同级强度百分位 (strength)、日/5日/20日/60日涨跌幅、趋势相位
    检测（上升/下降/震荡）、历史趋势值 MA5 序列 (trend_history)。

    Args:
        category: 可选，按分类筛选（匹配 L1/L2/L3），如 "ETF"、"宽基"、
            "跨境"、"股票"。不传则返回全部标的。
        detail: "full"（默认，完整序列）/ "lite"（每标的只保留最新
            K线/MACD 值，删除 trend_history 等长序列，响应体积约 1/10；
            只需要各指标最新值的扫描类场景请用 lite）。
        mode: "auto"（默认，按时段自动路由）/ "eod"（强制日K口径，
            盘中也不含今日形成中的K线）/ "intraday"（强制盘中快照口径，
            不可得时返回错误）。
    """
    return dashboard_payload(category=category, detail=detail, mode=mode)

# ---------------------------------------------------------------------------
# Tool 2 -- symbol_detail
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
    return symbol_detail_payload(symbol, days=days, rsi_period=rsi_period, intraday=intraday)

# ---------------------------------------------------------------------------
# Tool 3 -- calc_stop_loss
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
# Tool 3b -- calc_stop_loss_batch (bulk trial)
# ---------------------------------------------------------------------------

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
    try:
        results = compute_stop_loss_batch(items, stop_mode=stop_mode)
    except StopLossError as exc:
        return {"ok": False, "error": str(exc)}
    return summarize_stop_loss_batch(results)

# ---------------------------------------------------------------------------
# Tool 4 -- list_instruments
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
    return search_instruments(category=category, keyword=keyword, enabled_only=enabled_only)

# ---------------------------------------------------------------------------
# Tool 5 -- add_trade (manual trade entry)
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
# Tool 6 -- open_positions (realtime overview)
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
        return tr.open_positions_overview(user, stop_mode=stop_mode)
    except (tr.TradeAuthError, tr.TradePermissionError, tr.TradeRecordError) as exc:
        return {"ok": False, "error": str(exc)}
