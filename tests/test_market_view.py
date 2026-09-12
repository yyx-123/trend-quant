from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

import unittest
from datetime import date, timedelta
from unittest.mock import patch

import pandas as pd
from conftest import FAKE_INTRADAY_TREND_RESULT, make_daily_bars_ending, make_fresh_quote
from fastapi import HTTPException

from app.routers import market_view
from app.routers.market_view import build_market_payload, compute_market_indicators
from core.trend import calculate_trend_score_snapshot
from data import intraday_service


def sample_daily_bars(rows: int = 80) -> pd.DataFrame:
    """与 conftest.make_daily_bars_ending 同一序列（起始 2026-01-01 的版本）。

    conftest 工厂以 end_day 收尾：end_day = 2026-01-01 + (rows-1) 天即得同一数据。
    """
    end = date(2026, 1, 1) + timedelta(days=rows - 1)
    return make_daily_bars_ending(end, rows=rows)

class MarketViewIndicatorTest(unittest.TestCase):
    def test_indicator_series_align_with_daily_bars(self) -> None:
        df = sample_daily_bars()

        indicators = compute_market_indicators(df)

        for group in ("ma", "atr", "boll", "macd", "bias", "volume_ma"):
            for series in indicators[group].values():
                self.assertEqual(len(series), len(df), group)
        self.assertIn("40", indicators["ma"])
        self.assertIn("20", indicators["atr"])
        self.assertEqual(len(indicators["rsi"]["series"]), len(df))
        self.assertEqual(indicators["rsi"]["period"], 14)
        # E-BIAS 节点是混合结构（序列 + 周期 + 参考线），仿 rsi 单独断言，
        # 不并入上面「纯序列字典」的 group 元组。
        self.assertEqual(len(indicators["e_bias"]["series"]), len(df))
        self.assertEqual(indicators["e_bias"]["period"], 20)
        self.assertEqual(
            [line["value"] for line in indicators["e_bias"]["lines"]],
            [15.0, 5.0, -5.0, 0.0],
        )
        self.assertEqual(len(indicators["trend"]["score"]), len(df))
        self.assertEqual(len(indicators["trend"]["ma"]["5"]), len(df))
        self.assertEqual(len(indicators["trend"]["ma"]["10"]), len(df))

    def test_build_market_payload_uses_one_timeline_for_all_series(self) -> None:
        df = sample_daily_bars()

        payload = build_market_payload("518850.SS", df, "Gold")

        self.assertEqual(payload["meta"]["rows"], len(df))
        self.assertEqual(len(payload["dates"]), len(df))
        self.assertEqual(len(payload["candles"]), len(df))
        self.assertEqual(len(payload["volumes"]), len(df))
        self.assertEqual(payload["display_name"], "Gold")
        indicators = payload["indicators"]
        for group_name in ("ma", "atr", "boll", "macd", "bias", "volume_ma"):
            for series in indicators[group_name].values():
                self.assertEqual(len(series), len(payload["dates"]), group_name)
        self.assertIn("40", indicators["ma"])
        self.assertIn("20", indicators["atr"])
        self.assertEqual(len(indicators["rsi"]["series"]), len(payload["dates"]))
        self.assertEqual(len(indicators["e_bias"]["series"]), len(payload["dates"]))
        self.assertEqual(len(indicators["trend"]["score"]), len(payload["dates"]))
        self.assertEqual(len(indicators["trend"]["ma"]["5"]), len(payload["dates"]))
        self.assertEqual(len(indicators["trend"]["ma"]["10"]), len(payload["dates"]))

    def test_build_market_payload_preserves_candlestick_ohlc_order(self) -> None:
        df = pd.DataFrame(
            [
                {
                    "time": "2026-06-17",
                    "open": 521.0,
                    "high": 586.04,
                    "low": 521.0,
                    "close": 586.04,
                    "volume": 568000,
                    "amount": 31598894526,
                }
            ]
        )

        payload = build_market_payload("603986.SS", df, "兆易创新")

        self.assertEqual(payload["candles"][0], [521.0, 586.04, 521.0, 586.04])

    def test_moving_average_waits_for_full_window(self) -> None:
        df = sample_daily_bars()

        payload = build_market_payload("518850.SS", df)
        ma20 = payload["indicators"]["ma"]["20"]
        ma40 = payload["indicators"]["ma"]["40"]

        self.assertTrue(all(value is None for value in ma20[:19]))
        self.assertIsNotNone(ma20[19])
        self.assertTrue(all(value is None for value in ma40[:39]))
        self.assertIsNotNone(ma40[39])

    def test_trend_indicator_aligns_and_respects_custom_core_periods(self) -> None:
        df = sample_daily_bars(100)

        payload = build_market_payload(
            "518850.SS",
            df,
            trend_cfg={"n_short": 3, "n_mid": 6, "n_long": 12, "atr_period": 8},
        )
        trend = payload["indicators"]["trend"]

        self.assertEqual(payload["meta"]["trend_config"]["n_short"], 3)
        self.assertEqual(payload["meta"]["trend_config"]["n_mid"], 6)
        self.assertEqual(payload["meta"]["trend_config"]["n_long"], 12)
        self.assertEqual(payload["meta"]["trend_config"]["atr_period"], 8)
        self.assertEqual(len(trend["score"]), len(df))
        self.assertTrue(all(value is None for value in trend["score"][:13]))
        self.assertTrue(any(value is not None for value in trend["score"][13:]))

    def test_trend_indicator_matches_snapshot_core_formula(self) -> None:
        df = sample_daily_bars(100)
        cfg = {"n_short": 3, "n_mid": 6, "n_long": 12, "atr_period": 8}

        payload = build_market_payload("518850.SS", df, trend_cfg=cfg)
        snapshot = calculate_trend_score_snapshot(df, cfg)

        self.assertAlmostEqual(
            payload["indicators"]["trend"]["score"][-1],
            round(float(snapshot["trend_score"]), 6),
            places=6,
        )

    def test_build_market_payload_adds_category_label(self) -> None:
        df = sample_daily_bars()

        payload = build_market_payload(
            "510300.SS",
            df,
            "CSI300",
            {
                "category_l1": "Broad",
                "category_l2": "Large Cap",
                "category_l3": "CSI300",
                "factor_tags": ["Value"],
            },
        )

        self.assertEqual(payload["meta"]["category_path"], "Broad-Large Cap-CSI300")
        self.assertIn("Broad-Large Cap-CSI300", payload["display_label"])

