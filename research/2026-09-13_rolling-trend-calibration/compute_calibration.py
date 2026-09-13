"""滚动锚定周/月趋势值标定（一次性分析，输出供绘图脚本使用）。

背景：趋势值公式按日K 标定；周/月级此前有两条路径——vendor 周期K
（只覆盖已收盘周期，见 2026-09-12_trend-score-distribution）与在途合成 bar
（周期内量能爬坡/TR 爬坡，见 2026-09-13_phase-migration-live-synth）。
本研究基于 ``core/rolling_bars`` 的滚动锚定口径（第 k 根 bar = 截至当日的
倒数第 k 个 D 交易日窗口，每根 bar 恒为满 D 天：周 D=5、月 D=22），
对全市场 qfq 日K 重算 日 / 滚动周 / 滚动月 三条日频趋势值序列并标定分布。

输出（均在本目录 data/ 下）：

- ``series/{symbol}.npz``       每标的 (dates, d, w, m) 日频序列（预热期 NaN 保留）
- ``{asset}_{period}.npy``      六组池化样本（周/月为滚动口径、日频采样）
- ``stats.json``                六组描述性统计（schema 对齐前序研究 + ±5 分位）
- ``saturation.json``           抽样诊断：|norm_slope|/|norm_bias|>95 饱和率、
                                volume_factor / ER 分布；含等价性自检
- ``er_variants.json``          月尺度 er_period ∈ {3,4,6} 对比
- ``er_variant_monthly_er{4,6}.npy``  变体池化样本（er=3 即 {asset}_monthly.npy 之并）

运行：sudo -u trendquant .venv/bin/python research/2026-09-13_rolling-trend-calibration/compute_calibration.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import _common  # noqa: E402  # .env + src/scripts 路径 + DB_PATH

from core.rolling_bars import (  # noqa: E402
    _clean_daily,
    rolling_period_frame,
    rolling_period_trend_series,
    rolling_trend_cfg,
)
from core.strategy_config import get_strategy_config  # noqa: E402
from core.trend import calculate_trend_score_series  # noqa: E402
from data.storage.db import get_db, init_db  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "data"
SERIES_DIR = OUT_DIR / "series"
ASSET_TYPES = ("stock", "etf")
GROUP_PERIODS = ("daily", "weekly", "monthly")  # weekly/monthly = 滚动口径
CHUNK = 150

# 饱和率抽样：≥150 只有数据标的 × 每只 40 个均匀采样日（目标 180 留余量）
SEED = 20260913
SAMPLE_SYMBOLS = 180
SAMPLE_DAYS = 40
ER_VARIANTS = (3, 4, 6)  # 月尺度；3 = rolling_trend_cfg("1M") 现行值


def _describe(values: np.ndarray) -> dict:
    """与 2026-09-12 研究同口径的描述统计，新增 ±5 在分布中的分位。"""
    n = int(values.size)
    mean = float(np.mean(values))
    std = float(np.std(values))
    centered = values - mean
    m2 = float(np.mean(centered**2))
    m3 = float(np.mean(centered**3))
    m4 = float(np.mean(centered**4))
    skew = m3 / m2**1.5 if m2 > 0 else 0.0
    kurt = m4 / m2**2 - 3.0 if m2 > 0 else 0.0
    pct = np.percentile(values, [0.5, 1, 5, 10, 25, 50, 75, 90, 95, 99, 99.5])
    return {
        "n": n,
        "mean": mean,
        "std": std,
        "var": float(np.var(values)),
        "median": float(pct[5]),
        "skew": float(skew),
        "excess_kurtosis": float(kurt),
        "min": float(np.min(values)),
        "max": float(np.max(values)),
        "pct_positive": float(np.mean(values > 0)),
        "pct_ge_5": float(np.mean(values >= 5)),
        "pct_le_-5": float(np.mean(values <= -5)),
        "pct_abs_lt_5": float(np.mean(np.abs(values) < 5)),
        # ±5 卡在本分布的分位（P(X ≤ t)，连续分布下 pctile_at_5 ≈ 1 − pct_ge_5）
        "pctile_rank_at_neg5": float(np.mean(values <= -5.0)),
        "pctile_rank_at_pos5": float(np.mean(values <= 5.0)),
        "percentiles": {
            "p0.5": float(pct[0]), "p1": float(pct[1]), "p5": float(pct[2]),
            "p10": float(pct[3]), "p25": float(pct[4]), "p50": float(pct[5]),
            "p75": float(pct[6]), "p90": float(pct[7]), "p95": float(pct[8]),
            "p99": float(pct[9]), "p99.5": float(pct[10]),
        },
    }


def _align_to(series: pd.Series, t_idx: pd.Index) -> np.ndarray:
    """滚动序列（清洗后 time 索引）对齐到原始 bars 的日期序列，缺失为 NaN。"""
    if series.index.is_unique:
        return series.reindex(t_idx).to_numpy(dtype=float)
    lut = dict(zip(series.index, series.to_numpy(dtype=float)))
    return np.array([lut.get(t, np.nan) for t in t_idx], dtype=float)


def _state(values: np.ndarray) -> np.ndarray:
    """±5 三态：正=+1 / 负=-1 / 无=0。"""
    return np.sign(np.where(np.abs(values) < 5.0, 0.0, np.where(values > 0, 1.0, -1.0))).astype(int)


def main() -> int:
    t_start = time.time()
    init_db(_common.DB_PATH)
    db = get_db()
    cfg = get_strategy_config()
    cfg_w = rolling_trend_cfg("1w", base=cfg)
    cfg_m = rolling_trend_cfg("1M", base=cfg)
    cfg_m_er = {er: {**cfg_m, "er_period": er} for er in ER_VARIANTS}
    print(f"daily cfg: n={cfg.get('n_short')}/{cfg.get('n_mid')}/{cfg.get('n_long')} "
          f"atr={cfg.get('atr_period')} er={cfg.get('er_period')} vol_ma={cfg.get('vol_ma_period')}")
    print(f"weekly rolling cfg: atr={cfg_w['atr_period']} er={cfg_w['er_period']} vol_ma={cfg_w['vol_ma_period']}")
    print(f"monthly rolling cfg: atr={cfg_m['atr_period']} er={cfg_m['er_period']} vol_ma={cfg_m['vol_ma_period']}")

    instruments = db.list_instrument_metadata()
    by_asset: dict[str, list[str]] = {a: [] for a in ASSET_TYPES}
    asset_of: dict[str, str] = {}
    for item in instruments:
        asset = str(item.get("asset_type") or "").strip().lower()
        symbol = str(item.get("symbol") or "").strip().upper()
        if asset in by_asset and symbol:
            by_asset[asset].append(symbol)
            asset_of[symbol] = asset
    for asset, symbols in by_asset.items():
        print(f"{asset}: {len(symbols)} 只")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SERIES_DIR.mkdir(parents=True, exist_ok=True)

    groups: dict[str, list[np.ndarray]] = {
        f"{a}_{p}": [] for a in ASSET_TYPES for p in GROUP_PERIODS
    }
    group_symbols: dict[str, int] = {k: 0 for k in groups}
    group_dates: dict[str, list] = {k: [None, None] for k in groups}
    er_marginal: dict[int, list[np.ndarray]] = {er: [] for er in ER_VARIANTS}
    er_triples: list[np.ndarray] = []
    monthly_finite_days: dict[str, int] = {}  # 饱和率抽样的候选条件

    all_symbols = [s for a in ASSET_TYPES for s in by_asset[a]]
    for i in range(0, len(all_symbols), CHUNK):
        chunk = all_symbols[i : i + CHUNK]
        frames = db.load_market_data_many(chunk, price_mode="qfq", period="1d")
        for symbol, bars in frames.items():
            if bars is None or bars.empty:
                continue
            asset = asset_of[symbol]
            dates = pd.to_datetime(bars["time"], errors="coerce")
            t_idx = pd.Index(dates)

            d_vals = calculate_trend_score_series(bars, cfg)["trend_score"].to_numpy(dtype=float)
            w_ser = rolling_period_trend_series(bars, cfg_w, period="1w")
            m_ser = rolling_period_trend_series(bars, cfg_m_er[3], period="1M")
            w_vals = _align_to(w_ser, t_idx)
            m_vals = _align_to(m_ser, t_idx)

            np.savez(
                SERIES_DIR / f"{symbol}.npz",
                dates=dates.to_numpy().astype("datetime64[D]"),
                d=d_vals.astype(np.float32),
                w=w_vals.astype(np.float32),
                m=m_vals.astype(np.float32),
            )

            monthly_finite_days[symbol] = int(np.isfinite(m_vals).sum())
            for key, vals in (
                (f"{asset}_daily", d_vals),
                (f"{asset}_weekly", w_vals),
                (f"{asset}_monthly", m_vals),
            ):
                finite = vals[np.isfinite(vals)]
                if finite.size:
                    group_symbols[key] += 1
                    groups[key].append(finite)
                    dmin = dates[np.isfinite(vals)].min()
                    dmax = dates[np.isfinite(vals)].max()
                    lo, hi = group_dates[key]
                    group_dates[key] = [
                        dmin if lo is None or dmin < lo else lo,
                        dmax if hi is None or dmax > hi else hi,
                    ]

            # 月尺度 ER 变体（同一清洗索引，逐位对齐）
            m4 = rolling_period_trend_series(bars, cfg_m_er[4], period="1M")
            m6 = rolling_period_trend_series(bars, cfg_m_er[6], period="1M")
            for er, ser in ((4, m4), (6, m6)):
                v = ser.to_numpy(dtype=float)
                v = v[np.isfinite(v)]
                if v.size:
                    er_marginal[er].append(v)
            triple = pd.concat(
                [m_ser.rename("er3"), m4.rename("er4"), m6.rename("er6")], axis=1
            ).dropna()
            if len(triple):
                er_triples.append(triple.to_numpy(dtype=float))
        print(f"  主循环 {min(i + CHUNK, len(all_symbols))}/{len(all_symbols)} "
              f"({time.time() - t_start:.0f}s)", flush=True)

    # ---- (b) 六组分布统计 ----
    stats: dict[str, dict] = {}
    for key in groups:
        values = np.concatenate(groups[key]) if groups[key] else np.empty(0)
        np.save(OUT_DIR / f"{key}.npy", values)
        info = _describe(values) if values.size else {"n": 0}
        info["symbols_with_data"] = group_symbols[key]
        lo, hi = group_dates[key]
        info["date_min"] = str(lo)[:10] if lo is not None else None
        info["date_max"] = str(hi)[:10] if hi is not None else None
        stats[key] = info
        if values.size:
            print(f"[{key}] N={info['n']} 标的数={info['symbols_with_data']} "
                  f"mean={info['mean']:.3f} std={info['std']:.3f} "
                  f"|x|<5={info['pct_abs_lt_5'] * 100:.1f}% "
                  f"q(+5)={info['pctile_rank_at_pos5'] * 100:.1f}% "
                  f"q(-5)={info['pctile_rank_at_neg5'] * 100:.1f}%", flush=True)

    with open(OUT_DIR / "stats.json", "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    # ---- (c) 饱和率诊断抽样 + 等价性自检 ----
    rng = np.random.default_rng(SEED)
    candidates = [s for s in all_symbols if monthly_finite_days.get(s, 0) >= SAMPLE_DAYS]
    picked = [candidates[j] for j in rng.permutation(len(candidates))[:SAMPLE_SYMBOLS]]
    print(f"饱和率抽样: 候选 {len(candidates)} 只, 抽 {len(picked)} 只 × {SAMPLE_DAYS} 日")

    rec: dict[str, dict[str, list]] = {
        p: {"norm_slope": [], "norm_bias": [], "volume_factor": [], "er": []}
        for p in ("1w", "1M")
    }
    check_diffs: list[float] = []
    picked_asset = {"stock": 0, "etf": 0}
    frames = db.load_market_data_many(picked, price_mode="qfq", period="1d")
    for symbol in picked:
        bars = frames.get(symbol)
        if bars is None or bars.empty:
            continue
        picked_asset[asset_of[symbol]] += 1
        cleaned = _clean_daily(bars)  # 与 rolling 内部同一清洗，end_idx 用清洗后位置
        m_ser = rolling_period_trend_series(cleaned, cfg_m_er[3], period="1M")
        w_ser = rolling_period_trend_series(cleaned, cfg_w, period="1w")
        fin = np.flatnonzero(m_ser.notna().to_numpy())
        if fin.size < SAMPLE_DAYS:
            continue
        pos_list = fin[np.linspace(0, fin.size - 1, SAMPLE_DAYS).astype(int)]
        check_done = {"1w": False, "1M": False}
        for pos in pos_list:
            for period, cfg_p, ser in (("1w", cfg_w, w_ser), ("1M", cfg_m_er[3], m_ser)):
                frame = rolling_period_frame(cleaned, int(pos), period)
                if frame.empty:
                    continue
                row = calculate_trend_score_series(frame, cfg_p).iloc[-1]
                for col in ("norm_slope", "norm_bias", "volume_factor", "er"):
                    v = float(row[col])
                    if np.isfinite(v):
                        rec[period][col].append(v)
                # 等价性自检：每标的每周期抽 1 点比对滚动序列值
                if not check_done[period]:
                    ref = float(ser.iloc[int(pos)])
                    got = float(row["trend_score"])
                    if np.isfinite(ref) and np.isfinite(got):
                        check_diffs.append(abs(ref - got))
                        check_done[period] = True

    saturation: dict = {
        "meta": {
            "seed": SEED,
            "sample_days_per_symbol": SAMPLE_DAYS,
            "sample_symbols": len(picked),
            "symbols_processed": int(sum(picked_asset.values())),
            "symbols_by_asset": picked_asset,
            "note": "抽样日取自滚动月序列有效区间（月有效 ⇒ 周有效）；"
                    "中间量由 rolling_period_frame 截断序列喂 canonical 公式取得。",
        },
        "self_check": {
            "points": len(check_diffs),
            "max_abs_diff": float(np.max(check_diffs)) if check_diffs else None,
            "mismatches_gt_1e-9": int(np.sum(np.array(check_diffs) > 1e-9)),
        },
    }
    for period in ("1w", "1M"):
        r = rec[period]
        ns = np.asarray(r["norm_slope"])
        nb = np.asarray(r["norm_bias"])
        vf = np.asarray(r["volume_factor"])
        er = np.asarray(r["er"])
        saturation[period] = {
            "n_points": int(ns.size),
            "pct_abs_norm_slope_gt_95": float(np.mean(np.abs(ns) > 95)),
            "pct_abs_norm_bias_gt_95": float(np.mean(np.abs(nb) > 95)),
            "norm_slope_abs_median": float(np.median(np.abs(ns))),
            "norm_bias_abs_median": float(np.median(np.abs(nb))),
            "volume_factor": {
                "median": float(np.median(vf)),
                "pct_lt_0.2": float(np.mean(vf < 0.2)),
            },
            "er": {
                "median": float(np.median(er)),
                "pct_gt_0.8": float(np.mean(er > 0.8)),
            },
        }
        print(f"[饱和率 {period}] n={ns.size} "
              f"|slope|>95: {saturation[period]['pct_abs_norm_slope_gt_95'] * 100:.2f}% "
              f"|bias|>95: {saturation[period]['pct_abs_norm_bias_gt_95'] * 100:.2f}% "
              f"vf中位={saturation[period]['volume_factor']['median']:.3f} "
              f"vf<0.2={saturation[period]['volume_factor']['pct_lt_0.2'] * 100:.1f}% "
              f"ER中位={saturation[period]['er']['median']:.3f} "
              f"ER>0.8={saturation[period]['er']['pct_gt_0.8'] * 100:.1f}%", flush=True)

    with open(OUT_DIR / "saturation.json", "w", encoding="utf-8") as f:
        json.dump(saturation, f, ensure_ascii=False, indent=2)

    # ---- (d) 月尺度 ER 变体对比 ----
    er3_values = np.concatenate(
        [v for k in ("stock_monthly", "etf_monthly") for v in [np.load(OUT_DIR / f"{k}.npy")]]
    )
    er_values: dict[int, np.ndarray] = {3: er3_values}
    for er in (4, 6):
        er_values[er] = (
            np.concatenate(er_marginal[er]) if er_marginal[er] else np.empty(0)
        )
        np.save(OUT_DIR / f"er_variant_monthly_er{er}.npy", er_values[er].astype(np.float32))

    triples = np.concatenate(er_triples) if er_triples else np.empty((0, 3))
    er_json: dict = {
        "meta": {
            "note": "月尺度滚动趋势值 er_period 对比；边际分布为全市场池化（stock+etf），"
                    "相关/一致率在逐位对齐的完整三元组上计算（同一标的同一交易日）",
            "default_er": 3,
            "n_aligned_triples": int(len(triples)),
        },
        "variants": {f"er{er}": _describe(er_values[er]) for er in ER_VARIANTS},
        "pairwise": {},
    }
    if len(triples):
        for a, b, name in ((0, 1, "er3_er4"), (0, 2, "er3_er6"), (1, 2, "er4_er6")):
            x, y = triples[:, a], triples[:, b]
            er_json["pairwise"][name] = {
                "pearson": float(np.corrcoef(x, y)[0, 1]),
                "state_agreement": float(np.mean(_state(x) == _state(y))),
            }
    with open(OUT_DIR / "er_variants.json", "w", encoding="utf-8") as f:
        json.dump(er_json, f, ensure_ascii=False, indent=2)
    for er in ER_VARIANTS:
        v = er_json["variants"][f"er{er}"]
        print(f"[月 er={er}] N={v['n']} std={v['std']:.3f} IQR=[{v['percentiles']['p25']:.2f},"
              f"{v['percentiles']['p75']:.2f}] pct_ge_5={v['pct_ge_5'] * 100:.1f}%", flush=True)
    for name, pw in er_json["pairwise"].items():
        print(f"[{name}] pearson={pw['pearson']:.4f} 状态一致率={pw['state_agreement'] * 100:.2f}%",
              flush=True)

    # ---- 合理性 spot check ----
    spot = "510300.SS" if monthly_finite_days.get("510300.SS") else next(
        (s for s in all_symbols if monthly_finite_days.get(s, 0) > 0), None
    )
    if spot is None:
        print("spot check: 无有数据标的，跳过")
        return 0
    z = np.load(SERIES_DIR / f"{spot}.npz")
    print(f"spot check {spot}: dates 严格递增={bool(np.all(np.diff(z['dates'].astype('datetime64[D]').astype(int)) > 0))}")
    for key in ("d", "w", "m"):
        v = z[key].astype(float)
        fin = np.isfinite(v)
        if fin.any():
            first = int(np.argmax(fin))
            holes = int(np.sum(~fin[first:]))
            diffs = np.abs(np.diff(v[fin]))
            print(f"  {key}: 首个有效值后 NaN 空洞={holes}, 相邻日最大|Δ|={diffs.max():.2f}, "
                  f"末值={v[fin][-1]:.2f}")

    print(f"完成，耗时 {time.time() - t_start:.0f}s，输出目录: {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
