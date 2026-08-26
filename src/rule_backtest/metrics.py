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
    return [{"date": str(day), "drawdown": float(dd_)} for day, dd_ in zip(df["date"], dd)]


def flat_run_days(trades: list[dict], dates: list[str]) -> list[float]:
    """各空仓段的长度（交易日）：起点→首次 BUY 的前导段 + 每段 SELL→下一次 BUY 的间隔。

    期末仍空仓的尾段不计入——与 avg_holding_days 排除期末未平仓对称：窗口截断
    会低估最后一段。日期不在净值序列时回退自然日差。无任何交易时返回空列表
    （语义上全程空仓但从未被交易事件界定，调用方按 None 处理）。
    """
    if not trades or not dates:
        return []
    nav_index = {str(d)[:10]: i for i, d in enumerate(dates)}

    def span(a: str, b: str) -> float:
        if a in nav_index and b in nav_index:
            return float(nav_index[b] - nav_index[a])
        return float((pd.Timestamp(b) - pd.Timestamp(a)).days)

    runs: list[float] = []
    pending_sell: str | None = None
    first_buy_seen = False
    for t in trades:
        side = str(t.get("side", "")).upper()
        day = str(t.get("date", ""))[:10]
        if not day:
            continue
        if side == "BUY":
            if not first_buy_seen:
                first_buy_seen = True
                leading = span(str(dates[0])[:10], day)
                if leading > 0:
                    runs.append(leading)
            if pending_sell:
                gap = span(pending_sell, day)
                if gap > 0:
                    runs.append(gap)
                pending_sell = None
        elif side == "SELL":
            pending_sell = day
    return runs


def compute_summary(daily_nav: list[dict], trades: list[dict], turnover_total: float) -> dict:
    if not daily_nav:
        return {
            "total_return": 0.0,
            "annual_return": 0.0,
            "max_drawdown": 0.0,
            "sharpe": 0.0,
            "sortino": 0.0,
            "calmar": 0.0,
            "n_returns": 0,
            "mean_daily_return": 0.0,
            "std_daily_return": 0.0,
            "downside_std": 0.0,
            "max_dd_peak_date": None,
            "max_dd_peak_equity": None,
            "max_dd_trough_date": None,
            "max_dd_trough_equity": None,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "trade_count": 0,
            "avg_holding_days": 0.0,
            "avg_flat_days": None,
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

    # 最大回撤的峰/谷明细（供前端悬停展示计算过程；无回撤时为 None）
    max_dd_peak_date = max_dd_trough_date = None
    max_dd_peak_equity = max_dd_trough_equity = None
    if max_drawdown < 0:
        eq = df["equity"].reset_index(drop=True)
        dd_series = (eq / eq.cummax().replace(0, np.nan) - 1.0).fillna(0.0)
        trough_pos = int(dd_series.to_numpy().argmin())
        peak_pos = int(eq.iloc[: trough_pos + 1].to_numpy().argmax())
        dates = df["date"].reset_index(drop=True)
        max_dd_peak_date = str(dates.iloc[peak_pos])
        max_dd_trough_date = str(dates.iloc[trough_pos])
        max_dd_peak_equity = float(eq.iloc[peak_pos])
        max_dd_trough_equity = float(eq.iloc[trough_pos])

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

    # 持仓天数：单仓位引擎 BUY→SELL 顺序配对，按交易日计（daily_nav 索引差），
    # 日期不在净值序列时回退自然日；期末仍持有的未平仓交易不计入。
    nav_index = {str(row.get("date", ""))[:10]: i for i, row in enumerate(daily_nav)}
    holding_days: list[float] = []
    last_buy_date: str | None = None
    for t in trades:
        side = str(t.get("side", "")).upper()
        day = str(t.get("date", ""))[:10]
        if side == "BUY":
            last_buy_date = day
        elif side == "SELL" and last_buy_date:
            if day in nav_index and last_buy_date in nav_index:
                holding_days.append(float(nav_index[day] - nav_index[last_buy_date]))
            else:
                holding_days.append(float((pd.Timestamp(day) - pd.Timestamp(last_buy_date)).days))
            last_buy_date = None
    avg_holding_days = sum(holding_days) / len(holding_days) if holding_days else 0.0

    # 空仓天数：无任何交易记 None（全程空仓但未被交易事件界定，直接给全周期会误导）；
    # 有交易但无空仓段（如首日买入持有到底）记 0.0。
    flat_days = flat_run_days(trades, [str(row.get("date", ""))[:10] for row in daily_nav])
    if flat_days:
        avg_flat_days: float | None = sum(flat_days) / len(flat_days)
    else:
        avg_flat_days = 0.0 if trades else None

    return {
        "total_return": float(total_return),
        "annual_return": float(annual_return),
        "max_drawdown": float(max_drawdown),
        "sharpe": float(sharpe),
        "sortino": float(sortino),
        "calmar": float(calmar),
        "n_returns": len(returns),
        "mean_daily_return": float(mean_ret),
        "std_daily_return": float(std_ret),
        "downside_std": float(downside_std),
        "max_dd_peak_date": max_dd_peak_date,
        "max_dd_peak_equity": max_dd_peak_equity,
        "max_dd_trough_date": max_dd_trough_date,
        "max_dd_trough_equity": max_dd_trough_equity,
        "win_rate": float(win_rate),
        "profit_factor": float(profit_factor),
        "trade_count": len(trades),
        "closed_trade_count": len(sell_pnls),
        "avg_holding_days": float(avg_holding_days),
        "avg_flat_days": float(avg_flat_days) if avg_flat_days is not None else None,
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
