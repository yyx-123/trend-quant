"""分解：「取分位更好」到底是「趋势强度有信息」还是「暴露更低」。

上一步的证伪对照发现：随机保留 20% 候选（+15.98%，夏普 0.89）反而优于
按趋势强度取前 20%（+12.47%，夏普 0.71）。这有两种可能，必须分开：

  假设 A —— 趋势强度确实有截面信息，随机对照只是单次抽样的运气。
  假设 B —— 两者都在做同一件事：**降低暴露**（更少候选 → 更常空仓/更少持仓），
            与"挑谁"无关；此时趋势强度是无关变量。

判据：
  1. 多个随机种子跑随机子集 → 给出随机子集的分布。若趋势前 20% 落在该分布
     之内，则"趋势强度有信息"不成立。
  2. 直接扫目标持仓数（不任何过滤）→ 若"少持仓"本身就带来同等提升，
     则分位规则只是降低暴露的绕路，机制是 B。
  3. 扫分位阈值（0.1/0.2/0.35/0.5）→ 若收益随"更挑剔"单调上升，
     同样是暴露效应而非信息效应。

输出: data/sweep_positions.csv, data/sweep_pct.csv, data/sweep_random_seeds.csv
"""
from __future__ import annotations

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
from rule_backtest.portfolio.selector import RandomSubsetSelector  # noqa: E402

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DB_PATH = ROOT / "data" / "trend_quant.db"
STRATEGY_ID = "macd_20260719095419247961"
POOL = "etf"
START, END = "2019-01-01", "2026-09-11"


def prepare():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    strategy = json.loads(
        conn.execute("SELECT payload_json FROM rule_strategies WHERE id=?", (STRATEGY_ID,)).fetchone()[0]
    )
    strategy["indicator_config"] = json.loads(
        conn.execute("SELECT value FROM app_config WHERE key='strategy'").fetchone()[0]
    )
    meta = {
        r[0]: {"name": r[1], "asset_type": r[2], "category_l1": r[3], "category_l2": r[4]}
        for r in conn.execute(
            "SELECT symbol, name, asset_type, category_l1, category_l2 FROM instrument_metadata "
            "WHERE enabled=1 AND asset_type='etf'"
        )
    }
    warm = (pd.Timestamp(START) - pd.Timedelta(days=700)).strftime("%Y-%m-%d")
    bars = {}
    for symbol in meta:
        df = pd.read_sql_query(
            "SELECT time AS date, open, high, low, close, volume, amount FROM market_data_qfq "
            "WHERE symbol=? AND time>=? ORDER BY time",
            conn, params=(symbol, warm),
        )
        if df.empty:
            continue
        df["date"] = pd.to_datetime(df["date"])
        bars[symbol] = df
    conn.close()
    meta = {s: meta[s] for s in bars}
    return strategy, bars, meta


def run(strategy, bars, meta, selectors, positions, max_positions=None, max_new=None):
    result = PortfolioBacktestEngine().run(
        PortfolioBacktestRequest(
            strategy=strategy,
            symbols=list(bars),
            bars_by_symbol=bars,
            meta_by_symbol=meta,
            execution=BacktestExecutionConfig(instrument_type="etf"),
            start_date=pd.Timestamp(START).date(),
            end_date=pd.Timestamp(END).date(),
            candidate_mode="event",
            selectors=selectors,
            target_positions=positions,
            risk=PortfolioRiskConfig(
                max_positions=max_positions if max_positions is not None else positions,
                max_new_per_day=max_new or 0,
            ),
        )
    )
    sm = result["summary"]
    return {
        "annual_return": sm["annual_return"],
        "max_drawdown": sm["max_drawdown"],
        "sharpe": sm["sharpe"],
        "calmar": sm["calmar"],
        "trades": sm["trade_count"],
        "avg_positions": sm.get("avg_positions", 0.0),
        "max_positions_held": sm.get("max_positions_held", 0),
        "flat_day_pct": sm.get("flat_day_pct", 0.0),
        "turnover": sm["turnover"],
    }


