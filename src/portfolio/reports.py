"""组合级指标与报告（详设 §5.4.3）。

- 组合级：NAV 曲线 + 年化/回撤/Sharpe/Sortino/Calmar（复用
  rule_backtest/metrics.compute_summary，输入组合 NAV）；
- 业界通用补充：相对基准统计（alpha/beta/IR/TE/超额/上下行捕获率）、
  滚动 Sharpe、回撤持续期/修复时间、收益分布（偏度/峰度/尾部比/
  VaR/CVaR）、成本拖累（费用 ÷ 毛收益）；
- 组合特有：heat/槽位利用率/换手率/exposure/行业集中度时序；
- 事件级：round trips（R 倍数/MAE/MFE）、unfilled 统计（按原因）、
  风控拦截统计（按 gate）。

报告 benchmark 展示是多选（看策略在怀疑阶梯上的位置）——与 L4 实验的
"单一对照算 deltas"是两个概念（§5.4.3 口径声明）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from rule_backtest.metrics import compute_summary


def daily_returns(nav_rows: list[dict]) -> pd.Series:
    df = pd.DataFrame(nav_rows)
    if df.empty:
        return pd.Series(dtype=float)
    df["date"] = pd.to_datetime(df["date"])
    df["equity"] = pd.to_numeric(df["equity"], errors="coerce")
    df = df.dropna(subset=["date", "equity"]).sort_values("date")
    return pd.Series(df["equity"].pct_change().to_numpy(), index=df["date"])


def benchmark_relative(nav_rows: list[dict], bench_nav_rows: list[dict]) -> dict:
    """相对基准统计（两序列按日期对齐后计算）。"""
    rp = daily_returns(nav_rows).dropna()
    rb = daily_returns(bench_nav_rows).dropna()
    joined = pd.concat([rp.rename("p"), rb.rename("b")], axis=1).dropna()
    if len(joined) < 30:
        return {}
    p, b = joined["p"].to_numpy(), joined["b"].to_numpy()
    var_b = float(np.var(b, ddof=1))
    beta = float(np.cov(p, b, ddof=1)[0, 1] / var_b) if var_b > 0 else 0.0
    alpha_daily = float(p.mean() - beta * b.mean())
    excess = p - b
    te = float(excess.std(ddof=1) * np.sqrt(252)) if len(excess) > 1 else 0.0
    ir = float(excess.mean() / excess.std(ddof=1) * np.sqrt(252)) if excess.std(ddof=1) > 0 else 0.0
    up = b > 0
    down = b < 0
    up_capture = float(p[up].mean() / b[up].mean()) if up.any() and b[up].mean() != 0 else None
    down_capture = float(p[down].mean() / b[down].mean()) if down.any() and b[down].mean() != 0 else None
    return {
        "alpha_annual": alpha_daily * 252,
        "beta": beta,
        "information_ratio": ir,
        "tracking_error": te,
        "excess_return_annual": float(excess.mean() * 252),
        "up_capture": up_capture,
        "down_capture": down_capture,
    }


def rolling_sharpe(nav_rows: list[dict], window: int = 126) -> list[dict]:
    """滚动 Sharpe（6m=126 / 12m=252 窗口由调用方选）。"""
    r = daily_returns(nav_rows).dropna()
    if len(r) < window + 1:
        return []
    roll = r.rolling(window)
    sharpe = roll.mean() / roll.std(ddof=1) * np.sqrt(252)
    return [
        {"date": d.date().isoformat(), "sharpe": float(v)}
        for d, v in sharpe.dropna().items()
        if np.isfinite(v)
    ]


def drawdown_durations(nav_rows: list[dict]) -> dict:
    """水下曲线的时长维度：最长水下天数 + 最大回撤的修复天数。

    单位统一为**交易日**（loop-review R1-P3-6：NAV 序列的行本来就是交易日，
    修复天数此前按日历日 .days 计，与 max_underwater_days 单位不一致）。"""
    r = daily_returns(nav_rows)
    if r.empty:
        return {"max_underwater_days": 0, "max_dd_recovery_days": None}
    equity = (1.0 + r.fillna(0.0)).cumprod()
    cummax = equity.cummax()
    underwater = equity < cummax - 1e-12
    max_run = run = 0
    for flag in underwater:
        run = run + 1 if flag else 0
        max_run = max(max_run, run)
    dd = equity / cummax - 1.0
    trough = int(dd.to_numpy().argmin())
    recovery = None
    if dd.iloc[trough] < 0:
        peak_val = cummax.iloc[trough]
        after = equity.iloc[trough:]
        recovered = after[after >= peak_val - 1e-12]
        if not recovered.empty:
            # 交易日差 = 索引位置差（NAV 行即交易日轴）
            recovery = int(r.index.get_loc(recovered.index[0]) - trough)
    return {"max_underwater_days": max_run, "max_dd_recovery_days": recovery}


def return_distribution(nav_rows: list[dict]) -> dict:
    """偏度/峰度/尾部比/VaR/CVaR（日频）。"""
    r = daily_returns(nav_rows).dropna().to_numpy()
    if len(r) < 20:
        return {}
    std = float(r.std(ddof=1))
    skew = float(pd.Series(r).skew())
    kurt = float(pd.Series(r).kurt())
    var5 = float(np.percentile(r, 5))
    cvar5 = float(r[r <= var5].mean()) if (r <= var5).any() else var5
    tail = float(np.abs(np.percentile(r, 95) / var5)) if var5 != 0 else None
    return {
        "skew": skew, "kurtosis": kurt,
        "var_5": var5, "cvar_5": cvar5, "tail_ratio": tail,
        "std_daily": std,
    }


def cost_drag(fills: list[dict]) -> dict:
    """成本拖累：费用合计 ÷ 毛收益（A 股成本环境下每条规则的及格线）。"""
    total_fee = sum(float(f.get("fee_total", 0.0)) for f in fills)
    gross_pnl = 0.0
    # 毛收益 = 卖出成交额 − 买入成交额 的逐笔配对（FIFO per symbol）
    rounds = pair_round_trips(fills)
    gross_pnl = sum(r["pnl_gross"] for r in rounds)
    return {
        "total_fees": total_fee,
        "gross_pnl_before_fees": gross_pnl + total_fee,
        "cost_to_gross": (total_fee / gross_pnl) if gross_pnl > 0 else None,
    }


def pair_round_trips(fills: list[dict]) -> list[dict]:
    """fills → round trips（同标的首笔买入配对随后的卖出；MVP 单仓位）。

    fills 需带 ``side``（DB 路径由 EngineStore.load_fills 联 engine_orders
    补出；内存路径由回测器直接携带）。
    """
    rounds: list[dict] = []
    open_lots: dict[str, list[dict]] = {}
    for f in fills:
        symbol = f["symbol"]
        if str(f.get("side", "")).lower() == "buy":
            open_lots.setdefault(symbol, []).append(f)
        else:
            lots = open_lots.get(symbol)
            if not lots:
                continue
            entry = lots.pop(0)
            qty = int(f["quantity"])
            gross = qty * (float(f["fill_price"]) - float(entry["fill_price"]))
            rounds.append({
                "symbol": symbol,
                "entry_date": f0(entry), "exit_date": f0(f),
                "entry_price": float(entry["fill_price"]),
                "exit_price": float(f["fill_price"]),
                "qty": qty,
                "pnl_gross": gross,
                "pnl_net": gross - float(entry.get("fee_total", 0.0)) - float(f.get("fee_total", 0.0)),
            })
    return rounds


def f0(fill: dict) -> str:
    return str(fill.get("fill_date") or "")


def build_report(
    db,
    *,
    run_id: str,
    nav_rows: list[dict],
    fills: list[dict],
    unfilled: list[dict],
    gate_log: list[dict],
    benchmarks: dict[str, list[dict]] | None = None,
    positions_snapshots: list[dict] | None = None,
    slot_limit: int | None = None,
    round_trips: list[dict] | None = None,
) -> dict:
    """汇总一份组合回测报告（完整明细，不摘要化——§6.5.0 报告完整性）。"""
    summary = compute_summary(nav_rows, trades=[], turnover_total=_turnover(fills, nav_rows))
    bench_rel = {
        name: benchmark_relative(nav_rows, rows)
        for name, rows in (benchmarks or {}).items()
    }
    unfilled_by_reason: dict[str, int] = {}
    for u in unfilled:
        unfilled_by_reason[u["reason"]] = unfilled_by_reason.get(u["reason"], 0) + 1
    gate_by_name: dict[str, int] = {}
    for g in gate_log:
        gate_by_name[g["gate"]] = gate_by_name.get(g["gate"], 0) + 1

    report = {
        "run_id": run_id,
        "summary": summary,
        "benchmark_relative": bench_rel,
        "rolling_sharpe_6m": rolling_sharpe(nav_rows, 126),
        "rolling_sharpe_12m": rolling_sharpe(nav_rows, 252),
        "drawdown_durations": drawdown_durations(nav_rows),
        "return_distribution": return_distribution(nav_rows),
        "cost": cost_drag(fills),
        "turnover_total": _turnover(fills, nav_rows),
        "heat_series": [{"date": r["date"], "heat": r.get("heat")} for r in nav_rows],
        "exposure_series": [{"date": r["date"], "exposure": r.get("exposure")} for r in nav_rows],
        "slot_utilization": _slot_utilization(positions_snapshots or [], slot_limit),
        "concentration_series": _concentration_series(db, positions_snapshots or []),
        # round trips：优先回测器的富化版（R 倍数/MAE/MFE）；缺省由 fills 配对
        "round_trips": round_trips if round_trips is not None else pair_round_trips(fills),
        "unfilled_by_reason": unfilled_by_reason,
        "gate_rejections": gate_by_name,
        "trade_count": len(fills),
    }
    return report


def _turnover(fills: list[dict], nav_rows: list[dict]) -> float:
    traded = sum(abs(float(f["fill_price"]) * int(f["quantity"])) for f in fills)
    equities = [float(r["equity"]) for r in nav_rows if r.get("equity")]
    avg = sum(equities) / len(equities) if equities else 0.0
    return traded / avg if avg > 0 else 0.0


def _slot_utilization(snapshots: list[dict], slot_limit: int | None) -> list[dict]:
    if not slot_limit:
        return []
    by_day: dict[str, int] = {}
    for s in snapshots:
        by_day[s["date"]] = by_day.get(s["date"], 0) + 1
    return [
        {"date": d, "held": n, "limit": slot_limit, "utilization": n / slot_limit}
        for d, n in sorted(by_day.items())
    ]


def _concentration_series(db, snapshots: list[dict]) -> list[dict]:
    """行业集中度时序（§5.4.3）：逐日各 category_l2 持仓数（元数据经 L1.5 门面）。"""
    if not snapshots:
        return []
    from gateway.metadata import MetadataService

    meta = MetadataService(db).instruments([s["symbol"] for s in snapshots])
    cat_of = {s: str((m or {}).get("category_l2") or "") for s, m in meta.items()}
    by_day: dict[str, dict[str, int]] = {}
    for snap in snapshots:
        day = snap["date"]
        cat = cat_of.get(snap["symbol"], "")
        by_day.setdefault(day, {})
        by_day[day][cat] = by_day[day].get(cat, 0) + 1
    return [{"date": d, "counts": counts} for d, counts in sorted(by_day.items())]
