"""按「进入方向」拆分 27 组合的收益（边界生效口径；不重算，读本目录 events.csv）。

方向定义：状态值 正=+1 / 无=0 / 负=-1，delta = sum(to) - sum(from)：
- delta > 0 → 升级（如 负负负→负负无、无无无→正正无：组合整体转向看多）；
- delta < 0 → 降级（如 无负无→负负无、正正正→正正无：组合整体转向看空）；
- delta = 0 → 换挡（一级升一级降，如 正无无→无正无）。

动机：同一「首次出现」的组合，从升级/降级方向进入的行情含义相反
（实测 负负无：升级进入 1月 +8.81%/72% vs 降级进入 +2.23%/53%），
组合层面的混合数字必须配合本拆分阅读。

输出：
- data/combo_dir_stats.csv  to_combo × direction × 4 horizons 全套统计
- combo_dir_split.png       27 组合 × [升级/降级 × 1月/2月] 收益热力图 + 方向频次

运行：scripts/temp/plot-venv/bin/python research/2026-09-13_趋势相位迁移/dir_split.py
（仅需 matplotlib + pandas；字体缺失会自动下载）
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

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

MIN_N = 200  # 方向拆分后单元格的最小事件数
HORIZONS = ("1w", "2w", "1m", "2m")
STATE_VALUE = {"正": 1, "无": 0, "负": -1}
STATES = (-1, 0, 1)
STATE_LABEL = {-1: "负", 0: "无", 1: "正"}
COMBO_ORDER = sorted(
    ((d, w, m) for d in STATES for w in STATES for m in STATES),
    key=lambda c: (-(c[0] + c[1] + c[2]), -c[2], -c[1], -c[0]),
)
ORDER = ["".join(STATE_LABEL[s] for s in c) for c in COMBO_ORDER]


def add_direction(events: pd.DataFrame) -> pd.DataFrame:
    events = events.copy()
    events["delta"] = (
        events["to_combo"].map(lambda s: sum(STATE_VALUE[c] for c in s))
        - events["from_combo"].map(lambda s: sum(STATE_VALUE[c] for c in s))
    )
    events["direction"] = np.where(
        events["delta"] > 0, "升级", np.where(events["delta"] < 0, "降级", "换挡"))
    return events


def build_stats(events: pd.DataFrame) -> pd.DataFrame:
    out = {}
    keys = ["to_combo", "direction"]
    for name in HORIZONS:
        col = f"r_{name}"
        g = events.groupby(keys, observed=True)[col]
        out[f"{name}_n"] = g.count()
        out[f"{name}_mean"] = g.mean()
        out[f"{name}_median"] = g.median()
        out[f"{name}_std"] = g.std()
        out[f"{name}_win"] = g.apply(
            lambda s: float(np.mean(s.dropna() > 0)) if s.notna().any() else np.nan)
        out[f"{name}_p25"] = g.quantile(0.25)
        out[f"{name}_p75"] = g.quantile(0.75)
    stats = pd.concat(out, axis=1).reset_index()
    counts = events.groupby(keys, observed=True).size().rename("events")
    return stats.merge(counts.reset_index(), on=keys, how="left")


def plot(events: pd.DataFrame) -> None:
    combos = ORDER
    cells = [("升级", "1m"), ("降级", "1m"), ("升级", "2m"), ("降级", "2m")]
    means = np.full((27, len(cells)), np.nan)
    wins = np.full((27, len(cells)), np.nan)
    for j, (direction, h) in enumerate(cells):
        sub = events[events["direction"] == direction]
        g = sub.groupby("to_combo")[f"r_{h}"]
        mean, win, n = g.mean(), g.apply(lambda s: (s.dropna() > 0).mean()), g.count()
        for i, c in enumerate(combos):
            if c in n.index and n[c] >= MIN_N:
                means[i, j] = mean[c] * 100
                wins[i, j] = win[c] * 100

    fig, (ax, ax_n) = plt.subplots(
        1, 2, figsize=(11.5, 9), gridspec_kw={"width_ratios": [1.15, 1]})
    vmax = np.nanmax(np.abs(means))
    im = ax.imshow(np.ma.masked_invalid(means), cmap="RdYlGn_r",
                   vmin=-vmax, vmax=vmax, aspect="auto")
    ax.set_xticks(range(len(cells)), [f"{d}\n{h}" for d, h in cells], fontsize=9)
    ax.set_yticks(range(27), combos, fontsize=7)
    ax.set_title(f"首次出现后平均收益 %：按进入方向拆分（方向内事件数<{MIN_N} 遮蔽）", fontsize=11)
    for i in range(27):
        for j in range(len(cells)):
            if np.isfinite(means[i, j]):
                ax.text(j, i, f"{means[i, j]:.1f}\n({wins[i, j]:.0f}%)",
                        ha="center", va="center", fontsize=6.5)
    fig.colorbar(im, ax=ax, shrink=0.6, label="平均收益 %（括号内为胜率）")

    # 右侧：各组合三个方向的事件数（log）
    piv = (events.groupby(["to_combo", "direction"], observed=True).size()
           .unstack(fill_value=0).reindex(combos).fillna(0))
    left = np.zeros(27)
    for direction, color in (("升级", "#C44E52"), ("降级", "#55A868"), ("换挡", "#888888")):
        vals = piv.get(direction, pd.Series(0, index=combos)).to_numpy(dtype=float)
        ax_n.barh(range(27), vals, left=left, color=color, alpha=0.85, label=direction)
        left += vals
    ax_n.set_yticks(range(27), combos, fontsize=7)
    ax_n.invert_yaxis()
    ax_n.set_xscale("log")
    ax_n.set_xlabel("事件数（log；堆叠=升级/降级/换挡）")
    ax_n.legend(fontsize=9, loc="lower right")
    ax_n.grid(alpha=0.25, axis="x")
    fig.suptitle("趋势相位组合：按进入方向（升级/降级/换挡）拆分 · 边界生效口径", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    out = BASE_DIR / "combo_dir_split.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print(f"saved {out}")


def main() -> None:
    events = pd.read_csv(DATA_DIR / "events.csv")
    events = add_direction(events)
    stats = build_stats(events)
    stats.to_csv(DATA_DIR / "combo_dir_stats.csv", index=False)
    print(f"saved {DATA_DIR / 'combo_dir_stats.csv'}（{len(stats)} 行）")
    plot(events)
    # 控制台摘要：拆分差异最大的组合
    piv = stats[stats["direction"].isin(["升级", "降级"])]
    wide = piv.pivot_table(index="to_combo", columns="direction",
                           values=["1m_mean", "1m_win", "events"])
    wide.columns = [f"{a}_{b}" for a, b in wide.columns]
    both = wide.dropna(subset=["1m_mean_升级", "1m_mean_降级"])
    both = both[(both["events_升级"] >= MIN_N) & (both["events_降级"] >= MIN_N)]
    both["diff"] = (both["1m_mean_升级"] - both["1m_mean_降级"]) * 100
    print("\n升级-降级 1月均值差 TOP5（pp）：")
    for c, r in both.reindex(both["diff"].abs().sort_values(ascending=False).index).head(5).iterrows():
        print(f"  {c}: 升级 {r['1m_mean_升级'] * 100:+.2f}%/{r['1m_win_升级'] * 100:.0f}%"
              f"（N={int(r['events_升级'])}） vs 降级 {r['1m_mean_降级'] * 100:+.2f}%"
              f"/{r['1m_win_降级'] * 100:.0f}%（N={int(r['events_降级'])}）")


if __name__ == "__main__":
    main()
