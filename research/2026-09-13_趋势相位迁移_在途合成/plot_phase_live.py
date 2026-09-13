"""趋势相位迁移（在途合成版）—— 绘图脚本（中文标签；隔离 plot-venv 运行）。

读取本目录 data/*.csv 与旧版研究（../2026-09-13_趋势相位迁移）的对应文件，
产出五张图：

1. heatmap_transition_counts.png  27×27 迁移次数热力图（新口径）
2. combo_returns_heatmap.png      27 组合 × 4 期限收益 + 事件数（新口径）
3. dist_top_transitions.png       新口径频次最高的 6 条迁移的 1 月收益分布
4. timing_shift.png               月/周分量变化的发生日分布：旧口径 vs 新口径
5. compare_old_new.png            新旧口径对比：27 组合 1月收益散点 + 关键迁移对比

运行：scripts/temp/plot-venv/bin/python research/2026-09-13_趋势相位迁移_在途合成/plot_phase_live.py
（plot-venv 若不存在：python3 -m venv scripts/temp/plot-venv &&
 scripts/temp/plot-venv/bin/pip install matplotlib pandas）
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
OLD_DIR = BASE_DIR.parent / "2026-09-13_趋势相位迁移" / "data"
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

MIN_N = 30
HORIZON_LABEL = {"1w": "1周", "2w": "2周", "1m": "1月", "2m": "2月"}
CMAP_RET = "RdYlGn_r"
OLD_LABEL = "旧口径（周期收盘后生效）"
NEW_LABEL = "新口径（在途 bar 逐日合成）"

STATES = (-1, 0, 1)
STATE_LABEL = {-1: "负", 0: "无", 1: "正"}
COMBO_ORDER = sorted(
    ((d, w, m) for d in STATES for w in STATES for m in STATES),
    key=lambda c: (-(c[0] + c[1] + c[2]), -c[2], -c[1], -c[0]),
)
ORDER = ["".join(STATE_LABEL[s] for s in c) for c in COMBO_ORDER]


def _matrix(trans: pd.DataFrame, value_col: str, min_n: int = 0) -> np.ndarray:
    idx = {c: i for i, c in enumerate(ORDER)}
    mat = np.full((27, 27), np.nan)
    for _, r in trans.iterrows():
        i, j = idx[r["from_combo"]], idx[r["to_combo"]]
        if r["events"] >= min_n:
            mat[i, j] = r[value_col]
    return mat


def plot_counts(trans) -> None:
    counts = _matrix(trans, "events")
    fig, ax = plt.subplots(figsize=(11, 9.5))
    cmap = plt.get_cmap("viridis").copy()
    cmap.set_bad(color="white")
    im = ax.imshow(np.ma.masked_invalid(counts),
                   norm=mcolors.LogNorm(vmin=1, vmax=np.nanmax(counts)), cmap=cmap)
    ax.set_xticks(range(27), ORDER, rotation=90, fontsize=7)
    ax.set_yticks(range(27), ORDER, fontsize=7)
    ax.set_xlabel("迁移后组合（to）")
    ax.set_ylabel("迁移前组合（from）")
    ax.set_title("迁移次数热力图 · 在途合成口径（组合顺序=日/周/月；空白=从未出现；log 色标）")
    fig.colorbar(im, ax=ax, shrink=0.8, label=f"次数（总计 {int(np.nansum(counts)):,}）")
    fig.tight_layout()
    out = BASE_DIR / "heatmap_transition_counts.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print(f"saved {out}")


def plot_combo_returns(combo) -> None:
    combo = combo.set_index("combo").reindex(ORDER)
    horizons = ["1w", "2w", "1m", "2m"]
    means = np.full((27, 4), np.nan)
    for j, h in enumerate(horizons):
        m = combo[f"{h}_mean"].to_numpy(dtype=float)
        n_ = combo[f"{h}_n"].to_numpy(dtype=float)
        means[:, j] = np.where(n_ >= MIN_N, m, np.nan)

    fig, (ax, ax_n) = plt.subplots(
        1, 2, figsize=(10.5, 9), gridspec_kw={"width_ratios": [1, 1.6]})
    vmax = np.nanmax(np.abs(means)) * 100
    im = ax.imshow(means * 100, cmap=CMAP_RET, vmin=-vmax, vmax=vmax, aspect="auto")
    ax.set_xticks(range(4), [HORIZON_LABEL[h] for h in horizons])
    ax.set_yticks(range(27), ORDER, fontsize=7)
    ax.set_title("各组合首次出现后的平均前瞻收益（%）", fontsize=11)
    for i in range(27):
        for j in range(4):
            if np.isfinite(means[i, j]):
                ax.text(j, i, f"{means[i, j] * 100:.1f}", ha="center", va="center", fontsize=6.5)
    fig.colorbar(im, ax=ax, shrink=0.6, label=f"平均收益 %（事件数<{MIN_N} 遮蔽）")

    events = combo["events"].to_numpy(dtype=float)
    ax_n.barh(range(27), events, color="#4C72B0", alpha=0.8)
    ax_n.set_yticks(range(27), ORDER, fontsize=7)
    ax_n.invert_yaxis()
    ax_n.set_xscale("log")
    ax_n.set_xlabel("首次出现事件数（log）")
    ax_n.set_title("各组合的事件数", fontsize=11)
    for i, v in enumerate(events):
        ax_n.text(v, i, f" {int(v)}", va="center", fontsize=6.5)
    ax_n.grid(alpha=0.25, axis="x")
    fig.suptitle("趋势相位组合 · 在途合成口径（顺序=日/周/月）", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    out = BASE_DIR / "combo_returns_heatmap.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print(f"saved {out}")


def plot_top_dists(trans, events) -> None:
    top = trans.sort_values("events", ascending=False).head(6)
    fig, axes = plt.subplots(2, 3, figsize=(14, 7.5))
    for axis, (_, row) in zip(axes.flat, top.iterrows()):
        name = f"{row['from_combo']} → {row['to_combo']}"
        r = events[(events["from_combo"] == row["from_combo"])
                   & (events["to_combo"] == row["to_combo"])]["r_1m"].dropna()
        axis.hist(r * 100, bins=80, range=(-40, 40), color="#4C72B0", alpha=0.8)
        axis.axvline(0, color="gray", lw=0.8)
        axis.axvline(float(r.mean()) * 100, color="red", lw=1.2, label=f"均值 {r.mean() * 100:.1f}%")
        axis.axvline(float(r.median()) * 100, color="orange", lw=1.2, ls="--",
                     label=f"中位数 {r.median() * 100:.1f}%")
        axis.set_title(f"{name}（N={len(r):,}，胜率 {(r > 0).mean() * 100:.0f}%）", fontsize=10)
        axis.set_xlabel("1 月（21 个交易日）收益 %")
        axis.legend(fontsize=8)
        axis.grid(alpha=0.25)
    fig.suptitle("频次最高的 6 条迁移：1 月收益分布 · 在途合成口径", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    out = BASE_DIR / "dist_top_transitions.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print(f"saved {out}")


def plot_timing_shift(old_events, new_events) -> None:
    fig, (axm, axw) = plt.subplots(1, 2, figsize=(13, 4.8))
    bins = np.arange(1, 32.5, 1)
    for axis, pos, title in ((axm, 2, "月分量变化的发生日（日号）"), (axw, 1, "周分量变化的发生日（星期几）")):
        for ev, label, color in ((old_events, OLD_LABEL, "#888888"), (new_events, NEW_LABEL, "#C44E52")):
            ch = ev[ev["from_combo"].str[pos] != ev["to_combo"].str[pos]]
            if pos == 2:
                x = pd.to_datetime(ch["date"]).dt.day
                axis.hist(x, bins=bins, density=True, histtype="step", lw=1.6,
                          label=f"{label}（{len(ch):,} 次）", color=color)
                axis.set_xlabel("事件发生日（月内日号）")
            else:
                x = pd.to_datetime(ch["date"]).dt.dayofweek
                axis.hist(x, bins=np.arange(-0.5, 5.5, 1), density=True, histtype="step",
                          lw=1.6, label=f"{label}（{len(ch):,} 次）", color=color)
                axis.set_xticks(range(5), ["周一", "周二", "周三", "周四", "周五"])
        axis.set_title(title, fontsize=11)
        axis.set_ylabel("密度")
        axis.legend(fontsize=9)
        axis.grid(alpha=0.25)
    fig.suptitle("周期状态变化何时可见：旧口径只能落在周期边界，新口径分布到周期内每一天", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    out = BASE_DIR / "timing_shift.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print(f"saved {out}")


KEY_TRANSITIONS = [
    ("负负无", "负负负"), ("无负无", "负负负"), ("负无正", "负负正"),
    ("无正正", "正正正"), ("正正无", "正正正"), ("正正正", "无正正"),
]


def plot_compare(old_combo, new_combo, old_ts, new_ts) -> None:
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(13.5, 5.6))

    oc = old_combo.set_index("combo")
    nc = new_combo.set_index("combo")
    x = np.array([oc.loc[c, "1m_mean"] * 100 for c in ORDER])
    y = np.array([nc.loc[c, "1m_mean"] * 100 for c in ORDER])
    sizes = np.array([max(float(nc.loc[c, "events"]), 1.0) for c in ORDER])
    sizes = 20 + 180 * np.log10(sizes) / np.log10(sizes.max())
    ax.scatter(x, y, s=sizes, alpha=0.75, color="#4C72B0")
    lim = [min(x.min(), y.min()) - 0.5, max(x.max(), y.max()) + 0.5]
    ax.plot(lim, lim, "k--", lw=1, label="新旧相等线")
    for c in ("正正正", "负负负", "无负负", "负负无", "无无无", "正负负", "负正无", "正负正"):
        ax.annotate(c, (oc.loc[c, "1m_mean"] * 100, nc.loc[c, "1m_mean"] * 100),
                    textcoords="offset points", xytext=(5, 4), fontsize=8)
    ax.set_xlabel(f"1月平均收益 % · {OLD_LABEL}")
    ax.set_ylabel(f"1月平均收益 % · {NEW_LABEL}")
    ax.set_title("27 组合：新旧口径的 1月平均收益（点大小=新口径事件数）", fontsize=11)
    ax.legend(fontsize=9)
    ax.grid(alpha=0.25)

    labels, old_v, new_v = [], [], []
    for f, t in KEY_TRANSITIONS:
        ro = old_ts[(old_ts.from_combo == f) & (old_ts.to_combo == t)]
        rn = new_ts[(new_ts.from_combo == f) & (new_ts.to_combo == t)]
        o_v = float(ro["1m_mean"].iloc[0]) * 100 if len(ro) else np.nan
        n_v = float(rn["1m_mean"].iloc[0]) * 100 if len(rn) else np.nan
        labels.append(f"{f}→{t}\nN 新/旧={int(rn['events'].iloc[0]) if len(rn) else 0}"
                      f"/{int(ro['events'].iloc[0]) if len(ro) else 0}")
        old_v.append(o_v)
        new_v.append(n_v)
    xs = np.arange(len(labels))
    ax2.bar(xs - 0.2, old_v, width=0.4, color="#888888", label=OLD_LABEL)
    ax2.bar(xs + 0.2, new_v, width=0.4, color="#C44E52", label=NEW_LABEL)
    for x_, o_v, n_v in zip(xs, old_v, new_v):
        if np.isfinite(o_v):
            ax2.text(x_ - 0.2, o_v + 0.15, f"{o_v:.1f}", ha="center", fontsize=8)
        if np.isfinite(n_v):
            ax2.text(x_ + 0.2, n_v + 0.15, f"{n_v:.1f}", ha="center", fontsize=8)
    ax2.axhline(0, color="gray", lw=0.8)
    ax2.set_xticks(xs, labels, fontsize=7.5, rotation=0)
    ax2.set_ylabel("1月平均收益 %")
    ax2.set_title("关键迁移：新旧口径的 1月平均收益对比", fontsize=11)
    ax2.legend(fontsize=9)
    ax2.grid(alpha=0.25, axis="y")
    fig.tight_layout()
    out = BASE_DIR / "compare_old_new.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print(f"saved {out}")


def main() -> None:
    trans = pd.read_csv(DATA_DIR / "transition_stats.csv")
    combo = pd.read_csv(DATA_DIR / "combo_stats.csv")
    events = pd.read_csv(DATA_DIR / "events.csv")
    old_events = pd.read_csv(OLD_DIR / "events.csv")
    old_combo = pd.read_csv(OLD_DIR / "combo_stats.csv")
    old_ts = pd.read_csv(OLD_DIR / "transition_stats.csv")

    plot_counts(trans)
    plot_combo_returns(combo)
    plot_top_dists(trans, events)
    plot_timing_shift(old_events, events)
    plot_compare(old_combo, combo, old_ts, trans)


if __name__ == "__main__":
    main()
