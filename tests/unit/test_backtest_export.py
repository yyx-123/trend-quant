"""回测分析导出 + 旧批次 round-trip 回填重放（方案 2026-08-30 §8/§2.3）。"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from data.storage.market_store import MarketStore
from rule_backtest.batch_service import BatchBacktestService
from rule_backtest.loader import StrategyLoader
from rule_backtest.roundtrip_replay import replay_round_trips
from services.backtest_export import export_batch_analysis

from tests.unit.test_stop_diagnostics import make_cross_strategy, make_wave_bars


@pytest.fixture
def batch_db(test_db):
    db = test_db
    db.save_instrument_metadata(
        [{"symbol": "WAVE.SS", "name": "波动标的", "category_l1": "测试", "asset_type": "etf"}]
    )
    MarketStore(db=db).save_history("WAVE.SS", make_wave_bars())
    db.save_rule_strategy(make_cross_strategy("sma_cross"), overwrite=True)
    return db


def _run_batch(db, stop_profile="default") -> str:
    service = BatchBacktestService(db=db)
    batch = service.prepare_batch(
        categories=["测试"],
        strategy_ids=["sma_cross"],
        strategy_loader=StrategyLoader(db=db),
        stop_profile=stop_profile,
    )
    assert db.create_batch_run_if_idle(batch) is True
    service.run_batch(batch["batch_id"])
    return batch["batch_id"]


class TestExport:
    def test_export_files_and_schema(self, batch_db, tmp_path: Path) -> None:
        batch_id = _run_batch(batch_db)
        result = export_batch_analysis(batch_db, batch_id, out_dir=tmp_path, include_live=False)
        out = Path(result["export_dir"])
        for name in ("manifest.json", "cells.csv", "round_trips.csv", "annual_aggregates.csv", "stop_diagnostics.csv"):
            assert (out / name).exists(), name
        assert "live_trades.csv" not in result["files"]

        trips = pd.read_csv(out / "round_trips.csv")
        for col in (
            "symbol", "entry_date", "exit_date", "exit_reason", "pnl", "r_multiple",
            "mae_pct", "mfe_pct", "entry_atr", "entry_atr_pct", "hard_stop_atr_mul",
            "post_exit_ret_5d", "reentry_above_entry_5d", "strategy", "trend_score_avg",
        ):
            assert col in trips.columns, col

        cells = pd.read_csv(out / "cells.csv")
        assert "r_mean" in cells.columns and "stop_exit_ratio" in cells.columns

        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["batch"]["stop_profile"] == "default"
        assert manifest["batch"]["atr_basis"] == "prev_close"
        for key in ("stop_fill_assumption", "survivorship_bias", "atr_basis"):
            assert key in manifest["bias_disclosures"]
        assert "r_multiple" in manifest["round_trip_fields"]

    def test_export_with_compare(self, batch_db, tmp_path: Path) -> None:
        base_id = _run_batch(batch_db, "tight")
        alt_id = _run_batch(batch_db, "loose")
        result = export_batch_analysis(
            batch_db, base_id, alt_batch_id=alt_id, out_dir=tmp_path, include_live=False
        )
        out = Path(result["export_dir"])
        assert out.name == f"batch_{base_id}_vs_{alt_id}"
        for name in ("cells_alt.csv", "round_trips_alt.csv", "stop_diagnostics_alt.csv",
                     "compare_cell_diffs.csv", "compare_bootstrap_ci.csv", "compare_meta.json"):
            assert (out / name).exists(), name
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["alt_batch"]["stop_profile"] == "loose"

    def test_export_missing_batch_raises(self, batch_db, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            export_batch_analysis(batch_db, "nonexistent", out_dir=tmp_path)

    def test_live_trades(self, batch_db, tmp_path: Path) -> None:
        user = batch_db.create_user("trader", "pw")
        batch_db.create_manual_trade(
            user_id=user["id"], symbol="WAVE.SS",
            buy_date="2024-03-01", buy_price=10.0, shares=1000,
        )
        batch_db.close_manual_trade(1, "2024-03-05", 11.0)
        result = export_batch_analysis(batch_db, _run_batch(batch_db), out_dir=tmp_path)
        live = pd.read_csv(Path(result["export_dir"]) / "live_trades.csv")
        assert len(live) == 1
        row = live.iloc[0]
        assert row["realized_pnl"] == pytest.approx(1000.0)
        assert row["realized_pnl_pct"] == pytest.approx(10.0)
        assert "post_exit_ret_5d" in live.columns
        assert "reentry_above_entry_20d" in live.columns


class TestRoundtripReplay:
    """旧批次回填重放（trades_json + 行情推导）。"""

    def test_replay_pairs_and_fields(self) -> None:
        bars = make_wave_bars()
        bars["date"] = pd.to_datetime(bars["time"]).dt.date
        # day20 上穿买入、day63 下穿卖出（参数经数值验证）
        entry_date = str(bars["date"].iloc[20])
        exit_date = str(bars["date"].iloc[63])
        entry_price = float(bars["close"].iloc[20]) * 1.002
        exit_price = float(bars["close"].iloc[63]) * 0.998
        trades = [
            {"side": "BUY", "date": entry_date, "symbol": "WAVE.SS", "qty": 100,
             "exec_price": entry_price, "reason": "entry_conditions_passed"},
            {"side": "SELL", "date": exit_date, "symbol": "WAVE.SS", "qty": 100,
             "exec_price": exit_price, "pnl": (exit_price - entry_price) * 100 - 10.0,
             "reason": "exit_conditions_passed"},
        ]
        trips = replay_round_trips(
            trades, bars, make_cross_strategy(),
            asset_type="etf", category_l1="测试",
        )
        assert len(trips) == 1
        rt = trips[0]
        assert rt["entry_date"] == entry_date and rt["exit_date"] == exit_date
        assert rt["holding_days"] == 43
        assert rt["hard_stop_atr_mul"] == pytest.approx(1.5)
        # MAE/MFE 与直接扫行情一致
        lows = pd.to_numeric(bars["low"]).iloc[20:64]
        highs = pd.to_numeric(bars["high"]).iloc[20:64]
        assert rt["mae_pct"] == pytest.approx((float(lows.min()) / entry_price - 1.0) * 100.0)
        assert rt["mfe_pct"] == pytest.approx((float(highs.max()) / entry_price - 1.0) * 100.0)
        # 旧口径：入场 ATR 含入场日当根
        from core import indicators as core_ind

        atr_series = core_ind.atr(bars, period=20)
        assert rt["entry_atr"] == pytest.approx(float(atr_series.iloc[20]))
        assert rt["post_exit_ret_5d"] is not None
        assert rt["post_exit_ret_20d"] is not None

    def test_replay_empty(self) -> None:
        assert replay_round_trips([], make_wave_bars(), {}) == []
        assert replay_round_trips([{"side": "BUY"}], pd.DataFrame(), {}) == []
