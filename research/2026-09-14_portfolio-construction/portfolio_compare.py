"""阶段 C：用**产品里的组合引擎**跑选股规则对比。

刻意不在研究侧另写一套组合模拟 —— 数字必须来自未来长期使用的同一个引擎，
否则会出现「研究一套、实盘一套」。

口径：
  - 标的池：默认全部 ETF（202 只）；``--pool all`` 切到全池 875 只
  - 期间：2019-01-01 起（ETF 池在此之前样本太少）
  - 候选口径：event（当日新触发），对应研究建议的"把候选限定为当日新触发"
  - 每仓预算：等权，目标持仓数 5（研究给出的合理区间是 8–15，小 ETF 池取 5）
  - 费用与止损：与生产完全同源（引擎默认 + 策略里的止损条件）

输出: data/portfolio_compare.csv, portfolio_compare.png
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from rule_backtest.models import BacktestExecutionConfig  # noqa: E402
from rule_backtest.portfolio import (  # noqa: E402
    PortfolioBacktestEngine,
    PortfolioBacktestRequest,
    PortfolioRiskConfig,
    build_selector,
)

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DB_PATH = ROOT / "data" / "trend_quant.db"
STRATEGY_ID = "macd_20260719095419247961"  # MACD金叉进-止损出

#: 要对比的选股规则（研究结论 → 可检验假设）。
#: ``random_sub`` 是**证伪对照** —— 没有它就无法区分"趋势强度有信息"与
#: "只是更挑剔/更常空仓"。
RULES: list[tuple[str, str]] = [
    ("random", "随机选（基准）"),
    ("rank:trend_score", "趋势强度降序（当前做法）"),
    ("rank:trend_score:asc", "趋势强度升序（反向对照）"),
    ("random_sub:0.2|random", "随机保留 20%（证伪对照）"),
    ("pct:trend_score:0.2|rank:trend_score", "趋势强度前 20% 分位"),
    ("pct:trend_score:0.5|rank:trend_score", "趋势强度前 50% 分位"),
    ("young_golden:20|pct:trend_score:0.2|rank:trend_score", "金叉≤20天 + 强度前 20%"),
    ("rank:er", "路径平滑度 ER 降序"),
    ("rank:atr_pct", "波动率 ATR% 降序（预期是 beta 伪装）"),
]


def read_only_conn() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def load_universe(conn: sqlite3.Connection, pool: str) -> dict[str, dict]:
    sql = ("SELECT symbol, name, asset_type, category_l1, category_l2 FROM instrument_metadata "
           "WHERE enabled=1")
    if pool == "etf":
        sql += " AND asset_type='etf'"
    rows = conn.execute(sql).fetchall()
    return {
        r[0]: {"name": r[1], "asset_type": r[2], "category_l1": r[3], "category_l2": r[4]}
        for r in rows
    }


def load_bars(conn: sqlite3.Connection, symbols: list[str], start: str) -> dict[str, pd.DataFrame]:
    """一次性读全部标的日线；start 之前保留用于指标预热。

    预热长度按指标最长窗口估（趋势值 max(n_long, atr)=20 根 + SMA200），
    这里直接多留 400 个交易日，保证入场信号在回测期首日即已可比。
    """
    warm_start = (pd.Timestamp(start) - pd.Timedelta(days=700)).strftime("%Y-%m-%d")
    out: dict[str, pd.DataFrame] = {}
    for i, symbol in enumerate(symbols, 1):
        df = pd.read_sql_query(
            "SELECT time AS date, open, high, low, close, volume, amount "
            "FROM market_data_qfq WHERE symbol=? AND time>=? ORDER BY time",
            conn,
            params=(symbol, warm_start),
        )
        if df.empty:
            continue
        df["date"] = pd.to_datetime(df["date"])
        out[symbol] = df
        if i % 200 == 0:
            print(f"  载入 {i}/{len(symbols)}", flush=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default="etf", choices=["etf", "all"])
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default="2026-09-11")
    ap.add_argument("--positions", type=int, default=5)
    ap.add_argument("--candidate-mode", default="event", choices=["event", "state"])
    args = ap.parse_args()

    conn = read_only_conn()
    strategy = json.loads(
        conn.execute("SELECT payload_json FROM rule_strategies WHERE id=?", (STRATEGY_ID,)).fetchone()[0]
    )
    # 趋势参数取生产库 app_config.strategy —— 保证与看板/实盘同源。
    trend_cfg = json.loads(conn.execute("SELECT value FROM app_config WHERE key='strategy'").fetchone()[0])
    strategy["indicator_config"] = trend_cfg

    universe = load_universe(conn, args.pool)
    print(f"标的池 {args.pool}：{len(universe)} 只；期间 {args.start} .. {args.end}；"
          f"候选口径 {args.candidate_mode}；目标持仓 {args.positions}", flush=True)
    t0 = time.time()
    bars = load_bars(conn, list(universe), args.start)
    conn.close()
    print(f"行情载入完成：{len(bars)} 只 / {sum(len(b) for b in bars.values()):,} 根 "
          f"（{time.time() - t0:.1f}s）", flush=True)

    meta = {s: universe[s] for s in bars}
    start_date = pd.Timestamp(args.start).date()
    end_date = pd.Timestamp(args.end).date()
    execution = BacktestExecutionConfig(instrument_type="etf")

    rows: list[dict] = []
    navs: dict[str, list[dict]] = {}
    for rule, label in RULES:
        t1 = time.time()
        result = PortfolioBacktestEngine().run(
            PortfolioBacktestRequest(
                strategy=strategy,
                symbols=list(bars),
                bars_by_symbol=bars,
                meta_by_symbol=meta,
                execution=execution,
                start_date=start_date,
                end_date=end_date,
                candidate_mode=args.candidate_mode,
                selectors=[build_selector(rule)],
                target_positions=args.positions,
                risk=PortfolioRiskConfig(max_positions=args.positions),
            )
        )
        sm = result["summary"]
        rows.append(
            {
                "rule": rule,
                "label": label,
                "total_return": sm["total_return"],
                "annual_return": sm["annual_return"],
                "max_drawdown": sm["max_drawdown"],
                "sharpe": sm["sharpe"],
                "calmar": sm["calmar"],
                "win_rate": sm["win_rate"],
                "payoff_ratio": sm["payoff_ratio"],
                "trades": sm["trade_count"],
                "turnover": sm["turnover"],
                "avg_positions": sm.get("avg_positions", 0.0),
                "max_positions_held": sm.get("max_positions_held", 0),
                "flat_day_pct": sm.get("flat_day_pct", 0.0),
                "avg_holding_days": sm["avg_holding_days"],
                "total_cost": sm["total_trading_cost"],
            }
        )
        navs[rule] = result["daily_nav"]
        print(f"  {label:36s} 年化 {sm['annual_return']:+7.2%} 回撤 {sm['max_drawdown']:7.2%} "
              f"夏普 {sm['sharpe']:5.2f} 成交 {sm['trade_count']:5d} "
              f"平均持仓 {sm.get('avg_positions', 0):.2f} （{time.time() - t1:.1f}s）", flush=True)

    out = pd.DataFrame(rows)
    out.to_csv(DATA / "portfolio_compare.csv", index=False)
    pd.DataFrame(
        {rule: {row["date"]: row["equity"] for row in nav} for rule, nav in navs.items()}
    ).to_csv(DATA / "portfolio_compare_nav.csv")

    # 基准：等权全池买入持有。必须给出它自己的完整指标 —— 否则"策略年化 X%"
    # 无法判断是超额还是只是承担了更多风险。
    bench = None
    bench_metrics: dict = {}
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from rule_backtest.metrics import compute_summary

        closes = pd.DataFrame({s: b.set_index("date")["close"] for s, b in bars.items()}).sort_index()
        closes = closes.loc[pd.Timestamp(start_date) : pd.Timestamp(end_date)]
        first = closes.apply(lambda col: col.dropna().iloc[0] if col.notna().any() else None)
        norm = closes.divide(first)
        bench = norm.mean(axis=1).dropna()
        cap = float(execution.initial_capital)
        bench_nav = [{"date": str(d)[:10], "equity": cap * float(v)} for d, v in bench.items()]
        bench_metrics = compute_summary(daily_nav=bench_nav, trades=[], turnover_total=0.0)
        bench_metrics["rebalance_note"] = "等权、每日再平衡（构造上偏乐观，仅作参照）"
        print(
            f"\n等权全池买入持有基准：总收益 {bench_metrics['total_return']:+.2%}  "
            f"年化 {bench_metrics['annual_return']:+.2%}  "
            f"最大回撤 {bench_metrics['max_drawdown']:.2%}  "
            f"夏普 {bench_metrics['sharpe']:.2f}"
        )
    except Exception as exc:  # 基准失败不影响主结果
        print(f"基准计算失败：{exc}")

    print(f"\n输出 -> {DATA / 'portfolio_compare.csv'}")

    # ---- 图 ----
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(13, 7))
    for rule, label in RULES:
        nav = navs.get(rule) or []
        if not nav:
            continue
        s = pd.Series({r["date"]: r["equity"] for r in nav}).sort_index()
        ax.plot(pd.to_datetime(s.index), s / s.iloc[0], lw=1.3, label=label)
    if bench is not None and len(bench):
        ax.plot(pd.to_datetime(bench.index), bench, lw=2.0, color="#111", ls="--",
                label="等权全池买入持有（基准）")
    ax.axhline(1.0, color="#888", lw=0.8)
    ax.set_ylabel("净值（起点=1）")
    ax.set_title(f"组合层选股规则对比 · {args.pool} 池 · {args.candidate_mode} 候选 · "
                 f"目标持仓 {args.positions}", fontsize=12)
    ax.legend(fontsize=8, loc="upper left")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(HERE / "portfolio_compare.png", dpi=130)
    print(f"图 -> {HERE / 'portfolio_compare.png'}")


if __name__ == "__main__":
    main()
