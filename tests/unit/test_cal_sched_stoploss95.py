"""calendar / scheduler / stop_loss 覆盖率补测（目标 ≥95%）。"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import patch

import pandas as pd
import pytest


class TestCalendarInternals:
    def test_market_now_settings_unreadable_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """配置不可读时回退 Asia/Shanghai（calendar.py:51-52）。"""
        import core.calendar as cal

        monkeypatch.setattr(cal, "_market_tz", None)
        monkeypatch.setattr(
            "core.settings.load_settings",
            lambda: (_ for _ in ()).throw(RuntimeError("no config")),
        )
        now = cal.market_now()
        assert now.tzinfo is not None
        assert str(now.tzinfo) == "Asia/Shanghai"
        monkeypatch.setattr(cal, "_market_tz", None)  # 还原缓存

    def test_market_now_bad_tz_name_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """非法时区名回退 Asia/Shanghai（calendar.py:55-56）。"""
        import core.calendar as cal

        monkeypatch.setattr(cal, "_market_tz", None)
        monkeypatch.setattr(
            "core.settings.load_settings",
            lambda: type("S", (), {"app": type("A", (), {"timezone": "Mars/Olympus"})()})(),
        )
        now = cal.market_now()
        assert str(now.tzinfo) == "Asia/Shanghai"
        monkeypatch.setattr(cal, "_market_tz", None)

    def test_previous_trading_day_exhausts_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """20 天窗口内无交易日时返回兜底游标（calendar.py:155）。"""
        import core.calendar as cal

        monkeypatch.setattr(cal, "is_trading_day", lambda d: False)
        result = cal.previous_trading_day(date(2026, 8, 26))
        # 兜底返回第 20 天前的游标日期（而不是死循环或抛错）
        assert result == date(2026, 8, 6)

    def test_next_trading_day_default_today(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """不传参时以市场今日为起点（calendar.py:165）。"""
        import core.calendar as cal

        monkeypatch.setattr(cal, "is_trading_day", lambda d: d == date(2026, 8, 28))
        monkeypatch.setattr(cal, "market_now", lambda: datetime(2026, 8, 26, 10, 0).astimezone())
        assert cal.next_trading_day() == date(2026, 8, 28)


class TestCalendarDataStatus:
    def test_current_year_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """当年超库 → stale（calendar.py:180-181）。"""
        import core.calendar as cal

        monkeypatch.setattr(cal, "market_now", lambda: datetime(2026, 8, 26, 10, 0).astimezone())

        def _boom(d):
            raise NotImplementedError(d.year)

        monkeypatch.setattr(cal, "is_workday", _boom)
        status = cal.calendar_data_status()
        assert status["stale"] is True
        assert 2026 in status["stale_years"]
        assert "chinese_calendar" in status["message"]

    def test_december_window_next_year_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """12 月且次年未发布 → 升级窗口提示（calendar.py:185-188）。"""
        import core.calendar as cal

        monkeypatch.setattr(cal, "market_now", lambda: datetime(2026, 12, 15, 10, 0).astimezone())

        def fake_is_workday(d):
            if d.year >= 2027:
                raise NotImplementedError(d.year)
            return d.weekday() < 5

        monkeypatch.setattr(cal, "is_workday", fake_is_workday)
        status = cal.calendar_data_status()
        assert status["stale"] is True
        assert 2027 in status["stale_years"]

    def test_december_next_year_published_fresh(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """12 月且次年已发布 → 不误报。"""
        import core.calendar as cal

        monkeypatch.setattr(cal, "market_now", lambda: datetime(2026, 12, 15, 10, 0).astimezone())
        monkeypatch.setattr(cal, "is_workday", lambda d: d.weekday() < 5)
        assert cal.calendar_data_status()["stale"] is False


class TestSchedulerJobEvent:
    def test_error_and_missed_listener(self, caplog) -> None:
        """error/misfire 监听器：异常记 error、misfire 记 warning（scheduler.py:100-105）。"""
        import logging
        from types import SimpleNamespace

        import apscheduler.schedulers.background as bg

        from core.scheduler import SchedulerManager
        from core.settings import load_settings

        mgr = SchedulerManager(settings=load_settings())
        with patch.object(
            bg.BackgroundScheduler, "add_listener", autospec=True
        ) as mock_add_listener:
            mgr.start(update_job=lambda: None)
            assert mock_add_listener.called
            listener = mock_add_listener.call_args[0][1]  # autospec 首参为 self
        try:
            with caplog.at_level(logging.WARNING, logger="core.scheduler"):
                listener(SimpleNamespace(job_id="daily_update", exception=RuntimeError("boom")))
                listener(SimpleNamespace(job_id="daily_update", exception=None))
        finally:
            mgr.shutdown()
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("daily_update" in r.message and "raised" in r.message for r in errors)
        assert any("daily_update" in r.message and "missed" in r.message for r in warnings)


# ---------------------------------------------------------------------------
# stop_loss 剩余缺口
# ---------------------------------------------------------------------------
import services.stop_loss as sl


class TestSynthesizePrevVolume:
    def test_prev_volume_from_hist(self) -> None:
        """合成K线的兜底量取历史最后一根（stop_loss.py:68-70）。"""
        from core.calendar import market_now

        df = pd.DataFrame({"volume": [12345.0]})
        fresh_quote = {
            "price": 4.5, "open": 4.4, "high": 4.6, "low": 4.3,
            "ts": market_now().date().isoformat() + "T10:00:00",
        }
        bar = sl._synthesize_intraday_bar("X.SS", df, fresh_quote)
        assert bar is not None
        assert bar["close"] == 4.5

    def test_empty_hist_zero_volume(self) -> None:
        from core.calendar import market_now

        df = pd.DataFrame({"volume": pd.Series(dtype=float)})
        fresh_quote = {"price": 4.5, "ts": market_now().date().isoformat() + "T10:00:00"}
        bar = sl._synthesize_intraday_bar("X.SS", df, fresh_quote)
        assert bar is not None


class TestFetchIntradayBarsBatch:
    def test_all_persisted_skips_quotes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """全部标的今日K线已落库 → 不发批量报价（stop_loss.py:109 前段）。"""
        monkeypatch.setattr(sl, "is_past_market_open", lambda: True)
        monkeypatch.setattr(sl, "has_persisted_today_bar", lambda df: True)
        called: list = []
        monkeypatch.setattr(
            sl,
            "get_data_service",
            lambda: type("S", (), {"fetch_latest_quotes": lambda self, ss: called.append(ss)})(),
        )
        dfs = {"A.SS": pd.DataFrame({"volume": [1.0]}), "B.SS": pd.DataFrame({"volume": [1.0]})}
        assert sl.fetch_intraday_bars(dfs) == {"A.SS": None, "B.SS": None}
        assert called == []

    def test_synth_failure_in_loop_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """单标的合成异常只影响该标的（stop_loss.py:119-122）。"""
        monkeypatch.setattr(sl, "is_past_market_open", lambda: True)
        monkeypatch.setattr(sl, "has_persisted_today_bar", lambda df: False)
        monkeypatch.setattr(
            sl,
            "get_data_service",
            lambda: type("S", (), {"fetch_latest_quotes": lambda self, ss: {
                "A.SS": {"price": 4.5, "ts": __import__("datetime").date.today().isoformat() + "T10:00:00"},
            }})(),
        )
        monkeypatch.setattr(
            sl,
            "_synthesize_intraday_bar",
            lambda symbol, df, quote: (_ for _ in ()).throw(RuntimeError("bad bar")),
        )
        dfs = {"A.SS": pd.DataFrame({"volume": [1.0]})}
        assert sl.fetch_intraday_bars(dfs) == {"A.SS": None}


class TestComputeStopLossMetadataFailure:
    def test_metadata_error_falls_back_to_config(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        """标的元数据查询异常 → 记日志并用配置默认值（stop_loss.py:198-199）。"""
        from tests.unit.test_throat_coverage import _seed_bars

        _seed_bars(test_db)
        monkeypatch.setattr(
            test_db, "get_instrument_metadata",
            lambda symbol: (_ for _ in ()).throw(RuntimeError("db locked")),
        )
        result = sl.compute_stop_loss("510300.SS", "2026-07-01", 4.6, db=test_db, intraday=False)
        # 元数据不可用不阻断计算：回落到配置默认倍数（松止损口径）
        assert result["hard_stop_atr_mul"] == 1.5
        assert result["stop_mode"] == "loose"
        assert result["hard_stop_price"] is not None

    def test_empty_atr_raises(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        """ATR 序列为空 → 明确业务错误（stop_loss.py:209）。"""

        from tests.unit.test_throat_coverage import _seed_bars

        _seed_bars(test_db)
        monkeypatch.setattr(sl, "get_series", lambda symbol, indicator, db=None: pd.Series(dtype=float))
        with pytest.raises(sl.StopLossError, match="无法计算 ATR"):
            sl.compute_stop_loss("510300.SS", "2026-07-01", 4.6, db=test_db, intraday=False)

    def test_zero_atr_raises(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        """ATR 为 0 → 数据异常错误（stop_loss.py:213）。"""
        from tests.unit.test_throat_coverage import _seed_bars

        df = _seed_bars(test_db)
        monkeypatch.setattr(
            sl,
            "get_series",
            lambda symbol, indicator, db=None: pd.Series(
                [0.0] * len(df), index=pd.to_datetime(df["time"])
            ),
        )
        with pytest.raises(sl.StopLossError, match="ATR 值为 0"):
            sl.compute_stop_loss("510300.SS", "2026-07-01", 4.6, db=test_db, intraday=False)