def main() -> None:
    strategy, bars, meta = prepare()
    print(f"ETF 池 {len(bars)} 只，{START} .. {END}（event 候选）\n", flush=True)
    t0 = time.time()

    # ---- ① 扫目标持仓数（不做任何选股过滤）----
    print("【① 持仓数扫描】规则固定为「趋势强度降序」，只改目标/上限持仓数")
    print(f"{'持仓数':>6s} {'年化':>9s} {'回撤':>9s} {'夏普':>7s} {'成交':>7s} "
          f"{'平均持仓':>9s} {'空仓日占比':>10s} {'换手':>8s}")
    rows = []
    for n in (2, 3, 5, 8, 12, 15, 20, 30):
        m = run(strategy, bars, meta, [build_selector("rank:trend_score")], n)
        rows.append({"positions": n, **m})
        print(f"{n:>6d} {m['annual_return']:>+8.2%} {m['max_drawdown']:>9.2%} "
              f"{m['sharpe']:>7.2f} {m['trades']:>7d} {m['avg_positions']:>9.2f} "
              f"{m['flat_day_pct']:>10.1%} {m['turnover']:>8.1f}", flush=True)
    pd.DataFrame(rows).to_csv(DATA / "sweep_positions.csv", index=False)

    # ---- ①b 同样扫持仓数，但选股换成随机 —— 把"持仓数效应"与"选股效应"分开 ----
    print("\n【①b 持仓数扫描 · 选股换成随机】若结论与 ① 一致，说明效应来自持仓数本身")
    print(f"{'持仓数':>6s} {'年化':>9s} {'回撤':>9s} {'夏普':>7s} {'成交':>7s}")
    rows = []
    for n in (2, 5, 8, 12, 20, 30):
        m = run(strategy, bars, meta, [build_selector("random")], n)
        rows.append({"positions": n, **m})
        print(f"{n:>6d} {m['annual_return']:>+8.2%} {m['max_drawdown']:>9.2%} "
              f"{m['sharpe']:>7.2f} {m['trades']:>7d}", flush=True)
    pd.DataFrame(rows).to_csv(DATA / "sweep_positions_random.csv", index=False)

    # ---- ② 扫分位阈值（目标持仓固定 5）----
    print("\n【② 分位阈值扫描】目标持仓固定 5，只改「保留前多少分位」")
    print(f"{'分位':>6s} {'年化':>9s} {'回撤':>9s} {'夏普':>7s} {'成交':>7s} "
          f"{'平均持仓':>9s} {'空仓日占比':>10s}")
    rows = []
    for pct in (0.1, 0.2, 0.35, 0.5, 1.0):
        sel = [build_selector(f"pct:trend_score:{pct}|rank:trend_score")]
        m = run(strategy, bars, meta, sel, 5)
        rows.append({"top_pct": pct, **m})
        print(f"{pct:>6.2f} {m['annual_return']:>+8.2%} {m['max_drawdown']:>9.2%} "
              f"{m['sharpe']:>7.2f} {m['trades']:>7d} {m['avg_positions']:>9.2f} "
              f"{m['flat_day_pct']:>10.1%}", flush=True)
    pd.DataFrame(rows).to_csv(DATA / "sweep_pct.csv", index=False)

    # ---- ③ 随机子集多种子 —— 给出分布，判断趋势前 20% 是否落在其中 ----
    print("\n【③ 随机保留 20% × 5 个种子】给出随机子集的分布（目标持仓 5）")
    rows = []
    for seed in (1, 7, 101, 4242, 20260914):
        sel = [RandomSubsetSelector(top_pct=0.2, seed=seed)]
        sel.append(build_selector("random"))
        m = run(strategy, bars, meta, sel, 5)
        rows.append({"seed": seed, **m})
        print(f"seed={seed:>9d} 年化 {m['annual_return']:>+8.2%} 回撤 {m['max_drawdown']:>9.2%} "
              f"夏普 {m['sharpe']:>6.2f} 成交 {m['trades']:>5d} 平均持仓 {m['avg_positions']:.2f}",
              flush=True)
    df = pd.DataFrame(rows)
    df.to_csv(DATA / "sweep_random_seeds.csv", index=False)
    print(f"\n  随机子集（20%）年化：均值 {df['annual_return'].mean():+.2%}  "
          f"标准差 {df['annual_return'].std():.2%}  "
          f"区间 [{df['annual_return'].min():+.2%}, {df['annual_return'].max():+.2%}]")
    print(f"  随机子集（20%）夏普：均值 {df['sharpe'].mean():.2f}  "
          f"区间 [{df['sharpe'].min():.2f}, {df['sharpe'].max():.2f}]")
    print(f"  → 与「趋势强度前 20%」（年化 +12.47% / 夏普 0.71）比较："
          f"{'落在随机分布之内' if df['annual_return'].min() <= 0.1247 <= df['annual_return'].max() else '落在随机分布之外'}")
    print(f"\n总耗时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
