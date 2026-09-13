"""趋势相位迁移研究 —— 绘图脚本（中文标签；隔离 plot-venv 运行）。

读取 trend_phase_transitions.py 输出的 data/*.csv 与 summary.json，产出四张图：

1. heatmap_transition_counts.png   27×27 迁移次数热力图（log 色标，0 次留白）
2. combo_returns_heatmap.png       27 组合 × 4 期限的平均前瞻收益 + 事件数
3. heatmap_transition_returns.png  27×27 迁移后的 1 月平均收益（N<30 遮蔽）
4. dist_top_transitions.png        频次最高的 6 条迁移的 1 月收益分布

涨跌配色按 A 股习惯：红涨绿跌。

运行：scripts/temp/plot-venv/bin/python research/2026-09-13_phase-migration/plot_phase_transitions.py
（plot-venv 若不存在：python3 -m venv scripts/temp/plot-venv &&
 scripts/temp/plot-venv/bin/pip install matplotlib）
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
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

MIN_N = 30  # 收益统计的最小事件数（低于则遮蔽，不可靠）
HORIZON_LABEL = {"1w": "1周", "2w": "2周", "1m": "1月", "2m": "2月"}
CMAP_RET = "RdYlGn_r"  # 红正绿负（A股习惯）


def load_inputs():
    summary = json.loads((DATA_DIR / "summary.json").read_text(encoding="utf-8"))
    order = summary["combo_order"]  # 27 个组合的展示顺序（顺序=日/周/月）
    trans = pd.read_csv(DATA_DIR / "transition_stats.csv")
    combo = pd.read_csv(DATA_DIR / "combo_stats.csv")
    events = pd.read_csv(DATA_DIR / "events.csv")
    return summary, order, trans, combo, events


def _matrix(trans: pd.DataFrame, order: list[str], value_col: str,
            count_col: str = "events", min_n: int = 0) -> np.ndarray:
    idx = {c: i for i, c in enumerate(order)}
    mat = np.full((27, 27), np.nan)
    for _, r in trans.iterrows():
        i, j = idx[r["from_combo"]], idx[r["to_combo"]]
        if r[count_col] >= min_n:
            mat[i, j] = r[value_col]
    return mat


def plot_transition_counts(trans, order) -> None:
    counts = _matrix(trans, order, "events")
    masked = np.ma.masked_invalid(counts)
    fig, ax = plt.subplots(figsize=(11, 9.5))
    cmap = plt.get_cmap("viridis").copy()
    cmap.set_bad(color="white")
    vmax = np.nanmax(counts)
    im = ax.imshow(masked, norm=mcolors.LogNorm(vmin=1, vmax=vmax), cmap=cmap)
    ax.set_xticks(range(27), order, rotation=90, fontsize=7)
    ax.set_yticks(range(27), order, fontsize=7)
    ax.set_xlabel("迁移后组合（to）")
    ax.set_ylabel("迁移前组合（from）")
    ax.set_title("趋势相位迁移次数热力图（组合顺序=日/周/月；空白=从未出现；log 色标）")
    total = int(np.nansum(counts))
    fig.colorbar(im, ax=ax, shrink=0.8, label=f"次数（总计 {total:,}）")
    fig.tight_layout()
    out = BASE_DIR / "heatmap_transition_counts.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print(f"saved {out}")


def plot_combo_returns(combo, order) -> None:
    combo = combo.set_index("combo").reindex(order)
    horizons = ["1w", "2w", "1m", "2m"]
    means = np.full((27, 4), np.nan)
    for j, h in enumerate(horizons):
        m = combo[f"{h}_mean"].to_numpy(dtype=float)
        n = combo[f"{h}_n"].to_numpy(dtype=float)
        means[:, j] = np.where(n >= MIN_N, m, np.nan)

    fig, (ax, ax_n) = plt.subplots(
        1, 2, figsize=(10.5, 9), gridspec_kw={"width_ratios": [1, 1.6]})
    vmax = np.nanmax(np.abs(means)) * 100
    im = ax.imshow(means * 100, cmap=CMAP_RET, vmin=-vmax, vmax=vmax, aspect="auto")
    ax.set_xticks(range(4), [HORIZON_LABEL[h] for h in horizons])
    ax.set_yticks(range(27), order, fontsize=7)
    ax.set_title("各组合首次出现后的平均前瞻收益（%）", fontsize=11)
    for i in range(27):
        for j in range(4):
            if np.isfinite(means[i, j]):
                ax.text(j, i, f"{means[i, j] * 100:.1f}", ha="center", va="center",
                        fontsize=6.5)
    fig.colorbar(im, ax=ax, shrink=0.6, label=f"平均收益 %（事件数<{MIN_N} 的组合遮蔽）")

    events = combo["events"].to_numpy(dtype=float)
    ax_n.barh(range(27), events, color="#4C72B0", alpha=0.8)
    ax_n.set_yticks(range(27), order, fontsize=7)
    ax_n.invert_yaxis()
    ax_n.set_xscale("log")
    ax_n.set_xlabel("首次出现事件数（log）")
    ax_n.set_title("各组合的事件数", fontsize=11)
    for i, v in enumerate(events):
        ax_n.text(v, i, f" {int(v)}", va="center", fontsize=6.5)
    ax_n.grid(alpha=0.25, axis="x")
    fig.suptitle("趋势相位组合（顺序=日/周/月）：首次出现后收益 vs 出现频次", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    out = BASE_DIR / "combo_returns_heatmap.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print(f"saved {out}")


def plot_transition_returns(trans, order) -> None:
    means = _matrix(trans, order, "1m_mean", min_n=MIN_N)
    masked = np.ma.masked_invalid(means)
    fig, ax = plt.subplots(figsize=(11, 9.5))
    cmap = plt.get_cmap(CMAP_RET).copy()
    cmap.set_bad(color="#f0f0f0")
    vmax = np.nanmax(np.abs(means)) * 100
    im = ax.imshow(masked * 100, cmap=cmap, vmin=-vmax, vmax=vmax)
    ax.set_xticks(range(27), order, rotation=90, fontsize=7)
    ax.set_yticks(range(27), order, fontsize=7)
    ax.set_xlabel("迁移后组合（to）")
    ax.set_ylabel("迁移前组合（from）")
    ax.set_title(f"迁移后的 1 月（21 个交易日）平均收益（组合顺序=日/周/月；事件数<{MIN_N} 遮蔽）")
    fig.colorbar(im, ax=ax, shrink=0.8, label="平均收益 %（红正绿负）")
    fig.tight_layout()
    out = BASE_DIR / "heatmap_transition_returns.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print(f"saved {out}")


def plot_top_transition_dists(trans, events) -> None:
    top = trans.sort_values("events", ascending=False).head(6)
    fig, axes = plt.subplots(2, 3, figsize=(14, 7.5))
    for axis, (_, row) in zip(axes.flat, top.iterrows()):
        name = f"{row['from_combo']} → {row['to_combo']}"
        r = events[(events["from_combo"] == row["from_combo"])
                   & (events["to_combo"] == row["to_combo"])]["r_1m"].dropna()
        axis.hist(r * 100, bins=80, range=(-40, 40), color="#4C72B0", alpha=0.8)
        axis.axvline(0, color="gray", lw=0.8)
        axis.axvline(float(r.mean()) * 100, color="red", lw=1.2,
                     label=f"均值 {r.mean() * 100:.1f}%")
        axis.axvline(float(r.median()) * 100, color="orange", lw=1.2, ls="--",
                     label=f"中位数 {r.median() * 100:.1f}%")
        axis.set_title(f"{name}（N={len(r):,}，胜率 {(r > 0).mean() * 100:.0f}%）",
                       fontsize=10)
        axis.set_xlabel("1 月（21 个交易日）收益 %")
        axis.legend(fontsize=8)
        axis.grid(alpha=0.25)
    fig.suptitle("频次最高的 6 条迁移：迁移后 1 月收益分布（顺序=日/周/月）", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    out = BASE_DIR / "dist_top_transitions.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print(f"saved {out}")


def main() -> None:
    summary, order, trans, combo, events = load_inputs()
    plot_transition_counts(trans, order)
    plot_combo_returns(combo, order)
    plot_transition_returns(trans, order)
    plot_top_transition_dists(trans, events)


if __name__ == "__main__":
    main()
