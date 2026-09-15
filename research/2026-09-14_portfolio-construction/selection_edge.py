"""选股边际（selection edge）：直接回答「每天按这个特征挑前几名，到底有没有用」。

口径设计（关键）：
  - 只做**日内横截面**：同一天的候选互相比较，再对交易日等权平均。
    这是选股决策的真实形态（"今天这批候选里挑谁"），也是唯一能排除
    「市场涨跌」这个共同项的口径。
  - 双权重报告：
      day-equal   —— 每个交易日等权（回答"典型的一天里排序有没有用"）
      pp-weighted —— 以交易笔数加权（回答"平均每笔交易差多少"）
    两者背离时说明效应被少数高离散交易日主导，不可作为规则依据 ——
    必须两个口径同号才算稳健。
  - 同时报分半稳定（<=2020 vs >=2021）。
  - 随机基准 = 当日平均（构造上为 0），故任何边际都是相对随机的增益。

输出: data/selection_edge.csv, data/selection_edge_daily.csv, selection_edge.png
"""
from __future__ import annotations

import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

FEATURES = [
    "trend_score", "price_direction", "confidence", "er", "vol_ratio",
    "bias_atr", "atr_pct", "ret20", "ret60", "dist_ma200",
    "macd_hist_norm", "days_in_golden", "trend_age",
]
TARGETS = ["ret_net_pct", "fwd20"]
TOPK = 3
MIN_CANDIDATES = 10
SPLIT = pd.Timestamp("2021-01-01")


def per_day(d: pd.DataFrame, feat: str, target: str, k: int) -> pd.DataFrame:
    """逐日：当日候选数、当日平均、按 feat 降序前 k 名与后 k 名的均值。"""
    recs = []
    for day, g in d.groupby("date", sort=True):
        sub = g[[feat, target]].dropna()
        if len(sub) < MIN_CANDIDATES or sub[feat].nunique() < 5:
            continue
        m = float(sub[target].mean())
        ordered = sub.sort_values(feat, ascending=False)[target]
        recs.append(
            {
                "date": day,
                "n": len(sub),
                "day_mean": m,
                "top": float(ordered.head(k).mean()) - m,
                "bottom": float(ordered.tail(k).mean()) - m,
            }
        )
    return pd.DataFrame(recs)


def summarize(rec: pd.DataFrame) -> dict:
    if rec.empty:
        return {}
    w = rec["n"].to_numpy(dtype=float)
    return {
        "days": int(len(rec)),
        "day_equal_pp": float(rec["top"].mean()),
        "pp_weighted_pp": float(np.average(rec["top"], weights=w)),
        "win_day_pct": float((rec["top"] > 0).mean()),
        "bottom_day_equal_pp": float(rec["bottom"].mean()),
        "top_minus_bottom_pp": float((rec["top"] - rec["bottom"]).mean()),
    }


def main() -> None:
    rows: list[dict] = []
    daily_all: list[pd.DataFrame] = []
    for kind in ("state", "event"):
        df = pd.read_csv(DATA / f"entry_quality_{kind}.csv", parse_dates=["date"])
        print(f"\n{'=' * 104}\n[{kind} 口径] 每交易日候选中位数 "
              f"{df.groupby('date').size().median():.0f}  交易日 {df['date'].nunique()}  "
              f"总入场样本 {len(df):,}")
        for target in TARGETS:
            print(f"\n--- 目标 = {target}；每日按特征降序取前 {TOPK} 名（相对当日平均，单位 pp）---")
            print(f"{'特征':16s} {'日等权':>9s} {'笔数加权':>9s} {'胜日占比':>8s} "
                  f"{'前3-后3':>9s} {'前半段':>9s} {'后半段':>9s}  {'判定':>8s}")
            for feat in FEATURES:
                if df[feat].notna().sum() < 500 or df[feat].nunique() < 10:
                    continue
                rec = per_day(df, feat, target, TOPK)
                if rec.empty:
                    continue
                s = summarize(rec)
                h1 = summarize(per_day(df[df["date"] < SPLIT], feat, target, TOPK))
                h2 = summarize(per_day(df[df["date"] >= SPLIT], feat, target, TOPK))
                m1 = h1.get("day_equal_pp", np.nan)
                m2 = h2.get("day_equal_pp", np.nan)
                signs = [s["day_equal_pp"], s["pp_weighted_pp"], m1, m2]
                if all(np.isfinite(x) and x > 0 for x in signs):
                    verdict = "★稳健为正"
                elif all(np.isfinite(x) and x < 0 for x in signs):
                    verdict = "☆一致为负"
                elif not np.isfinite(m1) or not np.isfinite(m2) or m1 * m2 < 0:
                    verdict = "分半异号"
                else:
                    verdict = "口径背离"
                print(f"{feat:16s} {s['day_equal_pp'] * 100:>+8.3f}p "
                      f"{s['pp_weighted_pp'] * 100:>+8.3f}p {s['win_day_pct']:>8.1%} "
                      f"{s['top_minus_bottom_pp'] * 100:>+8.3f}p "
                      f"{m1 * 100:>+8.3f}p {m2 * 100:>+8.3f}p  {verdict:>8s}")
                rows.append({"kind": kind, "target": target, "feature": feat, "topk": TOPK,
                             **s, "half1_day_equal_pp": m1, "half2_day_equal_pp": m2,
                             "verdict": verdict})
                daily_all.append(rec.assign(kind=kind, target=target, feature=feat))

    out = pd.DataFrame(rows)
    out.to_csv(DATA / "selection_edge.csv", index=False)
    pd.concat(daily_all, ignore_index=True).to_csv(DATA / "selection_edge_daily.csv", index=False)
    print(f"\n输出 -> {DATA / 'selection_edge.csv'}")

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    fig, axes = plt.subplots(1, 2, figsize=(15, 6.5))
    for ax, kind, title in zip(axes, ("state", "event"), ("state（处于金叉状态）", "event（当日新金叉）")):
        sub = out[(out["target"] == "ret_net_pct") & (out["kind"] == kind)].sort_values("day_equal_pp")
        y = np.arange(len(sub))
        ax.barh(y - 0.2, sub["day_equal_pp"] * 100, height=0.38, color="#c0392b", label="日等权")
        ax.barh(y + 0.2, sub["pp_weighted_pp"] * 100, height=0.38, color="#2980b9", label="笔数加权")
        ax.set_yticks(y)
        ax.set_yticklabels(sub["feature"], fontsize=9)
        ax.axvline(0, color="#333", lw=1)
        ax.set_title(f"{title}\n每日取该特征最强 3 名 相对当日平均", fontsize=10)
        ax.grid(alpha=0.3, axis="x")
        ax.legend(fontsize=8)
    fig.suptitle("选股边际：同一天候选里按特征挑最强 3 名，是否优于随机（=当日平均 = 0 线）", fontsize=12)
    fig.tight_layout()
    fig.savefig(HERE / "selection_edge.png", dpi=130)
    print(f"图 -> {HERE / 'selection_edge.png'}")


if __name__ == "__main__":
    main()
