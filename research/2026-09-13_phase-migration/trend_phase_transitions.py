"""趋势相位迁移研究 —— 事件计算脚本（生产 venv 运行，只读生产库）。

口径（与本研究 REPORT.md 一致）：

- 每级趋势状态：trend_score > 5 → 正(+1)，< -5 → 负(-1)，其余 → 无(0)；
  预热期 NaN → 当日组合无效（剔除）。
- 生效时机：日K 当日收盘生效；周K/月K 在其周期 bar 收盘（标注日）后的
  第一个交易日生效（即「8 月月趋势转正」事件落在 9 月第一个交易日）。
  实现上：日 t 的周/月状态 = 最近一个标注日严格早于 t 的周/月 bar 状态。
- 事件：组合 (日,周,月) 在相邻两个有效交易日间发生变化 → 记一次事件，
  from=前一组合，to=新组合，事件日=t，前瞻收益 = close(t) → close(t+h)，
  h ∈ {5, 10, 21, 42} 个交易日（≈ 1周/2周/1月/2月）；末尾不足窗口记 NaN，
  各 horizon 统计时分别剔除。

输出（data/ 下）：
- events.csv          全部事件明细（gitignore，可再生）
- combo_stats.csv     按 to 组合聚合：事件数 + 各 horizon 收益统计 + 期间占比
- transition_stats.csv 按 (from,to) 聚合：迁移频次 + 各 horizon 收益统计
- summary.json        元信息（标的数、事件总数、参数口径）

运行：.venv/bin/python research/2026-09-13_phase-migration/trend_phase_transitions.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import _common  # noqa: E402  # .env + src/scripts 路径 + DB_PATH

from core.strategy_config import get_strategy_config  # noqa: E402
from core.trend import calculate_trend_score_series  # noqa: E402
from data.storage.db import get_db, init_db  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "data"
CHUNK = 100
HORIZONS = (("1w", 5), ("2w", 10), ("1m", 21), ("2m", 42))
THRESH = 5.0

STATES = (-1, 0, 1)  # 负 / 无 / 正
STATE_LABEL = {-1: "负", 0: "无", 1: "正"}
# 展示顺序：按看多程度（状态和）降序，同级按 月>周>日 字典序（长期权重更高）
COMBO_ORDER = sorted(
    ((d, w, m) for d in STATES for w in STATES for m in STATES),
    key=lambda c: (-(c[0] + c[1] + c[2]), -c[2], -c[1], -c[0]),
)
COMBO_LABEL = {c: "".join(STATE_LABEL[s] for s in c) for c in COMBO_ORDER}  # 顺序=日/周/月
COMBO_INDEX = {c: i for i, c in enumerate(COMBO_ORDER)}


def _states(df: pd.DataFrame, cfg: dict) -> np.ndarray:
    """OHLCV DataFrame → 每根 bar 的状态序列（+1/0/-1，预热期 NaN）。"""
    if df is None or df.empty:
        return np.empty(0)
    scores = calculate_trend_score_series(df, cfg)["trend_score"].to_numpy(dtype=float)
    out = np.full(scores.shape, np.nan)
    out[scores > THRESH] = 1.0
    out[scores < -THRESH] = -1.0
    out[np.isfinite(scores) & (np.abs(scores) <= THRESH)] = 0.0
    return out


def _dates(df: pd.DataFrame) -> np.ndarray:
    return df["time"].dt.strftime("%Y-%m-%d").to_numpy()


def symbol_events(daily: pd.DataFrame, weekly: pd.DataFrame, monthly: pd.DataFrame,
                  cfg: dict, symbol: str) -> tuple[list[tuple], np.ndarray]:
    """单标的的事件列表 + 每日组合索引序列（用于期间占比统计）。"""
    dates = _dates(daily)
    closes = pd.to_numeric(daily["close"], errors="coerce").to_numpy(dtype=float)
    n = len(dates)
    combos = np.full(n, -1, dtype=np.int8)  # -1 = 无效
    if n == 0:
        return [], combos

    d_state = _states(daily, cfg)
    levels = [d_state]
    for period_df in (weekly, monthly):
        if period_df is None or period_df.empty:
            levels.append(np.full(n, np.nan))
            continue
        p_dates = _dates(period_df)
        p_state = _states(period_df, cfg)
        # 最近一个标注日严格早于当日 t 的周期 bar（周期收盘后下一交易日起生效）
        idx = np.searchsorted(p_dates, dates, side="left") - 1
        mapped = np.full(n, np.nan)
        valid = idx >= 0
        mapped[valid] = p_state[idx[valid]]
        levels.append(mapped)

    d_arr, w_arr, m_arr = levels
    ok = np.isfinite(d_arr) & np.isfinite(w_arr) & np.isfinite(m_arr)
    combos[ok] = (
        (d_arr[ok].astype(np.int8) + 1) * 9
        + (w_arr[ok].astype(np.int8) + 1) * 3
        + (m_arr[ok].astype(np.int8) + 1)
    )  # 0..26，(d,w,m) 字典序编码（仅内部使用）

    events: list[tuple] = []
    prev = combos[:-1]
    curr = combos[1:]
    fire = (prev >= 0) & (curr >= 0) & (prev != curr)
    for i in np.nonzero(fire)[0] + 1:
        row = [symbol, dates[i], int(combos[i - 1]), int(combos[i])]
        for _, h in HORIZONS:
            j = i + h
            row.append(float(closes[j] / closes[i] - 1.0) if j < n else np.nan)
        events.append(tuple(row))
    return events, combos


def _agg(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """按 keys 聚合各 horizon 的收益统计。"""
    out = {}
    for name, _ in HORIZONS:
        col = f"r_{name}"
        g = df.groupby(keys, observed=True)[col]
        out[f"{name}_n"] = g.count()
        out[f"{name}_mean"] = g.mean()
        out[f"{name}_median"] = g.median()
        out[f"{name}_std"] = g.std()
        out[f"{name}_win"] = g.apply(lambda s: float(np.mean(s.dropna() > 0)) if s.notna().any() else np.nan)
        out[f"{name}_p25"] = g.quantile(0.25)
        out[f"{name}_p75"] = g.quantile(0.75)
    agg = pd.concat(out, axis=1).reset_index()
    return agg


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
    combo_days = np.zeros(27, dtype=np.int64)  # (d,w,m) 字典序编码的期间占比
    symbols_with_events = 0
    for i in range(0, len(symbols), CHUNK):
        chunk = symbols[i : i + CHUNK]
        daily_map = db.load_market_data_many(chunk, price_mode="qfq", period="1d")
        weekly_map = db.load_market_data_many(chunk, price_mode="qfq", period="1w")
        monthly_map = db.load_market_data_many(chunk, price_mode="qfq", period="1M")
        for symbol, daily in daily_map.items():
            if daily is None or daily.empty:
                continue
            events, combos = symbol_events(
                daily, weekly_map.get(symbol), monthly_map.get(symbol), cfg, symbol)
            if events:
                symbols_with_events += 1
                all_events.extend(events)
            valid = combos[combos >= 0]
            combo_days += np.bincount(valid, minlength=27)
        print(f"  {min(i + CHUNK, len(symbols))}/{len(symbols)}，累计事件 {len(all_events)}", flush=True)

    cols = ["symbol", "date", "from_idx", "to_idx"] + [f"r_{n}" for n, _ in HORIZONS]
    events_df = pd.DataFrame(all_events, columns=cols)

    # 内部编码 (d+1)*9+(w+1)*3+(m+1) → 可读标签（顺序=日/周/月）
    def _label(idx: int) -> str:
        d, w, m = idx // 9 - 1, (idx % 9) // 3 - 1, idx % 3 - 1
        return STATE_LABEL[d] + STATE_LABEL[w] + STATE_LABEL[m]

    events_df["from_combo"] = events_df["from_idx"].map(_label)
    events_df["to_combo"] = events_df["to_idx"].map(_label)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    events_df.to_csv(OUT_DIR / "events.csv", index=False)

    combo_stats = _agg(events_df, ["to_combo"]).rename(columns={"to_combo": "combo"})
    counts = events_df.groupby("to_combo", observed=True).size().rename("events")
    combo_stats = combo_stats.merge(counts.reset_index().rename(columns={"to_combo": "combo"}),
                                    on="combo", how="left")
    # 期间占比（该组合占全部有效交易日的份额）
    prev_df = pd.DataFrame(
        {"combo": [_label(i) for i in range(27)],
         "day_share": combo_days / max(1, int(combo_days.sum()))}
    )
    combo_stats = combo_stats.merge(prev_df, on="combo", how="outer")
    combo_stats["sort"] = combo_stats["combo"].map(
        {COMBO_LABEL[c]: i for i, c in enumerate(COMBO_ORDER)})
    combo_stats = combo_stats.sort_values("sort").drop(columns="sort")
    combo_stats.to_csv(OUT_DIR / "combo_stats.csv", index=False)

    transition_stats = _agg(events_df, ["from_combo", "to_combo"])
    tcounts = events_df.groupby(["from_combo", "to_combo"], observed=True).size().rename("events")
    transition_stats = transition_stats.merge(tcounts.reset_index(),
                                              on=["from_combo", "to_combo"], how="left")
    transition_stats.to_csv(OUT_DIR / "transition_stats.csv", index=False)

    summary = {
        "symbols": len(symbols),
        "symbols_with_events": symbols_with_events,
        "total_events": int(len(events_df)),
        "distinct_transitions": int(len(transition_stats)),
        "valid_symbol_days": int(combo_days.sum()),
        "threshold": THRESH,
        "horizons": {n: h for n, h in HORIZONS},
        "date_range": [str(events_df["date"].min()), str(events_df["date"].max())],
        "combo_order": [COMBO_LABEL[c] for c in COMBO_ORDER],
    }
    with open(OUT_DIR / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\n事件总数 {len(events_df)}，涉及标的 {symbols_with_events} 只，"
          f"不同迁移路径 {len(transition_stats)} 条")
    print(f"事件日期范围: {summary['date_range'][0]} ~ {summary['date_range'][1]}")
    print("频次最高的 10 条迁移：")
    top = transition_stats.sort_values("events", ascending=False).head(10)
    for _, r in top.iterrows():
        print(f"  {r['from_combo']} → {r['to_combo']}: {int(r['events'])} 次")
    print(f"输出目录: {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
