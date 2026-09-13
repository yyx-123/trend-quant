"""趋势值分布统计（一次性分析，输出供绘图脚本使用）。

按 标的类型(etf/stock) × 周期(1d/1w/1M) 共 6 组，对库内 qfq 行情逐标的
跑 ``calculate_trend_score_series``（与看板/标的查看页同一实现、同一
strategy 配置），收集全历史每根 K 线的 trend_score，输出：

- ``data/{asset}_{period}.npy``  该组全部 trend_score（预热期 NaN 已剔除）
- ``data/stats.json``            每组描述性统计（N/均值/方差/偏度/峰度/分位数/±5 占比）

运行：.venv/bin/python research/2026-09-12_trend-score-distribution/trend_score_distribution.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import _common  # noqa: E402  # .env + src/scripts 路径 + DB_PATH

from core.strategy_config import get_strategy_config  # noqa: E402
from core.trend import calculate_trend_score_series  # noqa: E402
from data.storage.db import get_db, init_db  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "data"
PERIODS = ("1d", "1w", "1M")
PERIOD_KEYS = {"1d": "daily", "1w": "weekly", "1M": "monthly"}
ASSET_TYPES = ("stock", "etf")
CHUNK = 150


def _describe(values: np.ndarray) -> dict:
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
        "percentiles": {
            "p0.5": float(pct[0]), "p1": float(pct[1]), "p5": float(pct[2]),
            "p10": float(pct[3]), "p25": float(pct[4]), "p50": float(pct[5]),
            "p75": float(pct[6]), "p90": float(pct[7]), "p95": float(pct[8]),
            "p99": float(pct[9]), "p99.5": float(pct[10]),
        },
    }


def main() -> int:
    init_db(_common.DB_PATH)
    db = get_db()
    cfg = get_strategy_config()
    print(f"strategy cfg: n={cfg.get('n_short')}/{cfg.get('n_mid')}/{cfg.get('n_long')} "
          f"atr={cfg.get('atr_period')} er={cfg.get('er_period')} vol_ma={cfg.get('vol_ma_period')}")

    instruments = db.list_instrument_metadata()
    by_asset: dict[str, list[str]] = {a: [] for a in ASSET_TYPES}
    for item in instruments:
        asset = str(item.get("asset_type") or "").strip().lower()
        symbol = str(item.get("symbol") or "").strip().upper()
        if asset in by_asset and symbol:
            by_asset[asset].append(symbol)
    for asset, symbols in by_asset.items():
        print(f"{asset}: {len(symbols)} 只")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stats: dict[str, dict] = {}

    for asset in ASSET_TYPES:
        symbols = by_asset[asset]
        for period in PERIODS:
            key = f"{asset}_{PERIOD_KEYS[period]}"
            chunks: list[np.ndarray] = []
            symbols_with_data = 0
            for i in range(0, len(symbols), CHUNK):
                chunk = symbols[i : i + CHUNK]
                frames = db.load_market_data_many(chunk, price_mode="qfq", period=period)
                for symbol, bars in frames.items():
                    if bars is None or bars.empty:
                        continue
                    series = calculate_trend_score_series(bars, cfg)
                    scores = series["trend_score"].to_numpy(dtype=float)
                    scores = scores[np.isfinite(scores)]
                    if scores.size:
                        symbols_with_data += 1
                        chunks.append(scores)
                print(f"  [{key}] {min(i + CHUNK, len(symbols))}/{len(symbols)} ...", flush=True)
            values = np.concatenate(chunks) if chunks else np.empty(0)
            np.save(OUT_DIR / f"{key}.npy", values)
            info = _describe(values) if values.size else {"n": 0}
            info["symbols_with_data"] = symbols_with_data
            stats[key] = info
            if values.size:
                print(f"[{key}] N={info['n']} 标的数={symbols_with_data} "
                      f"mean={info['mean']:.3f} std={info['std']:.3f} "
                      f"median={info['median']:.3f} skew={info['skew']:.3f}", flush=True)
            else:
                print(f"[{key}] 无有效趋势值（标的数={symbols_with_data}）", flush=True)

    with open(OUT_DIR / "stats.json", "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print(f"输出目录: {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
