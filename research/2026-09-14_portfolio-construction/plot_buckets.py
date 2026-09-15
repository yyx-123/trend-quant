"""绘制分桶分析图表（中文标签；隔离 plot-venv 运行，仅依赖 matplotlib）。

用法: scripts/temp/plot-venv/Scripts/python.exe plot_buckets.py
输入: data/buckets_*.csv / data/freshness_*.csv / stats.json（analyze_buckets.py 产物）
输出: fig_*.png（入库，REPORT.md 相对路径引用）

中文字体：优先 Windows 自带微软雅黑（C:/Windows/Fonts/msyh.ttc），
否则下载 Noto Sans CJK（OFL 协议，字体文件不入库，见 research/.gitignore）。
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd
from matplotlib import font_manager

BASE_DIR = Path(__file__).resolve().parent
OUT = BASE_DIR / "data"

FONT_PATH = BASE_DIR / "fonts" / "NotoSansCJKsc-Regular.otf"
_MS_YAHEI = Path("C:/Windows/Fonts/msyh.ttc")


def setup_font() -> None:
    if _MS_YAHEI.exists():
        font_manager.fontManager.addfont(str(_MS_YAHEI))
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(_MS_YAHEI)).get_name()
    else:
        if not FONT_PATH.exists():
            FONT_PATH.parent.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(
                "https://github.com/notofonts/noto-cjk/raw/main/"
                "Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf",
                FONT_PATH,
            )
        font_manager.fontManager.addfont(str(FONT_PATH))
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(FONT_PATH)).get_name()
    plt.rcParams["axes.unicode_minus"] = False


def fig_quintile_grids() -> None:
    """信号日 5 分桶 20 日收益条形图：两个股票信号队列 × 关键因子。"""
    factors_show = (
        "趋势值", "趋势质量 ER", "52 周新高距离", "止损宽度 ATR%",
        "过热 bias_atr", "近 20 日涨幅", "波动调整动量",
    )
    for key, title in (("trend_start_stock", "趋势产生信号（股票，n=80,354）"),
                       ("macd_cross_stock", "MACD金叉信号（股票，n=44,220）")):
        df = pd.read_csv(OUT / f"buckets_{key}.csv")
        df = df[df["factor"].str.startswith(factors_show)]
        fig, axes = plt.subplots(2, 4, figsize=(15, 6.2))
        for ax, (factor, sub) in zip(axes.flat, df.groupby("factor", sort=False)):
            vals = sub["mean_20d"].values
            colors = ["#d9534f" if v < 0 else "#2c7fb8" for v in vals]
            ax.bar(sub["quintile"], vals, color=colors)
            ax.axhline(df[df["factor"] == factor]["mean_20d"].mean(), ls="--",
                       lw=0.8, c="gray")
            ax.set_title(factor, fontsize=10)
            ax.tick_params(labelsize=8)
        for ax in axes.flat[len(df["factor"].unique()):]:
            ax.axis("off")
        fig.suptitle(f"{title} — 信号日内 5 分桶，20 日收益均值%（Q5=因子假设最优方向）",
                     fontsize=12)
        fig.tight_layout()
        fig.savefig(BASE_DIR / f"fig_buckets_{key}.png", dpi=130)
        plt.close(fig)


def fig_freshness() -> None:
    """信号新鲜度：距信号天数 vs 未来收益（4 条队列）。"""
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.2), sharey=False)
    for ax, key, title in (
        (axes[0], "trend_start_stock", "趋势产生（股票）"),
        (axes[1], "trend_start_etf", "趋势产生（ETF）"),
    ):
        df = pd.read_csv(OUT / f"freshness_{key}.csv")
        x = range(len(df))
        ax.plot(x, df["mean_10d%"], marker="o", label="10 日均值")
        ax.plot(x, df["mean_20d%"], marker="s", label="20 日均值")
        ax.plot(x, df["median_20d%"], marker="^", ls="--", label="20 日中位数")
        ax.set_xticks(list(x), df["距信号天数"])
        ax.set_xlabel("距信号天数")
        ax.set_ylabel("收益 %")
        ax.set_title(title)
        ax.legend(fontsize=9)
        ax.grid(alpha=0.3)
    fig.suptitle("信号新鲜度与未来收益（候选池 = 过去 10 天内出过信号的标的）", fontsize=12)
    fig.tight_layout()
    fig.savefig(BASE_DIR / "fig_freshness.png", dpi=130)
    plt.close(fig)


def fig_regime() -> None:
    """市场状态：(a) 信号池基线收益按 breadth 三档；(b) 关键因子在各档下的 20 日顶底价差。"""
    stats = json.loads((BASE_DIR / "stats.json").read_text(encoding="utf-8"))
    fig, axes = plt.subplots(1, 2, figsize=(12.5, 4.6))

    # (a) 基线：从 freshness 之外的 regime baseline —— 用 C 表重建整体均值
    # C 表只有分桶；基线单独从 events 重算太重，这里用 analyze 输出时打印的
    # 基线表直接硬编码会腐化——改为从 events csv 现算（一次，几秒）。
    ev = pd.read_csv(OUT / "events_trend_start_stock.csv", parse_dates=["date"],
                     usecols=["date", "fwd_ret_20"])
    panel = pd.read_parquet(OUT / "panel.parquet",
                            columns=["symbol", "date", "asset_type", "close", "sma200"])
    stock = panel[panel["asset_type"] == "stock"]
    breadth = (stock.assign(above=(stock["close"] > stock["sma200"]).astype(float))
               .groupby("date")["above"].mean())
    q1, q2 = breadth.quantile(1 / 3), breadth.quantile(2 / 3)
    ev["breadth"] = ev["date"].map(breadth)
    import numpy as np
    ev["regime"] = np.where(ev["breadth"] <= q1, "低(≤26%)",
                            np.where(ev["breadth"] <= q2, "中", "高(≥60%)"))
    g = ev.dropna(subset=["fwd_ret_20"]).groupby("regime")["fwd_ret_20"]
    order = ["低(≤26%)", "中", "高(≥60%)"]
    means = [g.mean()[k] * 100 for k in order]
    meds = [g.median()[k] * 100 for k in order]
    ns = [g.size()[k] for k in order]
    x = range(3)
    axes[0].bar([i - 0.18 for i in x], means, width=0.36, label="均值", color="#2c7fb8")
    axes[0].bar([i + 0.18 for i in x], meds, width=0.36, label="中位数", color="#fdae61")
    for i, (m, n) in enumerate(zip(means, ns)):
        axes[0].text(i - 0.18, m + 0.1, f"n={n:,}", ha="center", fontsize=8)
    axes[0].set_xticks(list(x), order)
    axes[0].set_xlabel("市场状态（全池股票站上 SMA200 比例）")
    axes[0].set_ylabel("20 日收益 %")
    axes[0].set_title("趋势产生信号池基线收益 × 市场状态")
    axes[0].legend()
    axes[0].grid(alpha=0.3, axis="y")

    # (b) 因子 20 日顶底价差 × 市场状态
    cdata = stats["C"]["trend_start_stock"]
    label_map = {"trend_score": "趋势值", "high250_dist": "52周新高距离",
                 "ret20": "近20日涨幅", "bias_atr": "过热bias", "atr_pct": "ATR%",
                 "er": "ER"}
    regimes = ["low", "mid", "high"]
    width = 0.13
    for i, (factor, label) in enumerate(label_map.items()):
        rows = {r["regime"]: r for r in cdata.get(factor, [])}
        vals = [rows[r]["spread_20d%"] if r in rows else 0 for r in regimes]
        axes[1].bar([j + (i - 2.5) * width for j in range(3)], vals, width=width,
                    label=label)
    axes[1].axhline(0, c="k", lw=0.8)
    axes[1].set_xticks(range(3), ["低", "中", "高"])
    axes[1].set_xlabel("市场状态（breadth 三档）")
    axes[1].set_ylabel("Q5−Q1 价差（20 日收益 %）")
    axes[1].set_title("排序因子顶底价差 × 市场状态（股票·趋势产生）")
    axes[1].legend(fontsize=8, ncol=2)
    axes[1].grid(alpha=0.3, axis="y")

    fig.tight_layout()
    fig.savefig(BASE_DIR / "fig_regime.png", dpi=130)
    plt.close(fig)


def main() -> None:
    setup_font()
    fig_quintile_grids()
    fig_freshness()
    fig_regime()
    print("figs written")


if __name__ == "__main__":
    main()
