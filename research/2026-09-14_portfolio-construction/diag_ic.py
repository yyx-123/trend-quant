"""诊断：为什么「全局分位 + 当日去均值」与「日内横截面 IC」符号相反？

以 atr_pct 为例，把两种统计量放到同一子样本上逐项对齐，找出分歧来源。
诊断项：
  1. 同一子样本上重算两个统计量，确认分歧是否真实存在（而非代码差异）
  2. 日内分位（每天单独 qcut）的当日去均值超额 —— 这才是真正的「日内排序」
  3. 特征与 asset_type / 年份 / 收盘价 的关系（结构性混淆）
  4. 分资产类型重算两个统计量
  5. atr_pct 的日内 IC 是否被少数几天主导
"""
from __future__ import annotations

import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
FEAT = "atr_pct"
TARGET = "ret_net_pct"


def daily_ic(day: pd.DataFrame, feat: str, target: str) -> float:
    sub = day[[feat, target]].dropna()
    if len(sub) < 5 or sub[feat].nunique() < 3:
        return np.nan
    return sub[feat].corr(sub[target], method="spearman")


def main() -> None:
    df = pd.read_csv(DATA / "entry_quality_state.csv", parse_dates=["date"])
    d = df.dropna(subset=[TARGET, FEAT]).copy()
    print(f"样本 {len(d):,} 笔 / {d['date'].nunique():,} 个交易日 "
          f"（每日期望 {len(d) / d['date'].nunique():.1f} 笔）")

    # ---- 1. 同子样本上重算两个统计量 ----
    ics = d.groupby("date").apply(lambda g: daily_ic(g, FEAT, TARGET)).dropna()
    print(f"\n[1] 日内横截面 Spearman IC：均值 {ics.mean():+.4f}  "
          f"中位 {ics.median():+.4f}  std {ics.std():.4f}  "
          f"正占比 {(ics > 0).mean():.1%}  (n_days={len(ics)})")

    day_mean = d.groupby("date")[TARGET].transform("mean")
    day_n = d.groupby("date")[TARGET].transform("size")
    d["excess"] = d[TARGET] - day_mean
    dd = d[day_n >= 5].copy()

    gq = pd.qcut(dd[FEAT], 10, labels=False, duplicates="drop")
    glob = dd.groupby(gq)["excess"].mean()
    print(f"[1] 全局分位 + 当日去均值：Q1 {glob.iloc[0] * 100:+.3f}pp  "
          f"Q10 {glob.iloc[-1] * 100:+.3f}pp  差 {(glob.iloc[-1] - glob.iloc[0]) * 100:+.3f}pp")

    # ---- 2. 真正的日内排序：每天单独分位 ----
    def within_day_decile(g: pd.DataFrame) -> pd.Series:
        if len(g) < 10 or g[FEAT].nunique() < 5:
            return pd.Series(dtype=float)
        try:
            q = pd.qcut(g[FEAT], 5, labels=False, duplicates="drop")
        except ValueError:
            return pd.Series(dtype=float)
        return pd.DataFrame({"q": q, "e": g["excess"]}).groupby("q")["e"].mean()

    per_day = dd.groupby("date").apply(within_day_decile)
    if not per_day.empty:
        mat = per_day.unstack()
        print(f"\n[2] 日内五分位（每天单独分位）的当日超额：")
        for q in mat.columns:
            print(f"    日内Q{int(q) + 1}（越低）" if q == 0 else f"    日内Q{int(q) + 1}", 
                  f" {mat[q].mean() * 100:+.3f}pp")
        if 0 in mat.columns and mat.columns.max() in mat.columns:
            print(f"    → 日内 Q5 − Q1 = {(mat[mat.columns.max()].mean() - mat[0].mean()) * 100:+.3f}pp"
                  f"  （负 = 日内这个特征越高越差）")

    # ---- 3. 结构性混淆 ----
    print(f"\n[3] 结构性混淆检查")
    print(f"    atr_pct 均值：股票 {d.loc[d.asset_type == 'stock', FEAT].mean():.4f}  "
          f"ETF {d.loc[d.asset_type == 'etf', FEAT].mean():.4f}")
    print(f"    样本里股票占比 {(d.asset_type == 'stock').mean():.1%}")
    q = pd.qcut(d[FEAT], 10, labels=False, duplicates="drop")
    comp = pd.DataFrame({"q": q, "stock": (d.asset_type == "stock").astype(float),
                         "year": d["date"].dt.year,
                         "ret": d[TARGET]})
    byq = comp.groupby("q").agg(stock_pct=("stock", "mean"), year_mean=("year", "mean"),
                                ret=("ret", "mean"))
    byq["ret_day_demean"] = dd.groupby(pd.qcut(dd[FEAT], 10, labels=False, duplicates="drop"))["excess"].mean()
    print(f"\n    {'分位':>4s} {'股票占比':>9s} {'平均年份':>9s} {'原始净收益':>10s} {'当日超额':>10s}")
    for i, r in byq.iterrows():
        print(f"    {int(i) + 1:>4d} {r['stock_pct']:>9.1%} {r['year_mean']:>9.1f} "
              f"{r['ret'] * 100:>9.3f}% "
              f"{r['ret_day_demean'] * 100 if pd.notna(r['ret_day_demean']) else float('nan'):>9.3f}%")

    # ---- 3b. 单日直查：把两个统计量放在同一天上看 ----
    big = d["date"].value_counts().idxmax()
    one = d[d["date"] == big]
    ic_1 = daily_ic(one, FEAT, TARGET)
    q1_ = pd.qcut(one[FEAT], 5, labels=False, duplicates="drop")
    qm = one.groupby(q1_)[TARGET].mean()
    raw_pool = d[FEAT].corr(d[TARGET], method="spearman")
    print(f"\n[3b] 单日直查（候选最多的交易日 {big.date()}，{len(one)} 个候选）")
    print(f"     该日 日内 Spearman IC = {ic_1:+.4f}")
    print(f"     该日 五分位净收益：" + "  ".join(f"Q{int(i) + 1}={v * 100:+.3f}%" for i, v in qm.items()))
    print(f"     全样本（不按日）整体 Spearman = {raw_pool:+.4f}")
    print(f"     → 单日内 IC 与五分位必须同号；若不同号说明统计口径有误")

    # ---- 4. 分资产类型 ----
    print(f"\n[4] 分资产类型：")
    for at, g in d.groupby("asset_type"):
        gi = g.groupby("date").apply(lambda x: daily_ic(x, FEAT, TARGET)).dropna()
        dm = g.groupby("date")[TARGET].transform("mean")
        ex = g[TARGET] - dm
        gg = ex.groupby(pd.qcut(g[FEAT], 10, labels=False, duplicates="drop")).mean()
        print(f"    {at:6s} n={len(g):>7,}  日内IC {gi.mean():+.4f}   "
              f"全局分位 Q1 {gg.iloc[0] * 100:+.3f}pp Q10 {gg.iloc[-1] * 100:+.3f}pp "
              f"差 {(gg.iloc[-1] - gg.iloc[0]) * 100:+.3f}pp")

    # ---- 5. IC 是否被少数几天主导 ----
    absr = ics.abs().sort_values(ascending=False)
    tot = absr.sum()
    print(f"\n[5] IC 集中度：|IC| 最大的 5% 天数 贡献了 |IC| 总和的 {absr.head(max(int(len(absr) * 0.05), 1)).sum() / tot:.1%}")
    print(f"    |IC| 中位 {absr.median():.4f}  最大 {absr.max():.4f}")
    # 用中位数重看方向
    print(f"    IC 符号：正 {(ics > 0).mean():.1%} / 负 {(ics < 0).mean():.1%}")

    # ---- 6. 用固定窗口前瞻收益替代 as-traded，排除出场机制影响 ----
    print(f"\n[6] 换成固定窗口前瞻收益（排除止损机制）：")
    for tgt in ("fwd5", "fwd20", "fwd60"):
        if tgt not in d.columns:
            continue
        dd2 = d.dropna(subset=[tgt]).copy()
        gi = dd2.groupby("date").apply(lambda x: daily_ic(x, FEAT, tgt)).dropna()
        dm2 = dd2.groupby("date")[tgt].transform("mean")
        ex2 = dd2[tgt] - dm2
        gg2 = ex2.groupby(pd.qcut(dd2[FEAT], 10, labels=False, duplicates="drop")).mean()
        print(f"    {tgt:6s} 日内IC {gi.mean():+.4f}   "
              f"全局分位 Q1 {gg2.iloc[0] * 100:+.3f}pp Q10 {gg2.iloc[-1] * 100:+.3f}pp "
              f"差 {(gg2.iloc[-1] - gg2.iloc[0]) * 100:+.3f}pp")


if __name__ == "__main__":
    main()
