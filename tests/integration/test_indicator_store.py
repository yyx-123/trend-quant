"""Integration tests for data.indicator_store — cache-first with live fallback."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core import indicators as core_ind
from core.indicators import INDICATOR_FORMULA_VERSION
from core.strategy_config import DEFAULT_STRATEGY_CONFIG
from core.trend import TREND_FORMULA_VERSION
from data.indicator_store import (
    compute_indicator_frame,
    compute_trend_frame,
    get_series,
)
from data.storage.db import Database


def _make_bars(seed: int = 1, n: int = 120) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    closes = 100 + np.cumsum(rng.normal(0.1, 1.0, n))
    dates = pd.date_range("2026-01-01", periods=n, freq="B")
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


@pytest.fixture
def seeded(db):
    bars = _make_bars()
    db.save_market_data("TEST.SS", bars, price_mode="qfq")
    ind = compute_indicator_frame(bars, DEFAULT_STRATEGY_CONFIG)
    trend = compute_trend_frame(bars, DEFAULT_STRATEGY_CONFIG)
    db.save_indicator_daily("TEST.SS", ind, formula_version=INDICATOR_FORMULA_VERSION)
    db.save_trend_daily("TEST.SS", trend, formula_version=TREND_FORMULA_VERSION)
    return bars


class TestCacheHit:
    def test_atr_single_source_matches_live(self, db, seeded) -> None:
        cached = get_series("TEST.SS", "atr", db=db)
        live = core_ind.atr(seeded, period=20)
        live.index = pd.to_datetime(seeded["time"])
        pd.testing.assert_series_equal(cached, live, check_names=False)

    def test_trend_columns_from_cache(self, db, seeded) -> None:
        cached = get_series("TEST.SS", "trend_score", db=db)
        assert len(cached) == len(seeded)
        assert pd.notna(cached.iloc[-1])

    def test_recursion_state_columns_cached(self, db, seeded) -> None:
        for col in ("rsi_avg_gain", "rsi_avg_loss", "macd_ema12", "macd_ema26"):
            cached = get_series("TEST.SS", col, db=db)
            assert len(cached) == len(seeded)


class TestFallback:
    def test_missing_symbol_falls_back_to_live(self, db) -> None:
        bars = _make_bars(seed=5)
        db.save_market_data("LIVE.SS", bars, price_mode="qfq")
        out = get_series("LIVE.SS", "atr", db=db)
        expected = core_ind.atr(bars, period=20)
        expected.index = pd.to_datetime(bars["time"])
        pd.testing.assert_series_equal(out, expected, check_names=False)

    def test_version_mismatch_falls_back(self, db, seeded) -> None:
        db.save_indicator_daily("TEST.SS", compute_indicator_frame(seeded, DEFAULT_STRATEGY_CONFIG), formula_version=999)
        out = get_series("TEST.SS", "sma20", db=db)
        expected = core_ind.sma(pd.to_numeric(seeded["close"]), 20)
        expected.index = pd.to_datetime(seeded["time"])
        pd.testing.assert_series_equal(out, expected, check_names=False)

    def test_stale_cache_falls_back(self, db, seeded) -> None:
        # Append a new market bar AFTER the cache was built → cache is stale.
        new_bar = _make_bars().tail(1).copy()
        new_bar["time"] = pd.Timestamp("2026-07-01")
        new_bar["close"] = 999.0
        db.save_market_data("TEST.SS", new_bar, price_mode="qfq")
        out = get_series("TEST.SS", "atr", db=db)
        # Live fallback recomputes over full history including the new bar.
        assert len(out) == len(seeded) + 1
        assert out.index[-1] == pd.Timestamp("2026-07-01")

    def test_empty_symbol_returns_empty(self, db) -> None:
        out = get_series("NOPE.SS", "atr", db=db)
        assert out.empty

    def test_unknown_indicator_raises(self, db, seeded) -> None:
        with pytest.raises(ValueError):
            get_series("TEST.SS", "not_an_indicator", db=db)


class TestParamSets:
    def test_save_and_get_param_set(self, db) -> None:
        db.save_param_set("default", '{"atr_period": 20}', True, TREND_FORMULA_VERSION)
        row = db.get_param_set("default")
        assert row is not None
        assert row["is_default"] == 1
        assert row["formula_version"] == TREND_FORMULA_VERSION

    def test_default_flag_exclusive(self, db) -> None:
        db.save_param_set("default", "{}", True, 1)
        db.save_param_set("p_abc", "{}", True, 2)
        assert db.get_param_set("default")["is_default"] == 0
        assert db.get_param_set("p_abc")["is_default"] == 1
