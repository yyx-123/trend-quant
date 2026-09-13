"""绘制趋势值分布图（中文标签；隔离 plot-venv 运行，仅依赖 matplotlib）。

读取 trend_score_distribution.py 输出的 data/*.npy 与 data/stats.json，
每组一张图：左面板线性刻度直方图（叠加同均值/方差的正态参照线与关键
分位线），右面板 log-y 直方图用于观察尾部。

中文字体：fonts/NotoSansCJKsc-Regular.otf（OFL 协议）。字体文件不入库，
缺失时自动从 noto-cjk 仓库下载（需联网）。

运行：scripts/temp/plot-venv/bin/python research/2026-09-12_trend-score-distribution/plot_trend_distribution.py
（plot-venv 若不存在：python3 -m venv scripts/temp/plot-venv &&
 scripts/temp/plot-venv/bin/pip install matplotlib）
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from matplotlib import font_manager

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
FONT_PATH = BASE_DIR / "fonts" / "NotoSansCJKsc-Regular.otf"
FONT_URL = (
    "https://github.com/notofonts/noto-cjk/raw/main/"
    "Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf"
)

if not FONT_PATH.exists():
    print(f"字体缺失，下载 {FONT_URL} ...")
    FONT_PATH.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(FONT_URL, FONT_PATH)

font_manager.fontManager.addfont(str(FONT_PATH))
plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(FONT_PATH)).get_name()
plt.rcParams["axes.unicode_minus"] = False

RANGE = (-60.0, 60.0)
BINS = 160

LABELS = {
    "stock_daily": "股票 · 日K (1d)",
    "stock_weekly": "股票 · 周K (1w)",
    "stock_monthly": "股票 · 月K (1M)",
    "etf_daily": "ETF · 日K (1d)",
    "etf_weekly": "ETF · 周K (1w)",
    "etf_monthly": "ETF · 月K (1M)",
}


def normal_pdf(x: np.ndarray, mean: float, std: float) -> np.ndarray:
    return np.exp(-0.5 * ((x - mean) / std) ** 2) / (std * np.sqrt(2 * np.pi))


def main() -> None:
    stats = json.loads((DATA_DIR / "stats.json").read_text(encoding="utf-8"))
    for key, label in LABELS.items():
        values = np.load(DATA_DIR / f"{key}.npy")
        info = stats[key]
        mean, std = info["mean"], info["std"]
        p25, p75 = info["percentiles"]["p25"], info["percentiles"]["p75"]
        outside = float(np.mean((values < RANGE[0]) | (values > RANGE[1])))

        fig, (ax, ax_log) = plt.subplots(1, 2, figsize=(13, 5.2))
        for axis, log in ((ax, False), (ax_log, True)):
            axis.hist(values, bins=BINS, range=RANGE, density=True,
                      color="#4C72B0", alpha=0.75, edgecolor="none", label="直方图")
            xs = np.linspace(RANGE[0], RANGE[1], 600)
            axis.plot(xs, normal_pdf(xs, mean, std), "r--", lw=1.2,
                      label=f"正态参照 (μ={mean:.2f}, σ={std:.2f})")
            axis.axvline(0, color="gray", lw=0.8)
            axis.axvline(mean, color="red", lw=1.4, label=f"均值 = {mean:.2f}")
            axis.axvline(-5, color="black", lw=1.2, ls="--")
            axis.axvline(5, color="black", lw=1.2, ls="--", label="现行阈值 ±5")
            axis.axvline(p25, color="#2196F3", lw=1.1, ls=":")
            axis.axvline(p75, color="#2196F3", lw=1.1, ls=":",
                         label=f"p25/p75 = {p25:.1f} / {p75:.1f}")
            axis.set_xlim(*RANGE)
            axis.set_xlabel("趋势值 trend_score")
            if log:
                axis.set_yscale("log")
                axis.set_title(f"{label} — 对数刻度（看尾部）", fontsize=11)
            else:
                axis.set_ylabel("密度")
                axis.set_title(f"{label} — 线性刻度", fontsize=11)
            axis.grid(alpha=0.25)
            axis.legend(fontsize=8, loc="upper right" if not log else "lower left")

        text = (
            f"N = {info['n']:,}（{info['symbols_with_data']} 只标的）\n"
            f"均值 = {mean:.3f}   标准差 = {std:.3f}\n"
            f"中位数 = {info['median']:.3f}   偏度 = {info['skew']:.2f}\n"
            f"超额峰度 = {info['excess_kurtosis']:.1f}\n"
            f"P(>0) = {info['pct_positive'] * 100:.1f}%\n"
            f"按 ±5 划分：负趋势 {info['pct_le_-5'] * 100:.1f}% / "
            f"无趋势 {info['pct_abs_lt_5'] * 100:.1f}% / "
            f"正趋势 {info['pct_ge_5'] * 100:.1f}%\n"
            f"p5/p95 = {info['percentiles']['p5']:.1f} / {info['percentiles']['p95']:.1f}\n"
            f"p1/p99 = {info['percentiles']['p1']:.1f} / {info['percentiles']['p99']:.1f}\n"
            f"±60 之外的质量：{outside * 100:.2f}%"
        )
        ax.text(0.015, 0.97, text, transform=ax.transAxes, fontsize=8.5,
                va="top", ha="left",
                bbox=dict(boxstyle="round", fc="white", ec="#999999", alpha=0.9))

        fig.suptitle(f"趋势值分布 — {label}（qfq，全历史，仅已收盘周期）", fontsize=12)
        fig.tight_layout(rect=(0, 0, 1, 0.95))
        out = BASE_DIR / f"dist_{key}.png"
        fig.savefig(out, dpi=130)
        plt.close(fig)
        print(f"saved {out}")


if __name__ == "__main__":
    main()
