"""Integration tests for the bulk DB readers and indicator_store.get_series_bulk.

批量读取方法（load_market_data_many / load_indicator_daily_many /
indicator_cache_info_many / get_market_data_summary_many / get_data_versions）
是 calc_stop_loss_batch 的 IO 底座：断言它们与逐标的版本结果完全一致。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.indicators import INDICATOR_FORMULA_VERSION
from core.strategy_config import DEFAULT_STRATEGY_CONFIG
from data.indicator_store import (
    compute_indicator_frame,
    get_series,
    get_series_bulk,
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
def two_symbols(db):
    bars_a = _make_bars(seed=1)
    bars_b = _make_bars(seed=7)
    db.save_market_data("AAA.SS", bars_a, price_mode="qfq")
    db.save_market_data("BBB.SS", bars_b, price_mode="qfq")
    return bars_a, bars_b


class TestLoadMarketDataMany:
    def test_matches_single_loads(self, db, two_symbols) -> None:
        bars_a, bars_b = two_symbols
        out = db.load_market_data_many(["AAA.SS", "BBB.SS", "MISSING.SS"])
        assert set(out) == {"AAA.SS", "BBB.SS"}  # 无数据的 symbol 不出现
        pd.testing.assert_frame_equal(
            out["AAA.SS"], db.load_market_data("AAA.SS")
        )
        pd.testing.assert_frame_equal(
            out["BBB.SS"], db.load_market_data("BBB.SS")
        )
        # 内容确实是原始数据（行数与收盘价一致）
        assert len(out["AAA.SS"]) == len(bars_a)

    def test_dedup_and_normalize(self, db, two_symbols) -> None:
        out = db.load_market_data_many(["aaa.ss", "AAA.SS", "AAA.SS", ""])
        assert list(out) == ["AAA.SS"]

    def test_empty_input(self, db) -> None:
        assert db.load_market_data_many([]) == {}


class TestGetMarketDataSummaryMany:
    def test_matches_single(self, db, two_symbols) -> None:
        out = db.get_market_data_summary_many(["AAA.SS", "BBB.SS", "MISSING.SS"])
        assert set(out) == {"AAA.SS", "BBB.SS"}
        for symbol in ("AAA.SS", "BBB.SS"):
            single = db.get_market_data_summary(symbol)
            assert out[symbol]["rows"] == single["rows"]
            assert str(out[symbol]["end"]) == str(single["end"])


class TestGetDataVersions:
    def test_versions_after_writes(self, db, two_symbols) -> None:
        names = [
            db.market_data_version_name("AAA.SS"),
            db.market_data_version_name("BBB.SS"),
            db.market_data_version_name("MISSING.SS"),
        ]
        out = db.get_data_versions(names)
        assert out[names[0]] == db.get_data_version(names[0]) > 0
        assert out[names[1]] == db.get_data_version(names[1]) > 0
        assert names[2] not in out  # 从未写入的名称缺省（调用方按 0 处理）


class TestLoadIndicatorDailyMany:
    def test_columns_and_content(self, db, two_symbols) -> None:
        bars_a, _ = two_symbols
        ind = compute_indicator_frame(bars_a, DEFAULT_STRATEGY_CONFIG)
        db.save_indicator_daily("AAA.SS", ind, formula_version=INDICATOR_FORMULA_VERSION)

        out = db.load_indicator_daily_many(["AAA.SS", "BBB.SS"], columns=("time", "atr"))
        assert set(out) == {"AAA.SS"}
        frame = out["AAA.SS"]
        assert set(frame.columns) == {"symbol", "time", "atr"}  # 只取需要的列
        full = db.load_indicator_daily("AAA.SS")
        pd.testing.assert_series_equal(
            pd.to_numeric(frame["atr"], errors="coerce"),
            pd.to_numeric(full["atr"], errors="coerce"),
            check_names=False,
        )

    def test_invalid_column_rejected(self, db) -> None:
        with pytest.raises(ValueError):
            db.load_indicator_daily_many(["AAA.SS"], columns=("time", "atr; DROP TABLE x"))


class TestIndicatorCacheInfoMany:
    def test_matches_single(self, db, two_symbols) -> None:
        bars_a, _ = two_symbols
        ind = compute_indicator_frame(bars_a, DEFAULT_STRATEGY_CONFIG)
        db.save_indicator_daily("AAA.SS", ind, formula_version=INDICATOR_FORMULA_VERSION)

        out = db.indicator_cache_info_many(["AAA.SS", "BBB.SS"])
        single = db.indicator_cache_info("AAA.SS")
        for key in (
            "indicator_rows",
            "indicator_last",
            "indicator_version",
            "indicator_data_version",
            "trend_rows",
        ):
            assert out["AAA.SS"][key] == single[key], key
        assert "BBB.SS" not in out  # 无缓存行的 symbol 不出现


class TestGetSeriesBulk:
    def test_matches_single_get_series(self, db, two_symbols) -> None:
        bars_a, bars_b = two_symbols
        ind = compute_indicator_frame(bars_a, DEFAULT_STRATEGY_CONFIG)
        db.save_indicator_daily("AAA.SS", ind, formula_version=INDICATOR_FORMULA_VERSION)
        # BBB.SS 无指标缓存 → live 回退（走 bars_map，不再读库）

        dfs = db.load_market_data_many(["AAA.SS", "BBB.SS"])
        bulk = get_series_bulk(["AAA.SS", "BBB.SS", "MISSING.SS"], "atr", db=db, bars_map=dfs)

        for symbol in ("AAA.SS", "BBB.SS"):
            single = get_series(symbol, "atr", db=db)
            pd.testing.assert_series_equal(
                bulk[symbol].reset_index(drop=True),
                single.reset_index(drop=True),
                check_names=False,
            )
        assert bulk["MISSING.SS"].empty

    def test_trend_column_rejected(self, db) -> None:
        with pytest.raises(ValueError):
            get_series_bulk(["AAA.SS"], "trend_score", db=db)

    def test_empty_input(self, db) -> None:
        assert get_series_bulk([], "atr", db=db) == {}
