"""研究 2 深挖：把「选股」这件事真正需要的口径算出来。

研究 2 的两个问题必须在写结论前解决：

【问题一】可交易性偏差
  固定名义本金 1 万元时，股价 > ~100 元的标的连 1 手都买不起，会被
  simulate_entry 静默丢弃。需要量出丢弃规模与样本偏移方向。

【问题二】pooled 与横截面的符号冲突
  合并全样本的「特征十分位 -> 收益」表，与「每日横截面 IC」出现了相反符号
  （atr_pct 最典型：分位表 +2.15%，IC = -0.257）。原因是 pooled 混了时间：
  高 atr_pct 的观测集中在高波动年份，而高波动年份随后涨幅大。**但选股是
  横截面行为**——在同一天的候选里挑，所以横截面口径才是答案。

本脚本产出「当日去均值」的十分位表：把每笔收益减去当日全部候选的均值，
再按特征分位平均。它直接回答「今天按这个特征挑最高的一档 vs 最低的一档，
结果差多少」——这才是选股决策的正确度量。

输出: data/entry_quality_corr.csv, data/entry_quality_excess.csv,
      data/entry_quality_ic_by_year.csv, entry_quality_deep.png
"""
from __future__ import annotations

import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

KEY_FEATURES = [
    "trend_score", "price_direction", "confidence", "er", "vol_ratio",
    "bias_atr", "atr_pct", "ret20", "ret60", "dist_ma200",
    "macd_hist_norm", "days_in_golden", "trend_age",
]

#: 名义本金档位。10k = 小账户真实处境；100k = 与引擎默认 initial_capital 同量级。
NOTIONALS = (10_000.0, 100_000.0)


def load(kind: str) -> pd.DataFrame:
    df = pd.read_csv(DATA / f"entry_quality_{kind}.csv", parse_dates=["date"])
    df["year"] = df["date"].dt.year
    return df


