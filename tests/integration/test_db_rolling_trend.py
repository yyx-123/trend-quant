"""滚动周/月趋势值表（trend_rolling_daily）的 DB 层。

口径：表随 _init_tables 建好；w/m 至少一个非 NaN 才成行（预热期双 NaN 行
不落库），单侧 NaN ↔ NULL 往返；replace 幂等整段重写；time 恒为 19 字符
'YYYY-MM-DD HH:MM:SS'；load_many 批量读取。
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd
import pytest


def _frame(rows: list[tuple]) -> pd.DataFrame:
    # time 统一用 pd.Timestamp（落库 19 字符）：混用 'YYYY-MM-DD' 字符串会让
    # 同一日期产生两个主键（upsert 失效的坑，与 market_data/fitted 表同）。
    return pd.DataFrame(
        [{"time": pd.Timestamp(d), "w_trend": w, "m_trend": m} for d, w, m in rows]
    )


class TestRollingTrendTable:
    def test_table_created(self, test_db) -> None:
        conn = sqlite3.connect(test_db.db_path)
        try:
            names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name = 'trend_rolling_daily'"
                ).fetchall()
            }
        finally:
            conn.close()
        assert names == {"trend_rolling_daily"}


class TestRollingTrendSaveLoad:
    def test_save_load_roundtrip_with_null(self, test_db) -> None:
        frame = _frame(
            [
                ("2026-09-07", np.nan, np.nan),  # 预热期双 NaN：不落库
                ("2026-09-08", 12.5, np.nan),    # m 仍在预热：NULL ↔ NaN
                ("2026-09-09", 13.0, -4.25),
            ]
        )
        written = test_db.save_rolling_trend_many([("AAA.SS", frame)])
        assert written == {"AAA.SS": 2}
        back = test_db.load_rolling_trend("AAA.SS")
        assert list(back.columns) == ["time", "w_trend", "m_trend"]
        assert len(back) == 2
        first = back.iloc[0]
        assert first["time"] == pd.Timestamp("2026-09-08")
        assert first["w_trend"] == pytest.approx(12.5)
        assert pd.isna(first["m_trend"])
        assert back.iloc[1]["m_trend"] == pytest.approx(-4.25)

    def test_load_with_date_range(self, test_db) -> None:
        frame = _frame(
            [("2026-09-07", 1.0, 1.0), ("2026-09-08", 2.0, 2.0), ("2026-09-09", 3.0, 3.0)]
        )
        test_db.save_rolling_trend_many([("AAA.SS", frame)])
        back = test_db.load_rolling_trend("AAA.SS", start="2026-09-08", end="2026-09-08")
        assert len(back) == 1
        assert back.iloc[0]["time"] == pd.Timestamp("2026-09-08")

    def test_time_stored_as_19char_text(self, test_db) -> None:
        test_db.save_rolling_trend_many([("AAA.SS", _frame([("2026-09-08", 12.5, 3.0)]))])
        conn = sqlite3.connect(test_db.db_path)
        try:
            rows = conn.execute(
                "SELECT time, LENGTH(time) FROM trend_rolling_daily"
            ).fetchall()
        finally:
            conn.close()
        assert rows == [("2026-09-08 00:00:00", 19)]

    def test_upsert_is_idempotent(self, test_db) -> None:
        frame = _frame([("2026-09-08", 12.5, 3.0)])
        test_db.save_rolling_trend_many([("AAA.SS", frame)])
        test_db.save_rolling_trend_many([("AAA.SS", frame)])
        assert len(test_db.load_rolling_trend("AAA.SS")) == 1

    def test_replace_rewrites_history_and_is_idempotent(self, test_db) -> None:
        frame = _frame([("2026-09-08", 12.5, 3.0), ("2026-09-09", 13.0, 4.0)])
        assert test_db.replace_rolling_trend("AAA.SS", frame) == 2
        assert test_db.replace_rolling_trend("AAA.SS", frame) == 2
        assert len(test_db.load_rolling_trend("AAA.SS")) == 2
        shorter = _frame([("2026-09-09", 1.0, 2.0)])
        assert test_db.replace_rolling_trend("AAA.SS", shorter) == 1
        back = test_db.load_rolling_trend("AAA.SS")
        assert [t.strftime("%Y-%m-%d") for t in back["time"]] == ["2026-09-09"]

    def test_replace_with_empty_clears(self, test_db) -> None:
        frame = _frame([("2026-09-08", 12.5, 3.0)])
        test_db.replace_rolling_trend("AAA.SS", frame)
        assert test_db.replace_rolling_trend("AAA.SS", pd.DataFrame()) == 0
        assert test_db.load_rolling_trend("AAA.SS").empty

    def test_symbols_isolated(self, test_db) -> None:
        test_db.save_rolling_trend_many([("AAA.SS", _frame([("2026-09-08", 12.5, 3.0)]))])
        assert test_db.load_rolling_trend("BBB.SS").empty


class TestRollingTrendLoadMany:
    def test_load_many(self, test_db) -> None:
        test_db.save_rolling_trend_many(
            [
                ("AAA.SS", _frame([("2026-09-08", 12.5, np.nan)])),
                ("BBB.SS", _frame([("2026-09-08", -3.0, 7.5), ("2026-09-09", -2.0, 8.0)])),
                ("EMPTY.SS", pd.DataFrame()),
            ]
        )
        out = test_db.load_rolling_trend_many(["AAA.SS", "BBB.SS", "NOPE.SS"])
        assert set(out) == {"AAA.SS", "BBB.SS"}
        assert len(out["AAA.SS"]) == 1
        assert pd.isna(out["AAA.SS"].iloc[0]["m_trend"])  # NULL → NaN
        assert len(out["BBB.SS"]) == 2
        assert out["BBB.SS"].iloc[1]["w_trend"] == pytest.approx(-2.0)

    def test_load_many_with_date_range(self, test_db) -> None:
        test_db.save_rolling_trend_many(
            [("BBB.SS", _frame([("2026-09-08", -3.0, 7.5), ("2026-09-09", -2.0, 8.0)]))]
        )
        out = test_db.load_rolling_trend_many(["BBB.SS"], start="2026-09-09")
        assert len(out["BBB.SS"]) == 1
        assert out["BBB.SS"].iloc[0]["time"] == pd.Timestamp("2026-09-09")
