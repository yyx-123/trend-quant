"""构建组合研究用的全市场日频面板。

输出: data/panel.parquet —— 每行一个 (symbol, date)，含生产同源的指标列。

指标口径与生产完全一致（不存在第二份实现）:
  - 趋势值/price_direction/confidence/er/vol_ratio: core.trend.calculate_trend_score_series
  - MACD(dif/dea/hist): core.indicators.macd(warmup=True) —— 回测口径
  - ATR20 / SMA20 / SMA200: core.indicators
  - 策略配置: core.strategy_config.get_strategy_config()

用法: python build_panel.py [--limit N]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd
import sqlite3

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from core import indicators as ind  # noqa: E402
from core.trend import calculate_trend_score_series  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE / "data"
DB_PATH = ROOT / "data" / "trend_quant.db"


def read_only_conn() -> sqlite3.Connection:
    """只读连接 —— 研究脚本绝不改动生产库。"""
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def load_cfg(conn: sqlite3.Connection) -> dict:
    """趋势值参数取生产库 app_config.strategy（与看板/回测同源）。

    直接读库而非依赖 core.strategy_config 的应用级单例：研究脚本不走
    init_db()，拿到的会是代码默认值 —— 二者当前恰好一致，但显式读库
    才能保证「生产改了参数、研究结论跟着变」。
    """
    import json

    row = conn.execute("SELECT value FROM app_config WHERE key='strategy'").fetchone()
    if row is None:
        raise SystemExit("app_config.strategy 缺失，无法保证口径一致")
    return json.loads(row[0])


def load_bars(conn: sqlite3.Connection) -> dict[str, pd.DataFrame]:
    """一次性读全量 qfq 日线，按 symbol 分组（内存约 250MB，可接受）。"""
    df = pd.read_sql_query(
        "SELECT symbol, time AS date, open, high, low, close, volume, amount "
        "FROM market_data_qfq ORDER BY symbol, time",
        conn,
    )
    df["date"] = pd.to_datetime(df["date"])
    bars: dict[str, pd.DataFrame] = {}
    for symbol, group in df.groupby("symbol", sort=False):
        g = group.drop(columns=["symbol"]).reset_index(drop=True)
        # open 缺失（极老 bar）用 close 兜底，避免 ATR 全 NaN。
        g["open"] = g["open"].fillna(g["close"])
        bars[symbol] = g
    return bars


def compute_symbol(symbol: str, bars: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    close = pd.to_numeric(bars["close"], errors="coerce")
    trend = calculate_trend_score_series(bars, cfg)
    macd = ind.macd(close, 12, 26, 9, warmup=True)
    atr20 = ind.atr(bars, period=20)
    sma20 = ind.sma(close, 20)
    sma200 = ind.sma(close, 200)
    out = pd.DataFrame(
        {
            "symbol": symbol,
            "date": bars["date"].values,
            "open": bars["open"].values,
            "high": bars["high"].values,
            "low": bars["low"].values,
            "close": close.values,
            "volume": pd.to_numeric(bars["volume"], errors="coerce").values,
            "amount": pd.to_numeric(bars["amount"], errors="coerce").values,
            "trend_score": trend["trend_score"].values,
            "trend_ma5": trend["trend_ma5"].values,
            "trend_ma10": trend["trend_ma10"].values,
            "price_direction": trend["price_direction"].values,
            "confidence": trend["confidence"].values,
            "er": trend["er"].values,
            "vol_ratio": trend["vol_ratio"].values,
            "atr": atr20.values,
            "dif": macd["dif"].values,
            "dea": macd["dea"].values,
            "hist": macd["hist"].values,
            "sma20": sma20.values,
            "sma200": sma200.values,
        }
    )
    out["ret20"] = close.pct_change(20).values
    out["ret60"] = close.pct_change(60).values
    out["atr_pct"] = (out["atr"] / close).values
    # 过热代理：价格偏离 SMA20 的 ATR 倍数（与系统里 bias_atr_normed 同口径）
    out["bias_atr"] = ((close - sma20) / atr20).values
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个标的（调试用）")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    conn = read_only_conn()
    cfg = load_cfg(conn)
    bars_map = load_bars(conn)
    symbols = sorted(bars_map)
    if args.limit:
        symbols = symbols[: args.limit]
    print(f"symbols={len(symbols)} cfg_keys={sorted(cfg)[:6]}...", flush=True)

    frames: list[pd.DataFrame] = []
    t0 = time.time()
    for i, symbol in enumerate(symbols, 1):
        try:
            frames.append(compute_symbol(symbol, bars_map[symbol], cfg))
        except Exception as exc:  # 单标的失败不拖垮整批
            print(f"  ! {symbol} failed: {exc}", file=sys.stderr)
        if i % 100 == 0:
            print(f"  {i}/{len(symbols)}  {time.time() - t0:.1f}s", flush=True)

    panel = pd.concat(frames, ignore_index=True)
    meta = pd.read_sql_query(
        "SELECT symbol, name, asset_type, category_l1, category_l2, category_l3, "
        "enabled, stop_atr_mul FROM instrument_metadata",
        conn,
    )
    panel = panel.merge(meta, on="symbol", how="left")
    path = OUT / "panel.parquet"
    panel.to_parquet(path, index=False)
    print(f"rows={len(panel):,} symbols={panel['symbol'].nunique()} -> {path}")
    print(f"date range {panel['date'].min().date()} .. {panel['date'].max().date()}")
    print(f"elapsed {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
