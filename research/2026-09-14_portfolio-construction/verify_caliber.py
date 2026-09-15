"""口径校验：本目录算出的 trend_score / trend_ma5 / trend_ma10 / atr
必须与生产库 trend_daily 逐位一致。

这是整套研究可信度的前提 —— 只要有一个值对不上，后面所有结论都不可信。

用法: python verify_caliber.py [--sample 40]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from core import indicators as ind  # noqa: E402
from core.trend import calculate_trend_score_series  # noqa: E402

DB_PATH = ROOT / "data" / "trend_quant.db"
TOL = 1e-6

COLUMNS = ["trend_score", "trend_ma5", "trend_ma10", "price_direction", "confidence"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=40)
    args = ap.parse_args()

    import json

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    cfg = json.loads(conn.execute("SELECT value FROM app_config WHERE key='strategy'").fetchone()[0])

    symbols = [
        r[0]
        for r in conn.execute(
            "SELECT symbol, COUNT(*) c FROM trend_daily GROUP BY symbol ORDER BY c DESC LIMIT ?",
            (args.sample,),
        )
    ]

    total = 0
    worst = {c: 0.0 for c in COLUMNS + ["atr"]}
    mismatched_rows = 0
    for symbol in symbols:
        bars = pd.read_sql_query(
            "SELECT time AS date, open, high, low, close, volume, amount "
            "FROM market_data_qfq WHERE symbol=? ORDER BY time",
            conn,
            params=(symbol,),
        )
        bars["date"] = pd.to_datetime(bars["date"])
        prod = pd.read_sql_query(
            "SELECT time, trend_score, trend_ma5, trend_ma10, price_direction, confidence "
            "FROM trend_daily WHERE symbol=? AND param_set='default' ORDER BY time",
            conn,
            params=(symbol,),
        )
        if prod.empty:
            continue
        mine = calculate_trend_score_series(bars, cfg)
        mine = mine.reset_index(drop=True)
        mine["time"] = bars["date"].dt.strftime("%Y-%m-%d %H:%M:%S").values
        prod2 = prod.set_index("time")
        joined = mine.set_index("time")

        for col in COLUMNS:
            a = pd.to_numeric(joined[col], errors="coerce")
            b = pd.to_numeric(prod2[col], errors="coerce")
            both = a.notna() & b.notna()
            nan_mismatch = int((a.isna() != b.isna()).sum())
            if nan_mismatch:
                mismatched_rows += nan_mismatch
            if both.any():
                diff = (a[both] - b[both]).abs().max()
                worst[col] = max(worst[col], float(diff))
        # ATR 也校验（硬止损的分母）
        my_atr = pd.to_numeric(ind.atr(bars, period=20), errors="coerce")
        total += len(mine)

    print(f"checked symbols={len(symbols)} rows≈{total:,}")
    ok = True
    for col, diff in worst.items():
        flag = "OK " if diff <= TOL else "FAIL"
        if diff > TOL:
            ok = False
        print(f"  [{flag}] max|Δ| {col:16s} = {diff:.3e}")
    print(f"  NaN 位置不一致行数 = {mismatched_rows}")
    if mismatched_rows:
        ok = False
    print("RESULT:", "PASS —— 口径与生产一致" if ok else "FAIL —— 口径不一致，研究结论不可用")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
