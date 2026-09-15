"""决定性检验：选股边际是「能力」还是「隐藏的高 beta 暴露」？

前序结果给出的所有正边际，都集中在 atr_pct / ret20 / macd_hist_norm /
price_direction / bias_atr 这类「高波动 + 涨幅大」的代理特征上，且在
2015-2018 为负、2019 之后转正。这高度符合「高 beta 暴露」而非「选股能力」：

  按「已经涨得多 / 波动大」的候选排序 = 买高 beta，样本期市场整体上涨，
  高 beta 放大涨幅 → 当日去均值后仍为正。市场下跌时必然翻负。

判据（真能力 vs beta 伪装）：
  真能力 —— 上涨环境与下跌环境下边际都为正（甚至下跌环境更明显，
            因为趋势系统的价值在于避开下跌）。
  beta 伪装 —— 上涨环境为正、下跌环境为负，符号随市场翻转。

市场方向的定义：该入场日全市场所有标的前瞻 20 日收益的中位数
（取全池当日横截面中位数，避免用指数引入额外数据依赖）。

输出: data/beta_test.csv
"""
from __future__ import annotations

import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

FEATURES = ["trend_score", "price_direction", "er", "confidence", "bias_atr",
            "atr_pct", "ret20", "ret60", "dist_ma200", "macd_hist_norm", "days_in_golden"]
TOPK = 3
MIN_CANDIDATES = 10


def market_regime(panel: pd.DataFrame) -> pd.Series:
    """每个交易日的「全池前瞻 20 日中位收益」——市场方向代理。"""
    p = panel.sort_values(["symbol", "date"])
    p = p.assign(fwd20=p.groupby("symbol", sort=False)["close"].shift(-20) / p["close"] - 1.0)
    return p.groupby("date")["fwd20"].median()


def edges(df: pd.DataFrame, feat: str, regime: pd.Series) -> tuple[pd.DataFrame, pd.DataFrame]:
    recs = []
    for day, g in df.groupby("date", sort=True):
        sub = g[[feat, "ret_net_pct"]].dropna()
        if len(sub) < MIN_CANDIDATES or sub[feat].nunique() < 5:
            continue
        m = float(sub["ret_net_pct"].mean())
        top = float(sub.sort_values(feat, ascending=False)["ret_net_pct"].head(TOPK).mean()) - m
        recs.append({"date": day, "top": top, "n": len(sub)})
    rec = pd.DataFrame(recs)
    if rec.empty:
        return rec, rec
    rec = rec.merge(regime.rename("regime"), left_on="date", right_index=True, how="left")
    up = rec[rec["regime"] > 0]
    down = rec[rec["regime"] <= 0]
    return up, down


def main() -> None:
    panel = pd.read_parquet(DATA / "panel.parquet")
    regime = market_regime(panel)
    print(f"市场方向代理（全池前瞻20日中位收益）：{regime.notna().sum()} 个交易日")
    print(f"  上涨环境 {int((regime > 0).sum())} 天 / 下跌环境 {int((regime <= 0).sum())} 天\n")

    all_rows = []
    for kind in ("state", "event"):
        df = pd.read_csv(DATA / f"entry_quality_{kind}.csv", parse_dates=["date"])
        print("=" * 100)
        print(f"【{kind} 口径】每日取特征最强 {TOPK} 名的边际（pp，日等权）—— 按市场方向分组")
        print(f"{'特征':16s} {'全样本':>10s} {'上涨环境':>10s} {'下跌环境':>10s} "
              f"{'上涨天数':>9s} {'下跌天数':>9s}  {'判定':>12s}")
        for feat in FEATURES:
            if df[feat].notna().sum() < 500 or df[feat].nunique() < 10:
                continue
            up, down = edges(df, feat, regime)
            if up.empty or down.empty:
                continue
            a, b, c = float(up["top"].mean()), float(down["top"].mean()), np.nan
            allr = pd.concat([up, down])
            c = float(allr["top"].mean())
            if a > 0 and b > 0:
                verdict = "两环境同为正"
            elif a > 0 and b < 0:
                verdict = "符号随市场翻转"
            elif a < 0 and b < 0:
                verdict = "两环境同为负"
            else:
                verdict = "仅在下跌环境为正"
            print(f"{feat:16s} {c * 100:>+9.3f}p {a * 100:>+9.3f}p {b * 100:>+9.3f}p "
                  f"{len(up):>9d} {len(down):>9d}  {verdict:>12s}")
            all_rows.append({"kind": kind, "feature": feat, "all_pp": c, "up_pp": a, "down_pp": b,
                             "up_days": len(up), "down_days": len(down), "verdict": verdict})
        print()

    out = pd.DataFrame(all_rows)
    out.to_csv(DATA / "beta_test.csv", index=False)
    print(f"输出 -> {DATA / 'beta_test.csv'}")
    print("\n判读：'符号随市场翻转' 的特征 = 隐藏的高 beta 暴露，不是选股能力——")
    print("      它在下跌环境里会把你的组合拖得比市场更惨。")


if __name__ == "__main__":
    main()
