"""研究 1：信号密度 —— 每天到底有多少标的产生入场信号？

这是「选哪个买」这个问题的规模前提。回答三件事：

  1. 状态型信号（策略字面定义，如 macd_line >= macd_signal）每天覆盖多少标的 ——
     它是「当前处于金叉状态」的比例，不是「今天刚金叉」；
  2. 事件型信号（当日新发生穿越）每天有多少 —— 这才是组合层真正需要排序的候选集；
  3. 事件的日历聚集性 —— 信号是平均分布，还是集中在少数几天（regime 依赖）。

输出: data/signal_density_daily.csv, signal_density_summary.json, signal_density.png
用法: python signal_density.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

#: 只统计 2015 年以后 —— 早年标的数极少（1993 年个位数），全历史均值无意义。
START = "2015-01-01"

ENTRY_DEFS = {
    "macd_state": "MACD 金叉状态（dif ≥ dea）",
    "macd_event": "MACD 当日新金叉（dif 上穿 dea）",
    "trend_state": "趋势值 ≥ 5（状态）",
    "trend_event": "趋势值 MA5 由 ≤0 转正（当日新出现）",
}


def build_daily(panel: pd.DataFrame) -> pd.DataFrame:
    panel = panel.sort_values(["symbol", "date"])
    # 指标就绪 = 该标的当日可参与交易（预热完毕）
    ready = (
        panel["dif"].notna()
        & panel["dea"].notna()
        & panel["atr"].notna()
        & panel["trend_score"].notna()
        & panel["trend_ma5"].notna()
    )
    panel = panel.assign(ready=ready)
    g = panel.groupby("symbol", sort=False)
    panel["prev_dif"] = g["dif"].shift(1)
    panel["prev_dea"] = g["dea"].shift(1)
    panel["prev_tm5"] = g["trend_ma5"].shift(1)
    panel["prev_ready"] = g["ready"].shift(1)

    panel["macd_state"] = panel["ready"] & (panel["dif"] >= panel["dea"])
    panel["macd_event"] = (
        panel["ready"]
        & panel["prev_ready"].fillna(False)
        & (panel["dif"] >= panel["dea"])
        & (panel["prev_dif"] < panel["prev_dea"])
    )
    panel["trend_state"] = panel["ready"] & (panel["trend_score"] >= 5.0)
    panel["trend_event"] = (
        panel["ready"]
        & panel["prev_ready"].fillna(False)
        & (panel["trend_ma5"] > 0)
        & (panel["prev_tm5"] <= 0)
    )

    panel = panel[panel["date"] >= START]
    daily = panel.groupby("date").agg(
        n_ready=("ready", "sum"),
        macd_state=("macd_state", "sum"),
        macd_event=("macd_event", "sum"),
        trend_state=("trend_state", "sum"),
        trend_event=("trend_event", "sum"),
    )
    daily["macd_state_pct"] = daily["macd_state"] / daily["n_ready"]
    daily["trend_state_pct"] = daily["trend_state"] / daily["n_ready"]
    return daily


def main() -> None:
    panel = pd.read_parquet(DATA / "panel.parquet")
    daily = build_daily(panel)
    daily.to_csv(DATA / "signal_density_daily.csv")

    summary: dict = {
        "start": START,
        "trading_days": int(len(daily)),
        "median_ready_universe": float(daily["n_ready"].median()),
        "signals": {},
    }
    quantiles = [0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99]
    for key in ENTRY_DEFS:
        s = daily[key].astype(float)
        entry = {
            "label": ENTRY_DEFS[key],
            "mean": float(s.mean()),
            "median": float(s.median()),
            "max": float(s.max()),
            "zero_days": int((s == 0).sum()),
            "zero_pct": float((s == 0).mean()),
            "pct_of_universe_median": float(s.median() / daily["n_ready"].median()),
        }
        for q in quantiles:
            entry[f"q{int(q * 100)}"] = float(s.quantile(q))
        # 聚集性：事件总量的前 10% 交易日贡献了多少份额？
        if key.endswith("_event"):
            total = s.sum()
            top = s.sort_values(ascending=False)
            k = max(int(len(top) * 0.10), 1)
            entry["top10pct_days_share"] = float(top.iloc[:k].sum() / total) if total else 0.0
        summary["signals"][key] = entry

    state_pct = {
        "macd_state_pct_mean": float(daily["macd_state_pct"].mean()),
        "macd_state_pct_median": float(daily["macd_state_pct"].median()),
        "trend_state_pct_mean": float(daily["trend_state_pct"].mean()),
        "trend_state_pct_median": float(daily["trend_state_pct"].median()),
    }
    summary["state_coverage"] = state_pct

    (DATA / "signal_density_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ---- 打印 ----
    print(f"交易日 {summary['trading_days']}  标的池中位数 {summary['median_ready_universe']:.0f}")
    print()
    print(f"{'信号类型':32s} {'中位':>6s} {'均值':>7s} {'P90':>6s} {'P99':>6s} {'最大':>6s} {'0信号日':>8s} {'占池中位':>8s}")
    for key, e in summary["signals"].items():
        print(
            f"{e['label']:32s} {e['median']:6.1f} {e['mean']:7.1f} {e['q90']:6.1f} "
            f"{e['q99']:6.1f} {e['max']:6.0f} {e['zero_pct']:7.1%} "
            f"{e['pct_of_universe_median']:7.1%}"
        )
    print()
    print("状态型覆盖（占可就交易标的比例）:")
    for k, v in state_pct.items():
        print(f"  {k:28s} = {v:.1%}")
    print()
    for key, e in summary["signals"].items():
        if "top10pct_days_share" in e:
            print(f"聚集性 {e['label']}: 前 10% 交易日贡献了 {e['top10pct_days_share']:.1%} 的事件")

    # ---- 图 ----
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    fig, axes = plt.subplots(2, 1, figsize=(13, 8))
    ax = axes[0]
    ax.plot(daily.index, daily["macd_state_pct"], lw=0.7, color="#c0392b", label="MACD 金叉状态占比")
    ax.plot(daily.index, daily["trend_state_pct"], lw=0.7, color="#2980b9", label="趋势值≥5 占比")
    ax.set_ylabel("占可交易标的比例")
    ax.set_title("状态型信号覆盖：每天有多大比例的标的「正处于」信号状态")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(alpha=0.3)

    ax = axes[1]
    ax.plot(daily.index, daily["macd_event"].rolling(20).mean(), lw=1.0, color="#c0392b", label="MACD 新金叉（20日均）")
    ax.plot(daily.index, daily["trend_event"].rolling(20).mean(), lw=1.0, color="#2980b9", label="趋势启动（20日均）")
    ax.set_ylabel("每日事件数（20 日均）")
    ax.set_title("事件型信号：每天「新发生」的信号数 —— 组合层真正要排序的候选集")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(HERE / "signal_density.png", dpi=130)
    print(f"\n图 -> {HERE / 'signal_density.png'}")


if __name__ == "__main__":
    main()
