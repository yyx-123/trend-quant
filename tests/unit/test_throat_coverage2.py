"""咽喉模块覆盖率补测（二）：data/service 抓取与回填、
main 的任务闭包与 AuthWall 续期路径。

（原 MCP list_instruments 用例已随逻辑下沉迁至 test_instrument_catalog.py。）
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

import data.service as ds_mod
from data.service import DataProviderError, DataService


def _bars(days: int = 5, start: date = date(2026, 8, 17)) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "time": (start + timedelta(days=i)).isoformat(),
            "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0,
            "volume": 100, "amount": 1000,
        }
        for i in range(days)
    ])


class _FakeProvider:
    def __init__(self, df: pd.DataFrame | None = None, fail: bool = False) -> None:
        self.df = df if df is not None else _bars()
        self.fail = fail

    def fetch_daily_history(self, symbol, start, end, adjust):
        if self.fail:
            raise RuntimeError("vendor down")
        return self.df.copy()

    def fetch_daily_histories(self, symbols, start, end, adjust, **kw):
        if self.fail:
            return {}, {s: "vendor down" for s in symbols}
        return {s: self.df.copy() for s in symbols}, {}

    def fetch_ex_factors(self, symbols):
        return {}, {}

    def fetch_latest_quote(self, symbol):
        return {"symbol": symbol, "price": 1.0}

    def fetch_latest_quotes(self, symbols):
        return {s: {"symbol": s, "price": 1.0} for s in symbols}


def _service(monkeypatch: pytest.MonkeyPatch, test_db, provider=None) -> DataService:
    import data.storage.db as db_module

    monkeypatch.setattr(db_module, "get_db", lambda: test_db)
    monkeypatch.setattr(db_module, "_db_instance", test_db)
    service = DataService.__new__(DataService)
    service.providers = {"tickflow": provider or _FakeProvider()}
    service.provider_priority = ["tickflow"]
    service.market_store = ds_mod.MarketStore(db=test_db)
    service.raw_store = ds_mod.MarketStore(db=test_db, price_mode="raw")
    return service


class TestFetchPaths:
    def test_fetch_daily_history_sets_provider(self, monkeypatch, test_db) -> None:
        service = _service(monkeypatch, test_db)
        df = service.fetch_daily_history("510300.SS", date(2026, 8, 1), date(2026, 8, 21))
        assert not df.empty
        assert (df["provider"] == "tickflow").all()

    def test_fetch_daily_history_empty_raises(self, monkeypatch, test_db) -> None:
        service = _service(monkeypatch, test_db, _FakeProvider(df=pd.DataFrame()))
        with pytest.raises(DataProviderError):
            service.fetch_daily_history("510300.SS", date(2026, 8, 1), date(2026, 8, 21))

    def test_fetch_daily_histories_batch(self, monkeypatch, test_db) -> None:
        service = _service(monkeypatch, test_db)
        data, errors = service.fetch_daily_histories(
            ["510300.SS", "510500.SS"], date(2026, 8, 1), date(2026, 8, 21)
        )
        assert set(data) == {"510300.SS", "510500.SS"}
        assert errors == {}
        assert (data["510300.SS"]["provider"] == "tickflow").all()

    def test_fetch_daily_histories_errors(self, monkeypatch, test_db) -> None:
        service = _service(monkeypatch, test_db, _FakeProvider(fail=True))
        data, errors = service.fetch_daily_histories(["510300.SS"], date(2026, 8, 1), date(2026, 8, 21))
        assert data == {}
        assert "510300.SS" in errors


class TestBackfillDailyHistory:
    def test_backfill_writes_raw_and_rematerializes(self, monkeypatch, test_db) -> None:
        service = _service(monkeypatch, test_db)
        result = service.backfill_daily_history(
            symbol="510300.SS",
            start_date=date(2026, 8, 17),
            end_date=date(2026, 8, 21),
        )
        assert result["status"] == "updated"
        assert result["added_rows"] > 0
        # raw 与 qfq 都落了
        assert not test_db.load_market_data("510300.SS", price_mode="raw").empty
        assert not test_db.load_market_data("510300.SS", price_mode="qfq").empty

    def test_backfill_no_data(self, monkeypatch, test_db) -> None:
        service = _service(monkeypatch, test_db, _FakeProvider(df=pd.DataFrame()))
        # 批量路径：空帧不 raise，结果记 no_data
        results = service.backfill_daily_histories(
            [{"symbol": "510300.SS", "start_date": date(2026, 8, 17)}],
            end_date=date(2026, 8, 21),
        )
        assert results[0]["result"]["status"] == "no_data"

    def test_backfill_histories_batch(self, monkeypatch, test_db) -> None:
        service = _service(monkeypatch, test_db)
        results = service.backfill_daily_histories(
            [{"symbol": "510300.SS", "start_date": date(2026, 8, 17)}],
            end_date=date(2026, 8, 21),
        )
        assert len(results) == 1
        assert results[0]["result"]["symbol"] == "510300.SS"


class TestEnsureDailyHistoryBranches:
    def test_incremental_append(self, monkeypatch, test_db) -> None:
        """本地已有部分数据 → 增量补齐缺口。"""
        service = _service(monkeypatch, test_db)
        test_db.save_market_data("510300.SS", _bars(3), price_mode="raw")
        result = service.ensure_daily_history(
            "510300.SS", date(2026, 8, 17), date(2026, 8, 21)
        )
        assert result["status"] in ("updated", "up_to_date")

    def test_factors_changed_rematerializes(self, monkeypatch, test_db) -> None:
        service = _service(monkeypatch, test_db)
        test_db.save_market_data("510300.SS", _bars(5), price_mode="raw")
        result = service.ensure_daily_history(
            "510300.SS",
            date(2026, 8, 17),
            date(2026, 8, 21),
            factors=[(date(2026, 8, 20), 1.1)],
            factors_changed=True,
        )
        assert result["factors_changed"] is True

    def test_rematerialize_raw_incomplete(self, monkeypatch, test_db) -> None:
        """raw 覆盖不如存量 qfq → raw_incomplete 拒绝物化。"""
        service = _service(monkeypatch, test_db)
        # raw 只有 2 根；qfq 存量 5 根且更早
        test_db.save_market_data("510300.SS", _bars(2, start=date(2026, 8, 20)), price_mode="raw")
        test_db.save_market_data("510300.SS", _bars(5, start=date(2026, 8, 1)), price_mode="qfq")
        result = service.rematerialize_qfq("510300.SS", db=test_db)
        assert result["status"] == "raw_incomplete"


# ---------------------------------------------------------------------------
# main.py 任务闭包与 AuthWall 续期
# ---------------------------------------------------------------------------
class TestMainJobClosures:
    def test_run_daily_update_full_path(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        """_run_daily_update：非跳过 → 跑盘后管线 + 落 indicator_rebuild job。"""
        import data.storage.db as db_module
        from app import main
        from tests.unit.test_main_lifespan import _run_lifespan_and_capture_threads

        monkeypatch.setattr(db_module, "get_db", lambda: test_db)
        monkeypatch.setattr(db_module, "_db_instance", test_db)
        monkeypatch.setattr(
            main,
            "daily_market_update_job",
            lambda settings, force=False: {"status": "ok", "total": 1, "success": 1, "failed": 0, "symbols": ["X.SS"]},
        )
        pipeline_calls: list = []
        monkeypatch.setattr(
            "services.indicator_builder.run_post_update_pipeline",
            lambda settings, service, payload, symbols, day: pipeline_calls.append(symbols) or {"status": "ok", "rebuilt": 1},
        )

        created = _run_lifespan_and_capture_threads(monkeypatch, test_db)
        # update_job 是传给 scheduler.start 的关键字参数——从 FakeSchedulerManager 拿不到，
        # 但补跑路径走同一入口：直接用捕获的 catchup 触发（behind schedule）
        test_db.record_job_run("daily_update", {"ts": "2026-08-20T16:35:00"}, run_date="2026-08-20", status="completed")
        monkeypatch.setattr("core.calendar.market_now", lambda: __import__("datetime").datetime(2026, 8, 21, 17, 0).astimezone())
        monkeypatch.setattr("core.calendar.is_trading_day", lambda d: True)
        monkeypatch.setattr("core.calendar.previous_trading_day", lambda d: date(2026, 8, 20))

        for t in created:
            if getattr(t.target, "__name__", "") == "_daily_update_catchup":
                t.target()
        assert pipeline_calls == [["X.SS"]]
        run = test_db.get_latest_job_run("indicator_rebuild")
        assert run is not None

    def test_auth_wall_renewed_cookie(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        """滑动续期触发时响应头重发 Set-Cookie。"""
        from app import main

        async def dummy_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        mw = main.AuthWallMiddleware(dummy_app)
        user = {"id": 1, "username": "u", "is_admin": False}
        monkeypatch.setattr(
            main.auth_service, "resolve_session", lambda token, db=None: (user, True)
        )
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/manual-trade",
            "headers": [(b"cookie", b"tq_session=tok")],
            "query_string": b"",
            "state": {},
        }
        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        asyncio = __import__("asyncio")
        asyncio.run(mw(scope, None, send))
        headers = dict(sent[0]["headers"])
        assert b"tq_session=tok" in headers.get(b"set-cookie", b"")
        assert scope["state"]["user"] == user
