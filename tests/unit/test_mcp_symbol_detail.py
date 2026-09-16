"""Unit tests for symbol_detail intraday overlay gating.

Rules under test (the overlay itself lives in
``data.intraday_service.build_intraday_overlay``, shared with the
/market-view/api/daily endpoint; the gate/quote/trend collaborators are
therefore patched in that module's namespace; the payload assembly lives in
``services.symbol_detail.symbol_detail_payload`` and its DB/name dependencies
are patched in that module's namespace):
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

from data import intraday_service
from services import symbol_detail as symbol_detail_service

pytest.importorskip("mcp")

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


def _fake_quote() -> dict:
    return {
        "symbol": "518850.SS",
        "name": "Gold",
        "price": 2.0,
        "open": 1.9,
        "high": 2.1,
        "low": 1.8,
        "volume": 500000,
        "amount": 1000000,
        # ts 必须是“今天”——陈旧报价（如停牌股的上一交易日快照）会被
        # is_quote_fresh 拒绝，不合成当日K线。
        "ts": date.today().isoformat() + "T15:00:00",
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
    def __init__(self, df: pd.DataFrame, rolling: pd.DataFrame | None = None) -> None:
        self.df = df
        self.rolling = rolling if rolling is not None else pd.DataFrame()

    def load_market_data(self, symbol: str, price_mode: str = "qfq") -> pd.DataFrame:
        df = self.df.copy()
        df["time"] = pd.to_datetime(df["time"])
        return df

    def load_rolling_trend(self, symbol: str, start=None, end=None) -> pd.DataFrame:
        return self.rolling.copy()

    def get_market_data_summary(self, symbol: str, price_mode: str = "qfq") -> dict:
        return {
            "rows": len(self.df),
            "start": str(self.df["time"].iloc[0]),
            "end": str(self.df["time"].iloc[-1]),
        }


def _call_symbol_detail(df: pd.DataFrame, *, past_open: bool):
    with (
        patch.object(symbol_detail_service, "get_db", return_value=_FakeDb(df)),
        patch.object(intraday_service, "is_past_market_open", return_value=past_open),
        patch.object(intraday_service, "get_data_service") as mock_ds_cls,
        patch.object(intraday_service, "compute_intraday_trend_score", return_value=FAKE_INTRADAY_RESULT),
        patch.object(symbol_detail_service, "load_instrument_name_map", return_value={}),
    ):
        mock_ds_cls.return_value.fetch_latest_quote.return_value = _fake_quote()
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
        assert payload["candles"]["close"][-1] == pytest.approx(_fake_quote()["price"])
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

    def test_stale_quote_no_overlay(self) -> None:
        """Rule 4: quote carries an old trade date (e.g. suspended symbol)
        -> no synthetic bar, fall back to EOD even though the gate passed."""
        df = _daily_bars_ending(date.today() - timedelta(days=1))
        stale_quote = dict(_fake_quote(), ts=(date.today() - timedelta(days=3)).isoformat() + "T15:00:00")

        with (
            patch.object(symbol_detail_service, "get_db", return_value=_FakeDb(df)),
            patch.object(intraday_service, "is_past_market_open", return_value=True),
            patch.object(intraday_service, "get_data_service") as mock_ds_cls,
            patch.object(intraday_service, "compute_intraday_trend_score", return_value=FAKE_INTRADAY_RESULT),
            patch.object(symbol_detail_service, "load_instrument_name_map", return_value={}),
        ):
            mock_ds_cls.return_value.fetch_latest_quote.return_value = stale_quote
            payload = server.symbol_detail("518850.SS", days=60, intraday=True)

        assert payload["meta"]["is_intraday"] is False
        assert len(payload["dates"]) == self.DAYS


class TestSymbolDetailContract:
    """services.symbol_detail.symbol_detail_payload 的基础契约（截尾 / 错误）。"""

    def test_days_tail(self) -> None:
        df = _daily_bars_ending(date(2026, 8, 27), rows=100)
        with (
            patch.object(symbol_detail_service, "get_db", return_value=_FakeDb(df)),
            patch.object(symbol_detail_service, "load_instrument_name_map", return_value={}),
        ):
            payload = symbol_detail_service.symbol_detail_payload("518850.SS", days=5)
        assert payload["ok"] is True
        assert len(payload["dates"]) == 5
        expected = [str(d.date()) for d in pd.to_datetime(df["time"])][-5:]
        assert payload["dates"] == expected
        assert payload["meta"]["is_intraday"] is False
        # E-BIAS 契约：与其余指标组一致 —— series 为全历史长度（symbol_detail
        # 只截尾 dates/candles，indicators 各组一律全量返回），周期/参考线为
        # 短配置字段。
        total = len(df)
        e_bias = payload["indicators"]["e_bias"]
        assert len(e_bias["series"]) == total
        assert len(payload["indicators"]["bias"]["6"]) == total
        assert e_bias["period"] == 20
        assert [line["value"] for line in e_bias["lines"]] == [15.0, 5.0, -5.0, 0.0]

    def test_empty_symbol_error(self) -> None:
        assert symbol_detail_service.symbol_detail_payload("")["ok"] is False

    def test_no_data_error(self) -> None:
        empty = pd.DataFrame(columns=["time", "close"])
        with patch.object(symbol_detail_service, "get_db", return_value=_FakeDb(empty)):
            payload = symbol_detail_service.symbol_detail_payload("999999.SS")
        assert payload["ok"] is False
        assert "未找到" in payload["error"]


class TestSymbolDetailTrendRolling:
    """indicators.trend_rolling 契约：滚动周/月趋势值（trend_rolling_daily）。

    与 indicators 其余各组同契约——全历史长度、按全历史日K日期轴对齐
    （不随 days 截尾），无滚动行的日期（预热期）为 None；标的完全没有
    滚动数据时不输出该组。与 Web 日K 接口共用
    services.market_indicators.align_rolling_trend。
    """

    def _rolling_rows(self, df: pd.DataFrame, idx_values: dict[int, tuple]) -> pd.DataFrame:
        times = pd.to_datetime(df["time"])
        return pd.DataFrame(
            {
                "time": [times[i] for i in idx_values],
                "w_trend": [v[0] for v in idx_values.values()],
                "m_trend": [v[1] for v in idx_values.values()],
            }
        )

    def _payload(self, df: pd.DataFrame, rolling: pd.DataFrame | None, **kwargs) -> dict:
        with (
            patch.object(symbol_detail_service, "get_db", return_value=_FakeDb(df, rolling)),
            patch.object(symbol_detail_service, "load_instrument_name_map", return_value={}),
        ):
            return symbol_detail_service.symbol_detail_payload("518850.SS", **kwargs)

    def test_aligned_full_history_axis(self) -> None:
        df = _daily_bars_ending(date(2026, 8, 27), rows=100)
        rolling = self._rolling_rows(df, {0: (1.5, 2.5), 50: (3.123456789, None), 99: (-4.0, 5.0)})

        payload = self._payload(df, rolling, days=5)

        node = payload["indicators"]["trend_rolling"]
        # 全历史长度（与 trend.score 同轴），不受 days=5 截尾影响
        assert len(node["weekly"]) == len(df)
        assert len(node["monthly"]) == len(df)
        assert node["weekly"][0] == 1.5
        assert node["monthly"][0] == 2.5
        # 无滚动行的日期 → None；数值按 number6_or_none 口径保留 6 位小数
        assert node["weekly"][1] is None
        assert node["weekly"][50] == 3.123457
        assert node["monthly"][50] is None
        assert node["weekly"][-1] == -4.0
        assert node["monthly"][-1] == 5.0

    def test_no_rolling_rows_key_absent(self) -> None:
        df = _daily_bars_ending(date(2026, 8, 27), rows=100)

        payload = self._payload(df, None, days=5)

        assert "trend_rolling" not in payload["indicators"]

    def test_intraday_bar_not_in_rolling_axis(self) -> None:
        """盘中合成 bar 只追加到 dates/candles：trend_rolling 仍锚定全历史
        EOD 轴（与其余指标组一致），长度不含当日合成 bar。"""
        df = _daily_bars_ending(date.today() - timedelta(days=1), rows=80)
        rolling = self._rolling_rows(df, {79: (2.0, 3.0)})
        with (
            patch.object(symbol_detail_service, "get_db", return_value=_FakeDb(df, rolling)),
            patch.object(intraday_service, "is_past_market_open", return_value=True),
            patch.object(intraday_service, "get_data_service") as mock_ds_cls,
            patch.object(intraday_service, "compute_intraday_trend_score", return_value=FAKE_INTRADAY_RESULT),
            patch.object(symbol_detail_service, "load_instrument_name_map", return_value={}),
        ):
            mock_ds_cls.return_value.fetch_latest_quote.return_value = _fake_quote()
            payload = server.symbol_detail("518850.SS", days=60, intraday=True)

        node = payload["indicators"]["trend_rolling"]
        assert payload["meta"]["is_intraday"] is True
        assert len(node["weekly"]) == len(df)
        assert node["weekly"][-1] == 2.0
