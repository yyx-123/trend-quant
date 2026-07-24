"""Unit tests for trend_mcp.server.symbol_detail intraday overlay gating.

Rules under test (mirrors the /market-view/api/daily overlay):
  1. Not a trading day / before 9:30 -> no overlay.
  2. Trading day past 9:30 and DB lacks today's bar -> synthesize one from
     live quotes (also covers the post-close window before the 16:30 daily
     write job persists today's bar).
  3. Trading day past 9:30 but DB already has today's bar -> use the DB
     data as-is; no quote fetch at all.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

import pandas as pd
import pytest

from trend_mcp import server


def _daily_bars_ending(end_day: date, rows: int = 80) -> pd.DataFrame:
    start = end_day - timedelta(days=rows - 1)
    items = []
    price = 1.0
    for idx in range(rows):
        day = start + timedelta(days=idx)
        price += 0.01
        open_price = price
        close_price = price + (0.01 if idx % 2 == 0 else -0.004)
        items.append(
            {
                "time": day.isoformat(),
                "open": open_price,
                "high": max(open_price, close_price) + 0.02,
                "low": min(open_price, close_price) - 0.02,
                "close": close_price,
                "volume": 100000 + idx * 1000,
                "amount": 200000 + idx * 1200,
            }
        )
    return pd.DataFrame(items)


FAKE_QUOTE = {
    "symbol": "518850.SS",
    "name": "Gold",
    "price": 2.0,
    "open": 1.9,
    "high": 2.1,
    "low": 1.8,
    "volume": 500000,
    "amount": 1000000,
    "ts": "2026-07-22T15:00:00",
}

FAKE_INTRADAY_RESULT = {
    "ok": True,
    "trend_score": 1.23,
    "price_direction": 1,
    "confidence": 0.5,
    "atr": 0.1,
    "price": 2.0,
    "ma_mid": 1.9,
    "calc_details": {},
}


class _FakeDb:
    def __init__(self, df: pd.DataFrame) -> None:
        self.df = df

    def load_market_data(self, symbol: str, price_mode: str = "qfq") -> pd.DataFrame:
        df = self.df.copy()
        df["time"] = pd.to_datetime(df["time"])
        return df

    def get_market_data_summary(self, symbol: str, price_mode: str = "qfq") -> dict:
        return {
            "rows": len(self.df),
            "start": str(self.df["time"].iloc[0]),
            "end": str(self.df["time"].iloc[-1]),
        }


def _call_symbol_detail(df: pd.DataFrame, *, past_open: bool):
    with (
        patch.object(server, "get_db", return_value=_FakeDb(df)),
        patch.object(server, "is_past_market_open", return_value=past_open),
        patch.object(server, "DataService") as mock_ds_cls,
        patch.object(server, "compute_intraday_trend_score", return_value=FAKE_INTRADAY_RESULT),
        patch.object(server, "_config_name_map", return_value={}),
        patch.object(server, "_load_instruments_raw", return_value=[]),
    ):
        mock_ds_cls.return_value.fetch_latest_quote.return_value = FAKE_QUOTE
        payload = server.symbol_detail("518850.SS", days=60, intraday=True)
    return payload, mock_ds_cls


class TestSymbolDetailIntradayOverlay:
    DAYS = 60  # symbol_detail tails output arrays to the requested window

    def test_before_open_no_overlay(self) -> None:
        """Rule 1: not past market open -> plain EOD data, no quote fetch."""
        df = _daily_bars_ending(date.today() - timedelta(days=1))

        payload, mock_ds_cls = _call_symbol_detail(df, past_open=False)

        assert payload["ok"] is True
        assert payload["meta"]["is_intraday"] is False
        assert len(payload["dates"]) == self.DAYS
        mock_ds_cls.assert_not_called()

    def test_past_open_missing_today_appends_synthetic_bar(self) -> None:
        """Rule 2: DB lacks today's bar -> append a synthetic one built
        from the live quote (intraday and post-close alike)."""
        df = _daily_bars_ending(date.today() - timedelta(days=1))

        payload, mock_ds_cls = _call_symbol_detail(df, past_open=True)

        assert payload["meta"]["is_intraday"] is True
        assert len(payload["dates"]) == self.DAYS + 1
        assert payload["dates"][-1] == date.today().isoformat()
        assert len(payload["candles"]["close"]) == self.DAYS + 1
        assert payload["candles"]["close"][-1] == pytest.approx(FAKE_QUOTE["price"])
        assert "trend_intraday" in payload["indicators"]
        mock_ds_cls.return_value.fetch_latest_quote.assert_called_once()

    def test_past_open_db_already_has_today_no_overlay(self) -> None:
        """Rule 3: DB already contains today's bar (write job done) -> no
        overlay, no quote fetch."""
        df = _daily_bars_ending(date.today())

        payload, mock_ds_cls = _call_symbol_detail(df, past_open=True)

        assert payload["meta"]["is_intraday"] is False
        assert len(payload["dates"]) == self.DAYS
        assert payload["dates"][-1] == date.today().isoformat()
        mock_ds_cls.assert_not_called()
