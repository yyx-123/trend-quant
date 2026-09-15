"""对账：本目录的交易模拟器 vs rule_backtest 真引擎。

方法：对一批标的跑真引擎（MACD金叉进-止损出），取出它实际成交的每一笔
(入场日, 出场日, 出场原因, 盈亏)；再把同样的「入场日」喂给模拟器，比较
出场日 / 出场原因 / 盈亏 / 收益率 是否完全一致。

只有对账通过，研究里所有基于模拟器的数字才可信。

用法: python verify_simulator.py [--symbols 30]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from rule_backtest.engine import SingleSymbolAllInBacktestEngine  # noqa: E402
from rule_backtest.models import (  # noqa: E402
    BacktestExecutionConfig,
    RuleBacktestRequest,
)
from core import indicators as ind  # noqa: E402

import trade_sim  # noqa: E402

HERE = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "trend_quant.db"
STRATEGY_ID = "macd_20260719095419247961"  # MACD金叉进-止损出


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=int, default=30)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    strategy = json.loads(
        conn.execute("SELECT payload_json FROM rule_strategies WHERE id=?", (STRATEGY_ID,)).fetchone()[0]
    )
    # 取数据量最多的标的 —— 交易笔数多，对账样本更有说服力
    symbols = [
        r[0]
        for r in conn.execute(
            "SELECT symbol, COUNT(*) c FROM market_data_qfq GROUP BY symbol ORDER BY c DESC LIMIT ?",
            (args.symbols,),
        )
    ]

    engine = SingleSymbolAllInBacktestEngine()
    total_trades = 0
    mismatch: list[str] = []
    checked = 0

    for symbol in symbols:
        bars = pd.read_sql_query(
            "SELECT time AS date, open, high, low, close, volume, amount "
            "FROM market_data_qfq WHERE symbol=? ORDER BY time",
            conn,
            params=(symbol,),
        )
        bars["date"] = pd.to_datetime(bars["date"])
        asset_type = conn.execute(
            "SELECT asset_type FROM instrument_metadata WHERE symbol=?", (symbol,)
        ).fetchone()
        asset_type = (asset_type[0] if asset_type else None) or "etf"

        result = engine.run(
            RuleBacktestRequest(
                strategy=strategy,
                symbol=symbol,
                bars=bars,
                execution=BacktestExecutionConfig(instrument_type=asset_type),
            )
        )
        sells = [t for t in result["trades"] if t["side"] == "SELL"]
        if not sells:
            continue
        total_trades += len(sells)

        # 引擎的入场日 = 前一笔 SELL 之后的第一次 BUY；从 trades 序列里取
        buys = [t for t in result["trades"] if t["side"] == "BUY"]
        date_to_idx = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(bars["date"])}
        arr = trade_sim.SymbolArrays(
            symbol=symbol,
            date=bars["date"].to_numpy(dtype="datetime64[ns]"),
            open=bars["open"].to_numpy(dtype=float),
            high=bars["high"].to_numpy(dtype=float),
            low=bars["low"].to_numpy(dtype=float),
            close=bars["close"].to_numpy(dtype=float),
            atr=ind.atr(bars, period=20).to_numpy(dtype=float),
            asset_type=asset_type,
            features={},
        )

        for buy, sell in zip(buys, sells):
            entry_date = str(buy["date"])[:10]
            if entry_date not in date_to_idx:
                continue
            idx = date_to_idx[entry_date]
            checked += 1
            sim = trade_sim.simulate_entry(arr, idx, notional=float(buy["total_cost"]))
            if sim is None:
                mismatch.append(f"{symbol} {entry_date}: 模拟器返回 None")
                continue
            # 引擎的盈亏基于它自己的整笔数量；模拟器按同等投入金额取整手，
            # 数量可能差一个 lot —— 用每手收益（收益率）比较，并要求出场一致。
            exp_exit = str(sell["date"])[:10]
            if sim.exit_date != exp_exit:
                mismatch.append(
                    f"{symbol} {entry_date}: 出场日 {sim.exit_date} != 引擎 {exp_exit} "
                    f"(原因 {sim.exit_reason} vs {sell['reason']})"
                )
                continue
            if sim.exit_reason != sell["reason"]:
                mismatch.append(
                    f"{symbol} {entry_date}: 出场原因 {sim.exit_reason} != 引擎 {sell['reason']}"
                )
                continue
            exp_ret = sell["pnl"] / buy["total_cost"]
            if abs(sim.ret_net_pct - exp_ret) > 2e-3:
                mismatch.append(
                    f"{symbol} {entry_date}: 收益率 {sim.ret_net_pct:.5f} != 引擎 {exp_ret:.5f}"
                )

    print(f"标的数 {len(symbols)}  引擎成交笔数 {total_trades}  可比对笔数 {checked}")
    if mismatch:
        print(f"\n不一致 {len(mismatch)} / {checked} 笔：")
        for m in mismatch[:25]:
            print("  " + m)
        print("\nRESULT: FAIL")
        sys.exit(1)
    print("\nRESULT: PASS —— 模拟器与引擎逐笔一致（出场日/原因/收益率）")


if __name__ == "__main__":
    main()
