"""手工交易 — 单笔买入的持仓指标聚合（手工交易页面的后端）。

止损价计算本身住在 ``services/stop_loss.py``（MCP 工具与本模块共用）；
这里只做持仓维度的聚合：净值序列、统计指标、止损触发检测。

持仓统计（最大回撤 / 夏普 / 索提诺 / 卡玛 / 年化）直接复用
``rule_backtest.metrics``，口径与回测完全一致。
"""

from __future__ import annotations

import math

import pandas as pd

from core.display import load_instrument_name_map
from data.storage.db import get_db
from rule_backtest.metrics import compute_summary
from services.stop_loss import UNSET_INTRADAY_BAR, StopLossError, compute_stop_loss

__all__ = ["ManualTradeError", "compute_manual_trade", "compute_position_sizing"]


class ManualTradeError(StopLossError):
    """手工交易聚合中的业务错误（如买入日期晚于最新数据）。"""


def compute_position_sizing(
    buy_price: float, hard_stop_price: float, risk_budget: float
) -> dict:
    """按风险预算计算最大可买入份数。

    每股风险 = 买入价 − 硬止损价（硬止损触发时的每股损失）；
    可买份数 = 风险预算 ÷ 每股风险，下取整到百位（ETF 一手 100 份）。
    如 12345.67 份 → 12300 份。
    """
    risk_per_share = round(buy_price - hard_stop_price, 4)
    max_qty = 0
    if risk_per_share > 0 and risk_budget > 0:
        # +1e-9 防浮点误差把整百倍误判少一手（如 0.3/0.001 类边界）
        max_qty = int(math.floor(risk_budget / risk_per_share / 100 + 1e-9)) * 100
    return {
        "risk_budget": risk_budget,
        "risk_per_share": risk_per_share,
        "max_qty": max_qty,
        "max_loss": round(max_qty * risk_per_share, 2),
        "position_value": round(max_qty * buy_price, 2),
    }