class FakeMarketViewDb:
    def __init__(
        self,
        df: pd.DataFrame,
        symbols: list[str] | None = None,
        metadata_map: dict[str, dict] | None = None,
        period_frames: dict[str, pd.DataFrame] | None = None,
    ) -> None:
        self.df = df
        self.symbols = symbols or ["518850.SS"]
        self.metadata_map = metadata_map or {}
        # 周期 → 该周期的行情（周期切换测试用）；缺省时任何周期都返回 df
        self.period_frames = period_frames or {}

    def load_market_data(self, symbol: str, price_mode: str = "qfq", period: str = "1d") -> pd.DataFrame:
        return self.period_frames.get(period, self.df).copy()

    def get_market_data_summary(
        self, symbol: str, price_mode: str = "qfq", period: str = "1d"
    ) -> dict:
        frame = self.period_frames.get(period, self.df)
        if frame.empty:
            return {"rows": 0, "start": None, "end": None}
        times = pd.to_datetime(frame["time"], errors="coerce").dropna()
        return {
            "rows": len(frame),
            "start": times.min().date().isoformat(),
            "end": times.max().date().isoformat(),
        }

    def get_instrument_metadata(self, symbol: str) -> dict | None:
        return self.metadata_map.get(symbol)

    def get_instrument_metadata_map(self) -> dict[str, dict]:
        return self.metadata_map

    def list_market_symbols(self) -> list[str]:
        return self.symbols

class MarketViewApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_daily_api_defaults_to_full_local_history(self) -> None:
        df = sample_daily_bars(1300)
        fake_db = FakeMarketViewDb(df)

        with patch.object(market_view, "get_db", return_value=fake_db):
            payload = await market_view.get_market_daily(
                symbol="518850.SS",
                limit=market_view.DEFAULT_LIMIT,
            )

        self.assertEqual(payload["meta"]["rows"], len(df))
        self.assertEqual(payload["meta"]["start"], "2026-01-01")
        self.assertEqual(payload["meta"]["end"], (date(2026, 1, 1) + timedelta(days=1299)).isoformat())

    async def test_daily_api_accepts_custom_trend_periods(self) -> None:
        df = sample_daily_bars(120)
        fake_db = FakeMarketViewDb(df)

        with patch.object(market_view, "get_db", return_value=fake_db):
            payload = await market_view.get_market_daily(
                symbol="518850.SS",
                limit=market_view.DEFAULT_LIMIT,
                trend_n_short=4,
                trend_n_mid=9,
                trend_n_long=18,
                trend_atr_period=11,
            )

        self.assertEqual(payload["meta"]["trend_config"]["n_short"], 4)
        self.assertEqual(payload["meta"]["trend_config"]["n_mid"], 9)
        self.assertEqual(payload["meta"]["trend_config"]["n_long"], 18)
        self.assertEqual(payload["meta"]["trend_config"]["atr_period"], 11)

    async def test_daily_api_accepts_custom_rsi_period(self) -> None:
        df = sample_daily_bars(80)
        fake_db = FakeMarketViewDb(df)

        with patch.object(market_view, "get_db", return_value=fake_db):
            payload = await market_view.get_market_daily(
                symbol="518850.SS",
                limit=market_view.DEFAULT_LIMIT,
                rsi_period=7,
                intraday=False,
            )

        self.assertEqual(payload["meta"]["rsi_config"]["period"], 7)
        self.assertEqual(payload["indicators"]["rsi"]["period"], 7)
        self.assertEqual(len(payload["indicators"]["rsi"]["series"]), len(df))

class MarketViewPeriodSwitchTest(unittest.IsolatedAsyncioTestCase):
    """周期切换：主图取该周期的 bar，副图指标按该周期 K 线重算。

    指标不读 indicator_daily 缓存，而是吃 build_market_payload 传入的 OHLCV，
    所以「切周期副图跟着切」是结构性的：同一函数喂不同周期的 K 线即可。
    """

    @staticmethod
    def _weekly_frame() -> pd.DataFrame:
        rows = []
        for idx in range(30):
            day = date(2026, 1, 9) + timedelta(days=7 * idx)
            price = 4.0 + 0.05 * idx
            rows.append(
                {
                    "time": day.isoformat(),
                    "open": price,
                    "high": price + 0.1,
                    "low": price - 0.1,
                    "close": price + 0.05,
                    "volume": 5_000_000,
                    "amount": 20_000_000,
                }
            )
        return pd.DataFrame(rows)

    async def test_weekly_uses_weekly_bars_and_recomputes_indicators(self) -> None:
        daily = sample_daily_bars(120)
        weekly = self._weekly_frame()
        fake_db = FakeMarketViewDb(daily, period_frames={"1w": weekly})

        with patch.object(market_view, "get_db", return_value=fake_db):
            weekly_payload = await market_view.get_market_daily(
                symbol="518850.SS", limit=market_view.DEFAULT_LIMIT, period="1w"
            )
            daily_payload = await market_view.get_market_daily(
                symbol="518850.SS", limit=market_view.DEFAULT_LIMIT
            )

        self.assertEqual(weekly_payload["meta"]["period"], "1w")
        self.assertEqual(weekly_payload["meta"]["period_label"], "周")
        self.assertTrue(weekly_payload["meta"]["only_closed_bars"])
        self.assertEqual(len(weekly_payload["dates"]), len(weekly))
        # 副图每个分组都按周K 长度重算（不是日K 的尾部切片）
        for group in ("ma", "atr", "boll", "macd", "bias", "e_bias", "volume_ma", "rsi", "trend"):
            node = weekly_payload["indicators"][group]
            if group == "rsi":
                self.assertEqual(len(node["series"]), len(weekly))
            elif group == "e_bias":
                self.assertEqual(len(node["series"]), len(weekly))
            elif group == "trend":
                self.assertEqual(len(node["score"]), len(weekly))
            else:
                for series in node.values():
                    self.assertEqual(len(series), len(weekly), group)
        # 与日K 的末 N 根不同值 —— 证明确实按周期重算而非沿用日K
        self.assertNotEqual(
            weekly_payload["indicators"]["ma"]["20"],
            daily_payload["indicators"]["ma"]["20"][-len(weekly):],
        )
        # 日K请求仍不受影响
        self.assertEqual(daily_payload["meta"]["period"], "1d")
        self.assertFalse(daily_payload["meta"]["only_closed_bars"])
        self.assertEqual(len(daily_payload["dates"]), len(daily))

    async def test_daily_span_always_comes_from_daily_table(self) -> None:
        """回测面板锚点：周/月视图下 daily_start/daily_end 仍是日K跨度。"""
        daily = sample_daily_bars(60)
        fake_db = FakeMarketViewDb(
            daily, period_frames={"1M": self._weekly_frame().head(3)}
        )

        with patch.object(market_view, "get_db", return_value=fake_db):
            payload = await market_view.get_market_daily(
                symbol="518850.SS", limit=market_view.DEFAULT_LIMIT, period="1M"
            )

        meta = payload["meta"]
        self.assertEqual(meta["daily_start"], "2026-01-01")
        self.assertEqual(meta["daily_end"], (date(2026, 1, 1) + timedelta(days=59)).isoformat())
        # 图上的日期是月K bar 的标注日，与 daily_end 不同
        self.assertNotEqual(meta["end"], meta["daily_end"])

    async def test_invalid_period_is_rejected(self) -> None:
        fake_db = FakeMarketViewDb(sample_daily_bars(60))
        with patch.object(market_view, "get_db", return_value=fake_db):
            for bad in ("1m", "hour"):
                with self.assertRaises(HTTPException) as ctx:
                    await market_view.get_market_daily(
                        symbol="518850.SS", limit=market_view.DEFAULT_LIMIT, period=bad
                    )
                self.assertEqual(ctx.exception.status_code, 400)

    async def test_intraday_overlay_skipped_for_weekly(self) -> None:
        """周/月不合成盘中 bar：intraday=true 也只返回已收盘周期。"""
        daily = sample_daily_bars(60)
        fake_db = FakeMarketViewDb(daily, period_frames={"1w": self._weekly_frame()})
        with patch.object(market_view, "get_db", return_value=fake_db):
            payload = await market_view.get_market_daily(
                symbol="518850.SS",
                limit=market_view.DEFAULT_LIMIT,
                period="1w",
                intraday=True,
            )
        self.assertFalse(payload["meta"]["is_intraday"])
        self.assertEqual(len(payload["dates"]), 30)


