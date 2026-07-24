"""手工交易 — 单笔买入的持仓指标聚合（手工交易页面的后端）。

止损价计算本身住在 ``services/stop_loss.py``（MCP 工具与本模块共用）；
这里只做持仓维度的聚合：净值序列、统计指标、止损触发检测。

持仓统计（最大回撤 / 夏普 / 索提诺 / 卡玛 / 年化）直接复用
``rule_backtest.metrics``，口径与回测完全一致。
"""

from __future__ import annotations

import pandas as pd

from core.display import load_instrument_name_map
from data.storage.db import get_db
from rule_backtest.metrics import compute_summary
from services.stop_loss import UNSET_INTRADAY_BAR, StopLossError, compute_stop_loss

__all__ = ["ManualTradeError", "compute_manual_trade"]


class ManualTradeError(StopLossError):
    """手工交易聚合中的业务错误（如买入日期晚于最新数据）。"""


def compute_manual_trade(
    symbol: str,
    buy_date: str,
    buy_price: float,
    db=None,
    intraday: bool = True,
    end_date: str | None = None,
    intraday_bar: dict | None | object = UNSET_INTRADAY_BAR,
) -> dict:
    """止损价 + 持仓指标的一站式计算（手工交易页面的后端）。

    持仓统计口径与回测一致：以买入价为初始净值 1.0，对买入日（含）之后的
    收盘价构造 daily_nav，复用 ``rule_backtest.metrics.compute_summary``
    （√252 年化夏普、cummax 最大回撤）。

    ``intraday=True``（默认）时，交易时段内会把实时报价合成的当日K线计入
    净值序列 / 止损触发 / 最高价 / 最新价（ATR 仍为历史完整K线口径，
    见 ``services/stop_loss.py`` docstring）。``intraday_bar`` 显式传入
    （含 None）时跳过实时拉取，直接复用该值（列表接口同 symbol 去重）。

    ``end_date`` 用于已清仓交易：净值序列截断到该日（含），强制关闭
    intraday，所有指标按截止日口径。

    Raises:
        StopLossError: 标的无效、无数据（来自 ``compute_stop_loss``）。
        ManualTradeError: 买入日期晚于最新数据。
    """
    stops = compute_stop_loss(
        symbol,
        buy_date,
        buy_price,
        db=db,
        intraday=intraday,
        end_date=end_date,
        intraday_bar=intraday_bar,
    )
    symbol = stops["symbol"]
    buy_ts = pd.Timestamp(buy_date)
    end_ts = pd.Timestamp(end_date) if end_date is not None else None

    db = db or get_db()
    df = db.load_market_data(symbol).copy()
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    if end_ts is not None:
        df = df[df["time"] <= end_ts]

    since = df[df["time"] >= buy_ts]
    if since.empty:
        raise ManualTradeError(f"买入日期 {buy_date} 晚于最新数据日期")

    closes = pd.to_numeric(since["close"], errors="coerce")
    daily_nav = [
        {"date": str(t.date()), "equity": float(c) / buy_price}
        for t, c in zip(since["time"], closes, strict=True)
        if pd.notna(c) and buy_price > 0
    ]
    if not daily_nav:
        raise ManualTradeError("买入日期之后无有效收盘价数据")

    # 盘中实时叠加：当日合成K线作为一个额外净值点（日期与最后EOD日不同才追加）
    intraday_bar = stops.get("intraday_bar")
    if intraday_bar and intraday_bar["date"] > daily_nav[-1]["date"]:
        daily_nav.append(
            {"date": intraday_bar["date"], "equity": intraday_bar["close"] / buy_price}
        )

    summary = compute_summary(daily_nav, trades=[], turnover_total=0.0)

    latest_close = stops["latest_price"]
    pnl_points = round(latest_close - buy_price, 4)
    pnl_pct = round((latest_close / buy_price - 1) * 100, 2)
    max_gain_pct = round((stops["highest_since_buy"] / buy_price - 1) * 100, 2)

    # 硬止损击穿检测：买入（含）以来最低价 ≤ 硬止损价
    hard_stop_price = stops["hard_stop_price"]
    hard_stop_triggered = False
    hard_stop_trigger_date: str | None = None
    lows = pd.to_numeric(since["low"], errors="coerce")
    for t, low in zip(since["time"], lows, strict=True):
        if pd.notna(low) and float(low) <= hard_stop_price:
            hard_stop_triggered = True
            hard_stop_trigger_date = str(t.date())
            break
    if not hard_stop_triggered and intraday_bar and intraday_bar["low"] <= hard_stop_price:
        hard_stop_triggered = True
        hard_stop_trigger_date = intraday_bar["date"]

    # 吊灯止损：最新收盘价是否已跌破
    chandelier_stop_price = stops["chandelier_stop_price"]
    chandelier_stop_triggered = bool(latest_close <= chandelier_stop_price)

    hard_distance_pct = (
        round((latest_close / hard_stop_price - 1) * 100, 2) if hard_stop_price > 0 else 0.0
    )
    chandelier_distance_pct = (
        round((latest_close / chandelier_stop_price - 1) * 100, 2)
        if chandelier_stop_price > 0
        else 0.0
    )

    name = load_instrument_name_map().get(symbol, "")

    return {
        "symbol": symbol,
        "name": name,
        "buy_date": buy_date,
        "buy_price": buy_price,
        "start_date": daily_nav[0]["date"],
        "latest_date": daily_nav[-1]["date"],
        "is_intraday": bool(stops.get("is_intraday")),
        "intraday_ts": stops.get("intraday_ts"),
        "stops": {
            **stops,
            "hard_stop_triggered": hard_stop_triggered,
            "hard_stop_trigger_date": hard_stop_trigger_date,
            "chandelier_stop_triggered": chandelier_stop_triggered,
            "hard_stop_distance_pct": hard_distance_pct,
            "chandelier_stop_distance_pct": chandelier_distance_pct,
        },
        "holding": {
            "hold_days": len(daily_nav),
            "pnl_points": pnl_points,
            "pnl_pct": pnl_pct,
            "max_gain_pct": max_gain_pct,
            "total_return": round(summary["total_return"] * 100, 2),
            "annual_return": round(summary["annual_return"] * 100, 2),
            "max_drawdown": round(summary["max_drawdown"] * 100, 2),
            "sharpe": round(summary["sharpe"], 2),
            "sortino": round(summary["sortino"], 2),
            "calmar": round(summary["calmar"], 2),
        },
    }
