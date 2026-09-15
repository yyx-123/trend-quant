"""研究 2：入场日的什么特征，能预测这笔交易的结果？

要回答的核心问题：
  Q1  按「趋势强度降序」选标的，是对的还是错的？（单调 / 倒 U / 无关）
  Q2  「强度最高的标的已经到趋势后期」这个直觉有没有数据支持？
      （用 er / bias_atr / 信号年龄 这几个「末期度」代理来检验）
  Q3  如果强度排序无效，什么特征才有效？

方法：
  - 入场事件两套口径：
      state  —— 「当前处于 MACD 金叉状态」（= 用户实盘看到的每日候选池）
      event  —— 「当日新金叉」
  - 对每个入场点算两类结果：
      (a) 固定窗口前瞻收益 close->close（用于干净的横截面 IC）
      (b) 按真实止损规则模拟的整笔结果（ret_net_pct / R / 持有天数 / 止损率）
  - 分析：
      (a) 特征十分位 -> 结果均值表（看单调性）
      (b) 每日横截面 Spearman IC（看「排序」这件事本身有没有信息）
      (c) 特征相关性矩阵（看哪些特征其实是同一个东西）

CAVEAT（必须写进报告）：state 口径下相邻日的交易高度重叠，名义样本量远大于
有效样本量，故不报 p 值，只报效应方向、幅度与单调性。

输出: data/entry_quality_*.csv, entry_quality_ic_*.csv
用法: python entry_quality.py [--start 2015-01-01] [--per-symbol-cap 400]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

import trade_sim

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

FEATURES = [
    "trend_score",
    "price_direction",
    "confidence",
    "er",
    "vol_ratio",
    "bias_atr",
    "atr_pct",
    "ret20",
    "ret60",
    "dist_ma200",
    "macd_hist_norm",
    "days_in_golden",
    "trend_age",
]

FWD_HORIZONS = (5, 10, 20, 60)


def _run_length(mask: pd.Series, codes: pd.Series) -> pd.Series:
    """连续 True 的计数（False 归 0）—— 分段 cumsum 实现。"""
    m = mask.fillna(False).astype(int)
    block = (m == 0).groupby(codes).cumsum()
    return m.groupby([codes, block]).cumsum().astype(float).where(m == 1, 0.0)


def prepare(panel: pd.DataFrame) -> pd.DataFrame:
    """补齐派生特征 + 信号年龄 + 两套入场口径标记。"""
    panel = panel.sort_values(["symbol", "date"]).reset_index(drop=True)
    codes = panel.groupby("symbol", sort=False).ngroup()
    g = panel.groupby("symbol", sort=False)

    panel["dist_ma200"] = panel["close"] / panel["sma200"] - 1.0
    panel["macd_hist_norm"] = panel["hist"] / panel["close"]
    ready = panel["ready"]

    golden = (panel["dif"] >= panel["dea"]) & ready
    panel["days_in_golden"] = _run_length(golden, codes)
    panel["trend_age"] = _run_length((panel["trend_ma5"] > 0) & panel["trend_ma5"].notna(), codes)

    panel["is_state"] = golden
    prev_ready = g["ready"].shift(1).eq(True)
    panel["is_event"] = (
        ready
        & prev_ready
        & (panel["dif"] >= panel["dea"])
        & (g["dif"].shift(1) < g["dea"].shift(1))
    )
    return panel


def add_forward_returns(panel: pd.DataFrame) -> pd.DataFrame:
    g = panel.groupby("symbol", sort=False)["close"]
    for h in FWD_HORIZONS:
        panel[f"fwd{h}"] = g.shift(-h) / panel["close"] - 1.0
    return panel


def collect(panel: pd.DataFrame, kind: str, cap: int) -> pd.DataFrame:
    """按 [kind] 口径收集入场样本并模拟整笔结果。"""
    rows: list[dict] = []
    for symbol, sub in panel.groupby("symbol", sort=False):
        sub = sub.reset_index(drop=True)
        idxs = np.flatnonzero(sub[kind].to_numpy())
        if len(idxs) == 0:
            continue
        if len(idxs) > cap:  # 控制重叠样本与运行时间
            idxs = idxs[:: max(len(idxs) // cap, 1)]
        arr = trade_sim.SymbolArrays(
            symbol=symbol,
            date=sub["date"].to_numpy(dtype="datetime64[ns]"),
            open=sub["open"].to_numpy(dtype=float),
            high=sub["high"].to_numpy(dtype=float),
            low=sub["low"].to_numpy(dtype=float),
            close=sub["close"].to_numpy(dtype=float),
            atr=sub["atr"].to_numpy(dtype=float),
            asset_type=str(sub["asset_type"].iloc[0] or "etf"),
            features={c: sub[c].to_numpy(dtype=float) for c in FEATURES},
        )
        fwd = {h: sub[f"fwd{h}"].to_numpy(dtype=float) for h in FWD_HORIZONS}
        for i in idxs:
            res = trade_sim.simulate_entry(arr, int(i))
            if res is None:
                continue
            row = {
                "symbol": symbol,
                "date": sub["date"].iloc[i],
                "asset_type": arr.asset_type,
                "ret_net_pct": res.ret_net_pct,
                "r_multiple": res.r_multiple if res.r_multiple is not None else np.nan,
                "holding_days": res.holding_days,
                "exit_reason": res.exit_reason,
                "mfe_r": res.mfe_r if res.mfe_r is not None else np.nan,
                "mae_pct": res.mae_pct,
                "stopped": res.exit_reason in ("hard_stop", "chandelier_stop"),
            }
            for c in FEATURES:
                v = res.features[c]
                row[c] = v if np.isfinite(v) else np.nan
            for h in FWD_HORIZONS:
                v = fwd[h][i]
                row[f"fwd{h}"] = float(v) if np.isfinite(v) else np.nan
            rows.append(row)
    return pd.DataFrame(rows)


def report_deciles(df: pd.DataFrame, kind: str, target: str = "ret_net_pct") -> None:
    print(f"\n--- 特征十分位 -> {target}（Q1 最低 -> Q10 最高）---")
    print(f"{'特征':16s}" + "".join(f"{'Q' + str(i + 1):>9s}" for i in range(10)) + f"{'Q10-Q1':>10s}")
    for feat in FEATURES:
        col = df[feat]
        if col.notna().sum() < 500 or col.nunique() < 10:
            continue
        try:
            q = pd.qcut(col, 10, labels=False, duplicates="drop")
        except ValueError:
            continue
        means = df.groupby(q)[target].mean()
        if len(means) < 10:
            continue
        vals = "".join(f"{v:9.2%}" for v in means.values)
        print(f"{feat:16s}{vals}{means.iloc[-1] - means.iloc[0]:+10.2%}")


def report_extra_by_decile(df: pd.DataFrame, feat: str) -> None:
    """对某个特征额外看止损率 / 持有天数 / 胜率 / MFE，判断「为什么」。"""
    col = df[feat]
    q = pd.qcut(col, 10, labels=False, duplicates="drop")
    g = df.groupby(q)
    print(f"\n--- {feat} 十分位的分项 ---")
    print(f"{'Q':>3s} {'样本':>7s} {'净收益':>8s} {'胜率':>7s} {'止损率':>7s} {'持有天':>7s} {'MFE(R)':>7s} {'MAE':>8s}")
    for i, sub in g:
        print(
            f"{int(i) + 1:>3d} {len(sub):>7,} {sub['ret_net_pct'].mean():>8.2%} "
            f"{(sub['ret_net_pct'] > 0).mean():>7.1%} {sub['stopped'].mean():>7.1%} "
            f"{sub['holding_days'].mean():>7.1f} {sub['mfe_r'].mean():>7.2f} {sub['mae_pct'].mean():>8.2%}"
        )


def report_ic(df: pd.DataFrame, kind: str) -> None:
    """每日横截面 Spearman IC：把「同日候选按该特征排序」当预测行为来检验。"""
    print(f"\n--- 每日横截面 Spearman IC（特征 vs 结果），只取当日信号≥5 的交易日 ---")
    out: list[dict] = []
    by_date = list(df.groupby("date"))
    for feat in FEATURES:
        if df[feat].notna().sum() < 500:
            continue
        for target in ("ret_net_pct", "r_multiple", "holding_days", "fwd20", "fwd60"):
            ics = []
            for _, day in by_date:
                sub = day[[feat, target]].dropna()
                if len(sub) < 5 or sub[feat].nunique() < 3:
                    continue
                ic = sub[feat].corr(sub[target], method="spearman")
                if np.isfinite(ic):
                    ics.append(ic)
            if len(ics) < 50:
                continue
            ics_a = np.array(ics)
            out.append(
                {
                    "feature": feat,
                    "target": target,
                    "days": len(ics_a),
                    "mean_ic": float(ics_a.mean()),
                    "std_ic": float(ics_a.std()),
                    "ic_ir": float(ics_a.mean() / ics_a.std()) if ics_a.std() > 0 else np.nan,
                    "pos_pct": float((ics_a > 0).mean()),
                }
            )
    res = pd.DataFrame(out)
    if res.empty:
        return
    res.to_csv(DATA / f"entry_quality_ic_{kind}.csv", index=False)
    pivot = res.pivot(index="feature", columns="target", values="mean_ic")
    order = [c for c in ("ret_net_pct", "r_multiple", "fwd20", "fwd60", "holding_days") if c in pivot.columns]
    print(f"{'特征':16s}" + "".join(f"{c:>14s}" for c in order))
    for feat in pivot.index:
        vals = "".join(
            f"{pivot.loc[feat, c]:+14.4f}"
            if c in pivot.columns and pd.notna(pivot.loc[feat, c])
            else f"{'--':>14s}"
            for c in order
        )
        print(f"{feat:16s}{vals}")
    print("\n（IC 为每日横截面秩相关均值。|IC|<0.02 ≈ 没有排序信息；"
          "正值=该特征越高结果越好，负值=越高越差）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--per-symbol-cap", type=int, default=400)
    args = ap.parse_args()

    panel = pd.read_parquet(DATA / "panel.parquet")
    panel["ready"] = (
        panel["dif"].notna() & panel["dea"].notna() & panel["atr"].notna()
        & panel["trend_score"].notna() & panel["trend_ma5"].notna()
    )
    panel = prepare(panel)
    panel = add_forward_returns(panel)
    panel = panel[panel["date"] >= args.start]
    print(f"面板 {len(panel):,} 行 / {panel['symbol'].nunique()} 标的 / 起 {args.start}")

    for kind in ("is_state", "is_event"):
        df = collect(panel, kind, args.per_symbol_cap)
        label = kind[3:]
        df.to_csv(DATA / f"entry_quality_{label}.csv", index=False)
        print(f"\n{'=' * 100}")
        print(f"【{label} 口径】入场样本 {len(df):,} 笔 | 整体 净收益均值 {df['ret_net_pct'].mean():+.2%}"
              f" | 胜率 {(df['ret_net_pct'] > 0).mean():.1%}"
              f" | 止损率 {df['stopped'].mean():.1%}"
              f" | 平均持有 {df['holding_days'].mean():.1f} 天"
              f" | 平均MFE {df['mfe_r'].mean():.2f}R")
        report_deciles(df, label)
        for feat in ("trend_score", "bias_atr", "er", "days_in_golden"):
            if feat in df.columns and df[feat].nunique() >= 10:
                report_extra_by_decile(df, feat)
        report_ic(df, label)


if __name__ == "__main__":
    main()
