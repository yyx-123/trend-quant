"""周/月K 表结构（market_data_{raw,qfq}_{weekly,monthly}）与 period 维度访问。

口径：周期表与日K逐列一致、互相隔离；period 参数默认 '1d' 保证既有日K
调用方行为不变；周期别名归一（'weekly'/'w'/'1w' 同一张表），分钟线 '1m'
必须被拒绝（否则会静默写进月表）。
"""

from __future__ import annotations

import sqlite3

import pandas as pd
import pytest


def _frame(rows: list[tuple[str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "time": time,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 1000.0,
                "amount": 1000.0 * price,
                "provider": "test",
            }
            for time, price in rows
        ]
    )


class TestPeriodTableResolution:
    def test_table_names(self, test_db) -> None:
        assert test_db._market_table("qfq") == "market_data_qfq"
        assert test_db._market_table("raw") == "market_data_raw"
        assert test_db._market_table("qfq", "1w") == "market_data_qfq_weekly"
        assert test_db._market_table("raw", "1w") == "market_data_raw_weekly"
        assert test_db._market_table("qfq", "1M") == "market_data_qfq_monthly"
        assert test_db._market_table("raw", "1M") == "market_data_raw_monthly"

    def test_aliases_resolve_to_same_table(self, test_db) -> None:
        for alias in ("1w", "w", "week", "weekly", "W"):
            assert test_db._market_table("qfq", alias) == "market_data_qfq_weekly"
        for alias in ("1M", "M", "month", "monthly"):
            assert test_db._market_table("qfq", alias) == "market_data_qfq_monthly"

    def test_minute_and_bad_period_rejected(self, test_db) -> None:
        with pytest.raises(ValueError):
            test_db._market_table("qfq", "1m")
        with pytest.raises(ValueError):
            test_db._market_table("qfq", "hour")
        with pytest.raises(ValueError):
            test_db._market_table("hfq")

    def test_all_period_tables_created(self, test_db) -> None:
        conn = sqlite3.connect(test_db.db_path)
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'market_data%'"
                )
            }
        finally:
            conn.close()
        assert names == {
            "market_data_qfq",
            "market_data_raw",
            "market_data_qfq_weekly",
            "market_data_raw_weekly",
            "market_data_qfq_monthly",
            "market_data_raw_monthly",
        }

    def test_version_name_includes_period(self, test_db) -> None:
        assert test_db.market_data_version_name("510300.SS") == "market_data_qfq:510300.SS"
        assert (
            test_db.market_data_version_name("510300.SS", "raw", "1w")
            == "market_data_raw_weekly:510300.SS"
        )


class TestPeriodMarketDataCRUD:
    def test_periods_are_isolated(self, test_db) -> None:
        test_db.save_market_data("AAA.SS", _frame([("2026-09-11", 10.0)]))
        test_db.save_market_data("AAA.SS", _frame([("2026-09-11", 20.0)]), period="1w")
        test_db.save_market_data("AAA.SS", _frame([("2026-08-31", 30.0)]), period="1M")

        daily = test_db.load_market_data("AAA.SS")
        weekly = test_db.load_market_data("AAA.SS", period="1w")
        monthly = test_db.load_market_data("AAA.SS", period="1M")
        assert float(daily.iloc[0]["close"]) == 10.0
        assert float(weekly.iloc[0]["close"]) == 20.0
        assert float(monthly.iloc[0]["close"]) == 30.0
        assert len(monthly) == 1

        summaries = test_db.list_market_data_summaries(period="1w")
        assert summaries["AAA.SS"]["rows"] == 1
        assert test_db.list_market_symbols(period="1M") == ["AAA.SS"]
        # 日K表不受周期写入影响
        assert test_db.list_market_data_summaries()["AAA.SS"]["rows"] == 1

    def test_raw_and_qfq_period_tables(self, test_db) -> None:
        test_db.save_market_data("BBB.SS", _frame([("2026-08-31", 1.0)]), price_mode="raw", period="1M")
        test_db.save_market_data("BBB.SS", _frame([("2026-08-31", 0.9)]), price_mode="qfq", period="1M")
        raw = test_db.get_market_data_summary("BBB.SS", price_mode="raw", period="1M")
        qfq = test_db.get_market_data_summary("BBB.SS", price_mode="qfq", period="1M")
        assert raw["rows"] == 1 and qfq["rows"] == 1
        assert float(test_db.load_market_data("BBB.SS", "raw", "1M").iloc[0]["close"]) == 1.0
        assert float(test_db.load_market_data("BBB.SS", "qfq", "1M").iloc[0]["close"]) == 0.9

    def test_save_is_upsert(self, test_db) -> None:
        test_db.save_market_data("CCC.SS", _frame([("2026-09-11", 5.0)]), period="1w")
        test_db.save_market_data("CCC.SS", _frame([("2026-09-11", 6.0)]), period="1w")
        loaded = test_db.load_market_data("CCC.SS", period="1w")
        assert len(loaded) == 1
        assert float(loaded.iloc[0]["close"]) == 6.0

    def test_clear_and_versions(self, test_db) -> None:
        test_db.save_market_data("DDD.SS", _frame([("2026-09-11", 5.0)]), period="1w")
        name = test_db.market_data_version_name("DDD.SS", "qfq", "1w")
        assert test_db.get_data_version(name) > 0
        assert test_db.get_data_version("market_data_qfq_weekly") > 0
        assert test_db.clear_market_data(period="1w") == 1
        assert test_db.load_market_data("DDD.SS", period="1w").empty

    def test_count_bars_by_symbol_period(self, test_db) -> None:
        test_db.save_market_data(
            "EEE.SS", _frame([("2026-08-31", 1.0), ("2026-09-11", 2.0)]), period="1M"
        )
        assert test_db.count_bars_by_symbol(period="1M") == {"EEE.SS": 2}


