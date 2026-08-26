"""咽喉模块覆盖率补测：stop_loss 边界分支、calendar 缺口、strategy_config 缓存、
provider_tickflow 剩余路径、data/service 回填/物化、main 异常处理与备份任务。"""

from __future__ import annotations

import asyncio
import os
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import services.stop_loss as sl
from services.stop_loss import StopLossError, compute_stop_loss


def _seed_bars(test_db, symbol: str = "510300.SS", rows: int = 60) -> pd.DataFrame:
    start = date(2026, 6, 1)
    items = []
    price = 4.0
    for idx in range(rows):
        day = start + timedelta(days=idx)
        price += 0.02
        items.append(
            {
                "time": day.isoformat(),
                "open": price,
                "high": price + 0.1,
                "low": price - 0.1,
                "close": price + 0.03,
                "volume": 100000 + idx * 100,
                "amount": 400000 + idx * 400,
            }
        )
    df = pd.DataFrame(items)
    test_db.save_market_data(symbol, df, price_mode="qfq")
    return df


class TestComputeStopLossEdges:

    def test_invalid_buy_date(self, test_db) -> None:
        with pytest.raises(StopLossError, match="无效的买入日期"):
            compute_stop_loss("510300.SS", "not-a-date", 4.0, db=test_db)


    def test_invalid_end_date(self, test_db) -> None:
        with pytest.raises(StopLossError, match="无效的截止日期"):
            compute_stop_loss("510300.SS", "2026-07-01", 4.0, end_date="bad", db=test_db)

    def test_end_before_buy(self, test_db) -> None:
        with pytest.raises(StopLossError, match="早于买入日期"):
            compute_stop_loss("510300.SS", "2026-07-10", 4.0, end_date="2026-07-01", db=test_db)


    def test_end_date_no_data_before(self, test_db) -> None:
        """end_date 截断后 df 为空（但 ATR 可从全量历史算出）→ 明确报错。"""
        df = _seed_bars(test_db).copy()  # 6-7 月数据
        df["time"] = pd.to_datetime(df["time"])
        july_only = df[df["time"] >= pd.Timestamp("2026-07-01")]
        with pytest.raises(StopLossError, match="之前无数据"):
            compute_stop_loss(
                "510300.SS", "2026-06-01", 4.2, end_date="2026-06-15", db=test_db, df=july_only
            )

    def test_loose_with_instrument_override(self, test_db) -> None:
        _seed_bars(test_db)
        test_db.save_instrument_metadata([
            {"symbol": "510300.SS", "name": "A", "category_l1": "a", "category_l2": "b",
             "category_l3": "c", "stop_atr_mul": 2.0, "enabled": True},
        ])
        result = compute_stop_loss("510300.SS", "2026-07-01", 4.6, db=test_db, intraday=False)
        assert result["hard_stop_price"] is not None
        # 标的级 stop_atr_mul=2.0 覆盖默认 1.5 → 松止损离买入价更远（更低）；
        # tight 固定 1.0 → 更高（更近）
        tight = compute_stop_loss("510300.SS", "2026-07-01", 4.6, db=test_db, intraday=False, stop_mode="tight")
        assert tight["stop_mode"] == "tight"
        assert tight["hard_stop_price"] > result["hard_stop_price"]

    def test_preloaded_df_skips_db_read(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        df = _seed_bars(test_db).copy()
        df["time"] = pd.to_datetime(df["time"])

        def _boom(*args, **kwargs):
            raise AssertionError("db.load_market_data should not be called")

        monkeypatch.setattr(test_db, "load_market_data", _boom)
        # ATR 走预计算序列，隔离指标缓存路径（本测试锁定 df 参数契约）
        import numpy as np

        atr = pd.Series(np.full(len(df), 0.1), index=df["time"])
        monkeypatch.setattr(sl, "get_series", lambda symbol, indicator, db=None: atr)
        result = compute_stop_loss("510300.SS", "2026-07-01", 4.6, db=test_db, intraday=False, df=df)
        assert result["hard_stop_price"] is not None


class TestIntradayBarSynthesis:
    def test_synthesize_rejects_stale_quote(self) -> None:
        df = pd.DataFrame({"volume": [100.0]})
        stale = {"price": 1.0, "ts": "2020-01-01T10:00:00"}
        assert sl._synthesize_intraday_bar("X.SS", df, stale) is None
        assert sl._synthesize_intraday_bar("X.SS", df, None) is None
        assert sl._synthesize_intraday_bar("X.SS", df, {"price": None}) is None

    def test_fetch_intraday_bar_not_past_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sl, "is_past_market_open", lambda: False)
        assert sl._fetch_intraday_bar("X.SS", pd.DataFrame()) is None

    def test_fetch_intraday_bar_db_has_today(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sl, "is_past_market_open", lambda: True)
        monkeypatch.setattr(sl, "has_persisted_today_bar", lambda df: True)
        assert sl._fetch_intraday_bar("X.SS", pd.DataFrame()) is None

    def test_fetch_intraday_bar_quote_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sl, "is_past_market_open", lambda: True)
        monkeypatch.setattr(sl, "has_persisted_today_bar", lambda df: False)
        monkeypatch.setattr(
            sl,
            "get_data_service",
            lambda: type("S", (), {"fetch_latest_quote": lambda self, s: (_ for _ in ()).throw(RuntimeError("down"))})(),
        )
        assert sl._fetch_intraday_bar("X.SS", pd.DataFrame({"volume": [1.0]})) is None


