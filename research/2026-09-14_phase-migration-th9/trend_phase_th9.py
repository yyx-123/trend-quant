"""趋势相位迁移（新阈值版 v4）—— 事件计算脚本（生产 venv 运行，只读生产库）。

与 v3（``2026-09-13_phase-migration-rolling``，滚动锚定口径）的唯一差别是
**三态阈值**与周/月趋势值的**数据来源**：

- 日级：±5 不变（>5 正 / <−5 负），仍用 ``calculate_trend_score_series``
  + DB strategy 默认 cfg 现算（与 v3 完全相同）；
- 周/月级：±5 → **±9**（>9 正 / <−9 负 / 其余无）。依据标定研究
  ``2026-09-13_rolling-trend-calibration`` §6：滚动周/月趋势分布 σ≈11.5~12.3，
  ±9 恢复日K 的「约 2/3 时间无趋势」语义（±5 偏窄，噪声事件偏多）；
- 周/月趋势值**不再现算**，直接读库表 ``trend_rolling_daily``
  （symbol, time, w_trend, m_trend，全市场全历史滚动周/月趋势，
  与 ``core.rolling_bars.rolling_period_trend_series`` 现算零偏差——
  本脚本内置自检复核这一点）。

其余口径与 v3 逐项相同：组合 (日,周,月) 相邻有效交易日变化记一次事件；
前瞻收益 = 事件日收盘 → 其后 5/10/21/42 个交易日收盘；27×27 迁移矩阵；
标的池 = instrument_metadata 的 stock+etf。

内置自检：抽 40 只有数据的标的，把 ``db.load_rolling_trend_many`` 读出的
w/m 全序列与 ``rolling_period_trend_series`` 现算逐点比对（容差 1e-9），
并验证三态切分确实按 日±5 / 周月±9 执行，不一致则拒绝落盘。

输出（data/ 下，schema 与 v3 一致）：
- events.csv（gitignore，可再生）/ combo_stats.csv / transition_stats.csv /
  summary.json（含 v4 阈值口径与 w/m 数据源说明）
- trading_calendar.csv（全样本交易日历 + 月内第几个交易日，供日内分布图用）

运行：.venv/bin/python research/2026-09-14_phase-migration-th9/trend_phase_th9.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import _common  # noqa: E402  # .env + src/scripts 路径 + DB_PATH

from core.rolling_bars import (  # noqa: E402
    ROLLING_BAR_DAYS,
    ROLLING_TREND_OVERRIDES,
    rolling_period_trend_series,
    rolling_trend_cfg,
)
from core.strategy_config import get_strategy_config  # noqa: E402
from core.trend import calculate_trend_score_series  # noqa: E402
from data.storage.db import get_db, init_db  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "data"
CHUNK = 100
HORIZONS = (("1w", 5), ("2w", 10), ("1m", 21), ("2m", 42))
THRESH_D = 5.0   # 日级阈值（与 v3 相同）
THRESH_WM = 9.0  # 周/月级阈值（v4 新口径，依据 rolling-trend-calibration §6）
SELFCHECK_SYMBOLS = 40  # 抽 N 只有数据的标的做「库读 vs 现算」全序列精确比对

STATE_LABEL = {-1: "负", 0: "无", 1: "正"}


def _states_from_scores(scores: np.ndarray, thresh: float) -> np.ndarray:
    out = np.full(scores.shape, np.nan)
    out[scores > thresh] = 1.0
    out[scores < -thresh] = -1.0
    out[np.isfinite(scores) & (np.abs(scores) <= thresh)] = 0.0
    return out


def selfcheck_symbol(daily: pd.DataFrame, dtime: pd.DatetimeIndex,
                     rt: pd.DataFrame) -> float:
    """库读 w/m（按日K时间轴对齐）与 rolling_period_trend_series 现算逐点比对，
    返回全序列最大绝对差（无效/有效判定不一致返回 inf）。"""
    worst = 0.0
    for period, col in (("1w", "w_trend"), ("1M", "m_trend")):
        ref = rolling_period_trend_series(
            daily, rolling_trend_cfg(period), period=period
        ).reindex(pd.Index(dtime)).to_numpy(float)
        mine = rt[col].to_numpy(float)
        ref_nan = ~np.isfinite(ref)
        mine_nan = ~np.isfinite(mine)
        if not np.array_equal(ref_nan, mine_nan):
            return float("inf")
        both = ~(ref_nan | mine_nan)
        if np.any(both):
            worst = max(worst, float(np.max(np.abs(ref[both] - mine[both]))))
    return worst


def main() -> int:
    init_db(_common.DB_PATH)
    db = get_db()
    cfg = get_strategy_config()

    instruments = db.list_instrument_metadata()
    symbols: list[str] = []
    seen: set[str] = set()
    for item in instruments:
        asset = str(item.get("asset_type") or "").strip().lower()
        symbol = str(item.get("symbol") or "").strip().upper()
        if asset in ("stock", "etf") and symbol and symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    print(f"标的池: {len(symbols)} 只（stock+etf）")

    all_events: list[tuple] = []
    combo_days = np.zeros(27, dtype=np.int64)
    symbols_with_events = 0
    check_diffs: list[float] = []
    checked = 0
    calendar: set[str] = set()  # 全样本交易日并集（月内第几个交易日分布用）

    for i in range(0, len(symbols), CHUNK):
        chunk = symbols[i : i + CHUNK]
        daily_map = db.load_market_data_many(chunk, price_mode="qfq", period="1d")
        rt_map = db.load_rolling_trend_many(chunk)
        for symbol, daily in daily_map.items():
            if daily is None or daily.empty:
                continue
            n = len(daily)
            dtime = pd.to_datetime(daily["time"])
            dates = dtime.dt.strftime("%Y-%m-%d").to_numpy()
            calendar.update(dates.tolist())
            closes = pd.to_numeric(daily["close"], errors="coerce").to_numpy(float)

            # 日级：完整 bar 收盘即生效（与 v3 一致）
            d_scores = calculate_trend_score_series(daily, cfg)["trend_score"].to_numpy(float)
            # 周/月级：直接读 trend_rolling_daily（与滚动锚定现算零偏差，自检见下）
            rt = rt_map.get(symbol)
            if rt is None or rt.empty:
                continue
            rt = rt.set_index("time").reindex(pd.Index(dtime))
            w_scores = rt["w_trend"].to_numpy(float)
            m_scores = rt["m_trend"].to_numpy(float)

            # --- 精确性自检：库读 w/m 全序列 vs rolling_period_trend_series 现算 ---
            if checked < SELFCHECK_SYMBOLS and n > 260:
                try:
                    check_diffs.append(selfcheck_symbol(daily, dtime, rt))
                    checked += 1
                except Exception as exc:  # 自检样本异常不阻断主流程，但打印出来
                    print(f"  [selfcheck] {symbol} 异常: {exc}")

            d_state = _states_from_scores(d_scores, THRESH_D)
            w_state = _states_from_scores(w_scores, THRESH_WM)
            m_state = _states_from_scores(m_scores, THRESH_WM)

            combos = np.full(n, -1, dtype=np.int8)
            ok = np.isfinite(d_state) & np.isfinite(w_state) & np.isfinite(m_state)
            combos[ok] = (
                (d_state[ok].astype(np.int8) + 1) * 9
                + (w_state[ok].astype(np.int8) + 1) * 3
                + (m_state[ok].astype(np.int8) + 1)
            )
            valid = combos[combos >= 0]
            combo_days += np.bincount(valid, minlength=27)

            prev, curr = combos[:-1], combos[1:]
            fire = (prev >= 0) & (curr >= 0) & (prev != curr)
            for j in np.nonzero(fire)[0] + 1:
                row = [symbol, dates[j], int(combos[j - 1]), int(combos[j])]
                for _, h in HORIZONS:
                    k = j + h
                    row.append(float(closes[k] / closes[j] - 1.0) if k < n else np.nan)
                all_events.append(tuple(row))
            if np.any(fire):
                symbols_with_events += 1
        print(f"  {min(i + CHUNK, len(symbols))}/{len(symbols)}，累计事件 {len(all_events)}", flush=True)

    # --- 自检结论 ---
    diffs = np.asarray([d for d in check_diffs if np.isfinite(d)], dtype=float)
    hard_fail = any(not np.isfinite(d) for d in check_diffs)
    print(f"\n[selfcheck] 库读 w/m vs 现算：{checked} 只标的全序列比对，max|Δ| = "
          f"{diffs.max() if diffs.size else float('nan'):.3e}，"
          f"有效/无效不一致: {'有' if hard_fail else '无'}")
    if hard_fail or (diffs.size and diffs.max() > 1e-9):
        print("[selfcheck] trend_rolling_daily 与 rolling 现算不一致，终止（不落盘）")
        return 1
    if checked < 20:
        print(f"[selfcheck] 有效抽样标的不足 20 只（{checked}），终止（不落盘）")
        return 1
    # 阈值口径自检：三态切分必须严格按 日±5 / 周月±9
    probe = np.array([-10.0, -9.0, -8.999, -5.0, -4.999, 0.0, 4.999, 5.0, 8.999, 9.0, 10.0])
    expect_d = np.array([-1.0, -1.0, -1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0])
    expect_wm = np.array([-1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0])
    if not (np.array_equal(_states_from_scores(probe, THRESH_D), expect_d)
            and np.array_equal(_states_from_scores(probe, THRESH_WM), expect_wm)):
        print("[selfcheck] 三态阈值切分不符合 日±5/周月±9，终止（不落盘）")
        return 1
    print("[selfcheck] 三态切分口径确认：日 ±5 / 周月 ±9（边界 = 含于「无」）")

    cols = ["symbol", "date", "from_idx", "to_idx"] + [f"r_{n}" for n, _ in HORIZONS]
    events_df = pd.DataFrame(all_events, columns=cols)

    def _label(idx: int) -> str:
        d, w, m = idx // 9 - 1, (idx % 9) // 3 - 1, idx % 3 - 1
        return STATE_LABEL[d] + STATE_LABEL[w] + STATE_LABEL[m]

    events_df["from_combo"] = events_df["from_idx"].map(_label)
    events_df["to_combo"] = events_df["to_idx"].map(_label)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    events_df.to_csv(OUT_DIR / "events.csv", index=False)

    # 全样本交易日历：月内第几个交易日（tdom），供月分量变化的日内分布图用
    cal = pd.DataFrame({"date": sorted(calendar)})
    cal["month"] = cal["date"].str[:7]
    cal["tdom"] = cal.groupby("month").cumcount() + 1
    cal.to_csv(OUT_DIR / "trading_calendar.csv", index=False)

    def _agg(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
        out = {}
        for name, _ in HORIZONS:
            col = f"r_{name}"
            g = df.groupby(keys, observed=True)[col]
            out[f"{name}_n"] = g.count()
            out[f"{name}_mean"] = g.mean()
            out[f"{name}_median"] = g.median()
            out[f"{name}_std"] = g.std()
            out[f"{name}_win"] = g.apply(
                lambda s: float(np.mean(s.dropna() > 0)) if s.notna().any() else np.nan)
            out[f"{name}_p25"] = g.quantile(0.25)
            out[f"{name}_p75"] = g.quantile(0.75)
        return pd.concat(out, axis=1).reset_index()

    combo_stats = _agg(events_df, ["to_combo"]).rename(columns={"to_combo": "combo"})
    counts = events_df.groupby("to_combo", observed=True).size().rename("events")
    combo_stats = combo_stats.merge(
        counts.reset_index().rename(columns={"to_combo": "combo"}), on="combo", how="left")
    prev_df = pd.DataFrame({
        "combo": [_label(k) for k in range(27)],
        "day_share": combo_days / max(1, int(combo_days.sum())),
    })
    combo_stats = combo_stats.merge(prev_df, on="combo", how="outer")
    combo_stats.to_csv(OUT_DIR / "combo_stats.csv", index=False)

    transition_stats = _agg(events_df, ["from_combo", "to_combo"])
    tcounts = events_df.groupby(["from_combo", "to_combo"], observed=True).size().rename("events")
    transition_stats = transition_stats.merge(
        tcounts.reset_index(), on=["from_combo", "to_combo"], how="left")
    transition_stats.to_csv(OUT_DIR / "transition_stats.csv", index=False)

    summary = {
        "model": "滚动锚定周/月K（同 v3：第 k 根 bar=截至当日倒数第 k 个 D 交易日窗口；"
                 "周 D=5 atr8/vol_ma8/er4，月 D=22 atr6/vol_ma6/er3，MA/EMA 3/5/8 与 "
                 "tanh/权重不动；日级与 v3 相同）。v4 差别：周/月趋势值直接读库表 "
                 "trend_rolling_daily（与 rolling_period_trend_series 现算零偏差，"
                 "本脚本自检复核），不再现算",
        "version": "v4",
        "previous_versions": {
            "v1": "research/2026-09-13_phase-migration（边界生效口径）",
            "v2": "research/2026-09-13_phase-migration-live-synth（在途合成口径）",
            "v3": "research/2026-09-13_phase-migration-rolling（滚动锚定口径，阈值同 ±5）",
        },
        "thresholds": {
            "daily": THRESH_D,
            "weekly_monthly": THRESH_WM,
            "rule": "日级 >5 正 / <−5 负 / 其余无；周/月级 >9 正 / <−9 负 / 其余无（边界 = 含于无）",
            "rationale": "research/2026-09-13_rolling-trend-calibration §6：滚动周/月趋势分布 "
                         "σ≈11.5~12.3，±9 恢复日K「约 2/3 时间无趋势」语义；v3 的 ±5 偏窄，"
                         "周/月分量噪声事件偏多",
        },
        "wm_source": "trend_rolling_daily（symbol, time, w_trend, m_trend；"
                     "db.load_rolling_trend_many 批量读取）",
        "rolling_params": {
            "bar_days": {p: ROLLING_BAR_DAYS[p] for p in ("1w", "1M")},
            "trend_overrides": ROLLING_TREND_OVERRIDES,
            "warmup_trading_days": {"1w": 50, "1M": 220},
        },
        "symbols": len(symbols),
        "symbols_with_events": symbols_with_events,
        "total_events": int(len(events_df)),
        "distinct_transitions": int(len(transition_stats)),
        "valid_symbol_days": int(combo_days.sum()),
        "horizons": {n: h for n, h in HORIZONS},
        "date_range": [str(events_df["date"].min()), str(events_df["date"].max())],
        "selfcheck": {"symbols": int(checked), "samples": len(check_diffs),
                      "max_abs_diff": float(diffs.max()) if diffs.size else None,
                      "threshold_probe": "日±5/周月±9 边界探针通过"},
    }
    with open(OUT_DIR / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\n事件总数 {len(events_df)}，涉及标的 {symbols_with_events} 只，"
          f"不同迁移路径 {len(transition_stats)} 条")
    print(f"事件日期范围: {summary['date_range'][0]} ~ {summary['date_range'][1]}")
    share = combo_days / max(1, int(combo_days.sum()))
    print(f"无无无 期间占比 {share[13] * 100:.1f}%（v3: 24.2%），"
          f"正正正 {share[26] * 100:.1f}%，负负负 {share[0] * 100:.1f}%")
    print("频次最高的 10 条迁移：")
    for _, r in transition_stats.sort_values("events", ascending=False).head(10).iterrows():
        print(f"  {r['from_combo']} → {r['to_combo']}: {int(r['events'])} 次")
    print(f"输出目录: {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