class TestSaveMany:
    """批量写（日更 800+ 标的走这条路：单连接单事务，而非逐标的建连接）。"""

    def test_writes_all_symbols_in_one_transaction(self, test_db) -> None:
        items = [
            ("M1.SS", _frame([("2026-09-11", 1.0)])),
            ("M2.SS", _frame([("2026-09-11", 2.0)])),
            ("M3.SS", _frame([("2026-08-31", 3.0), ("2026-09-11", 4.0)])),
        ]
        written = test_db.save_market_data_many(items, period="1w")
        assert written == {"M1.SS": 1, "M2.SS": 1, "M3.SS": 2}
        assert test_db.get_market_data_summary("M3.SS", "qfq", "1w")["rows"] == 2
        assert float(test_db.load_market_data("M2.SS", period="1w").iloc[0]["close"]) == 2.0

    def test_empty_and_blank_frames_are_skipped(self, test_db) -> None:
        items = [
            ("E1.SS", pd.DataFrame()),
            ("E2.SS", None),
            ("E3.SS", _frame([("2026-09-11", 1.0)])),
        ]
        assert test_db.save_market_data_many(items, period="1w") == {"E3.SS": 1}

    def test_versions_bumped_per_symbol_and_table(self, test_db) -> None:
        test_db.save_market_data_many(
            [("V1.SS", _frame([("2026-09-11", 1.0)])), ("V2.SS", _frame([("2026-09-11", 2.0)]))],
            period="1w",
        )
        for symbol in ("V1.SS", "V2.SS"):
            assert test_db.get_data_version(f"market_data_qfq_weekly:{symbol}") == 1
        assert test_db.get_data_version("market_data_qfq_weekly") == 1

    def test_non_positive_prices_dropped_per_symbol(self, test_db) -> None:
        """防御性拦截逐标的生效：脏标的不写入，同批其他标的照常落库。"""
        bad = _frame([("2026-09-11", -1.0)])
        good = _frame([("2026-09-11", 1.0)])
        written = test_db.save_market_data_many([("BAD.SS", bad), ("GOOD.SS", good)], period="1w")
        assert written == {"GOOD.SS": 1}
        assert test_db.load_market_data("BAD.SS", period="1w").empty

    def test_bulk_reader_matches_bulk_writer(self, test_db) -> None:
        test_db.save_market_data_many(
            [("R1.SS", _frame([("2026-09-04", 1.0), ("2026-09-11", 2.0)])),
             ("R2.SS", _frame([("2026-09-11", 3.0)]))],
            period="1w",
        )
        loaded = test_db.load_market_data_many(["R1.SS", "R2.SS", "MISSING.SS"], period="1w")
        assert set(loaded) == {"R1.SS", "R2.SS"}
        assert len(loaded["R1.SS"]) == 2
        assert test_db.get_market_data_summary_many(["R1.SS"], period="1w")["R1.SS"]["rows"] == 2


class TestMarketStorePeriod:
    def test_store_binds_period(self, test_db) -> None:
        from data.storage.market_store import MarketStore

        store = MarketStore(db=test_db, price_mode="qfq", period="1w")
        path = store.save_history("FFF.SS", _frame([("2026-09-11", 7.0)]))
        assert path.endswith("/1w/FFF.SS")
        assert store.list_stored_symbols() == ["FFF.SS"]
        assert float(store.load_history("FFF.SS").iloc[0]["close"]) == 7.0
        # 日K表里不应出现该标的
        assert test_db.load_market_data("FFF.SS").empty