# ---------------------------------------------------------------- 问题一
def tradability(panel: pd.DataFrame) -> pd.DataFrame:
    """每个标的在给定名义本金下能否买到至少 1 手。"""
    last = panel.sort_values(["symbol", "date"]).groupby("symbol").tail(1)
    rows = []
    for notional in NOTIONALS:
        per_share = last["close"] * 1.002 * 1.0000854
        qty = (notional // per_share // 100 * 100).clip(lower=0)
        tradable = qty >= 100
        rows.append(
            {
                "notional": notional,
                "tradable_symbols": int(tradable.sum()),
                "total_symbols": int(len(last)),
                "pct_symbols": float(tradable.mean()),
                "max_price_tradable": float(last.loc[tradable, "close"].max()) if tradable.any() else 0.0,
                "tradable_etf": int((tradable & (last["asset_type"] == "etf")).sum()),
                "tradable_stock": int((tradable & (last["asset_type"] == "stock")).sum()),
            }
        )
    out = pd.DataFrame(rows)
    print("\n=== 可交易性覆盖（能否买到 ≥1 手）===")
    print(f"{'名义本金':>10s} {'可交易标的':>10s} {'占比':>8s} {'价格上限':>10s} {'其中ETF':>8s} {'其中股票':>8s}")
    for _, r in out.iterrows():
        print(
            f"{r['notional']:>10,.0f} {int(r['tradable_symbols']):>10d} {r['pct_symbols']:>8.1%} "
            f"{r['max_price_tradable']:>10.2f} {int(r['tradable_etf']):>8d} {int(r['tradable_stock']):>8d}"
        )
    priced = last[["symbol", "close", "asset_type", "category_l1"]].copy()
    print(f"\n全池 {len(priced)} 标的，收盘价中位数 {priced['close'].median():.2f}")
    print(f"  股价 > 100 元的标的数：{(priced['close'] > 100).sum()} "
          f"（占 {(priced['close'] > 100).mean():.1%}）")
    return out


def sample_composition(kind: str, panel: pd.DataFrame, df: pd.DataFrame) -> None:
    """落地的入场样本 vs 全池，在各维度的构成差异。"""
    last = panel.groupby("symbol").tail(1)[["symbol", "close", "asset_type", "category_l1"]]
    got = df.merge(last, on="symbol", how="left", suffixes=("", "_pool"))
    print(f"\n--- [{kind}] 入场样本构成 vs 全池 ---")
    print(f"  样本涉及标的 {df['symbol'].nunique()} / 全池 {panel['symbol'].nunique()}")
    for col in ("asset_type", "category_l1"):
        a = got[col].value_counts(normalize=True).head(8)
        b = last[col].value_counts(normalize=True)
        print(f"  [{col}]  样本占比 / 全池占比")
        for k in a.index:
            print(f"     {str(k)[:14]:16s} {a.get(k, 0):>7.1%} / {b.get(k, 0):>7.1%}")


# ---------------------------------------------------------------- 问题二
def feature_corr(df: pd.DataFrame) -> pd.DataFrame:
    """特征间 Spearman 相关 —— 看哪些特征其实是同一个东西。"""
    corr = df[KEY_FEATURES].corr(method="spearman")
    corr.to_csv(DATA / "entry_quality_corr.csv")
    print("\n=== 特征间秩相关（|ρ|>0.5 视为高度共线）===")
    print(f"{'':16s}" + "".join(f"{c[:7]:>9s}" for c in KEY_FEATURES))
    for a in KEY_FEATURES:
        cells = ""
        for b in KEY_FEATURES:
            v = corr.loc[a, b]
            cells += f"{v:>9.2f}" if pd.notna(v) else f"{'--':>9s}"
        print(f"{a:16s}{cells}")
    hi = [
        (a, b, corr.loc[a, b])
        for i, a in enumerate(KEY_FEATURES)
        for b in KEY_FEATURES[i + 1 :]
        if pd.notna(corr.loc[a, b]) and abs(corr.loc[a, b]) > 0.5
    ]
    if hi:
        print("\n  高共线对：")
        for a, b, v in sorted(hi, key=lambda t: -abs(t[2])):
            print(f"    {a:16s} ~ {b:16s} ρ={v:+.2f}")
    return corr


def excess_deciles(df: pd.DataFrame, kind: str, target: str = "ret_net_pct") -> pd.DataFrame:
    """当日去均值的十分位表 —— 选股决策的正确口径。

    每笔结果减去「当日全部候选的均值」，消除市场/时点共同项，剩下的是
    「同一天里挑谁」的差异。
    """
    d = df.dropna(subset=[target]).copy()
    day_mean = d.groupby("date")[target].transform("mean")
    day_n = d.groupby("date")[target].transform("size")
    d["excess"] = d[target] - day_mean
    d = d[day_n >= 5]  # 当日候选太少时横截面无意义

    print(f"\n=== [{kind}] 当日去均值的十分位超额（单位：百分点）===")
    print("（读法：同一天里按该特征排序，取最高那一档比当日平均好/差多少）")
    print(f"{'特征':16s}" + "".join(f"{'Q' + str(i + 1):>8s}" for i in range(10)) + f"{'Q10-Q1':>10s} {'Q10':>8s} {'Q1':>8s}")
    rows = []
    for feat in KEY_FEATURES:
        col = d[feat]
        if col.notna().sum() < 500 or col.nunique() < 10:
            continue
        try:
            q = pd.qcut(col, 10, labels=False, duplicates="drop")
        except ValueError:
            continue
        if q.nunique() < 10:
            continue
        means = d.groupby(q)["excess"].mean()
        vals = "".join(f"{v * 100:8.3f}" for v in means.values)
        q1, q10 = means.iloc[0], means.iloc[-1]
        print(f"{feat:16s}{vals}{((q10 - q1) * 100):+10.3f} {q10 * 100:+8.3f} {q1 * 100:+8.3f}")
        rows.append({"kind": kind, "target": target, "feature": feat, "q1_excess": q1, "q10_excess": q10,
                     "spread": q10 - q1, **{f"q{i+1}": means.iloc[i] for i in range(10)}})
    out = pd.DataFrame(rows)
    return out


def ic_by_group(df: pd.DataFrame, kind: str, by: str | None = None) -> pd.DataFrame:
    """分资产类型 / 分年份的横截面 IC，检查结论是否是某个子样本的特例。"""
    rows = []
    groups = [(by, g) for by, g in df.groupby(by)] if by else [("ALL", df)]
    by_date_cache = {label: list(g.groupby("date")) for label, g in groups}
    for feat in KEY_FEATURES:
        for target in ("ret_net_pct", "fwd20"):
            for label, _ in groups:
                ics = []
                for _, day in by_date_cache[label]:
                    sub = day[[feat, target]].dropna()
                    if len(sub) < 5 or sub[feat].nunique() < 3:
                        continue
                    ic = sub[feat].corr(sub[target], method="spearman")
                    if np.isfinite(ic):
                        ics.append(ic)
                if len(ics) < 30:
                    continue
                arr = np.array(ics)
                rows.append(
                    {
                        "kind": kind, "group_by": by or "none", "group": str(label),
                        "feature": feat, "target": target, "days": len(arr),
                        "mean_ic": float(arr.mean()),
                        "ic_ir": float(arr.mean() / arr.std()) if arr.std() > 0 else np.nan,
                        "pos_pct": float((arr > 0).mean()),
                        # IC 的集中度：绝对值最大的 5% 天数贡献了多少 |IC| 总和
                        "concentration": _concentration(arr),
                        "half1": float(arr[: len(arr) // 2].mean()),
                        "half2": float(arr[len(arr) // 2 :].mean()),
                    }
                )
    res = pd.DataFrame(rows)
    if by:
        res.to_csv(DATA / f"entry_quality_ic_by_{by}_{kind}.csv", index=False)
    return res


def _concentration(arr: np.ndarray) -> float:
    """前 5% 天数（按 |IC| 排序）占 |IC| 总和的比重。接近 1 说明被少数几天主导。"""
    absr = np.abs(arr)
    total = absr.sum()
    if total <= 0:
        return np.nan
    k = max(int(len(absr) * 0.05), 1)
    return float(np.sort(absr)[::-1][:k].sum() / total)


def main() -> None:
    panel = pd.read_parquet(DATA / "panel.parquet")
    tradability(panel)

    frames = []
    for kind in ("state", "event"):
        df = load(kind)
        sample_composition(kind, panel, df)
        feature_corr(df)
        frames.append(excess_deciles(df, kind))
        frames.append(excess_deciles(df, kind, target="fwd20"))

        print(f"\n--- [{kind}] 横截面 IC 的稳定性 ---")
        for by in ("asset_type", "year"):
            res = ic_by_group(df, kind, by)
            if res.empty:
                continue
            piv = res[res["target"] == "ret_net_pct"].pivot_table(
                index="feature", columns="group", values="mean_ic"
            )
            cols = list(piv.columns)
            print(f"\n  [{by}] 特征 x {by} 的 mean IC (target=ret_net_pct)")
            print(f"  {'特征':16s}" + "".join(f"{str(c)[:8]:>10s}" for c in cols))
            for feat in piv.index:
                print(f"  {feat:16s}" + "".join(
                    f"{piv.loc[feat, c]:+10.4f}" if pd.notna(piv.loc[feat, c]) else f"{'--':>10s}"
                    for c in cols
                ))
            agg = res[res["target"] == "ret_net_pct"].groupby("feature")[["concentration", "half1", "half2"]].mean()
            print(f"  {'特征':16s}{'IC集中度':>10s}{'前半段IC':>10s}{'后半段IC':>10s}")
            for feat in agg.index:
                r = agg.loc[feat]
                print(f"  {feat:16s}{r['concentration']:>10.2f}{r['half1']:>+10.4f}{r['half2']:>+10.4f}")

    pd.concat(frames, ignore_index=True).to_csv(DATA / "entry_quality_excess.csv", index=False)
    print(f"\n输出 -> {DATA / 'entry_quality_excess.csv'}")


if __name__ == "__main__":
    main()