class MarketViewIntradayOverlayTest(unittest.IsolatedAsyncioTestCase):
    """Overlay gating for GET /market-view/api/daily?intraday=true.

    Rules under test:
      1. Not a trading day / before 9:30 -> no overlay.
      2. Trading day past 9:30 and DB lacks today's bar -> synthesize one
         from live quotes (covers the post-close window before the 16:30
         write job persists today's bar).
      3. Trading day past 9:30 but DB already has today's bar -> use the
         DB data as-is; no quote fetch at all.
    """

    async def _call_daily(self, df: pd.DataFrame, *, past_open: bool, quote=None):
        # quote 默认在体内生成：避免函数定义期（导入期）固化 ts 造成跨午夜 flake
        quote = quote if quote is not None else make_fresh_quote()
        fake_db = FakeMarketViewDb(df)
        with (
            patch.object(market_view, "get_db", return_value=fake_db),
            patch.object(intraday_service, "is_past_market_open", return_value=past_open),
            patch.object(intraday_service, "get_data_service") as mock_ds_cls,
            patch.object(
                intraday_service, "compute_intraday_trend_score", return_value=FAKE_INTRADAY_TREND_RESULT
            ),
        ):
            if isinstance(quote, Exception):
                mock_ds_cls.return_value.fetch_latest_quote.side_effect = quote
            else:
                mock_ds_cls.return_value.fetch_latest_quote.return_value = quote
            payload = await market_view.get_market_daily(
                symbol="518850.SS",
                start_date="",
                end_date="",
                limit=market_view.DEFAULT_LIMIT,
                trend_n_short=None,
                trend_n_mid=None,
                trend_n_long=None,
                trend_atr_period=None,
                rsi_period=14,
                intraday=True,
            )
        return payload, mock_ds_cls

    async def test_before_open_no_overlay(self) -> None:
        """Rule 1: not past market open -> plain EOD data, no quote fetch."""
        df = make_daily_bars_ending(date.today() - timedelta(days=1))

        payload, mock_ds_cls = await self._call_daily(df, past_open=False)

        self.assertFalse(payload["meta"]["is_intraday"])
        self.assertEqual(len(payload["dates"]), len(df))
        mock_ds_cls.assert_not_called()

    async def test_past_open_missing_today_appends_synthetic_bar(self) -> None:
        """Rule 2 (intraday or post-close pre-write): DB lacks today's bar
        -> append a synthetic one from the live quote."""
        df = make_daily_bars_ending(date.today() - timedelta(days=1))

        payload, mock_ds_cls = await self._call_daily(df, past_open=True)

        self.assertTrue(payload["meta"]["is_intraday"])
        self.assertEqual(len(payload["dates"]), len(df) + 1)
        self.assertEqual(payload["dates"][-1], date.today().isoformat())
        self.assertEqual(len(payload["candles"]), len(df) + 1)
        # The quote's own volume is preferred over the prev-day approximation.
        self.assertEqual(payload["volumes"][-1], make_fresh_quote()["volume"])
        self.assertIn("trend_intraday", payload["indicators"])
        mock_ds_cls.return_value.fetch_latest_quote.assert_called_once()

    async def test_intraday_recomputes_all_indicators_with_synth_bar(self) -> None:
        """同花顺-style: every indicator series gains a live value for today
        (history + synthetic bar), computed in memory only."""
        df = make_daily_bars_ending(date.today() - timedelta(days=1))

        payload, _ = await self._call_daily(df, past_open=True)

        indicators = payload["indicators"]
        expected_len = len(df) + 1
        # Series-aligned indicators all carry today's recomputed point.
        self.assertEqual(len(indicators["macd"]["dif"]), expected_len)
        self.assertIsNotNone(indicators["macd"]["dif"][-1])
        self.assertIsNotNone(indicators["macd"]["dea"][-1])
        self.assertIsNotNone(indicators["macd"]["bar"][-1])
        for period in ("5", "10", "20", "30", "40", "60"):
            self.assertEqual(len(indicators["ma"][period]), expected_len)
            self.assertIsNotNone(indicators["ma"][period][-1])
        self.assertIsNotNone(indicators["boll"]["mid"][-1])
        self.assertIsNotNone(indicators["rsi"]["series"][-1])
        self.assertIsNotNone(indicators["atr"]["20"][-1])
        self.assertIsNotNone(indicators["volume_ma"]["5"][-1])
        self.assertIsNotNone(indicators["volume_ma"]["10"][-1])
        # E-BIAS 同样含当日合成K线，且仍是百分比口径（序列 + 参考线结构）。
        self.assertEqual(len(indicators["e_bias"]["series"]), expected_len)
        self.assertIsNotNone(indicators["e_bias"]["series"][-1])
        self.assertEqual(indicators["e_bias"]["period"], 20)
        self.assertEqual(len(indicators["e_bias"]["lines"]), 4)
        # MA5 of today = mean of the last 4 historical closes + live price.
        hist_closes = list(df["close"].tail(4)) + [make_fresh_quote()["price"]]
        self.assertAlmostEqual(indicators["ma"]["5"][-1], sum(hist_closes) / 5, places=5)
        # E-BIAS 末位 == ln(实时价) − EMA(ln 收盘, 20)（含合成K线）× 100。
        from core.indicators import e_bias as _core_e_bias

        closes_with_today = list(df["close"]) + [make_fresh_quote()["price"]]
        self.assertAlmostEqual(
            indicators["e_bias"]["series"][-1],
            float(_core_e_bias(pd.Series(closes_with_today), 20).iloc[-1]) * 100,
            places=4,
        )
        # The fixed-semantics intraday trend snapshot is still injected.
        self.assertEqual(
            indicators["trend_intraday"]["score"], FAKE_INTRADAY_TREND_RESULT["trend_score"]
        )

    async def test_no_intraday_indicators_match_eod_lengths(self) -> None:
        """Without the overlay (before open), indicators stay EOD-only."""
        df = make_daily_bars_ending(date.today() - timedelta(days=1))

        payload, _ = await self._call_daily(df, past_open=False)

        self.assertFalse(payload["meta"]["is_intraday"])
        self.assertEqual(len(payload["indicators"]["macd"]["dif"]), len(df))
        self.assertNotIn("trend_intraday", payload["indicators"])

    async def test_past_open_db_already_has_today_no_overlay(self) -> None:
        """Rule 3: DB already contains today's bar (write job done) -> no
        overlay, no quote fetch."""
        df = make_daily_bars_ending(date.today())

        payload, mock_ds_cls = await self._call_daily(df, past_open=True)

        self.assertFalse(payload["meta"]["is_intraday"])
        self.assertEqual(len(payload["dates"]), len(df))
        self.assertEqual(payload["dates"][-1], date.today().isoformat())
        mock_ds_cls.assert_not_called()

    async def test_quote_failure_falls_back_to_eod(self) -> None:
        """Quote fetch error -> silent fallback to plain EOD data."""
        df = make_daily_bars_ending(date.today() - timedelta(days=1))

        payload, _mock_ds_cls = await self._call_daily(df, past_open=True, quote=RuntimeError("boom"))

        self.assertFalse(payload["meta"]["is_intraday"])
        self.assertEqual(len(payload["dates"]), len(df))

if __name__ == "__main__":
    unittest.main()
