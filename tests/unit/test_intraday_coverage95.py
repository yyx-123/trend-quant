"""intraday_service 覆盖率补测（目标 ≥95%）：报价日期解析、合成K线、
缓存递推路径与盘中看板内部聚合的边界分支。"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest

import data.intraday_service as ivs


class TestQuoteTradeDate:
    def test_epoch_seconds_and_ms(self) -> None:
        ts_sec = int(datetime(2026, 8, 25, 2, 0, tzinfo=UTC).timestamp())
        assert ivs.quote_trade_date({"ts": str(ts_sec)}) == date(2026, 8, 25)
        ts_ms = ts_sec * 1000
        assert ivs.quote_trade_date({"ts": str(ts_ms)}) == date(2026, 8, 25)

    def test_iso_string_and_invalid(self) -> None:
        assert ivs.quote_trade_date({"ts": "2026-08-25T10:00:00"}) == date(2026, 8, 25)
        assert ivs.quote_trade_date({"ts": "not-a-date"}) is None
        assert ivs.quote_trade_date({"ts": ""}) is None
        assert ivs.quote_trade_date({}) is None


class TestBuildSyntheticBar:
    def test_zero_ohlc_falls_back_to_price(self) -> None:
        quote = {"price": 4.5, "open": 0, "high": 0, "low": 0, "volume": 100}
        bar = ivs.build_synthetic_bar(quote, prev_volume=50)
        # open/high/low 缺失或为 0 时用最新价兜底，不塌陷为 0
        assert bar["open"] == 4.5
        assert bar["low"] == 4.5
        assert bar["high"] == 4.5

    def test_partial_ohlc(self) -> None:
        quote = {"price": 4.5, "open": 4.4, "high": 0, "low": 4.3, "volume": 100}
        bar = ivs.build_synthetic_bar(quote, prev_volume=50)
        assert bar["open"] == 4.4
        assert bar["high"] == 4.5  # max(open, price)
        assert bar["low"] == 4.3


class TestHasPersistedTodayBar:
    def test_empty_and_missing_time(self) -> None:
        assert ivs.has_persisted_today_bar(pd.DataFrame()) is False
        assert ivs.has_persisted_today_bar(pd.DataFrame({"close": [1.0]})) is False

    def test_nat_time_returns_false(self) -> None:
        df = pd.DataFrame({"time": [None], "close": [1.0]})
        assert ivs.has_persisted_today_bar(df) is False


class TestIntradayOverlayBranches:
    def test_trend_not_ok_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """compute_intraday_trend_score 失败 → overlay None（intraday_service.py:198-199）。"""
        monkeypatch.setattr(ivs, "is_past_market_open", lambda: True)
        monkeypatch.setattr(ivs, "has_persisted_today_bar", lambda hist: False)
        monkeypatch.setattr(
            ivs,
            "get_data_service",
            lambda: type("S", (), {"fetch_latest_quote": lambda self, s: {
                "price": 2.0, "ts": date.today().isoformat() + "T10:00:00",
            }})(),
        )
        monkeypatch.setattr(
            ivs, "compute_intraday_trend_score", lambda hist, quote, cfg: {"ok": False}
        )
        hist = pd.DataFrame({
            "time": ["2026-08-20"], "open": [1.0], "high": [1.1], "low": [0.9],
            "close": [1.0], "volume": [100], "amount": [100],
        })
        assert ivs.build_intraday_overlay("X.SS", hist, {}) is None


class TestComputeIntradayTrendCached:
    def _tail(self) -> pd.DataFrame:
        return pd.DataFrame({
            "time": pd.date_range("2026-08-01", periods=30, freq="D"),
            "close": [10.0 + i * 0.1 for i in range(30)],
            "volume": [1000.0] * 30,
        })

    def test_stale_anchor_rejected(self) -> None:
        cache_row = {"time": "2026-07-01", "atr": 0.5, "ema_s": 1, "ema_m": 1, "ema_l": 1}
        quote = {"price": 13.0, "volume": 500, "amount": 2000}
        result = ivs.compute_intraday_trend_cached("X.SS", quote, self._tail(), cache_row, {})
        assert result["ok"] is False
        assert result["reason"] == "stale_anchor"

    def test_invalid_atr_rejected(self) -> None:
        cache_row = {"time": "2026-08-30", "atr": 0.0, "ema_s": 1, "ema_m": 1, "ema_l": 1}
        quote = {"price": 13.0, "volume": 500, "amount": 2000}
        result = ivs.compute_intraday_trend_cached("X.SS", quote, self._tail(), cache_row, {})
        assert result["ok"] is False
        assert result["reason"] == "invalid_atr"

    def test_success_matches_full_history_shape(self) -> None:
        cache_row = {"time": "2026-08-30", "atr": 0.5, "ema_s": 11.0, "ema_m": 11.0, "ema_l": 11.0}
        quote = {"price": 13.0, "volume": 500, "amount": 2000}
        result = ivs.compute_intraday_trend_cached("X.SS", quote, self._tail(), cache_row, {})
        assert result["ok"] is True
        assert "trend_score" in result and "atr" in result


class _DashboardFixtures:
    def _fake_db(self, hists: dict[str, pd.DataFrame]):
        class FakeDb:
            def get_instrument_metadata_map(self):
                return {
                    s: {"name": f"测试{s}", "category_l1": "ETF", "category_l2": "宽基", "category_l3": "沪深300"}
                    for s in hists
                }

            def load_market_data(self, symbol, price_mode="qfq"):
                return hists.get(symbol, pd.DataFrame()).copy()

            def load_market_tail(self, days, price_mode="qfq"):
                rows = []
                for symbol, df in hists.items():
                    for _, row in df.tail(days).iterrows():
                        rows.append({"symbol": symbol, **row.to_dict()})
                return rows

            def load_indicator_latest(self, formula_version=None):
                return {}

            def load_trend_daily_bulk(self, since, param_set="default", formula_version=None):
                return []

        return FakeDb()

    def _hist(self, days: int = 30) -> pd.DataFrame:
        return pd.DataFrame({
            "time": pd.date_range("2026-07-01", periods=days, freq="D"),
            "open": [10.0] * days,
            "high": [10.5] * days,
            "low": [9.5] * days,
            "close": [10.0 + i * 0.05 for i in range(days)],
            "volume": [1000.0 + i * 10 for i in range(days)],
            "amount": [10000.0 + i * 100 for i in range(days)],
        })

    def _run(self, monkeypatch: pytest.MonkeyPatch, symbols: list[str], quotes: dict, hists: dict | None = None):
        hists = hists or {s: self._hist() for s in symbols}
        db = self._fake_db(hists)
        ds = type("DS", (), {"fetch_latest_quotes": lambda self, ss: quotes})()
        monkeypatch.setattr(ivs, "compute_intraday_trend_score", lambda hist, quote, cfg: {
            "ok": True, "trend_score": 10.0, "price_direction": 1.0, "confidence": 0.5,
            "atr": 0.1, "price": quote["price"], "ma_mid": 1.0,
        })
        monkeypatch.setattr(ivs, "compute_intraday_trend_cached", lambda *a, **k: {"ok": False, "reason": "missing_cache"})
        events: list = []
        return ivs.build_intraday_dashboard(
            symbols, db, ds, {}, progress_callback=lambda e: events.append(e)
        ), events

class TestBuildIntradayDashboard(_DashboardFixtures):

    def test_failed_symbols_recorded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """报价缺失/陈旧/无价格的标的进 failed_symbols（intraday_service.py:552-560）。"""
        monkeypatch.setattr(ivs, "is_past_market_open", lambda: True)
        monkeypatch.setattr(ivs, "is_realtime_available", lambda: False)
        monkeypatch.setattr(ivs, "is_quote_fresh", lambda q: q.get("fresh", True))
        payload, _ = self._run(
            monkeypatch,
            ["OK.SS", "STALE.SS", "NOQ.SS"],
            {
                "OK.SS": {"price": 11.0, "amount": 1000, "volume": 100},
                "STALE.SS": {"price": 11.0, "fresh": False, "amount": 100, "volume": 10},
            },
        )
        assert set(payload["failed_symbols"]) == {"STALE.SS", "NOQ.SS"}
        assert payload["instrument_count"] == 1

    def test_progress_events(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """进度回调发出 quotes/done 事件（intraday_service.py:502-505）。"""
        monkeypatch.setattr(ivs, "is_past_market_open", lambda: True)
        monkeypatch.setattr(ivs, "is_realtime_available", lambda: False)
        monkeypatch.setattr(ivs, "is_quote_fresh", lambda q: True)
        _payload, events = self._run(
            monkeypatch, ["OK.SS"], {"OK.SS": {"price": 11.0, "amount": 100, "volume": 10}}
        )
        stages = {e["stage"] for e in events}
        assert "quotes" in stages
        assert "done" in stages

    def test_all_failed_returns_empty_groups(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """全部报价失败 → 空看板（intraday_service.py:758-759 空 source 分支）。"""
        monkeypatch.setattr(ivs, "is_past_market_open", lambda: True)
        monkeypatch.setattr(ivs, "is_realtime_available", lambda: False)
        monkeypatch.setattr(ivs, "is_quote_fresh", lambda q: False)
        payload, _ = self._run(monkeypatch, ["A.SS"], {"A.SS": {"price": 11.0}})
        assert payload["groups"] == []
        assert payload["instrument_count"] == 0

    def test_return_computed_from_tail_closes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """日涨跌幅由「今收/昨收」重算（intraday_service.py:739-753）。"""
        monkeypatch.setattr(ivs, "is_past_market_open", lambda: True)
        monkeypatch.setattr(ivs, "is_realtime_available", lambda: False)
        monkeypatch.setattr(ivs, "is_quote_fresh", lambda q: True)
        payload, _ = self._run(
            monkeypatch, ["OK.SS"], {"OK.SS": {"price": 12.0, "amount": 100, "volume": 10}}
        )
        # 找到标的行：return_1d 应为 (12/最近历史收盘-1)*100
        def find_inst(groups):
            for g in groups:
                for l2 in g.get("items", []):
                    for l3 in l2.get("children", []):
                        for inst in l3.get("children", []):
                            return inst
        inst = find_inst(payload["groups"])
        last_close = 10.0 + 29 * 0.05
        assert inst["daily_change_pct"] == pytest.approx((12.0 / last_close - 1) * 100, rel=1e-3)
        assert inst["change_5d"] is not None



class TestQuoteTradeDateTz:
    def test_tz_aware_string_converted(self) -> None:
        """带时区的时间戳转换到市场时区（intraday_service.py:64-66）。"""
        result = ivs.quote_trade_date({"ts": "2026-08-25T23:30:00+00:00"})
        assert result == date(2026, 8, 26)  # UTC 23:30 → 北京次日 07:30


class TestCleanDailyHist:
    def test_dropna_and_sort(self) -> None:
        """清洗：丢 NaT/缺列行并按时间排序（intraday_service.py:135-138）。"""
        df = pd.DataFrame({
            "time": ["2026-08-21", None, "2026-08-20"],
            "open": [1.0, 1.0, 1.0],
            "high": [1.1, 1.1, 1.1],
            "low": [0.9, 0.9, 0.9],
            "close": [1.0, 1.0, 1.0],
        })
        cleaned = ivs._clean_daily_hist(df)
        assert len(cleaned) == 2
        assert str(cleaned["time"].iloc[0].date()) == "2026-08-20"


class TestTrendCachedGuards:
    def test_missing_cache_row(self) -> None:
        result = ivs.compute_intraday_trend_cached("X.SS", {"price": 1.0}, pd.DataFrame(), None, {})
        assert result["ok"] is False
        assert result["reason"] == "missing_cache"

    def test_empty_close_tail(self) -> None:
        tail = pd.DataFrame({"close": [None], "volume": [1.0]})
        result = ivs.compute_intraday_trend_cached("X.SS", {"price": 1.0}, tail, {"atr": 1}, {})
        assert result["ok"] is False
        assert result["reason"] == "insufficient_tail"


class TestWeightedTrendSeriesFallback:
    def test_missing_amount_equal_weight(self) -> None:
        """成交额缺失成员按等权 1.0 兜底（intraday_service.py:463-466）。"""
        rows = pd.DataFrame([
            {
                "trend_score_series": [10.0, 20.0],
                "trend_series_dates": ["2026-08-20", "2026-08-21"],
                "trend_series_amounts": [0.0, None],
            }
        ])
        days, scores = ivs._weighted_daily_trend_series(rows)
        assert days == ["2026-08-20", "2026-08-21"]
        assert scores == [10.0, 20.0]


class TestDashboardHistFallback(_DashboardFixtures):
    def test_cached_path_fails_falls_back_to_full_hist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """无指标缓存的标的走全量历史计算路径（intraday_service.py:577-604）。

        FakeDb.load_indicator_latest 返回 {} → missing_cache 短路（此短路发生在
        compute_intraday_trend_cached 被调用之前，故全量历史是唯一可行路径）。
        """
        monkeypatch.setattr(ivs, "is_past_market_open", lambda: True)
        monkeypatch.setattr(ivs, "is_realtime_available", lambda: False)
        monkeypatch.setattr(ivs, "is_quote_fresh", lambda q: True)
        # _run 内 compute_intraday_trend_cached 恒失败 → 成功标的必然走了
        # 全量历史回退路径（intraday_service.py:577-604）
        payload, _ = self._run(
            monkeypatch, ["OK.SS"], {"OK.SS": {"price": 12.0, "amount": 100, "volume": 10}}
        )
        assert payload["instrument_count"] == 1
        assert payload["failed_symbols"] == []

    def test_insufficient_hist_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """历史不足 20 根 → failed（intraday_service.py:585-586）。"""
        monkeypatch.setattr(ivs, "is_past_market_open", lambda: True)
        monkeypatch.setattr(ivs, "is_realtime_available", lambda: False)
        monkeypatch.setattr(ivs, "is_quote_fresh", lambda q: True)
        monkeypatch.setattr(ivs, "compute_intraday_trend_cached", lambda *a, **k: {"ok": False, "reason": "missing_cache"})
        short_hist = self._hist(days=5)
        payload, _ = self._run(
            monkeypatch,
            ["SHORT.SS"],
            {"SHORT.SS": {"price": 12.0, "amount": 100, "volume": 10}},
            hists={"SHORT.SS": short_hist},
        )
        # 报价有效但历史不足 → 无标的中标（early return 不带 failed_symbols 键）
        assert payload["instrument_count"] == 0
        assert payload["groups"] == []


class TestReturnComputationGuards(_DashboardFixtures):
    def test_missing_tail_frame_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """tail 收盘价不足 2 根的标的 return 保持初始 None（intraday_service.py:741-744）。"""
        monkeypatch.setattr(ivs, "is_past_market_open", lambda: True)
        monkeypatch.setattr(ivs, "is_realtime_available", lambda: False)
        monkeypatch.setattr(ivs, "is_quote_fresh", lambda q: True)
        full_hist = self._hist(days=30)

        # 定制 db：load_market_tail 只给 1 行（收盘价不足 2 根），
        # 但 load_market_data 给全量 30 根（全量回退路径仍出标的）
        class CustomDb(self._fake_db({"ONE.SS": full_hist}).__class__):
            def load_market_tail(self, days, price_mode="qfq"):
                row = full_hist.tail(1).iloc[0]
                return [{"symbol": "ONE.SS", **row.to_dict()}]

        db = CustomDb()
        ds = type("DS", (), {"fetch_latest_quotes": lambda self, ss: {
            "ONE.SS": {"price": 12.0, "amount": 100, "volume": 10}}})()
        payload = ivs.build_intraday_dashboard(["ONE.SS"], db, ds, {})

        def find_inst(groups):
            for g in groups:
                for l2 in g.get("items", []):
                    for l3 in l2.get("children", []):
                        for inst in l3.get("children", []):
                            return inst
        inst = find_inst(payload["groups"])
        assert inst is not None
        assert inst["daily_change_pct"] is None


class TestMetricsSummaryGuards(_DashboardFixtures):
    def test_all_none_trend_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """趋势值全 None 的标的不产出聚合行（intraday_service.py:813-821）。"""
        monkeypatch.setattr(ivs, "is_past_market_open", lambda: True)
        monkeypatch.setattr(ivs, "is_realtime_available", lambda: False)
        monkeypatch.setattr(ivs, "is_quote_fresh", lambda q: True)
        # trend_score 返回 None（无有效趋势值的标的）
        monkeypatch.setattr(ivs, "compute_intraday_trend_score", lambda hist, quote, cfg: {
            "ok": True, "trend_score": None, "price_direction": None, "confidence": 0.0,
            "atr": 0.1, "price": quote["price"], "ma_mid": 1.0,
        })
        monkeypatch.setattr(ivs, "compute_intraday_trend_cached", lambda *a, **k: {"ok": False, "reason": "missing_cache"})
        hists = {"OK.SS": self._hist()}
        db = self._fake_db(hists)
        ds = type("DS", (), {"fetch_latest_quotes": lambda self, ss: {
            "OK.SS": {"price": 12.0, "amount": 100, "volume": 10}}})()
        payload = ivs.build_intraday_dashboard(["OK.SS"], db, ds, {})
        # trend 全 None 的行组不产生聚合行 → 无类目分组
        assert payload["groups"] == []
