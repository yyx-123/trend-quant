"""趋势相位迁移（在途合成版）—— 事件计算脚本（生产 venv 运行，只读生产库）。

与 ``2026-09-13_趋势相位迁移`` 的唯一差别在周/月级状态的**生效时机**：

- 旧口径：周/月状态在周期收盘后的第一个交易日才生效（月趋势变化只能落在
  下月 1 日前后）——信息滞后，漏掉了周期中后段的行情；
- 本口径（实时/在途合成）：**每一天**把在途周/月 bar 用日K 累计合成
  （open=周期首日 open，high/low=周期内累计极值，close=当日收盘，
  volume=周期内累计成交量），追加在该标的已收盘周/月 bar（vendor 周期表）
  序列尾部，逐日计算周/月趋势值——与「每天收盘后真实能看到的信息」一致。

增量算法（精确，非近似）：已收盘历史上的 EMA(adjust=False)/ATR(SMA)/MA/
ER/vol_ma 等指标状态随已收盘序列只计算一次；在途 bar 的趋势值由这些状态
O(1) 推出，数学上与 ``core/trend.calculate_trend_score_series`` 逐点一致。
``--selfcheck`` 已内置在主流程：抽样把「已收盘+合成 bar」拼成真实
DataFrame 调用生产函数，与增量值逐点比对（容差 1e-9）。

阈值/事件/收益口径与旧版完全相同：>5 正 / <-5 负；组合 (日,周,月) 相邻有效
交易日变化记一次事件；前瞻收益 = 事件日收盘 → 5/10/21/42 个交易日收盘。

输出（data/ 下，schema 与旧版一致）：
- events.csv（gitignore，可再生）/ combo_stats.csv / transition_stats.csv / summary.json

运行：.venv/bin/python research/2026-09-13_趋势相位迁移_在途合成/trend_phase_live.py
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
SELFCHECK_SYMBOLS = 120  # 前 N 只有数据的标的各抽 周/月 各 1 天做精确性自检

STATE_LABEL = {-1: "负", 0: "无", 1: "正"}


# --------------------------------------------------------------------------
# 在途合成增量算法
# --------------------------------------------------------------------------

class PeriodState:
    """一个周期级别（周或月）的预计算状态：已收盘 vendor 序列 + 各类累计量。"""

    def __init__(self, period_df: pd.DataFrame, cfg: dict):
        self.n_list = (int(cfg.get("n_short", 3)), int(cfg.get("n_mid", 5)),
                       int(cfg.get("n_long", 8)))
        self.atr_period = int(cfg.get("atr_period", 20))
        self.er_period = int(cfg.get("er_period", 10))
        self.vol_ma_period = int(cfg.get("vol_ma_period", 20))
        self.min_bars = max(self.n_list[2], self.atr_period) + 2
        self.w_bias = (float(cfg.get("w_bias_short", 0.4)), float(cfg.get("w_bias_mid", 0.4)),
                       float(cfg.get("w_bias_long", 0.2)))
        self.w_slope = (float(cfg.get("w_slope_short", 0.4)), float(cfg.get("w_slope_mid", 0.4)),
                        float(cfg.get("w_slope_long", 0.2)))
        self.w_bn = float(cfg.get("w_bias_norm", 0.5))
        self.w_sn = float(cfg.get("w_slope_norm", 0.5))
        self.w_vol = float(cfg.get("w_vol", 0.3))
        self.w_er = float(cfg.get("w_er", 0.7))

        if period_df is None or period_df.empty:
            self.labels = np.array([], dtype="datetime64[ns]")
            self.closes = np.empty(0)
            self.ready = False
            return
        self.labels = period_df["time"].to_numpy(dtype="datetime64[ns]")
        self.closes = pd.to_numeric(period_df["close"], errors="coerce").to_numpy(float)
        highs = pd.to_numeric(period_df["high"], errors="coerce").to_numpy(float)
        lows = pd.to_numeric(period_df["low"], errors="coerce").to_numpy(float)
        vols = pd.to_numeric(period_df["volume"], errors="coerce").fillna(0.0).to_numpy(float)

        prev_close = np.roll(self.closes, 1)
        prev_close[0] = np.nan
        tr = np.nanmax(
            np.vstack([highs - lows, np.abs(highs - prev_close), np.abs(lows - prev_close)]),
            axis=0,
        )
        tr[0] = highs[0] - lows[0]  # 首根无昨收，与 pandas max(skipna) 一致
        dabs = np.abs(np.diff(self.closes, prepend=np.nan))
        dabs[0] = 0.0

        self.cum_close = np.concatenate([[0.0], np.cumsum(self.closes)])
        self.cum_tr = np.concatenate([[0.0], np.cumsum(tr)])
        self.cum_vol = np.concatenate([[0.0], np.cumsum(vols)])
        self.cum_dabs = np.concatenate([[0.0], np.cumsum(dabs)])
        self.emas = {
            n: self.closes  # 占位，下面覆盖
            for n in self.n_list
        }
        s = pd.Series(self.closes)
        for n in self.n_list:
            self.emas[n] = s.ewm(span=n, adjust=False).mean().to_numpy()
        self.ready = True

    def completed_count(self, group_starts: np.ndarray) -> np.ndarray:
        """每个日K分组的第一个交易日前，已收盘 bar 数（标注日严格早于分组起点）。"""
        return np.searchsorted(self.labels, group_starts, side="left")


def live_scores(ps: PeriodState, p_arr: np.ndarray, syn: dict[str, np.ndarray]) -> np.ndarray:
    """逐日计算在途周期的 trend_score（无效 → NaN）。全部向量化。

    p_arr: 每日对应的已收盘 bar 数 p（在途 bar 是第 p+1 根，0 基索引 p）。
    syn: 在途 bar 的 open/high/low/close/volume 逐日数组。
    """
    p = p_arr.astype(np.int64)
    c = syn["close"]
    has = p > 0

    def at(arr: np.ndarray, idx: np.ndarray) -> np.ndarray:
        """安全取值 arr[idx]（idx<0 处给 NaN）。"""
        out = np.full(idx.shape, np.nan)
        ok = idx >= 0
        out[ok] = arr[idx[ok]]
        return out

    c_end = np.where(has, at(ps.closes, np.where(has, p - 1, -1)), np.nan)

    with np.errstate(divide="ignore", invalid="ignore"):
        # --- ATR（SMA 平滑，min_periods=1）---
        tr_syn = np.where(
            has,
            np.nanmax(np.vstack([syn["high"] - syn["low"],
                                 np.abs(syn["high"] - c_end),
                                 np.abs(syn["low"] - c_end)]), axis=0),
            syn["high"] - syn["low"],
        )
        tail = np.minimum(p, ps.atr_period - 1)  # 窗口内已收盘根数
        tr_tail = ps.cum_tr[p] - ps.cum_tr[p - tail]
        atr = (tr_tail + tr_syn) / np.minimum(p + 1, ps.atr_period)

        # --- bias_n = (close - MA_n) / ATR（窗口不足 → 0，复刻 fillna(0)）---
        bias_parts = []
        for n in ps.n_list:
            enough = (p + 1) >= n
            ma_n = (ps.cum_close[p] - ps.cum_close[np.maximum(p - (n - 1), 0)] + c) / n
            bias_n = np.where(enough, (c - ma_n) / atr, 0.0)
            bias_parts.append(np.nan_to_num(bias_n, nan=0.0, posinf=0.0, neginf=0.0))

        # --- slope_n = diff(EMA_n) / (ATR·n)（p=0 时 diff 为 NaN → 0）---
        slope_parts = []
        for n in ps.n_list:
            alpha = 2.0 / (n + 1)
            ema_end = at(ps.emas[n], np.where(has, p - 1, -1))
            ema_new = np.where(has, alpha * c + (1 - alpha) * ema_end, c)
            ema_diff = np.where(has, ema_new - ema_end, 0.0)
            slope_n = ema_diff / (atr * n)
            slope_parts.append(np.nan_to_num(slope_n, nan=0.0, posinf=0.0, neginf=0.0))

        bias_mix = ps.w_bias[0] * bias_parts[0] + ps.w_bias[1] * bias_parts[1] + ps.w_bias[2] * bias_parts[2]
        slope_mix = ps.w_slope[0] * slope_parts[0] + ps.w_slope[1] * slope_parts[1] + ps.w_slope[2] * slope_parts[2]
        norm_bias = np.tanh(bias_mix / 2.0) * 100.0
        norm_slope = np.tanh(slope_mix) * 100.0
        price_direction = ps.w_bn * norm_bias + ps.w_sn * norm_slope

        # --- ER(Kaufman, min_periods=1；volatility=0/change 不足 → 0) ---
        change = np.where(p >= ps.er_period,
                          np.abs(c - at(ps.closes, np.where(p >= ps.er_period, p - ps.er_period, -1))),
                          np.nan)
        dtail = np.minimum(p, ps.er_period - 1)
        volatility = ps.cum_dabs[p] - ps.cum_dabs[p - dtail] + np.where(has, np.abs(c - c_end), 0.0)
        er = np.where(volatility > 0, change / np.where(volatility > 0, volatility, 1.0), 0.0)
        er = np.clip(np.nan_to_num(er, nan=0.0), 0.0, 1.0)

        # --- volume factor ---
        vtail = np.minimum(p, ps.vol_ma_period - 1)
        vol_ma = (ps.cum_vol[p] - ps.cum_vol[p - vtail] + syn["volume"]) / np.minimum(p + 1, ps.vol_ma_period)
        vol_ratio = np.where(vol_ma > 0, syn["volume"] / np.where(vol_ma > 0, vol_ma, 1.0), np.nan)
        volume_factor = np.clip(np.nan_to_num(vol_ratio / 3.0, nan=0.0), 0.0, 1.0)

        confidence = (volume_factor ** ps.w_vol) * (er ** ps.w_er)
        score = np.clip(price_direction * confidence, -100.0, 100.0)

    valid = ((p + 1) >= ps.min_bars) & (atr > 0)
    return np.where(valid, score, np.nan)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def _states_from_scores(scores: np.ndarray) -> np.ndarray:
    out = np.full(scores.shape, np.nan)
    out[scores > THRESH] = 1.0
    out[scores < -THRESH] = -1.0
    out[np.isfinite(scores) & (np.abs(scores) <= THRESH)] = 0.0
    return out


def _daily_groups(daily: pd.DataFrame, freq: str) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """按 ISO 周/自然月分组，返回每日的已收盘 bar 参考（分组起点数组）与合成 bar。"""
    key = daily["time"].dt.to_period(freq)
    g = daily.groupby(key, sort=True)
    first_time = g["time"].transform("min").to_numpy(dtype="datetime64[ns]")
    syn = {
        "open": g["open"].transform("first").to_numpy(float),
        "high": g["high"].cummax().to_numpy(float),
        "low": g["low"].cummin().to_numpy(float),
        "close": pd.to_numeric(daily["close"], errors="coerce").to_numpy(float),
        "volume": g["volume"].cumsum().to_numpy(float),
    }
    return first_time, syn


def selfcheck_one(ps: PeriodState, p: int, syn_row: dict[str, float],
                  period_df: pd.DataFrame, cfg: dict) -> float:
    """把「已收盘[:p] + 合成 bar」拼成 DataFrame 调生产函数，返回与增量值的绝对差。"""
    tail = pd.DataFrame([syn_row])
    full = pd.concat(
        [period_df.iloc[:p][["open", "high", "low", "close", "volume"]], tail],
        ignore_index=True,
    )
    ref = calculate_trend_score_series(full, cfg)["trend_score"].iloc[-1]
    mine = live_scores(ps, np.array([p]),
                       {k: np.array([v]) for k, v in syn_row.items()})[0]
    if pd.isna(ref) and not np.isfinite(mine):
        return 0.0
    if pd.isna(ref) != (not np.isfinite(mine)):
        return float("inf")
    return float(abs(ref - mine))


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
    rng = np.random.default_rng(20260913)

    for i in range(0, len(symbols), CHUNK):
        chunk = symbols[i : i + CHUNK]
        daily_map = db.load_market_data_many(chunk, price_mode="qfq", period="1d")
        weekly_map = db.load_market_data_many(chunk, price_mode="qfq", period="1w")
        monthly_map = db.load_market_data_many(chunk, price_mode="qfq", period="1M")
        for symbol, daily in daily_map.items():
            if daily is None or daily.empty:
                continue
            n = len(daily)
            dates = daily["time"].dt.strftime("%Y-%m-%d").to_numpy()
            closes = pd.to_numeric(daily["close"], errors="coerce").to_numpy(float)

            # 日级：完整 bar 收盘即生效（与旧版一致）
            d_scores = calculate_trend_score_series(daily, cfg)["trend_score"].to_numpy(float)

            weekly_df = weekly_map.get(symbol)
            monthly_df = monthly_map.get(symbol)
            w_ps = PeriodState(weekly_df, cfg)
            m_ps = PeriodState(monthly_df, cfg)

            w_start, w_syn = _daily_groups(daily, "W-SUN")
            m_start, m_syn = _daily_groups(daily, "M")
            w_p = w_ps.completed_count(w_start) if w_ps.ready else np.zeros(n, dtype=int)
            m_p = m_ps.completed_count(m_start) if m_ps.ready else np.zeros(n, dtype=int)
            w_scores = live_scores(w_ps, w_p, w_syn) if w_ps.ready else np.full(n, np.nan)
            m_scores = live_scores(m_ps, m_p, m_syn) if m_ps.ready else np.full(n, np.nan)

            # --- 精确性自检：抽 周/月 各一天，与生产函数逐点比对 ---
            if checked < SELFCHECK_SYMBOLS and w_ps.ready and m_ps.ready and n > 60:
                cand = np.arange(30, n - 45) if n > 75 else np.arange(30, n)
                if cand.size:
                    i_w = int(rng.choice(cand))
                    i_m = int(rng.choice(cand))
                    try:
                        check_diffs.append(selfcheck_one(
                            w_ps, int(w_p[i_w]), {k: float(v[i_w]) for k, v in w_syn.items()},
                            weekly_df, cfg))
                        check_diffs.append(selfcheck_one(
                            m_ps, int(m_p[i_m]), {k: float(v[i_m]) for k, v in m_syn.items()},
                            monthly_df, cfg))
                        checked += 1
                    except Exception as exc:  # 自检样本异常不阻断主流程，但打印出来
                        print(f"  [selfcheck] {symbol} 异常: {exc}")

            d_state = _states_from_scores(d_scores)
            w_state = _states_from_scores(w_scores)
            m_state = _states_from_scores(m_scores)

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
    print(f"\n[selfcheck] 抽样 {len(check_diffs)} 个点，max|Δ| = "
          f"{diffs.max() if diffs.size else float('nan'):.3e}，"
          f"有效/无效不一致: {'有' if hard_fail else '无'}")
    if hard_fail or (diffs.size and diffs.max() > 1e-9):
        print("[selfcheck] 增量算法与生产函数不一致，终止（不落盘）")
        return 1

    cols = ["symbol", "date", "from_idx", "to_idx"] + [f"r_{n}" for n, _ in HORIZONS]
    events_df = pd.DataFrame(all_events, columns=cols)

    def _label(idx: int) -> str:
        d, w, m = idx // 9 - 1, (idx % 9) // 3 - 1, idx % 3 - 1
        return STATE_LABEL[d] + STATE_LABEL[w] + STATE_LABEL[m]

    events_df["from_combo"] = events_df["from_idx"].map(_label)
    events_df["to_combo"] = events_df["to_idx"].map(_label)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    events_df.to_csv(OUT_DIR / "events.csv", index=False)

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
        "model": "live_inflight（在途周/月 bar 逐日合成，日级当日生效）",
        "symbols": len(symbols),
        "symbols_with_events": symbols_with_events,
        "total_events": int(len(events_df)),
        "distinct_transitions": int(len(transition_stats)),
        "valid_symbol_days": int(combo_days.sum()),
        "threshold": THRESH,
        "horizons": {n: h for n, h in HORIZONS},
        "date_range": [str(events_df["date"].min()), str(events_df["date"].max())],
        "selfcheck": {"samples": len(check_diffs),
                      "max_abs_diff": float(diffs.max()) if diffs.size else None},
    }
    with open(OUT_DIR / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\n事件总数 {len(events_df)}，涉及标的 {symbols_with_events} 只，"
          f"不同迁移路径 {len(transition_stats)} 条")
    print(f"事件日期范围: {summary['date_range'][0]} ~ {summary['date_range'][1]}")
    print("频次最高的 10 条迁移：")
    for _, r in transition_stats.sort_values("events", ascending=False).head(10).iterrows():
        print(f"  {r['from_combo']} → {r['to_combo']}: {int(r['events'])} 次")
    print(f"输出目录: {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
