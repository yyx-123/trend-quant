"""Integration tests for intraday_service — the module that had last night's bugs.

These tests specifically verify:
- Bug 1: trend_history / trend_dates must NOT be empty (sparklines work)
- Bug 2: trend_ma5 must differ from trend_score (MA5 is a smoothed average)

This module was the blind spot — 0 % tested before, now covered.
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from data.intraday_service import (
    _ma5,
    _number,
    _priority,
    build_synthetic_bar,
    compute_intraday_trend_score,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_hist(n_days: int = 90) -> pd.DataFrame:
    """Generate synthetic daily OHLCV data."""
    rng = np.random.default_rng(42)
    base = date(2025, 8, 1)
    records = []
    price = 10.0
    for i in range(n_days):
        day = base + pd.Timedelta(days=i)
        if day.weekday() >= 5:
            continue
        change = 0.01 + rng.normal(0, 0.008)
        close = price * (1 + change)
        records.append({
            "time": day,
            "open": price,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1_000_000 + i * 10_000,
            "amount": (1_000_000 + i * 10_000) * close,
        })
        price = close
    return pd.DataFrame(records)


def _make_quote(price: float = 12.0) -> dict:
    return {
        "symbol": "TEST.SS",
        "name": "Test ETF",
        "price": price,
        "open": price * 0.99,
        "high": price * 1.02,
        "low": price * 0.98,
        "volume": 500_000,
        "amount": price * 500_000,
        "time": datetime.now().isoformat(),
    }


def _default_cfg() -> dict:
    return {
        "n_short": 3, "n_mid": 5, "n_long": 8,
        "atr_period": 20, "vol_ma_period": 20, "er_period": 10,
        "w_bias_short": 0.4, "w_bias_mid": 0.4, "w_bias_long": 0.2,
        "w_slope_short": 0.4, "w_slope_mid": 0.4, "w_slope_long": 0.2,
        "w_bias_norm": 0.5, "w_slope_norm": 0.5,
        "w_vol": 0.3, "w_er": 0.7,
    }


# ---------------------------------------------------------------------------
# Pure-function unit tests (fast, no DB needed)
# ---------------------------------------------------------------------------

class TestNumber:
    def test_valid_float(self) -> None:
        assert _number(3.14) == 3.14

    def test_int(self) -> None:
        assert _number(42) == 42.0

    def test_none(self) -> None:
        assert _number(None) is None

    def test_nan(self) -> None:
        assert _number(float("nan")) is None

    def test_string(self) -> None:
        assert _number("2.5") == 2.5

    def test_invalid_string(self) -> None:
        assert _number("abc") is None


class TestPriority:
    def test_returns_int(self) -> None:
        assert _priority(3) == 3

    def test_returns_int_from_string(self) -> None:
        assert _priority("5") == 5

    def test_invalid_returns_large(self) -> None:
        assert _priority(None) == 999999
        assert _priority("abc") == 999999


class TestBuildSyntheticBar:
    def test_all_fields_present(self) -> None:
        quote = _make_quote(11.5)
        bar = build_synthetic_bar(quote, 1_000_000)
        for key in ("time", "open", "high", "low", "close", "volume", "amount"):
            assert key in bar, f"Missing key: {key}"

    def test_close_equals_quote_price(self) -> None:
        quote = _make_quote(11.5)
        bar = build_synthetic_bar(quote, 1_000_000)
        assert bar["close"] == 11.5

    def test_high_not_less_than_close(self) -> None:
        quote = _make_quote(10.0)
        quote["high"] = 9.0  # lower than price
        bar = build_synthetic_bar(quote, 1_000_000)
        assert bar["high"] >= bar["close"]

    def test_volume_from_prev(self) -> None:
        quote = _make_quote(10.0)
        bar = build_synthetic_bar(quote, 2_000_000)
        assert bar["volume"] == 2_000_000


class TestMA5:
    def test_single_value_returns_none(self) -> None:
        """With only 1 value, min_periods=5 → all entries are None."""
        result = _ma5([10.0])
        assert len(result) == 1
        assert result[0] is None

    def test_five_values(self) -> None:
        result = _ma5([10.0, 11.0, 12.0, 13.0, 14.0])
        # First 4 are None (min_periods=5), 5th = mean(10..14) = 12.0
        assert result[:4] == [None, None, None, None]
        assert result[4] == pytest.approx(12.0)

    def test_with_none_mixed(self) -> None:
        """With None (→NaN), rolling mean with min_periods=5 returns NaN → None."""
        result = _ma5([10.0, None, 12.0, 13.0, 14.0])
        assert len(result) == 5
        assert result[4] is None  # NaN → _number → None

    def test_empty_list(self) -> None:
        assert _ma5([]) == []


class TestComputeIntradayTrendScore:
    def test_returns_snapshot_structure(self) -> None:
        hist = _make_hist(60)
        quote = _make_quote(12.0)
        cfg = _default_cfg()
        result = compute_intraday_trend_score(hist, quote, cfg)
        assert result["ok"] is True
        assert "trend_score" in result
        assert "is_intraday" in result
        assert result["is_intraday"] is True

    def test_insufficient_history(self) -> None:
        hist = _make_hist(3)  # only 3 bars — not enough for trend score
        quote = _make_quote(12.0)
        cfg = _default_cfg()
        result = compute_intraday_trend_score(hist, quote, cfg)
        assert result["ok"] is False
        assert "insufficient" in result["reason"]


# ---------------------------------------------------------------------------
# Integration test: build_intraday_dashboard
# ---------------------------------------------------------------------------

class TestBuildIntradayDashboard:
    """The two regressions from last night:"""

    @pytest.fixture
    def fake_deps(self, test_db) -> tuple[MagicMock, MagicMock, dict]:
        """Create a Database + DataService with realistic fake data."""
        # Setup database with 3 classified instruments
        test_db.save_instrument_metadata([
            {"symbol": "A.SS", "name": "Alpha", "category_l1": "宽基", "category_l2": "大盘",
             "category_l3": "沪深300", "priority_l1": 1, "priority_l2": 1, "priority_l3": 1, "sort_order": 1},
            {"symbol": "B.SS", "name": "Beta", "category_l1": "宽基", "category_l2": "大盘",
             "category_l3": "沪深300", "priority_l1": 1, "priority_l2": 1, "priority_l3": 2, "sort_order": 2},
            {"symbol": "C.SS", "name": "Gamma", "category_l1": "宽基", "category_l2": "小盘",
             "category_l3": "中证2000", "priority_l1": 1, "priority_l2": 2, "priority_l3": 1, "sort_order": 3},
        ])

        # Store 90 days of historical data in DB for each symbol
        for sym in ("A.SS", "B.SS", "C.SS"):
            hist = _make_hist(90)
            test_db.save_market_data(sym, hist)

        # Fake DataService — returns quotes and uses real DB
        ds = MagicMock()
        ds.fetch_latest_quotes.return_value = {
            "A.SS": {"symbol": "A.SS", "price": 12.0, "open": 11.8, "high": 12.2,
                     "low": 11.7, "volume": 500_000, "amount": 6_000_000},
            "B.SS": {"symbol": "B.SS", "price": 25.0, "open": 24.5, "high": 25.3,
                     "low": 24.4, "volume": 300_000, "amount": 7_500_000},
            "C.SS": {"symbol": "C.SS", "price": 8.0, "open": 7.9, "high": 8.1,
                     "low": 7.8, "volume": 200_000, "amount": 1_600_000},
        }

        cfg = _default_cfg()
        return ds, test_db, cfg

    @staticmethod
    def _trend_items_at_l2(result: dict) -> list[dict]:
        """Extract all L2-level items (which carry trend data) from the dashboard."""
        items: list[dict] = []
        for l1 in result.get("groups", []):
            for l2 in l1.get("items", []):
                items.append(l2)
                # L3 is under 'children', not 'items'
                items.extend(l2.get("children", []))
        return items

    def test_trend_history_not_empty(self, fake_deps) -> None:
        """BUG-1: Verify trend_history has entries for sparklines."""
        ds, db, cfg = fake_deps
        from data.intraday_service import build_intraday_dashboard

        result = build_intraday_dashboard(
            symbols=["A.SS", "B.SS", "C.SS"],
            db=db,
            data_service=ds,
            trend_config=cfg,
        )

        items = self._trend_items_at_l2(result)
        assert len(items) > 0, "Expected trend-bearing items in dashboard"

        for item in items:
            history = item.get("trend_history", [])

            # BUG FIX VERIFICATION: history must NOT be empty
            assert history is not None, (
                f"BUG 1a: trend_history is None for '{item.get('category_l3', item.get('category_l2'))}'"
            )
            assert len(history) > 0, (
                "BUG 1b: trend_history is empty — sparklines won't render"
            )

            dates = item.get("trend_dates")
            assert dates is not None and len(dates) == len(history), (
                "trend_dates length != trend_history length"
            )

    def test_trend_ma5_differs_from_trend_score(self, fake_deps) -> None:
        """BUG-2: Verify MA5 is different from raw trend score."""
        ds, db, cfg = fake_deps
        from data.intraday_service import build_intraday_dashboard

        result = build_intraday_dashboard(
            symbols=["A.SS", "B.SS", "C.SS"],
            db=db,
            data_service=ds,
            trend_config=cfg,
        )

        items = self._trend_items_at_l2(result)
        for item in items:
            ts = item.get("trend_score")
            ma5 = item.get("trend_ma5")
            history = item.get("trend_history", [])

            if ts is None or ma5 is None:
                continue

            # Precondition: history must be non-empty for MA5 to be meaningful
            assert len(history) > 0, (
                f"BUG 2 PRECONDITION FAILED: trend_history is empty for "
                f"'{item.get('category_l3', item.get('category_l2'))}' — "
                f"can't compute valid MA5 without daily series"
            )

            # Only if ALL daily scores are identical can MA5 == trend_score
            all_same = all(v == history[0] for v in history)
            # 测试数据为递增序列，MA5 必然不等于当日 trend_score（无恒真漏洞）
            assert not all_same, "fixture data unexpectedly constant"
            assert ts != ma5, (
                f"BUG 2: trend_score ({ts}) == trend_ma5 ({ma5}) for "
                f"'{item.get('category_l3', item.get('category_l2'))}'"
                f" — MA5 should be a 5-day smoothed average. "
                f"trend_history has {len(history)} values, unique={len(set(history))}"
            )

    def test_intraday_flag_set(self, fake_deps) -> None:
        ds, db, cfg = fake_deps
        from data.intraday_service import build_intraday_dashboard

        result = build_intraday_dashboard(
            symbols=["A.SS", "B.SS", "C.SS"],
            db=db,
            data_service=ds,
            trend_config=cfg,
        )
        assert result.get("is_intraday") is True
        assert "intraday_ts" in result

    def test_empty_symbols_returns_empty(self, fake_deps) -> None:
        ds, db, cfg = fake_deps
        from data.intraday_service import build_intraday_dashboard

        result = build_intraday_dashboard(
            symbols=[], db=db, data_service=ds, trend_config=cfg,
        )
        assert result["instrument_count"] == 0
        assert result["groups"] == []

    def test_dashboard_structure_matches_eod(self, fake_deps) -> None:
        """Verify the intraday result has the same top-level keys as the EOD path."""
        ds, db, cfg = fake_deps
        from data.intraday_service import build_intraday_dashboard

        result = build_intraday_dashboard(
            symbols=["A.SS", "B.SS", "C.SS"],
            db=db,
            data_service=ds,
            trend_config=cfg,
        )

        # Top-level keys expected by frontend
        for key in ("groups", "secondary_count", "category_count",
                     "instrument_count", "is_intraday", "as_of"):
            assert key in result, f"Missing top-level key: {key}"

        # Per-group keys expected by frontend (L1 groups are dicts with keys)
        if result["groups"]:
            g = result["groups"][0]
            for key in ("category_l1", "count", "items"):
                assert key in g, f"missing group key: {key}"

    def test_cache_first_path_with_indicator_cache(self, fake_deps) -> None:
        """回归：缓存路径下 amount 权重取自 1y tail —— load_market_tail 必须含
        amount 列，否则 amount_src=tail 时 KeyError（hist 为 None 才触发）。"""
        ds, db, cfg = fake_deps
        from core.indicators import INDICATOR_FORMULA_VERSION
        from core.trend import TREND_FORMULA_VERSION
        from data.indicator_store import compute_indicator_frame, compute_trend_frame

        # 契约钉住：缓存路径的成交额加权聚合直接读 tail 的 amount 列。
        tail_rows = db.load_market_tail(days=365)
        assert tail_rows and "amount" in tail_rows[0]

        for sym in ("A.SS", "B.SS", "C.SS"):
            hist = db.load_market_data(sym, price_mode="qfq")
            db.save_indicator_daily(sym, compute_indicator_frame(hist), formula_version=INDICATOR_FORMULA_VERSION)
            db.save_trend_daily(sym, compute_trend_frame(hist, cfg), formula_version=TREND_FORMULA_VERSION)

        from data.intraday_service import build_intraday_dashboard

        result = build_intraday_dashboard(
            symbols=["A.SS", "B.SS", "C.SS"],
            db=db,
            data_service=ds,
            trend_config=cfg,
        )
        assert result["instrument_count"] == 3
        instruments = [
            inst
            for group in result["groups"]
            for l2 in group["items"]
            for l3 in l2["children"]
            for inst in l3["children"]
        ]
        for inst in instruments:
            assert inst.get("macd_phase") in ("golden", "dead", None)
            assert 0 < len(inst.get("kline") or []) <= 30
            # 缓存路径下 trend_history（成交额加权 MA5 序列）也必须非空。
            assert len(inst.get("trend_history") or []) > 0

    def test_instrument_rows_carry_macd_phase_and_kline(self, fake_deps) -> None:
        """标的行带 MACD 相位与近30日K线（末根=盘中实时合成K线）；类目行为占位。"""
        ds, db, cfg = fake_deps
        from data.intraday_service import build_intraday_dashboard

        result = build_intraday_dashboard(
            symbols=["A.SS", "B.SS", "C.SS"],
            db=db,
            data_service=ds,
            trend_config=cfg,
        )

        instruments = [
            inst
            for group in result["groups"]
            for l2 in group["items"]
            for l3 in l2["children"]
            for inst in l3["children"]
        ]
        assert instruments, "expected instrument rows in intraday dashboard"
        quotes = ds.fetch_latest_quotes.return_value
        for inst in instruments:
            assert inst.get("macd_phase") in ("golden", "dead", None)
            for key in ("macd_phase_days", "macd_phase_change_pct", "macd_phase_signal_date"):
                assert key in inst, f"Missing key: {key}"
            kline = inst.get("kline") or []
            assert 0 < len(kline) <= 30
            assert len(inst.get("kline_ma5") or []) == len(kline)
            # 当日K线未落库 → 末根为实时合成K线，close = 实时价。
            assert kline[-1]["c"] == pytest.approx(quotes[inst["symbol"]]["price"])

        for group in result["groups"]:
            for l2 in group["items"]:
                assert l2["macd_phase"] is None
                assert l2["kline"] == []
                l2_instruments = [inst for l3 in l2["children"] for inst in l3["children"]]
                assert l2["macd_golden_count"] == sum(1 for i in l2_instruments if i["macd_phase"] == "golden")
                assert l2["macd_dead_count"] == sum(1 for i in l2_instruments if i["macd_phase"] == "dead")
                for l3 in l2["children"]:
                    assert l3["macd_phase"] is None
                    assert l3["kline"] == []
                    assert l3["macd_golden_count"] == sum(1 for i in l3["children"] if i["macd_phase"] == "golden")
                    assert l3["macd_dead_count"] == sum(1 for i in l3["children"] if i["macd_phase"] == "dead")