def compute_manual_trade(
    symbol: str,
    buy_date: str,
    buy_price: float,
    db=None,
    intraday: bool = True,
    end_date: str | None = None,
    intraday_bar: dict | None | object = UNSET_INTRADAY_BAR,
    stop_mode: str | None = None,
    risk_budget: float | None = None,
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
    intraday，所有指标按截止日口径。``stop_mode`` 透传给止损计算
    （"tight" 紧止损 / None|"loose" 松止损）。``risk_budget`` 非空时
    附带按硬止损价推算的最大可买入份数（``position_sizing`` 字段）。

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
        stop_mode=stop_mode,
    )
    symbol = stops["symbol"]
    buy_ts = pd.Timestamp(buy_date)
    end_ts = pd.Timestamp(end_date) if end_date is not None else None

    db = db or get_db()
    df = db.load_market_data(symbol).copy()
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    if end_ts is not None:
        df = df[df["time"] <= end_ts]
    # 纯 EOD 收盘序列（不含下方可能追加的盘中合成K线），供当日涨跌幅取前收
    eod_closes = pd.to_numeric(df["close"], errors="coerce").dropna()

    since = df[df["time"] >= buy_ts]
    if since.empty:
        # 买入日为当日且日K尚未落库（盘中 / 收盘后补库任务前的窗口）：
        # 用 compute_stop_loss 已由实时报价合成的当日K线补齐，使当日试算可用；
        # 持仓净值/止损触发随每次试算的最新报价刷新（ATR 仍为前一交易日口径）。
        ib = stops.get("intraday_bar")
        if ib and pd.Timestamp(ib["date"]) >= buy_ts:
            df = pd.concat(
                [
                    df,
                    pd.DataFrame(
                        [
                            {
                                "time": pd.Timestamp(ib["date"]),
                                "open": ib["open"],
                                "high": ib["high"],
                                "low": ib["low"],
                                "close": ib["close"],
                                "volume": 0.0,
                                "amount": 0.0,
                            }
                        ]
                    ),
                ],
                ignore_index=True,
            )
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

    # 指标口径：以买入价为初始净值 1.0（含买入日收益）。
    # daily_nav 首点是买入日收盘净值（已含当日浮动），compute_summary 以序列
    # 首点为基准，故前补买入价对应的 1.0 起点；hold_days/起止日期仍用
    # daily_nav（交易日计数语义不变）。
    metric_nav = [{"date": str(buy_ts.date()), "equity": 1.0}] + daily_nav
    summary = compute_summary(metric_nav, trades=[], turnover_total=0.0)

    latest_close = stops["latest_price"]
    pnl_points = round(latest_close - buy_price, 4)
    pnl_pct = round((latest_close / buy_price - 1) * 100, 2)
    max_gain_pct = round((stops["highest_since_buy"] / buy_price - 1) * 100, 2)

    # 当日涨跌幅：最新价（盘中为实时价）相对前一交易日收盘价。
    # intraday_bar 存在 → 当日K线未落库，前收 = 最后一根 EOD 收盘；
    # 否则最新价即最后一根 EOD 收盘，前收取倒数第二根（非交易时段时
    # 即"最新交易日涨跌幅"）。
    if intraday_bar is not None:
        prev_close = round(float(eod_closes.iloc[-1]), 4) if len(eod_closes) else None
    else:
        prev_close = round(float(eod_closes.iloc[-2]), 4) if len(eod_closes) >= 2 else None
    daily_change_pct = (
        round((latest_close / prev_close - 1) * 100, 2) if prev_close else None
    )

    # 硬止损击穿检测：买入（含）以来最低价 ≤ 硬止损价。
    # 记录首次击穿日的价格明细（最低/收盘），供前端徽章悬停提示展示历史。
    hard_stop_price = stops["hard_stop_price"]
    hard_stop_triggered = False
    hard_stop_trigger_date: str | None = None
    hard_stop_trigger_low: float | None = None
    hard_stop_trigger_close: float | None = None
    lows = pd.to_numeric(since["low"], errors="coerce")
    closes_since = pd.to_numeric(since["close"], errors="coerce")
    for t, low, close in zip(since["time"], lows, closes_since, strict=True):
        if pd.notna(low) and float(low) <= hard_stop_price:
            hard_stop_triggered = True
            hard_stop_trigger_date = str(t.date())
            hard_stop_trigger_low = round(float(low), 4)
            hard_stop_trigger_close = round(float(close), 4) if pd.notna(close) else None
            break
    if not hard_stop_triggered and intraday_bar and intraday_bar["low"] <= hard_stop_price:
        hard_stop_triggered = True
        hard_stop_trigger_date = intraday_bar["date"]
        hard_stop_trigger_low = round(float(intraday_bar["low"]), 4)
        hard_stop_trigger_close = round(float(intraday_bar["close"]), 4)

    # 吊灯止损：最新收盘价是否已跌破
    chandelier_stop_price = stops["chandelier_stop_price"]
    chandelier_stop_triggered = bool(latest_close <= chandelier_stop_price)
    chandelier_stop_ratchet_price = stops.get("chandelier_stop_ratchet_price", 0.0)
    chandelier_stop_ratchet_triggered = bool(
        chandelier_stop_ratchet_price > 0 and latest_close <= chandelier_stop_ratchet_price
    )

    hard_distance_pct = (
        round((latest_close / hard_stop_price - 1) * 100, 2) if hard_stop_price > 0 else 0.0
    )
    chandelier_distance_pct = (
        round((latest_close / chandelier_stop_price - 1) * 100, 2)
        if chandelier_stop_price > 0
        else 0.0
    )
    chandelier_ratchet_distance_pct = (
        round((latest_close / chandelier_stop_ratchet_price - 1) * 100, 2)
        if chandelier_stop_ratchet_price > 0
        else 0.0
    )

    name = load_instrument_name_map().get(symbol, "")

    result = {
        "symbol": symbol,
        "name": name,
        "buy_date": buy_date,
        "buy_price": buy_price,
        "start_date": daily_nav[0]["date"],
        "latest_date": daily_nav[-1]["date"],
        "prev_close": prev_close,
        "daily_change_pct": daily_change_pct,
        "is_intraday": bool(stops.get("is_intraday")),
        "intraday_ts": stops.get("intraday_ts"),
        "stops": {
            **stops,
            "hard_stop_triggered": hard_stop_triggered,
            "hard_stop_trigger_date": hard_stop_trigger_date,
            "hard_stop_trigger_low": hard_stop_trigger_low,
            "hard_stop_trigger_close": hard_stop_trigger_close,
            "chandelier_stop_triggered": chandelier_stop_triggered,
            "chandelier_stop_ratchet_triggered": chandelier_stop_ratchet_triggered,
            "hard_stop_distance_pct": hard_distance_pct,
            "chandelier_stop_distance_pct": chandelier_distance_pct,
            "chandelier_stop_ratchet_distance_pct": chandelier_ratchet_distance_pct,
        },
        "holding": {
            "hold_days": len(daily_nav),
            "pnl_points": pnl_points,
            "pnl_pct": pnl_pct,
            "max_gain_pct": max_gain_pct,
            "highest_since_buy": stops["highest_since_buy"],
            "highest_since_buy_date": stops.get("highest_since_buy_date"),
            "total_return": round(summary["total_return"] * 100, 2),
            "annual_return": round(summary["annual_return"] * 100, 2),
            "max_drawdown": round(summary["max_drawdown"] * 100, 2),
            "max_dd_peak_date": summary["max_dd_peak_date"],
            "max_dd_peak_equity": summary["max_dd_peak_equity"],
            "max_dd_trough_date": summary["max_dd_trough_date"],
            "max_dd_trough_equity": summary["max_dd_trough_equity"],
            "sharpe": round(summary["sharpe"], 2),
            "sortino": round(summary["sortino"], 2),
            "calmar": round(summary["calmar"], 2),
            "n_returns": summary["n_returns"],
            "mean_daily_return": round(summary["mean_daily_return"] * 100, 4),
            "std_daily_return": round(summary["std_daily_return"] * 100, 4),
            "downside_std": round(summary["downside_std"] * 100, 4),
        },
    }
    if risk_budget is not None:
        result["position_sizing"] = compute_position_sizing(
            buy_price, hard_stop_price, risk_budget
        )
    return result
