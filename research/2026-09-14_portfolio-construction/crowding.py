"""候选池的并发与相关性：8 个仓位实际上等于几个独立下注？

这是「买多少 / 要不要分散」的量化前提。核心公式（Meucci 有效下注数）：

    N_eff = 1 / (1/N + ρ̄·(1 − 1/N))

ρ̄ = 候选池内平均两两相关。ρ̄=0.5 时，无论持 5 个还是 50 个，
N_eff 上限都是 1/ρ̄ = 2。这一条直接决定「多买几只」有没有用。

方法：
  - 相关用入场日前 60 个交易日的日收益（只用 t 日及之前，无未来函数）
  - 对每个入场日，取当日候选池，算平均两两相关（候选多时抽样以控时）
  - 同时统计候选池的类别集中度（category_l1/l2）

输出: data/crowding.csv
"""
from __future__ import annotations

import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

WINDOW = 60
MAX_POOL = 120
SAMPLE_EVERY = 5
MIN_POOL = 5


def main() -> None:
    panel = pd.read_parquet(DATA / "panel.parquet")
    panel = panel.sort_values(["symbol", "date"])
    panel["ret"] = panel.groupby("symbol", sort=False)["close"].pct_change()

    dates = np.array(sorted(panel["date"].unique()))
    ret_mat = panel.pivot_table(index="date", columns="symbol", values="ret", aggfunc="first")
    ret_mat = ret_mat.reindex(dates)
    meta = panel.groupby("symbol").tail(1).set_index("symbol")[["category_l1", "category_l2", "asset_type"]]

    rows = []
    for kind, fname in (("state", "entry_quality_state.csv"), ("event", "entry_quality_event.csv")):
        df = pd.read_csv(DATA / fname, parse_dates=["date"])
        sig = df.groupby("date")["symbol"].apply(list)
        for t in dates[::SAMPLE_EVERY]:
            if t not in sig.index:
                continue
            pool = sorted(set(sig.loc[t]))
            if len(pool) < MIN_POOL:
                continue
            full_pool_n = len(pool)
            if len(pool) > MAX_POOL:
                rng = np.random.default_rng(abs(hash(str(t))) % (2**32))
                pool = list(rng.choice(pool, MAX_POOL, replace=False))
            i = ret_mat.index.get_loc(t)
            if i < WINDOW:
                continue
            win = ret_mat.iloc[i - WINDOW : i][[c for c in pool if c in ret_mat.columns]]
            if win.shape[1] < MIN_POOL:
                continue
            w = win.dropna(axis=1)
            if w.shape[1] < MIN_POOL or len(w) < 20:
                continue
            corr = w.corr().to_numpy()
            iu = np.triu_indices_from(corr, k=1)
            rho = float(np.nanmean(corr[iu]))
            n = w.shape[1]
            n_eff = 1.0 / (1.0 / n + rho * (1 - 1.0 / n)) if rho > -1 else float(n)
            cats = meta.reindex([c for c in pool if c in meta.index])
            l1 = cats["category_l1"].value_counts(normalize=True)
            l2 = cats["category_l2"].value_counts(normalize=True)
            rows.append({
                "kind": kind, "date": t, "pool_size": full_pool_n, "eval_pool": n,
                "rho_bar": rho, "n_eff": n_eff,
                "top_category_l1_share": float(l1.iloc[0]) if len(l1) else np.nan,
                "top_category_l2_share": float(l2.iloc[0]) if len(l2) else np.nan,
                "n_categories_l1": int(len(l1)), "n_categories_l2": int(len(l2)),
            })

    out = pd.DataFrame(rows)
    out.to_csv(DATA / "crowding.csv", index=False)

    print("=" * 96)
    print(f"候选池相关性（{WINDOW} 日滚动，每 {SAMPLE_EVERY} 个交易日采样一次，池上限 {MAX_POOL}）")
    print(f"\n{'口径':8s} {'样本日':>7s} {'池中位':>7s} {'ρ̄中位':>9s} {'ρ̄均值':>9s} "
          f"{'N_eff(池=8)':>12s} {'N_eff(池=15)':>13s} {'一类占比中位':>12s}")
    for kind in ("state", "event"):
        s = out[out["kind"] == kind]
        if s.empty:
            continue
        rho = s["rho_bar"]
        for cap, label in ((8, "N_eff(池=8)"), (15, "N_eff(池=15)")):
            pass
        neff8 = 1.0 / (1.0 / 8 + rho.median() * (7.0 / 8))
        neff15 = 1.0 / (1.0 / 15 + rho.median() * (14.0 / 15))
        print(f"{kind:8s} {len(s):>7d} {s['pool_size'].median():>7.0f} "
              f"{rho.median():>9.3f} {rho.mean():>9.3f} "
              f"{neff8:>12.2f} {neff15:>13.2f} "
              f"{s['top_category_l2_share'].median():>12.1%}")

    print(f"\n候选池规模分布：")
    for kind in ("state", "event"):
        s = out[out["kind"] == kind]["pool_size"]
        if s.empty:
            continue
        print(f"  {kind:6s} 中位 {s.median():.0f}  P25 {s.quantile(.25):.0f}  "
              f"P75 {s.quantile(.75):.0f}  P95 {s.quantile(.95):.0f}  最大 {s.max():.0f}")

    print(f"\nρ̄ 分布（state）：")
    s = out[out["kind"] == "state"]["rho_bar"]
    for q in (0.1, 0.25, 0.5, 0.75, 0.9):
        print(f"  P{int(q * 100):>3d} = {s.quantile(q):+.3f}")
    print(f"\n  → 把 ρ̄ 代入 N_eff = 1/(1/N + ρ̄·(1−1/N))：")
    rho_m = s.median()
    for n in (3, 5, 8, 12, 15, 20, 30, 50):
        neff = 1.0 / (1.0 / n + rho_m * (1 - 1.0 / n))
        print(f"     持 {n:>2d} 只 → 有效独立下注数 {neff:>5.2f}   "
              f"（相对等权无相关的分散效率 {neff / n:>5.1%}）")
    print(f"     ρ̄ 上限对应：N→∞ 时 N_eff → 1/ρ̄ = {1 / rho_m:.2f}")

    print(f"\n输出 -> {DATA / 'crowding.csv'}")


if __name__ == "__main__":
    main()