class TestFetchIntradayBars:
    def test_not_past_open_returns_none_map(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sl, "is_past_market_open", lambda: False)
        dfs = {"A.SS": pd.DataFrame(), "B.SS": pd.DataFrame()}
        assert sl.fetch_intraday_bars(dfs) == {"A.SS": None, "B.SS": None}

    def test_batch_quote_failure_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sl, "is_past_market_open", lambda: True)
        monkeypatch.setattr(sl, "has_persisted_today_bar", lambda df: False)
        monkeypatch.setattr(
            sl,
            "get_data_service",
            lambda: type("S", (), {"fetch_latest_quotes": lambda self, ss: (_ for _ in ()).throw(RuntimeError("down"))})(),
        )
        dfs = {"A.SS": pd.DataFrame({"volume": [1.0]})}
        assert sl.fetch_intraday_bars(dfs) == {"A.SS": None}

    def test_error_quote_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sl, "is_past_market_open", lambda: True)
        monkeypatch.setattr(sl, "has_persisted_today_bar", lambda df: False)
        monkeypatch.setattr(
            sl,
            "get_data_service",
            lambda: type("S", (), {"fetch_latest_quotes": lambda self, ss: {"A.SS": {"error": "x"}}})(),
        )
        dfs = {"A.SS": pd.DataFrame({"volume": [1.0]})}
        assert sl.fetch_intraday_bars(dfs) == {"A.SS": None}


# ---------------------------------------------------------------------------
# calendar 缺口
# ---------------------------------------------------------------------------
class TestCalendarGaps:
    def test_previous_next_trading_day_defaults(self) -> None:
        from core.calendar import next_trading_day, previous_trading_day

        prev = previous_trading_day()
        nxt = next_trading_day()
        assert isinstance(prev, date) and isinstance(nxt, date)
        assert prev <= date.today() <= nxt

    def test_calendar_data_status_current(self) -> None:
        from core.calendar import calendar_data_status

        status = calendar_data_status()
        assert status["stale"] is False
        assert status["message"] == ""

    def test_is_realtime_and_past_open(self) -> None:
        from core.calendar import is_past_market_open, is_realtime_available

        assert isinstance(is_realtime_available(), bool)
        assert isinstance(is_past_market_open(), bool)


