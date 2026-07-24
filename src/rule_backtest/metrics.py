from __future__ import annotations

from collections import defaultdict

import numpy as np
import pandas as pd


def compute_drawdown(daily_nav: list[dict]) -> list[dict]:
    if not daily_nav:
        return []
    df = pd.DataFrame(daily_nav)
    equity = pd.to_numeric(df["equity"], errors="coerce")
    rolling_max = equity.cummax().replace(0, np.nan)
    dd = (equity / rolling_max - 1.0).fillna(0.0)
    return [{"date": str(row["date"]), "drawdown": float(dd.iloc[idx])} for idx, row in df.iterrows()]


def compute_summary(daily_nav: list[dict], trades: list[dict], turnover_total: float) -> dict:
    if not daily_nav:
        return {
            "total_return": 0.0,
            "annual_return": 0.0,
            "max_drawdown": 0.0,
            "sharpe": 0.0,
            "sortino": 0.0,
            "calmar": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "trade_count": 0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "payoff_ratio": 0.0,
            "turnover": 0.0,
            "total_commission": 0.0,
            "total_stamp_tax": 0.0,
            "total_trading_cost": 0.0,
        }

    df = pd.DataFrame(daily_nav)
    df["equity"] = pd.to_numeric(df["equity"], errors="coerce")
    df = df.dropna(subset=["equity"])
    start_equity = float(df["equity"].iloc[0])
    end_equity = float(df["equity"].iloc[-1])
    total_return = end_equity / start_equity - 1.0 if start_equity else 0.0
    n_days = max(len(df) - 1, 1)
    annual_return = (1.0 + total_return) ** (252.0 / n_days) - 1.0 if total_return > -1 else -1.0

    returns = df["equity"].pct_change().dropna()
    mean_ret = float(returns.mean()) if not returns.empty else 0.0
    std_ret = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    sharpe = mean_ret / std_ret * np.sqrt(252.0) if std_ret > 0 else 0.0
    downside = returns[returns < 0]
    downside_std = float(downside.std(ddof=1)) if len(downside) > 1 else 0.0
    sortino = mean_ret / downside_std * np.sqrt(252.0) if downside_std > 0 else 0.0

    dd_rows = compute_drawdown(daily_nav)
    max_drawdown = min((float(row["drawdown"]) for row in dd_rows), default=0.0)
    calmar = annual_return / abs(max_drawdown) if max_drawdown < 0 else 0.0

    sell_pnls = [float(t.get("pnl", 0.0) or 0.0) for t in trades if str(t.get("side", "")).upper() == "SELL"]
    wins = [x for x in sell_pnls if x > 0]
    losses = [x for x in sell_pnls if x < 0]
    win_rate = len(wins) / len(sell_pnls) if sell_pnls else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    avg_win = gross_profit / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    payoff_ratio = avg_win / avg_loss if avg_loss > 0 else 0.0
    avg_equity = float(df["equity"].mean()) if not df.empty else 0.0
    turnover = turnover_total / avg_equity if avg_equity > 0 else 0.0
    total_commission = sum(float(t.get("commission", 0.0) or 0.0) for t in trades)
    total_stamp_tax = sum(float(t.get("stamp_tax", 0.0) or 0.0) for t in trades)

    return {
        "total_return": float(total_return),
        "annual_return": float(annual_return),
        "max_drawdown": float(max_drawdown),
        "sharpe": float(sharpe),
        "sortino": float(sortino),
        "calmar": float(calmar),
        "win_rate": float(win_rate),
        "profit_factor": float(profit_factor),
        "trade_count": int(len(trades)),
        "closed_trade_count": int(len(sell_pnls)),
        "avg_win": float(avg_win),
        "avg_loss": float(avg_loss),
        "payoff_ratio": float(payoff_ratio),
        "turnover": float(turnover),
        "total_commission": float(total_commission),
        "total_stamp_tax": float(total_stamp_tax),
        "total_trading_cost": float(total_commission + total_stamp_tax),
    }


