"""Integration tests for data.storage.db — Database CRUD operations.

Uses an isolated SQLite database via the autouse ``isolate_get_db`` fixture
from ``tests/integration/conftest.py``.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest


class TestMarketDataCRUD:
    def test_save_and_load(self, test_db) -> None:
        df = pd.DataFrame([{
            "time": "2025-01-06",
            "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
            "volume": 1_000_000, "amount": 10_500_000, "provider": "test",
        }])
        test_db.save_market_data("TEST.SS", df, price_mode="qfq")
        loaded = test_db.load_market_data("TEST.SS", price_mode="qfq")
        assert len(loaded) == 1
        assert float(loaded.iloc[0]["close"]) == 10.5

    def test_separate_price_modes(self, test_db) -> None:
        qfq_df = pd.DataFrame([{
            "time": "2025-01-06", "open": 10.0, "high": 11.0,
            "low": 9.0, "close": 10.5, "volume": 1_000_000, "amount": 1_050_000,
            "provider": "test",
        }])
        raw_df = pd.DataFrame([{
            "time": "2025-01-06", "open": 12.0, "high": 13.0,
            "low": 11.0, "close": 12.5, "volume": 1_000_000, "amount": 1_250_000,
            "provider": "test",
        }])
        test_db.save_market_data("TEST2.SS", qfq_df, price_mode="qfq")
        test_db.save_market_data("TEST2.SS", raw_df, price_mode="raw")
        assert float(test_db.load_market_data("TEST2.SS", "qfq").iloc[0]["close"]) == 10.5
        assert float(test_db.load_market_data("TEST2.SS", "raw").iloc[0]["close"]) == 12.5

    def test_list_market_symbols(self, test_db) -> None:
        df = pd.DataFrame([{
            "time": "2025-01-06", "open": 10.0, "high": 11.0,
            "low": 9.0, "close": 10.5, "volume": 1_000_000, "amount": 1_050_000,
            "provider": "test",
        }])
        test_db.save_market_data("A.SS", df, price_mode="qfq")
        test_db.save_market_data("B.SZ", df, price_mode="qfq")
        symbols = test_db.list_market_symbols(price_mode="qfq")
        assert "A.SS" in symbols
        assert "B.SZ" in symbols

    def test_get_market_data_summary(self, test_db) -> None:
        df = pd.DataFrame([{
            "time": "2025-01-06", "open": 10.0, "high": 11.0,
            "low": 9.0, "close": 10.5, "volume": 1_000_000, "amount": 1_050_000,
            "provider": "test",
        }])
        test_db.save_market_data("SUM.SS", df, price_mode="qfq")
        summary = test_db.get_market_data_summary("SUM.SS", "qfq")
        assert summary["rows"] == 1
        assert summary["symbol"] == "SUM.SS" if "symbol" in summary else True


class TestMarketSymbolsCache:
    """list_market_symbols is cached in-process (the DISTINCT over ~1M rows is
    expensive on a cold page cache); writes must invalidate the cache."""

    @staticmethod
    def _df() -> pd.DataFrame:
        return pd.DataFrame([{
            "time": "2025-01-06", "open": 10.0, "high": 11.0,
            "low": 9.0, "close": 10.5, "volume": 1_000_000, "amount": 1_050_000,
            "provider": "test",
        }])

    def test_second_call_serves_from_cache(self, test_db) -> None:
        test_db.save_market_data("A.SS", self._df(), price_mode="qfq")
        first = test_db.list_market_symbols(price_mode="qfq")
        assert "A.SS" in first
        # Bypass save_market_data so no invalidation fires; the cached answer
        # must be served (and must be an independent copy).
        with test_db._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO market_data_qfq "
                "(symbol, time, open, high, low, close, volume, amount, provider) "
                "VALUES ('GHOST.SS', '2025-01-06', 1, 1, 1, 1, 1, 1, 'test')"
            )
        second = test_db.list_market_symbols(price_mode="qfq")
        assert "GHOST.SS" not in second
        first.append("MUTATED.SS")
        assert "MUTATED.SS" not in test_db.list_market_symbols(price_mode="qfq")

    def test_save_invalidates_cache(self, test_db) -> None:
        test_db.save_market_data("A.SS", self._df(), price_mode="qfq")
        assert "A.SS" in test_db.list_market_symbols(price_mode="qfq")
        test_db.save_market_data("NEW.SZ", self._df(), price_mode="qfq")
        assert "NEW.SZ" in test_db.list_market_symbols(price_mode="qfq")

    def test_clear_invalidates_cache(self, test_db) -> None:
        test_db.save_market_data("A.SS", self._df(), price_mode="qfq")
        assert "A.SS" in test_db.list_market_symbols(price_mode="qfq")
        test_db.clear_market_data(price_mode="qfq")
        assert "A.SS" not in test_db.list_market_symbols(price_mode="qfq")

    def test_price_modes_cached_independently(self, test_db) -> None:
        test_db.save_market_data("Q.SS", self._df(), price_mode="qfq")
        test_db.save_market_data("R.SS", self._df(), price_mode="raw")
        assert test_db.list_market_symbols(price_mode="qfq") == ["Q.SS"]
        assert test_db.list_market_symbols(price_mode="raw") == ["R.SS"]


class TestInstrumentMetadata:
    def test_save_and_list(self, test_db) -> None:
        test_db.save_instrument_metadata([
            {"symbol": "A.SS", "name": "Alpha", "category_l1": "宽基",
             "category_l2": "大盘", "category_l3": "沪深300", "sort_order": 1},
        ])
        items = test_db.list_instrument_metadata()
        assert len(items) == 1
        assert items[0]["symbol"] == "A.SS"

    def test_get_instrument_metadata_map(self, test_db) -> None:
        test_db.save_instrument_metadata([
            {"symbol": "A.SS", "name": "Alpha"},
            {"symbol": "B.SZ", "name": "Beta"},
        ])
        m = test_db.get_instrument_metadata_map()
        assert m["A.SS"]["name"] == "Alpha"
        assert m["B.SZ"]["name"] == "Beta"

    def test_instrument_categories(self, test_db) -> None:
        test_db.save_instrument_categories([
            {"path": "宽基", "level": 1, "name": "宽基", "priority": 1},
            {"path": "宽基-大盘", "level": 2, "name": "大盘", "parent_path": "宽基", "priority": 1},
        ])
        cats = test_db.list_instrument_categories()
        assert len(cats) == 2

    def test_replace_instrument_categories(self, test_db) -> None:
        test_db.save_instrument_categories([{"path": "old", "level": 1, "name": "Old", "priority": 1}])
        n = test_db.replace_instrument_categories([{"path": "new", "level": 1, "name": "New", "priority": 1}])
        assert n == 1
        cats = test_db.list_instrument_categories()
        assert cats[0]["path"] == "new"


class TestRuleStrategies:
    def test_save_and_list(self, test_db) -> None:
        strategy = {
            "schema_version": 1,
            "id": "test_rule",
            "name": "Test Rule",
            "trade_mode": "single_symbol_all_in",
        }
        test_db.save_rule_strategy(strategy)
        items = test_db.list_rule_strategies()
        assert any(s["id"] == "test_rule" for s in items)

    def test_get_and_delete(self, test_db) -> None:
        test_db.save_rule_strategy({"schema_version": 1, "id": "del_me", "name": "X", "trade_mode": "single_symbol_all_in"})
        assert test_db.get_rule_strategy("del_me") is not None
        assert test_db.delete_rule_strategy("del_me") is True
        assert test_db.get_rule_strategy("del_me") is None




class TestJobRuns:
    def test_record_and_get_latest(self, test_db) -> None:
        test_db.record_job_run("daily_update", {"status": "ok", "success": 600}, run_date="2026-07-18")
        test_db.record_job_run("daily_update", {"status": "ok", "success": 607}, run_date="2026-07-19")
        latest = test_db.get_latest_job_run("daily_update")
        assert latest is not None
        assert latest["payload"]["success"] == 607
        assert latest["run_date"] == "2026-07-19"

    def test_get_latest_empty(self, test_db) -> None:
        assert test_db.get_latest_job_run("nope") is None

    def test_list_job_runs(self, test_db) -> None:
        for i in range(3):
            test_db.record_job_run("instrument_add", {"seq": i})
        runs = test_db.list_job_runs("instrument_add", limit=2)
        assert len(runs) == 2
        assert runs[0]["payload"]["seq"] == 2  # newest first


class TestAppConfig:
    def test_set_and_get(self, test_db) -> None:
        test_db.set_config("strategy", {"atr_period": 20, "weights": [0.4, 0.4, 0.2]})
        cfg = test_db.get_config("strategy")
        assert cfg["atr_period"] == 20
        assert cfg["weights"] == [0.4, 0.4, 0.2]

    def test_get_default(self, test_db) -> None:
        assert test_db.get_config("missing", default={}) == {}

    def test_upsert_overwrites(self, test_db) -> None:
        test_db.set_config("k", {"v": 1})
        test_db.set_config("k", {"v": 2})
        assert test_db.get_config("k")["v"] == 2

    def test_get_all_config(self, test_db) -> None:
        test_db.set_config("a", {"x": 1})
        test_db.set_config("b", {"y": 2})
        all_cfg = test_db.get_all_config()
        assert all_cfg["a"] == {"x": 1}
        assert all_cfg["b"] == {"y": 2}