# ---------------------------------------------------------------------------
# strategy_config 缓存
# ---------------------------------------------------------------------------
class TestStrategyConfigCache:
    def test_cache_hit_and_invalidation(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        import core.strategy_config as sc
        import data.storage.db as db_module

        monkeypatch.setattr(db_module, "get_db", lambda: test_db)
        monkeypatch.setattr(db_module, "_db_instance", test_db)
        sc.invalidate_strategy_config_cache()

        first = sc.get_strategy_config()
        # 改库后 30s TTL 内仍返回缓存
        test_db.set_config("strategy", {**first, "adjust": "hfq"})
        cached = sc.get_strategy_config()
        assert cached["adjust"] == first["adjust"]
        # 写时失效后立即生效
        sc.invalidate_strategy_config_cache()
        assert sc.get_strategy_config()["adjust"] == "hfq"
        sc.invalidate_strategy_config_cache()

    def test_db_unavailable_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import core.strategy_config as sc
        import data.storage.db as db_module

        monkeypatch.setattr(db_module, "get_db", lambda: (_ for _ in ()).throw(RuntimeError("down")))
        sc.invalidate_strategy_config_cache()
        cfg = sc.get_strategy_config()
        assert cfg["adjust"] == "qfq"  # 代码默认


# ---------------------------------------------------------------------------
# provider_tickflow 剩余路径
# ---------------------------------------------------------------------------
class TestProviderGaps:
    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "k"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_fetch_instrument_name(self, tickflow_cls: MagicMock) -> None:
        from data.provider_tickflow import TickFlowProvider

        provider = TickFlowProvider()
        client = tickflow_cls.return_value
        client.instruments.get.return_value = {"name": "沪深300ETF"}
        assert provider.fetch_instrument_name("510300.SS") == "沪深300ETF"

        client.instruments.get.return_value = {}
        assert provider.fetch_instrument_name("510300.SS") is None

    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "k"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_fetch_ex_factors_chunk_error(self, tickflow_cls: MagicMock) -> None:
        from data.provider_tickflow import TickFlowProvider

        provider = TickFlowProvider()
        client = tickflow_cls.return_value
        client.klines.ex_factors.side_effect = RuntimeError("vendor down")
        factors, errors = provider.fetch_ex_factors(["510300.SS"])
        assert factors == {}
        assert "510300.SS" in errors

    def test_throttle_waits(self) -> None:
        import data.provider_tickflow as pt

        provider = pt.TickFlowProvider()
        # 模块级限流状态：interval 内的第二次调用会 sleep（用极小 interval 验证语义）
        pt._NEXT_REQUEST_AT.clear()
        provider._throttle("test_op", 0.05)
        started = pt.time_module.monotonic()
        provider._throttle("test_op", 0.05)
        assert pt.time_module.monotonic() - started >= 0.04

    def test_close_idempotent(self) -> None:
        from data.provider_tickflow import TickFlowProvider

        provider = TickFlowProvider()
        provider.close()  # client 为 None 时不炸
        provider.close()
        # close 后 _client 状态可安全再次懒初始化（幂等语义的可观察面）
        assert provider._client is None


# ---------------------------------------------------------------------------
# data/service 回填与物化
# ---------------------------------------------------------------------------
class TestDataServiceGaps:
    def test_rematerialize_qfq_no_raw(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        import data.service as ds_mod
        import data.storage.db as db_module

        monkeypatch.setattr(db_module, "get_db", lambda: test_db)
        monkeypatch.setattr(db_module, "_db_instance", test_db)

        service = ds_mod.DataService.__new__(ds_mod.DataService)
        service.market_store = ds_mod.MarketStore(db=test_db)
        service.raw_store = ds_mod.MarketStore(db=test_db, price_mode="raw")
        result = service.rematerialize_qfq("NOPE.SS", db=test_db)
        assert result["status"] == "no_raw"

    def test_rematerialize_qfq_ok(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        import data.service as ds_mod
        import data.storage.db as db_module

        monkeypatch.setattr(db_module, "get_db", lambda: test_db)
        monkeypatch.setattr(db_module, "_db_instance", test_db)

        bars = pd.DataFrame([
            {"time": "2026-08-20", "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0, "volume": 1, "amount": 1},
            {"time": "2026-08-21", "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0, "volume": 1, "amount": 1},
        ])
        test_db.save_market_data("AAA.SS", bars, price_mode="raw")

        service = ds_mod.DataService.__new__(ds_mod.DataService)
        service.market_store = ds_mod.MarketStore(db=test_db)
        service.raw_store = ds_mod.MarketStore(db=test_db, price_mode="raw")
        result = service.rematerialize_qfq("AAA.SS", db=test_db)
        assert result["status"] == "ok"
        assert result["rows"] == 2

    def test_is_trading_day_delegate(self) -> None:
        import data.service as ds_mod

        service = ds_mod.DataService.__new__(ds_mod.DataService)
        assert service.is_trading_day(date(2026, 8, 22)) is False  # 周六


# ---------------------------------------------------------------------------
# main.py 异常处理与备份任务
# ---------------------------------------------------------------------------
class TestMainGaps:
    def test_unhandled_exception_handler(self) -> None:
        from app.main import unhandled_exception_handler

        resp = asyncio.run(
            unhandled_exception_handler(
                type("R", (), {"method": "GET", "url": type("U", (), {"path": "/x"})()})(),
                RuntimeError("boom"),
            )
        )
        assert resp.status_code == 500

    def test_http_exception_5xx_and_4xx(self) -> None:
        from starlette.exceptions import HTTPException as StarletteHTTPException

        from app.main import http_exception_handler

        request = type("R", (), {"method": "GET", "url": type("U", (), {"path": "/x"})()})()
        resp4 = asyncio.run(http_exception_handler(request, StarletteHTTPException(status_code=404, detail="nf")))
        assert resp4.status_code == 404
        resp5 = asyncio.run(http_exception_handler(request, StarletteHTTPException(status_code=502, detail="bad")))
        assert resp5.status_code == 502

    def test_ensure_builtin_admin_creates_and_promotes(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.main import _ensure_builtin_admin

        _ensure_builtin_admin(test_db)
        user = test_db.get_user_by_username("yyx")
        assert user is not None and user["is_admin"] is True
        # 幂等：再跑不重置密码
        test_db.set_user_admin("yyx", False)
        _ensure_builtin_admin(test_db)
        assert test_db.get_user_by_username("yyx")["is_admin"] is True

    def test_backup_job_writes_backup(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        # backup_job 是 lifespan 闭包（传给 scheduler.start 的关键字参数）不可直达，
        # 这里验证其核心契约 backup_to(keep=1)：真实 VACUUM INTO 落盘且非源库
        dest = test_db.backup_to(keep=1)
        assert dest.exists()
        assert dest != test_db.db_path
        assert dest.parent.name == "backups"
        # 备份是有效 SQLite 库（quick_check 通过）
        import sqlite3

        conn = sqlite3.connect(str(dest))
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        conn.close()