def annual_returns(daily_nav: list[dict]) -> list[dict]:
    if not daily_nav:
        return []
    df = pd.DataFrame(daily_nav)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["equity"] = pd.to_numeric(df["equity"], errors="coerce")
    df = df.dropna(subset=["date", "equity"]).sort_values("date")
    if df.empty:
        return []
    df["year"] = df["date"].dt.year
    year_end = df.groupby("year", as_index=False).last()[["year", "equity"]]
    rows: list[dict] = []
    prev = float(df["equity"].iloc[0])
    for _, row in year_end.iterrows():
        equity = float(row["equity"])
        rows.append({"year": int(row["year"]), "return": equity / prev - 1.0 if prev else 0.0})
        prev = equity
    return rows


def monthly_returns(daily_nav: list[dict]) -> list[dict]:
    if not daily_nav:
        return []
    df = pd.DataFrame(daily_nav)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["equity"] = pd.to_numeric(df["equity"], errors="coerce")
    df = df.dropna(subset=["date", "equity"]).sort_values("date")
    if df.empty:
        return []
    monthly = df.set_index("date")["equity"].resample("ME").last().dropna()
    returns = monthly.pct_change().dropna()
    return [{"month": ts.strftime("%Y-%m"), "return": float(value)} for ts, value in returns.items()]


_MONTH_LABELS = ["01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12"]


def compute_monthly_heatmap(daily_nav: list[dict]) -> dict:
    """月度收益热力图数据，格式与组合回测页一致。

    Returns:
        {"years": [2024, ...], "months": ["01".."12"],
         "data": [[month_idx, year_idx, return_pct], ...]}
    """
    if not daily_nav:
        return {"years": [], "months": list(_MONTH_LABELS), "data": []}
    df = pd.DataFrame(daily_nav)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["equity"] = pd.to_numeric(df["equity"], errors="coerce")
    df = df.dropna(subset=["date", "equity"]).sort_values("date")
    if df.empty:
        return {"years": [], "months": list(_MONTH_LABELS), "data": []}

    monthly = df.set_index("date")["equity"].resample("ME").last().dropna()
    monthly_ret = monthly.pct_change().dropna()

    years = sorted(monthly_ret.index.year.unique().tolist())
    year_idx = {y: i for i, y in enumerate(years)}

    data: list[list[float]] = []
    for ts, ret in monthly_ret.items():
        y = int(ts.year)
        m = int(ts.month) - 1
        data.append([m, year_idx[y], float(ret * 100.0)])

    return {"years": years, "months": list(_MONTH_LABELS), "data": data}


def _annual_sharpe_map(daily_nav: list[dict]) -> dict[int, float]:
    if not daily_nav:
        return {}
    df = pd.DataFrame(daily_nav)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["equity"] = pd.to_numeric(df["equity"], errors="coerce")
    df = df.dropna(subset=["date", "equity"]).sort_values("date")
    if df.empty:
        return {}
    df["year"] = df["date"].dt.year

    out: dict[int, float] = {}
    for year, group in df.groupby("year"):
        returns = group["equity"].pct_change().dropna()
        mean_ret = float(returns.mean()) if not returns.empty else 0.0
        std_ret = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
        sharpe = (mean_ret / std_ret * np.sqrt(252.0)) if std_ret > 0 else 0.0
        out[int(year)] = float(sharpe)
    return out


def _annual_max_drawdown_map(daily_nav: list[dict]) -> dict[int, float]:
    """每个自然年内的最大回撤（含上一年末净值作为回撤基准起点）。"""
    if not daily_nav:
        return {}
    df = pd.DataFrame(daily_nav)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["equity"] = pd.to_numeric(df["equity"], errors="coerce")
    df = df.dropna(subset=["date", "equity"]).sort_values("date")
    if df.empty:
        return {}
    df["year"] = df["date"].dt.year

    out: dict[int, float] = {}
    prev = float(df["equity"].iloc[0])
    for year, group in df.groupby("year"):
        series = pd.concat([pd.Series([prev]), group["equity"]], ignore_index=True)
        rolling_max = series.cummax().replace(0, np.nan)
        dd = (series / rolling_max - 1.0).fillna(0.0)
        out[int(year)] = float(dd.min())
        prev = float(group["equity"].iloc[-1])
    return out


