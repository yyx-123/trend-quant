"""信号日横截面分桶分析：回答「每天几十个信号，该买谁」。

数据: data/panel.parquet（build_panel.py 生成，生产同源指标）。

两个分析：

A. 信号日分桶（因子有效性）
   每个信号日 t 收盘出信号（MACD金叉 / 趋势产生），t+1 开盘价买入，
   持有 N ∈ {5,10,20} 个交易日至收盘。按因子值在当日信号池内分 5 桶，
   看各桶未来收益的单调性与顶底价差。同时给 rank IC / ICIR。

B. 信号新鲜度分组（「越灵敏越好」的横截面版检验）
   每天 t，候选 = 过去 10 天内出过信号的标的；按「距信号天数 d」分组
   {0,1,2,3-5,6-10}，t+1 开盘价买入，看未来收益随 d 的衰减/反转。

可执行性口径（A股）：
   - t+1 无 bar（停牌）→ 剔除；
   - t+1 一字板（high==low）→ 无法成交，剔除；
   - 20 日均成交额 < 5000 万 → 剔除；
   - 上市 < 60 根 bar → 剔除；ST → 剔除；仅 enabled 标的。

用法: python analyze_buckets.py
输出: data/buckets_*.csv / data/freshness_*.csv / stats.json（入库）；
      data/events_*.csv 明细不入库（见 research/.gitignore）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
OUT = HERE / "data"

HORIZONS = (5, 10, 20)
MIN_PER_DAY_QUINTILE = 15      # 当日信号数少于此值不做分桶（但计入 baseline/IC? 不, IC 也要分桶同批样本）
MIN_PER_DAY_IC = 10            # rank IC 的最小当日样本
AMOUNT_MIN = 5e7               # 20 日均成交额下限（元）
FRESH_WINDOWS = [(0, 0), (1, 1), (2, 2), (3, 5), (6, 10)]

# (因子列, 方向假设: +1 = 值大期望收益高, -1 = 值小期望收益高)
# 注: days_since_<自身信号> 在信号日队列里恒为 0（无横截面变化），由
# SKIP_CONSTANT 自动跳过；新鲜度由分析 B 专门检验。
FACTORS: list[tuple[str, int, str]] = [
    ("trend_score", +1, "趋势值（用户现行做法：越大越好）"),
    ("days_since_macd_cross", -1, "金叉新鲜度（天，越小越新鲜）"),
    ("days_since_trend_start", -1, "趋势产生新鲜度（天）"),
    ("er", +1, "趋势质量 ER"),
    ("mom_12_1", +1, "12-1 动量（跳过近 21 日）"),
    ("mom_vol_adj", +1, "波动调整动量 ret/σ"),
    ("high250_dist", +1, "52 周新高距离（越近越强）"),
    ("slope_r2", +1, "回归斜率×R²（Clenow）"),
    ("atr_pct", -1, "止损宽度 ATR%（越小越好）"),
    ("bias_atr", -1, "过热 bias_atr（越小越好）"),
    ("ret20", +1, "近 20 日涨幅（不跳过，对照组——文献预期它在 A 股无效或反向）"),
]

# 分析 C（市场状态条件）只做这几个关键因子
REGIME_FACTORS = ("trend_score", "atr_pct", "bias_atr", "ret20", "high250_dist", "er")


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    """按 symbol 组内计算派生因子与信号。全部向量化/rolling，无逐行循环。"""
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    g = df.groupby("symbol", group_keys=False)

    df["bar_no"] = g.cumcount()
    df["ret1"] = g["close"].pct_change()

    # --- 信号 ---
    macd_up = (df["dif"] > df["dea"]) & df["dif"].notna() & df["dea"].notna()
    df["sig_macd_cross"] = macd_up & ~macd_up.groupby(df["symbol"]).shift(1, fill_value=False)
    trend_up = (df["trend_score"] >= 5) & (df["trend_ma5"] >= 0)
    df["sig_trend_start"] = trend_up & ~trend_up.groupby(df["symbol"]).shift(1, fill_value=False)

    # --- 距上次信号天数（新鲜度）---
    for sig, col in (("sig_macd_cross", "days_since_macd_cross"),
                     ("sig_trend_start", "days_since_trend_start")):
        # 每 symbol：当前 bar_no - 最近一次信号 bar_no
        sig_bar = df["bar_no"].where(df[sig])
        last_sig = sig_bar.groupby(df["symbol"]).ffill()
        df[col] = df["bar_no"] - last_sig

    # --- 动量类因子（均跳过近 21 日，ret20 除外作对照）---
    df["mom_12_1"] = g["close"].transform(lambda s: s.shift(21) / s.shift(250) - 1)
    mom_6_1 = g["close"].transform(lambda s: s.shift(21) / s.shift(126) - 1)
    vol60 = g["ret1"].transform(lambda s: s.rolling(60, min_periods=40).std())
    df["mom_vol_adj"] = mom_6_1 / (vol60 * np.sqrt(126))

    # --- 52 周新高距离 ---
    high250 = g["close"].transform(lambda s: s.rolling(250, min_periods=120).max())
    df["high250_dist"] = df["close"] / high250

    # --- 回归斜率 × R²（90 日 log 价格对时间，向量化 rolling 公式）---
    df["slope_r2"] = np.nan
    w = 90
    x_mean, x_var = (w - 1) / 2.0, (w * w - 1) / 12.0
    for sym, idx in g.indices.items():
        close = df["close"].values[idx]
        y = np.log(np.where(close > 0, close, np.nan))
        s_y = pd.Series(y, index=idx)
        sy = s_y.rolling(w, min_periods=w).sum()
        syy = (s_y * s_y).rolling(w, min_periods=w).sum()
        j = pd.Series(np.arange(len(idx), dtype=float), index=idx)
        sjy = (j * s_y).rolling(w, min_periods=w).sum()
        # 窗内 x = j - (j_end - w + 1)，x 均值/方差恒定
        x_base = j - (w - 1)                       # 窗内第一个点的原始序号
        cov = sjy / w - x_base * sy / w - x_mean * sy / w
        var_y = syy / w - (sy / w) ** 2
        r2 = np.where(var_y > 0, (cov ** 2) / (x_var * var_y), np.nan)
        ann_slope = cov / x_var * 250              # 日斜率年化
        df.loc[idx, "slope_r2"] = np.clip(ann_slope, -10, 10) * r2

    # --- 流动性与可执行性 ---
    df["amount_ma20"] = g["amount"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    gt = df.groupby("symbol", group_keys=False)
    df["next_open"] = gt["open"].shift(-1)
    df["next_high"] = gt["high"].shift(-1)
    df["next_low"] = gt["low"].shift(-1)
    for n in HORIZONS:
        df[f"fwd_close_{n}"] = gt["close"].shift(-n)
        df[f"fwd_ret_{n}"] = df[f"fwd_close_{n}"] / df["next_open"] - 1

    return df


def universe_mask(df: pd.DataFrame, asset: str) -> pd.Series:
    m = (
        (df["asset_type"] == asset)
        & (df["enabled"] == 1)
        & ~df["name"].fillna("").str.contains("ST")
        & (df["bar_no"] >= 60)
        & (df["amount_ma20"] >= AMOUNT_MIN)
        & df["atr"].notna()
        & df["next_open"].notna()
        & ~(df["next_high"] == df["next_low"])   # t+1 一字板不可成交
    )
    return m


def bucket_stats(events: pd.DataFrame, factor: str, direction: int) -> tuple[pd.DataFrame, dict]:
    """对单个因子做信号日内 5 分桶 + rank IC。返回 (分桶表, 摘要dict)。"""
    ev = events.dropna(subset=[factor]).copy()
    day_counts = ev.groupby("date")[factor].transform("size")
    ev_q = ev[day_counts >= MIN_PER_DAY_QUINTILE].copy()
    # direction<0 的因子翻转符号，使「Q5 = 期望最好」统一
    ev_q["f"] = ev_q[factor] * direction
    ev_q["q"] = ev_q.groupby("date")["f"].transform(
        lambda s: pd.qcut(s.rank(method="first"), 5, labels=False)
    )
    rows = {"quintile": [f"Q{i+1}" for i in range(5)]}
    summary: dict = {"factor": factor, "direction": direction, "n_events": len(ev)}
    for n in HORIZONS:
        col = f"fwd_ret_{n}"
        piv = ev_q.groupby("q")[col].agg(["mean", "median", "size"])
        rows[f"mean_{n}d"] = (piv["mean"] * 100).round(2).values
        rows[f"median_{n}d"] = (piv["median"] * 100).round(2).values
        rows[f"n_{n}d"] = piv["size"].values
        spread = piv["mean"].iloc[-1] - piv["mean"].iloc[0]
        summary[f"spread_{n}d_pct"] = round(float(spread * 100), 2)
        # 5 个桶均值的 spearman = 均值排名与桶序号的 pearson（避免 scipy 依赖）
        ranks = pd.Series(piv["mean"].values).rank().values
        if np.std(ranks) > 0 and np.nanstd(piv["mean"].values) > 0:
            with np.errstate(invalid="ignore", divide="ignore"):
                summary[f"spearman_{n}d"] = round(
                    float(np.corrcoef(ranks, np.arange(1.0, 6.0))[0, 1]), 3
                )
        else:
            summary[f"spearman_{n}d"] = None
    # rank IC（每日因子值与未来收益的 spearman，跨日平均）
    ev_ic = ev[ev.groupby("date")[factor].transform("size") >= MIN_PER_DAY_IC]
    for n in HORIZONS:
        col = f"fwd_ret_{n}"
        ic = (
            ev_ic.groupby("date")
            .apply(lambda d: d[factor].rank().corr(d[col].rank()), include_groups=False)
            .dropna()
        )
        summary[f"IC_{n}d"] = round(float(ic.mean()), 4)
        summary[f"ICIR_{n}d"] = round(float(ic.mean() / ic.std()), 3) if ic.std() > 0 else None
    return pd.DataFrame(rows), summary


def freshness_table(df: pd.DataFrame, sig_col: str, fresh_col: str,
                    mask: pd.Series) -> pd.DataFrame:
    """分析 B：候选池 = 过去 10 天内出过信号；按距信号天数分组看未来收益。"""
    cand = df[mask & df[fresh_col].notna() & (df[fresh_col] <= 10)].copy()
    rows = []
    for lo, hi in FRESH_WINDOWS:
        sub = cand[(cand[fresh_col] >= lo) & (cand[fresh_col] <= hi)]
        row = {"距信号天数": f"{lo}" if lo == hi else f"{lo}-{hi}", "样本数": len(sub)}
        for n in HORIZONS:
            col = f"fwd_ret_{n}"
            row[f"mean_{n}d%"] = round(float(sub[col].mean() * 100), 2)
            row[f"median_{n}d%"] = round(float(sub[col].median() * 100), 2)
        rows.append(row)
    return pd.DataFrame(rows)


def add_regime(df: pd.DataFrame, mask: pd.Series) -> pd.Series:
    """市场状态 = 当日全池股票中 close > SMA200 的比例（breadth），按全样本三分位
    切成 low/mid/high 三档，逐日标注。只用 panel 内数据，不引入外部指数。"""
    stock = df[df["asset_type"] == "stock"]
    breadth = (stock.assign(above=(stock["close"] > stock["sma200"]).astype(float))
               .groupby("date")["above"].mean())
    q1, q2 = breadth.quantile(1 / 3), breadth.quantile(2 / 3)
    regime = pd.Series(
        np.where(breadth <= q1, "low", np.where(breadth <= q2, "mid", "high")),
        index=breadth.index,
    )
    print(f"breadth terciles: {q1:.2f} / {q2:.2f}", flush=True)
    return df["date"].map(regime)


def regime_table(events: pd.DataFrame, factor: str, direction: int,
                 regime: pd.Series) -> list[dict]:
    """分析 C：分市场状态（low/mid/high breadth）的信号日 5 分桶价差。"""
    ev = events.dropna(subset=[factor]).copy()
    ev["regime"] = regime.loc[ev.index]
    day_counts = ev.groupby("date")[factor].transform("size")
    ev = ev[day_counts >= MIN_PER_DAY_QUINTILE].copy()
    ev["f"] = ev[factor] * direction
    ev["q"] = ev.groupby("date")["f"].transform(
        lambda s: pd.qcut(s.rank(method="first"), 5, labels=False)
    )
    rows = []
    for rg in ("low", "mid", "high"):
        sub = ev[ev["regime"] == rg]
        if len(sub) < 200:
            continue
        row: dict = {"regime": rg, "n_events": len(sub)}
        for n in (10, 20):
            col = f"fwd_ret_{n}"
            piv = sub.groupby("q")[col].mean()
            row[f"Q1_{n}d%"] = round(float(piv.iloc[0] * 100), 2)
            row[f"Q5_{n}d%"] = round(float(piv.iloc[-1] * 100), 2)
            row[f"spread_{n}d%"] = round(float((piv.iloc[-1] - piv.iloc[0]) * 100), 2)
        rows.append(row)
    return rows


def main() -> None:
    t0 = time.time()
    df = pd.read_parquet(OUT / "panel.parquet")
    print(f"panel rows={len(df):,}", flush=True)
    df = add_features(df)
    print(f"features done {time.time()-t0:.1f}s", flush=True)

    stats: dict = {"generated": pd.Timestamp.now().isoformat(), "A": {}, "B": {}, "C": {}}
    stock_mask = universe_mask(df, "stock")
    regime = add_regime(df, stock_mask)
    for asset in ("stock", "etf"):
        mask = universe_mask(df, asset)
        for sig_col, sig_name in (("sig_macd_cross", "macd_cross"),
                                  ("sig_trend_start", "trend_start")):
            key = f"{sig_name}_{asset}"
            events = df[mask & df[sig_col]].copy()
            # 导出明细（不入库）
            keep = ["symbol", "date", "close", "next_open"] + \
                   [f for f, _, _ in FACTORS] + [f"fwd_ret_{n}" for n in HORIZONS]
            events[keep].to_csv(OUT / f"events_{key}.csv", index=False)
            print(f"[{key}] signal events: {len(events):,} "
                  f"({events['date'].min().date()} .. {events['date'].max().date()})", flush=True)

            stats["A"][key] = {"n_signal_events": len(events), "factors": {}}
            bucket_frames = []
            for factor, direction, label in FACTORS:
                if events[factor].nunique() < 5:
                    # 队列内常数因子（如自身信号的新鲜度在信号日恒为 0）
                    print(f"  {factor:>22} skipped (constant within cohort)", flush=True)
                    continue
                table, summary = bucket_stats(events, factor, direction)
                table.insert(0, "factor", label)
                bucket_frames.append(table)
                stats["A"][key]["factors"][factor] = summary
                print(f"  {factor:>22} 10d IC={summary.get('IC_10d')} "
                      f"spread={summary.get('spread_10d_pct')}% "
                      f"mono={summary.get('spearman_10d')}", flush=True)
            pd.concat(bucket_frames, ignore_index=True).to_csv(
                OUT / f"buckets_{key}.csv", index=False)

            fresh_col = "days_since_macd_cross" if sig_col == "sig_macd_cross" \
                else "days_since_trend_start"
            ft = freshness_table(df, sig_col, fresh_col, mask)
            ft.to_csv(OUT / f"freshness_{key}.csv", index=False)
            stats["B"][key] = ft.to_dict(orient="records")
            print(f"[freshness {key}]\n{ft.to_string(index=False)}", flush=True)

            # 分析 C：市场状态条件分桶（只跑股票队列，ETF 样本太少）
            if asset == "stock":
                stats["C"][key] = {}
                dir_map = {f: d for f, d, _ in FACTORS}
                for factor in REGIME_FACTORS:
                    rows = regime_table(events, factor, dir_map[factor], regime)
                    stats["C"][key][factor] = rows
                    for r in rows:
                        print(f"  [regime {factor:>14} {r['regime']:>4}] n={r['n_events']:>6} "
                              f"spread10={r['spread_10d%']}% spread20={r['spread_20d%']}%",
                              flush=True)

    with open(HERE / "stats.json", "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print(f"done {time.time()-t0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
