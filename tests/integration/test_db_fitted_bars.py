"""拟合周/月K 表（market_data_qfq_*_fitted）的 DB 层。

口径：两张表随 _init_tables 建好、互相隔离；save/load/replace 语义与
market_data 一致；provider 恒为 'local_fitted'；period_start 列原样往返；
日周期与分钟线必须被拒绝。
"""

from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from core.bars import fitted_period_rows


def _daily(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"time": d, "open": p, "high": p + 0.1, "low": p - 0.1, "close": p,
             "volume": v, "amount": v * p}
            for d, p, v in rows
        ]
    )


def _seed(test_db, symbol: str = "AAA.SS") -> pd.DataFrame:
    daily = _daily(
        [
            ("2026-09-07", 10.0, 100.0),
            ("2026-09-08", 11.0, 200.0),
            ("2026-09-14", 12.0, 300.0),
        ]
    )
    test_db.save_market_data(symbol, daily, price_mode="qfq")
    return fitted_period_rows(daily, "1w")


class TestFittedTableResolution:
    def test_table_names(self, test_db) -> None:
        assert test_db._fitted_table("1w") == "market_data_qfq_weekly_fitted"
        assert test_db._fitted_table("1M") == "market_data_qfq_monthly_fitted"
        assert test_db._fitted_table("weekly") == "market_data_qfq_weekly_fitted"

    def test_daily_and_minute_rejected(self, test_db) -> None:
        with pytest.raises(ValueError):
            test_db._fitted_table("1d")
        with pytest.raises(ValueError):
            test_db._fitted_table("1m")

    def test_tables_created(self, test_db) -> None:
        conn = sqlite3.connect(test_db.db_path)
        try:
            names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%fitted%'"
                ).fetchall()
            }
        finally:
            conn.close()
        assert "market_data_qfq_weekly_fitted" in names
        assert "market_data_qfq_monthly_fitted" in names


class TestFittedSaveLoad:
    def test_save_load_roundtrip(self, test_db) -> None:
        fitted = _seed(test_db)
        written = test_db.save_fitted_period_bars("AAA.SS", fitted, "1w")
        assert written == 3
        back = test_db.load_fitted_period_bars("AAA.SS", "1w")
        assert len(back) == 3
        row = back.iloc[1]
        assert row["period_start"] == "2026-09-07"
        assert row["open"] == pytest.approx(10.0)
        assert row["volume"] == pytest.approx(300.0)
        assert row["provider"] == "local_fitted"

    def test_load_with_date_range(self, test_db) -> None:
        test_db.save_fitted_period_bars("AAA.SS", _seed(test_db), "1w")
        back = test_db.load_fitted_period_bars("AAA.SS", "1w", start="2026-09-08", end="2026-09-08")
        assert len(back) == 1
        assert back.iloc[0]["time"].strftime("%Y-%m-%d") == "2026-09-08"

    def test_upsert_is_idempotent(self, test_db) -> None:
        fitted = _seed(test_db)
        test_db.save_fitted_period_bars("AAA.SS", fitted, "1w")
        test_db.save_fitted_period_bars("AAA.SS", fitted, "1w")
        assert len(test_db.load_fitted_period_bars("AAA.SS", "1w")) == 3

    def test_periods_isolated(self, test_db) -> None:
        daily = _daily([("2026-09-07", 10.0, 100.0)])
        test_db.save_market_data("AAA.SS", daily, price_mode="qfq")
        test_db.save_fitted_period_bars("AAA.SS", fitted_period_rows(daily, "1w"), "1w")
        assert test_db.load_fitted_period_bars("AAA.SS", "1M").empty

    def test_replace_rewrites_history(self, test_db) -> None:
        test_db.save_fitted_period_bars("AAA.SS", _seed(test_db), "1w")
        shorter = _seed(test_db).head(1)
        written = test_db.replace_fitted_period_bars("AAA.SS", shorter, "1w")
        assert written == 1
        assert len(test_db.load_fitted_period_bars("AAA.SS", "1w")) == 1

    def test_replace_with_empty_clears(self, test_db) -> None:
        test_db.save_fitted_period_bars("AAA.SS", _seed(test_db), "1w")
        test_db.replace_fitted_period_bars("AAA.SS", pd.DataFrame(), "1w")
        assert test_db.load_fitted_period_bars("AAA.SS", "1w").empty

    def test_nonpositive_prices_dropped(self, test_db) -> None:
        fitted = _seed(test_db)
        fitted.loc[0, "close"] = -1.0
        written = test_db.save_fitted_period_bars("AAA.SS", fitted, "1w")
        assert written == 2  # 非正价格行被拦截（与 market_data 同防御）


class TestFittedSaveMany:
    def test_save_many(self, test_db) -> None:
        daily = _daily([("2026-09-07", 10.0, 100.0)])
        test_db.save_market_data("AAA.SS", daily, price_mode="qfq")
        test_db.save_market_data("BBB.SS", daily, price_mode="qfq")
        items = [
            ("AAA.SS", fitted_period_rows(daily, "1w")),
            ("BBB.SS", fitted_period_rows(daily, "1w")),
            ("EMPTY.SS", pd.DataFrame()),
        ]
        written = test_db.save_fitted_period_bars_many(items, "1w")
        assert written == {"AAA.SS": 1, "BBB.SS": 1}
        assert len(test_db.load_fitted_period_bars("AAA.SS", "1w")) == 1
        assert len(test_db.load_fitted_period_bars("BBB.SS", "1w")) == 1
