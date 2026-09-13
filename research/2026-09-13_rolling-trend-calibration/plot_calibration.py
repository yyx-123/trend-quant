"""绘制滚动锚定周/月趋势值标定图（中文标签；隔离 plot-venv 运行，仅依赖 matplotlib）。

读取 compute_calibration.py 输出的 data/*.npy 与 data/*.json：

- ``dist_{key}.png``       六组分布直方图（左线性 / 右对数，±5 竖线与分位注释）
- ``diag_saturation.png``  周/月 |norm_slope|>95、|norm_bias|>95 饱和率对比柱状图
                           （附 volume_factor / ER 抽样诊断数字）
- ``diag_er_variants.png`` 月尺度 er_period ∈ {3,4,6} 分布叠加（CDF 全图 + ±5 附近放大）

中文字体：fonts/NotoSansCJKsc-Regular.otf（OFL 协议）。字体文件不入库，
缺失时自动从 noto-cjk 仓库下载（需联网）。

运行：scripts/temp/plot-venv/bin/python research/2026-09-13_rolling-trend-calibration/plot_calibration.py
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
    "stock_weekly": "股票 · 滚动周K (5日/bar)",
    "stock_monthly": "股票 · 滚动月K (22日/bar)",
    "etf_daily": "ETF · 日K (1d)",
    "etf_weekly": "ETF · 滚动周K (5日/bar)",
    "etf_monthly": "ETF · 滚动月K (22日/bar)",
}


def normal_pdf(x: np.ndarray, mean: float, std: float) -> np.ndarray:
    return np.exp(-0.5 * ((x - mean) / std) ** 2) / (std * np.sqrt(2 * np.pi))


def plot_distributions(stats: dict) -> None:
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
            f"按 ±5 划分：负 {info['pct_le_-5'] * 100:.1f}% / "
            f"无 {info['pct_abs_lt_5'] * 100:.1f}% / "
            f"正 {info['pct_ge_5'] * 100:.1f}%\n"
            f"±5 所处分位：−5 → {info['pctile_rank_at_neg5'] * 100:.1f}%   "
            f"+5 → {info['pctile_rank_at_pos5'] * 100:.1f}%\n"
            f"p5/p95 = {info['percentiles']['p5']:.1f} / {info['percentiles']['p95']:.1f}\n"
            f"p1/p99 = {info['percentiles']['p1']:.1f} / {info['percentiles']['p99']:.1f}\n"
            f"±60 之外的质量：{outside * 100:.2f}%"
        )
        ax.text(0.015, 0.97, text, transform=ax.transAxes, fontsize=8.5,
                va="top", ha="left",
                bbox=dict(boxstyle="round", fc="white", ec="#999999", alpha=0.9))

        fig.suptitle(f"趋势值分布（滚动锚定口径，日频采样） — {label}（qfq，全历史）", fontsize=12)
        fig.tight_layout(rect=(0, 0, 1, 0.95))
        out = BASE_DIR / f"dist_{key}.png"
        fig.savefig(out, dpi=130)
        plt.close(fig)
        print(f"saved {out}")


def plot_saturation(sat: dict) -> None:
    periods = (("1w", "滚动周K"), ("1M", "滚动月K"))
    categories = (
        ("pct_abs_norm_slope_gt_95", "tanh 饱和\n|norm_slope| > 95", lambda d: d["pct_abs_norm_slope_gt_95"]),
        ("pct_abs_norm_bias_gt_95", "tanh 饱和\n|norm_bias| > 95", lambda d: d["pct_abs_norm_bias_gt_95"]),
        ("vf_lt_0.2", "量能不足\nP(volume_factor < 0.2)", lambda d: d["volume_factor"]["pct_lt_0.2"]),
        ("er_gt_0.8", "高效率比\nP(ER > 0.8)", lambda d: d["er"]["pct_gt_0.8"]),
    )
    colors = {"1w": "#4C72B0", "1M": "#DD8452"}

    fig, ax = plt.subplots(figsize=(9.5, 5.0))
    x = np.arange(len(categories))
    width = 0.32
    for i, (pkey, plabel) in enumerate(periods):
        vals = [get(sat[pkey]) * 100 for _, _, get in categories]
        bars = ax.bar(x + (i - 0.5) * width, vals, width, label=plabel, color=colors[pkey])
        for rect, v in zip(bars, vals):
            ax.text(rect.get_x() + rect.get_width() / 2, rect.get_height() + 0.4,
                    f"{v:.2f}%" if v < 1 else f"{v:.1f}%", ha="center", fontsize=9)
    ax.set_xticks(x)
    ax.set_xticklabels([c[1] for c in categories], fontsize=9)
    ax.set_ylabel("占抽样点比例 %")
    ax.set_ylim(0, max(get(sat[p]) for p, _ in periods for _, _, get in categories) * 100 * 1.3 + 2)
    ax.grid(alpha=0.25, axis="y")
    ax.legend()

    meta = sat["meta"]
    lines = [f"抽样 {meta['symbols_processed']} 只标的 × {meta['sample_days_per_symbol']} 日"
             f"（股票 {meta['symbols_by_asset']['stock']} / ETF {meta['symbols_by_asset']['etf']}）"]
    for pkey, plabel in periods:
        d = sat[pkey]
        lines.append(
            f"{plabel}：|slope| 中位 {d['norm_slope_abs_median']:.1f}、|bias| 中位 {d['norm_bias_abs_median']:.1f}、"
            f"vf 中位 {d['volume_factor']['median']:.2f}、ER 中位 {d['er']['median']:.2f}"
        )
    sc = sat["self_check"]
    lines.append(f"等价性自检：{sc['points']} 点最大偏差 {sc['max_abs_diff']:.1e}（>1e-9 计 0 处）")
    ax.text(0.02, 0.97, "\n".join(lines),
            transform=ax.transAxes, fontsize=8.5, va="top", ha="left",
            bbox=dict(boxstyle="round", fc="white", ec="#999999", alpha=0.9))

    fig.suptitle("滚动周/月趋势公式中间量诊断：tanh 饱和率与量能/ER 分布", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    out = BASE_DIR / "diag_saturation.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print(f"saved {out}")


def plot_er_variants(er_json: dict) -> None:
    series = {
        3: np.concatenate([np.load(DATA_DIR / "stock_monthly.npy"),
                           np.load(DATA_DIR / "etf_monthly.npy")]),
        4: np.load(DATA_DIR / "er_variant_monthly_er4.npy"),
        6: np.load(DATA_DIR / "er_variant_monthly_er6.npy"),
    }
    labels = {3: "er=3（现行）", 4: "er=4", 6: "er=6"}
    colors = {3: "#4C72B0", 4: "#55A868", 6: "#DD8452"}

    fig, (ax, ax_zoom) = plt.subplots(1, 2, figsize=(13, 5.2))
    for er in (3, 4, 6):
        vals = np.sort(series[er].astype(float))
        cdf = np.arange(1, vals.size + 1) / vals.size
        v = er_json["variants"][f"er{er}"]
        label = (f"{labels[er]}  σ={v['std']:.2f}  "
                 f"P(≥5)={v['pct_ge_5'] * 100:.1f}%  P(|x|<5)={v['pct_abs_lt_5'] * 100:.1f}%")
        for axis in (ax, ax_zoom):
            axis.plot(vals, cdf, lw=1.4, color=colors[er], label=label)
    for axis, xlim, title in ((ax, (-40, 40), "全区间"), (ax_zoom, (-15, 15), "±5 阈值附近放大")):
        axis.axvline(-5, color="black", lw=1.0, ls="--")
        axis.axvline(5, color="black", lw=1.0, ls="--", label="现行阈值 ±5")
        axis.set_xlim(*xlim)
        axis.set_xlabel("趋势值 trend_score")
        axis.set_ylabel("累积分布 CDF")
        axis.set_title(f"月尺度滚动趋势值 — ER 窗口变体（{title}）", fontsize=11)
        axis.grid(alpha=0.25)
        axis.legend(fontsize=8, loc="upper left")

    pw = er_json["pairwise"]
    text = (
        f"逐位对齐三元组 N = {er_json['meta']['n_aligned_triples']:,}\n"
        f"er3 vs er4：ρ={pw['er3_er4']['pearson']:.4f}，"
        f"三态一致率 {pw['er3_er4']['state_agreement'] * 100:.1f}%\n"
        f"er3 vs er6：ρ={pw['er3_er6']['pearson']:.4f}，"
        f"三态一致率 {pw['er3_er6']['state_agreement'] * 100:.1f}%\n"
        f"er4 vs er6：ρ={pw['er4_er6']['pearson']:.4f}，"
        f"三态一致率 {pw['er4_er6']['state_agreement'] * 100:.1f}%"
    )
    ax.text(0.98, 0.03, text, transform=ax.transAxes, fontsize=8.5,
            va="bottom", ha="right",
            bbox=dict(boxstyle="round", fc="white", ec="#999999", alpha=0.9))

    fig.suptitle("月尺度 er_period ∈ {3,4,6} 分布叠加（全市场池化，滚动口径）", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    out = BASE_DIR / "diag_er_variants.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print(f"saved {out}")


def main() -> None:
    stats = json.loads((DATA_DIR / "stats.json").read_text(encoding="utf-8"))
    sat = json.loads((DATA_DIR / "saturation.json").read_text(encoding="utf-8"))
    er_json = json.loads((DATA_DIR / "er_variants.json").read_text(encoding="utf-8"))
    plot_distributions(stats)
    plot_saturation(sat)
    plot_er_variants(er_json)


if __name__ == "__main__":
    main()
