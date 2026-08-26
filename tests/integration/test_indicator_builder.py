"""Tests for services.indicator_builder — rebuild pipeline, param registry,
dividend detection, and backup."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from core.indicators import INDICATOR_FORMULA_VERSION
from core.strategy_config import DEFAULT_STRATEGY_CONFIG
from core.trend import TREND_FORMULA_VERSION
from data.indicator_store import get_series
from data.storage.db import Database
from services.indicator_builder import (
    default_param_set_needs_rebuild,
    detect_adjustment_breaks,
    normalized_params_json,
    rebuild_all,
    rebuild_if_needed,
    rebuild_symbol,
    run_post_update_pipeline,
)


def _make_bars(seed: int = 1, n: int = 60, start: str = "2026-01-01") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    closes = 100 + np.cumsum(rng.normal(0.1, 1.0, n))
    dates = pd.date_range(start, periods=n, freq="B")
    return pd.DataFrame(
        {
            "time": dates,
            "open": closes,
            "high": closes + 0.5,
            "low": closes - 0.5,
            "close": closes,
            "volume": np.abs(rng.normal(1e6, 2e5, n)),
        }
    )


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


class TestRebuild:
    def test_rebuild_symbol_populates_caches(self, db) -> None:
        db.save_market_data("T.SS", _make_bars(), price_mode="qfq")
        result = rebuild_symbol("T.SS", DEFAULT_STRATEGY_CONFIG, db=db)
        assert result["status"] == "rebuilt"
        info = db.indicator_cache_info("T.SS")
        assert info["indicator_rows"] == 60
        assert info["trend_rows"] == 60
        assert info["indicator_version"] == INDICATOR_FORMULA_VERSION
        assert info["trend_version"] == TREND_FORMULA_VERSION

    def test_rebuild_all_and_get_series(self, db) -> None:
        db.save_market_data("A.SS", _make_bars(seed=1), price_mode="qfq")
        db.save_market_data("B.SS", _make_bars(seed=2), price_mode="qfq")
        result = rebuild_all(trend_cfg=DEFAULT_STRATEGY_CONFIG, db=db)
        assert result["rebuilt"] == 2
        cached = get_series("A.SS", "atr", db=db)
        assert len(cached) == 60
        assert pd.notna(cached.iloc[-1])

    def test_rebuild_symbol_no_data(self, db) -> None:
        assert rebuild_symbol("NOPE.SS", DEFAULT_STRATEGY_CONFIG, db=db)["status"] == "no_data"


class TestParamRegistry:
    def test_first_run_needs_rebuild(self, db) -> None:
        assert default_param_set_needs_rebuild(DEFAULT_STRATEGY_CONFIG, db=db) is True

    def test_after_register_no_rebuild(self, db) -> None:
        db.save_param_set("default", normalized_params_json(DEFAULT_STRATEGY_CONFIG), True, TREND_FORMULA_VERSION)
        assert default_param_set_needs_rebuild(DEFAULT_STRATEGY_CONFIG, db=db) is False

    def test_config_change_triggers_rebuild(self, db) -> None:
        db.save_param_set("default", normalized_params_json(DEFAULT_STRATEGY_CONFIG), True, TREND_FORMULA_VERSION)
        changed = {**DEFAULT_STRATEGY_CONFIG, "atr_period": 14}
        assert default_param_set_needs_rebuild(changed, db=db) is True

    def test_rebuild_if_needed_bootstraps_and_registers(self, db) -> None:
        db.save_market_data("A.SS", _make_bars(), price_mode="qfq")
        # Point the store at our tmp db via monkeypatched get_db.
        import services.indicator_builder as builder

        builder_get_db = builder.get_db
        builder.get_db = lambda: db  # type: ignore[assignment]
        try:
            result = rebuild_if_needed(db=db)
        finally:
            builder.get_db = builder_get_db  # type: ignore[assignment]
        assert result["status"] == "rebuilt"
        assert result["rebuilt"] == 1
        assert default_param_set_needs_rebuild(DEFAULT_STRATEGY_CONFIG, db=db) is False

    def test_normalized_json_deterministic(self) -> None:
        a = normalized_params_json({"b": 1, "a": 0.4})
        b = normalized_params_json({"a": 0.4, "b": 1})
        assert a == b == '{"a":0.4,"b":1}'


class FakeDataService:
    """raw 真源架构下的假 DataService：除权检测 = 因子 diff，修复 = 本地重物化。"""

    def __init__(self, changed: list[str] | None = None) -> None:
        self.changed = list(changed or [])
        self.rematerialized: list[str] = []
        self.backfilled: list[str] = []

    def sync_ex_factors(self, symbols, *, db=None):
        return ({symbol: [] for symbol in symbols}, list(self.changed))

    def rematerialize_qfq(self, symbol, factors=None, *, db=None):
        self.rematerialized.append(symbol)
        return {"symbol": symbol, "status": "ok", "rows": 10}

    def backfill_daily_history(self, symbol, start_date, end_date, adjust="none"):
        self.backfilled.append(symbol)
        return {"symbol": symbol, "status": "updated", "added_rows": 100}


class TestDividendDetection:
    def test_detects_break(self) -> None:
        svc = FakeDataService(changed=["T.SS"])
        broken = detect_adjustment_breaks(["T.SS"], svc, date(2026, 3, 10))
        assert broken == ["T.SS"]

    def test_no_break_when_identical(self) -> None:
        svc = FakeDataService(changed=[])
        assert detect_adjustment_breaks(["T.SS"], svc, date(2026, 3, 10)) == []

    def test_pipeline_repairs_and_rebuilds(self, db) -> None:
        bars = _make_bars()
        db.save_market_data("T.SS", bars, price_mode="qfq")
        svc = FakeDataService(changed=["T.SS"])
        payload = {"results": [{"symbol": "T.SS", "status": "updated"}]}
        import services.indicator_builder as builder

        builder_get_db = builder.get_db
        builder.get_db = lambda: db  # type: ignore[assignment]
        try:
            result = run_post_update_pipeline(None, svc, payload, ["T.SS"], date(2026, 3, 10), db=db)
        finally:
            builder.get_db = builder_get_db  # type: ignore[assignment]
        assert result["dividend_breaks"] == ["T.SS"]
        assert svc.rematerialized == ["T.SS"]
        assert result["rebuilt"] == 1


class TestBackup:
    def test_backup_to_creates_snapshot_and_prunes(self, db, tmp_path) -> None:
        backup_dir = tmp_path / "backups"
        for _ in range(4):
            dest = db.backup_to(backup_dir=backup_dir, keep=2)
            assert dest.exists()
        assert len(list(backup_dir.glob("trend_quant-*.db"))) == 2

    def test_wal_mode_enabled(self, db) -> None:
        import sqlite3

        conn = sqlite3.connect(db.db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode.lower() == "wal"