def _annual_calmar(return_value: float | None, max_drawdown: float | None) -> float | None:
    if return_value is None or max_drawdown is None:
        return None
    return float(return_value / abs(max_drawdown)) if max_drawdown < 0 else 0.0


def _parse_trade_year(value: object) -> int | None:
    text = str(value or "").strip()
    if len(text) >= 4 and text[:4].isdigit():
        return int(text[:4])
    dt = pd.to_datetime(text, errors="coerce")
    if pd.isna(dt):
        return None
    return int(dt.year)


def _profit_factor_from_pnls(pnl_rows: list[float]) -> float:
    if not pnl_rows:
        return 0.0
    gain = sum(x for x in pnl_rows if x > 0)
    loss = abs(sum(x for x in pnl_rows if x < 0))
    if loss <= 0:
        return 999.0 if gain > 0 else 0.0
    return gain / loss


def _annual_trade_stats_map(trades: list[dict]) -> dict[int, dict]:
    pnl_map: dict[int, list[float]] = defaultdict(list)
    for t in trades or []:
        if str(t.get("side", "")).upper() != "SELL":
            continue
        year = _parse_trade_year(t.get("date"))
        if year is None:
            continue
        pnl_map[year].append(float(t.get("pnl", 0.0) or 0.0))

    out: dict[int, dict] = {}
    for year, pnl_rows in pnl_map.items():
        trade_count = len(pnl_rows)
        win_rate = (sum(1 for x in pnl_rows if x > 0) / trade_count) if trade_count > 0 else 0.0
        out[int(year)] = {
            "trade_count": int(trade_count),
            "win_rate": float(win_rate),
            "profit_factor": float(_profit_factor_from_pnls(pnl_rows)),
        }
    return out


def compute_annual_returns(
    daily_nav: list[dict],
    trades: list[dict] | None = None,
    benchmark_daily_nav: list[dict] | None = None,
) -> list[dict]:
    """年度收益表数据，字段与组合回测页一致。

    每行包含: year / return / sharpe / max_drawdown / calmar /
    trade_count / win_rate / profit_factor，
    若提供基准净值则附带 benchmark_return / benchmark_sharpe /
    benchmark_max_drawdown / benchmark_calmar。
    """
    strategy_rows = annual_returns(daily_nav)
    if not strategy_rows:
        return []

    strategy_sharpe = _annual_sharpe_map(daily_nav)
    strategy_mdd = _annual_max_drawdown_map(daily_nav)
    trade_stats = _annual_trade_stats_map(trades or [])
    benchmark_rows = annual_returns(benchmark_daily_nav or [])
    benchmark_return_map = {int(r["year"]): float(r["return"]) for r in benchmark_rows}
    benchmark_sharpe_map = _annual_sharpe_map(benchmark_daily_nav or [])
    benchmark_mdd_map = _annual_max_drawdown_map(benchmark_daily_nav or [])

    out: list[dict] = []
    for row in strategy_rows:
        year = int(row["year"])
        year_return = float(row["return"])
        year_mdd = float(strategy_mdd.get(year, 0.0))
        bench_return = benchmark_return_map.get(year)
        bench_mdd = benchmark_mdd_map.get(year)
        tstats = trade_stats.get(year, {})
        out.append(
            {
                "year": year,
                "return": year_return,
                "sharpe": float(strategy_sharpe.get(year, 0.0)),
                "max_drawdown": year_mdd,
                "calmar": _annual_calmar(year_return, year_mdd),
                "trade_count": int(tstats.get("trade_count", 0)),
                "win_rate": float(tstats.get("win_rate", 0.0)),
                "profit_factor": float(tstats.get("profit_factor", 0.0)),
                "benchmark_return": bench_return,
                "benchmark_sharpe": benchmark_sharpe_map.get(year),
                "benchmark_max_drawdown": bench_mdd,
                "benchmark_calmar": _annual_calmar(bench_return, bench_mdd),
            }
        )
    return out
