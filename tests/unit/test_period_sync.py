"""provider/service 的周期（周/月K）路径。

- provider：fetch_history/fetch_histories 把 period 透传给 vendor，
  日K 包装方法（fetch_daily_history / fetch_daily_histories）行为不变；
- service：update_pool_periods 的增量/整段重取计划、未收盘 bar 丢弃、
  除权后 qfq 整段重取、失败重试与 job_runs 记录。
"""

from __future__ import annotations

import os
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import data.service as data_service
from data.provider_tickflow import TickFlowProvider
from data.service import _period_fetch_start


def _weekly_frame(rows: list[tuple[str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "time": time,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 100.0,
                "amount": 100.0 * price,
            }
            for time, price in rows
        ]
    )


# ---------------------------------------------------------------------------
# provider
# ---------------------------------------------------------------------------
class TestProviderPeriod:
    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "k"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_fetch_history_passes_period(self, tickflow_cls: MagicMock) -> None:
        client = tickflow_cls.return_value
        client.klines.get.return_value = pd.DataFrame(
            [{"trade_date": "2026-09-11", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "amount": 1}]
        )
        provider = TickFlowProvider()
        provider.fetch_history("510300.SS", date(2026, 9, 1), date(2026, 9, 11), "qfq", period="1w")
        _args, kwargs = client.klines.get.call_args
        assert kwargs["period"] == "1w"
        provider.close()

    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "k"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_monthly_alias_and_case(self, tickflow_cls: MagicMock) -> None:
        """'monthly' 与 '1M' 都必须是月线；'1M' 不能被当成分钟线。"""
        client = tickflow_cls.return_value
        client.klines.get.return_value = pd.DataFrame(
            [{"trade_date": "2026-08-31", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "amount": 1}]
        )
        provider = TickFlowProvider()
        for period in ("1M", "monthly", "M"):
            provider.fetch_history("510300.SS", date(2026, 8, 1), date(2026, 8, 31), "none", period=period)
            _args, kwargs = client.klines.get.call_args
            assert kwargs["period"] == "1M"
        provider.close()

    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "k"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_fetch_histories_passes_period_to_batch(self, tickflow_cls: MagicMock) -> None:
        client = tickflow_cls.return_value
        client.klines.batch.return_value = {
            "510300.SH": {"timestamp": [1789056000000], "open": [1], "high": [1], "low": [1], "close": [1], "volume": [1], "amount": [1]}
        }
        provider = TickFlowProvider()
        data, errors = provider.fetch_histories(
            ["510300.SS"], date(2026, 8, 1), date(2026, 9, 12), "none", period="1M",
            batch_size=10, request_interval_seconds=0,
        )
        assert errors == {}
        _args, kwargs = client.klines.batch.call_args
        assert kwargs["period"] == "1M"
        assert list(data) == ["510300.SS"]
        provider.close()

    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "k"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_daily_wrappers_still_send_1d(self, tickflow_cls: MagicMock) -> None:
        client = tickflow_cls.return_value
        client.klines.get.return_value = pd.DataFrame(
            [{"trade_date": "2026-09-11", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "amount": 1}]
        )
        client.klines.batch.return_value = {}
        provider = TickFlowProvider()
        provider.fetch_daily_history("510300.SS", date(2026, 9, 1), date(2026, 9, 11), "qfq")
        assert client.klines.get.call_args.kwargs["period"] == "1d"
        provider.fetch_daily_histories(
            ["510300.SS"], date(2026, 9, 1), date(2026, 9, 11), "qfq", request_interval_seconds=0
        )
        assert client.klines.batch.call_args.kwargs["period"] == "1d"
        provider.close()

    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "k"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_minute_period_raises_before_network(self, tickflow_cls: MagicMock) -> None:
        client = tickflow_cls.return_value
        provider = TickFlowProvider()
        with pytest.raises(ValueError):
            provider.fetch_history("510300.SS", date(2026, 9, 1), date(2026, 9, 11), "none", period="1m")
        client.klines.get.assert_not_called()
        provider.close()


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------
class _FakeProvider:
    """可控 provider：按 (period, adjust) 返回预设帧，并记录抓取窗口。"""

    name = "tickflow"

    def __init__(self, frames: dict, errors: dict | None = None) -> None:
        self.frames = frames
        self.errors = errors or {}
        self.calls: list[dict] = []

    def fetch_history(self, symbol, start, end, adjust, period="1d", **kwargs):
        data, errors = self.fetch_histories([symbol], start, end, adjust, period=period, **kwargs)
        if symbol in errors:
            raise RuntimeError(errors[symbol])
        if symbol not in data:
            return pd.DataFrame()
        return data[symbol]

    def fetch_histories(self, symbols, start, end, adjust, period="1d", **kwargs):
        self.calls.append({"symbols": list(symbols), "start": start, "end": end, "adjust": adjust, "period": period})
        data: dict[str, pd.DataFrame] = {}
        errors: dict[str, str] = {}
        for symbol in symbols:
            key = (period, adjust, symbol)
            if key in self.errors:
                errors[symbol] = self.errors[key]
            elif key in self.frames:
                frame = self.frames[key]
                if not frame.empty:
                    frame = frame.copy()
                    mask = (pd.to_datetime(frame["time"]).dt.date >= start) & (
                        pd.to_datetime(frame["time"]).dt.date <= end
                    )
                    frame = frame.loc[mask].reset_index(drop=True)
                if not frame.empty:
                    data[symbol] = frame
        return data, errors

    def close(self) -> None:  # pragma: no cover - 接口占位
        pass


def _make_service(monkeypatch, provider: _FakeProvider, test_db) -> data_service.DataService:
    import data.storage.db as db_module

    monkeypatch.setattr(db_module, "get_db", lambda: test_db)
    monkeypatch.setattr(db_module, "_db_instance", test_db)
    service = data_service.DataService.__new__(data_service.DataService)
    service.providers = {"tickflow": provider}
    service.provider_priority = ["tickflow"]
    service.tickflow_settings = None
    return service


class TestPeriodFetchStart:
    """增量窗口起点：晚于已收盘 bar 的下一个周期首日（下限夹紧）。"""

    def test_week_bar_moves_to_next_monday(self) -> None:
        # 周 bar 标在周五，次日仍是同周期（周六）——必须推到下周一
        assert _period_fetch_start(date(2026, 8, 28), "1w", date(1990, 1, 1)) == date(2026, 8, 31)
        # 短周（国庆前最后交易日 9-30）：下一个 ISO 周从 10-06 开始，
        # 10-01~10-05 仍属同一周，窗口起点不能早于 10-06
        assert _period_fetch_start(date(2025, 9, 30), "1w", date(1990, 1, 1)) == date(2025, 10, 6)

    def test_month_bar_moves_to_next_month(self) -> None:
        assert _period_fetch_start(date(2026, 8, 31), "1M", date(1990, 1, 1)) == date(2026, 9, 1)
        # 停牌标的的月 bar 标在月中，不能只加一天（否则会把当月后续 bar 一起返回）
        assert _period_fetch_start(date(2015, 12, 18), "1M", date(1990, 1, 1)) == date(2016, 1, 1)

    def test_floor_is_respected(self) -> None:
        # 下限更晚时以它为准
        assert _period_fetch_start(date(2026, 8, 28), "1w", date(2026, 9, 30)) == date(2026, 9, 30)
        # 下限落在下一周期内时保留下限
        assert _period_fetch_start(date(2026, 8, 28), "1w", date(2026, 9, 2)) == date(2026, 9, 2)

    def test_never_returns_earlier_than_floor(self) -> None:
        for after, not_before in (
            (date(2026, 8, 28), date(2026, 8, 29)),
            (date(2015, 12, 18), date(2015, 12, 19)),
            (date(2026, 9, 11), date(1990, 1, 1)),
        ):
            for period in ("1w", "1M"):
                assert _period_fetch_start(after, period, not_before) >= not_before


class TestUpdatePoolPeriods:
    def test_full_bootstrap_then_incremental(self, monkeypatch, test_db) -> None:
        frames = {
            ("1w", "none", "AAA.SS"): _weekly_frame([("2026-08-28", 1.0), ("2026-09-04", 2.0), ("2026-09-11", 3.0)]),
            ("1w", "qfq", "AAA.SS"): _weekly_frame([("2026-08-28", 0.9), ("2026-09-04", 1.8), ("2026-09-11", 2.7)]),
        }
        provider = _FakeProvider(frames)
        service = _make_service(monkeypatch, provider, test_db)
        monkeypatch.setattr(service, "sync_ex_factors", lambda symbols, db=None: ({}, []))

        payload = service.update_pool_periods(["AAA.SS"], ("1w",), end_date=date(2026, 9, 12), job_type="")
        info = payload["periods"]["1w"]
        assert info["updated"] == 1
        assert info["full_refetch"] == 1
        assert payload["failed"] == 0
        assert test_db.get_market_data_summary("AAA.SS", "qfq", "1w")["rows"] == 3
        assert test_db.get_market_data_summary("AAA.SS", "raw", "1w")["rows"] == 3
        # 首段起点是 vendor 全历史
        assert provider.calls[0]["start"] == data_service.PERIOD_HISTORY_START

        # 第二轮：已最新 → 零网络请求、零写入
        provider.calls.clear()
        payload2 = service.update_pool_periods(["AAA.SS"], ("1w",), end_date=date(2026, 9, 12), job_type="")
        info2 = payload2["periods"]["1w"]
        assert info2["updated"] == 0 and info2["up_to_date"] == 1
        assert info2["planned"] == 0
        assert info2["full_refetch"] == 0
        assert provider.calls == []

    def test_incremental_skips_week_of_last_stored_bar(self, monkeypatch, test_db) -> None:
        """已在库的周 bar 不得在下一轮被当作「在途 bar」重复抓回。"""
        test_db.save_market_data("INC.SS", _weekly_frame([("2026-08-28", 1.0)]), price_mode="raw", period="1w")
        test_db.save_market_data("INC.SS", _weekly_frame([("2026-08-28", 1.0)]), price_mode="qfq", period="1w")
        frames = {
            ("1w", "none", "INC.SS"): _weekly_frame([("2026-08-28", 9.9), ("2026-09-04", 2.0)]),
            ("1w", "qfq", "INC.SS"): _weekly_frame([("2026-08-28", 9.9), ("2026-09-04", 2.0)]),
        }
        provider = _FakeProvider(frames)
        service = _make_service(monkeypatch, provider, test_db)
        monkeypatch.setattr(service, "sync_ex_factors", lambda symbols, db=None: ({}, []))
        service.update_pool_periods(["INC.SS"], ("1w",), end_date=date(2026, 9, 12), job_type="")
        assert provider.calls[0]["start"] == date(2026, 8, 31)
        stored = test_db.load_market_data("INC.SS", period="1w")
        assert [str(day)[:10] for day in stored["time"]] == ["2026-08-28", "2026-09-04"]
        # 历史行未被改写（窗口已排除该 bar）
        assert float(stored.iloc[0]["close"]) == 1.0

    def test_open_period_bars_are_not_stored(self, monkeypatch, test_db) -> None:
        """在途周/月 bar 不落库：9 月的月 bar 在 9 月中不可入库。"""
        frames = {
            ("1M", "none", "BBB.SS"): _weekly_frame([("2026-07-31", 1.0), ("2026-08-31", 2.0), ("2026-09-11", 3.0)]),
            ("1M", "qfq", "BBB.SS"): _weekly_frame([("2026-07-31", 1.0), ("2026-08-31", 2.0), ("2026-09-11", 3.0)]),
        }
        service = _make_service(monkeypatch, _FakeProvider(frames), test_db)
        monkeypatch.setattr(service, "sync_ex_factors", lambda symbols, db=None: ({}, []))
        payload = service.update_pool_periods(["BBB.SS"], ("1M",), end_date=date(2026, 9, 12), job_type="")
        info = payload["periods"]["1M"]
        assert info["dropped_open_period"] >= 1
        stored = test_db.load_market_data("BBB.SS", period="1M")
        assert [str(day)[:10] for day in stored["time"]] == ["2026-07-31", "2026-08-31"]

    def test_ex_factor_change_refetches_qfq_full_range(self, monkeypatch, test_db) -> None:
        """除权后 qfq 必须整段重取（历史 bar 的前复权价全部变化）。"""
        test_db.save_market_data("CCC.SS", _weekly_frame([("2026-08-28", 1.0)]), price_mode="raw", period="1w")
        test_db.save_market_data("CCC.SS", _weekly_frame([("2026-08-28", 1.0)]), price_mode="qfq", period="1w")
        frames = {
            ("1w", "none", "CCC.SS"): _weekly_frame([("2026-08-21", 6.0), ("2026-08-28", 7.0)]),
            ("1w", "qfq", "CCC.SS"): _weekly_frame([("2026-08-21", 0.5), ("2026-08-28", 0.7)]),
        }
        provider = _FakeProvider(frames)
        service = _make_service(monkeypatch, provider, test_db)
        monkeypatch.setattr(service, "sync_ex_factors", lambda symbols, db=None: ({}, ["CCC.SS"]))

        payload = service.update_pool_periods(["CCC.SS"], ("1w",), end_date=date(2026, 9, 12), job_type="")
        info = payload["periods"]["1w"]
        assert info["full_refetch"] == 1
        qfq_call = next(call for call in provider.calls if call["adjust"] == "qfq")
        assert qfq_call["start"] == data_service.PERIOD_HISTORY_START
        # 历史 qfq 行被整段覆盖（含 08-21 这根之前不存在的 bar）
        assert float(test_db.load_market_data("CCC.SS", "qfq", "1w").iloc[0]["close"]) == 0.5
        # raw 表仍保留旧行、并 upsert 新行
        assert len(test_db.load_market_data("CCC.SS", "raw", "1w")) == 2

    def test_failure_is_retried_then_reported(self, monkeypatch, test_db) -> None:
        provider = _FakeProvider({}, errors={("1w", "none", "DDD.SS"): "网络超时"})
        service = _make_service(monkeypatch, provider, test_db)
        monkeypatch.setattr(service, "sync_ex_factors", lambda symbols, db=None: ({}, []))
        payload = service.update_pool_periods(
            ["DDD.SS"], ("1w",), end_date=date(2026, 9, 12), max_retries=1, retry_interval_seconds=0, job_type=""
        )
        info = payload["periods"]["1w"]
        assert info["failed"] == 1 and info["failed_symbols"] == ["DDD.SS"]
        assert payload["failed"] == 1
        assert payload["status"] == "failed"
        # 每轮 raw+qfq 各一次 → 2 轮共 4 次抓取
        assert len(provider.calls) == 4

    def test_non_retryable_permission_error_skips_retry(self, monkeypatch, test_db) -> None:
        provider = _FakeProvider(
            {}, errors={("1w", "none", "EEE.SS"): "无日/周/月K线查询批量查询权限"}
        )
        service = _make_service(monkeypatch, provider, test_db)
        monkeypatch.setattr(service, "sync_ex_factors", lambda symbols, db=None: ({}, []))
        payload = service.update_pool_periods(
            ["EEE.SS"], ("1w",), end_date=date(2026, 9, 12), max_retries=3, retry_interval_seconds=0, job_type=""
        )
        assert payload["periods"]["1w"]["failed"] == 1
        assert len(provider.calls) == 2  # 只试了一轮（raw+qfq）

    def test_full_and_incremental_are_batched_separately(self, monkeypatch, test_db) -> None:
        """整段重取不应把同批增量的抓取窗口一起拉长。"""
        test_db.save_market_data("OLD.SS", _weekly_frame([("2026-08-28", 1.0)]), price_mode="raw", period="1w")
        test_db.save_market_data("OLD.SS", _weekly_frame([("2026-08-28", 1.0)]), price_mode="qfq", period="1w")
        frames = {
            ("1w", "none", "OLD.SS"): _weekly_frame([("2026-08-28", 1.0)]),
            ("1w", "qfq", "OLD.SS"): _weekly_frame([("2026-08-28", 1.0)]),
            ("1w", "none", "NEW.SS"): _weekly_frame([("2026-09-04", 2.0)]),
            ("1w", "qfq", "NEW.SS"): _weekly_frame([("2026-09-04", 2.0)]),
        }
        provider = _FakeProvider(frames)
        service = _make_service(monkeypatch, provider, test_db)
        monkeypatch.setattr(service, "sync_ex_factors", lambda symbols, db=None: ({}, []))
        service.update_pool_periods(["OLD.SS", "NEW.SS"], ("1w",), end_date=date(2026, 9, 12), job_type="")
        batches = [(tuple(call["symbols"]), call["start"]) for call in provider.calls if call["adjust"] == "none"]
        assert (("NEW.SS",), data_service.PERIOD_HISTORY_START) in batches
        assert (("OLD.SS",), date(2026, 8, 31)) in batches

    def test_job_run_recorded(self, monkeypatch, test_db) -> None:
        frames = {
            ("1w", "none", "FFF.SS"): _weekly_frame([("2026-09-11", 1.0)]),
            ("1w", "qfq", "FFF.SS"): _weekly_frame([("2026-09-11", 1.0)]),
        }
        service = _make_service(monkeypatch, _FakeProvider(frames), test_db)
        monkeypatch.setattr(service, "sync_ex_factors", lambda symbols, db=None: ({}, []))
        service.update_pool_periods(["FFF.SS"], ("1w",), end_date=date(2026, 9, 12))
        run = test_db.get_latest_job_run("period_update")
        assert run is not None
        assert run["status"] == "completed"

    def test_daily_period_argument_is_ignored(self, monkeypatch, test_db) -> None:
        """传 ('1d',) 不抓任何周期表——日K归日更流程管。"""
        provider = _FakeProvider({})
        service = _make_service(monkeypatch, provider, test_db)
        monkeypatch.setattr(service, "sync_ex_factors", lambda symbols, db=None: ({}, []))
        payload = service.update_pool_periods(["AAA.SS"], ("1d",), end_date=date(2026, 9, 12), job_type="")
        assert payload["status"] == "noop"
        assert provider.calls == []

    def test_empty_symbols_is_noop(self, monkeypatch, test_db) -> None:
        service = _make_service(monkeypatch, _FakeProvider({}), test_db)
        payload = service.update_pool_periods([], ("1w",), end_date=date(2026, 9, 12), job_type="")
        assert payload["status"] == "noop"
        assert payload["periods"] == {}


class TestBatchedPeriodWrites:
    """一批标的只落一次库（逐标的写库每次新建连接 + 独立事务，是日更的主要耗时）。"""

    def test_batch_is_written_in_one_call_per_price_mode(self, monkeypatch, test_db) -> None:
        from data.storage.market_store import MarketStore

        frames = {}
        for symbol in ("A1.SS", "A2.SS", "A3.SS"):
            frames[("1w", "none", symbol)] = _weekly_frame([("2026-09-11", 1.0)])
            frames[("1w", "qfq", symbol)] = _weekly_frame([("2026-09-11", 0.9)])
        service = _make_service(monkeypatch, _FakeProvider(frames), test_db)
        monkeypatch.setattr(service, "sync_ex_factors", lambda symbols, db=None: ({}, []))

        calls: list[tuple[str, int]] = []
        original = MarketStore.save_history_many

        def spy(self, items):
            pairs = list(items)
            calls.append((self.price_mode, len(pairs)))
            return original(self, pairs)

        monkeypatch.setattr(MarketStore, "save_history_many", spy)
        payload = service.update_pool_periods(
            ["A1.SS", "A2.SS", "A3.SS"], ("1w",), end_date=date(2026, 9, 12), job_type=""
        )

        # raw 与 qfq 各一次批量写、各带 3 只标的；而不是 3×2 次单标的写
        assert sorted(calls) == [("qfq", 3), ("raw", 3)]
        assert payload["periods"]["1w"]["updated"] == 3
        for symbol in ("A1.SS", "A2.SS", "A3.SS"):
            assert test_db.get_market_data_summary(symbol, "qfq", "1w")["rows"] == 1

    def test_batched_write_failure_falls_back_to_per_symbol(self, monkeypatch, test_db) -> None:
        """批量写失败不牵连整批：退回逐标的写，坏的只影响自己。"""
        from data.storage.market_store import MarketStore

        frames = {}
        for symbol in ("B1.SS", "B2.SS"):
            frames[("1w", "none", symbol)] = _weekly_frame([("2026-09-11", 1.0)])
            frames[("1w", "qfq", symbol)] = _weekly_frame([("2026-09-11", 1.0)])
        service = _make_service(monkeypatch, _FakeProvider(frames), test_db)
        monkeypatch.setattr(service, "sync_ex_factors", lambda symbols, db=None: ({}, []))

        original_many = MarketStore.save_history_many
        original_one = MarketStore.save_history
        single_calls: list[tuple[str, str]] = []

        def failing_many(self, items):
            if self.price_mode == "qfq":
                raise RuntimeError("simulated write failure")
            return original_many(self, items)

        def spy_one(self, symbol, df):
            single_calls.append((self.price_mode, symbol))
            return original_one(self, symbol, df)

        monkeypatch.setattr(MarketStore, "save_history_many", failing_many)
        monkeypatch.setattr(MarketStore, "save_history", spy_one)
        payload = service.update_pool_periods(["B1.SS", "B2.SS"], ("1w",), end_date=date(2026, 9, 12), job_type="")

        assert sorted(single_calls) == [("qfq", "B1.SS"), ("qfq", "B2.SS")]
        assert payload["periods"]["1w"]["updated"] == 2

    def test_coverage_reflects_db_even_when_nothing_fetched(self, monkeypatch, test_db) -> None:
        """全部已最新时覆盖区间仍反映库内现状（而不是 None ~ None）。"""
        test_db.save_market_data("C1.SS", _weekly_frame([("2026-09-11", 1.0)]), price_mode="raw", period="1w")
        test_db.save_market_data("C1.SS", _weekly_frame([("2026-09-11", 1.0)]), price_mode="qfq", period="1w")
        provider = _FakeProvider({})
        service = _make_service(monkeypatch, provider, test_db)
        monkeypatch.setattr(service, "sync_ex_factors", lambda symbols, db=None: ({}, []))
        payload = service.update_pool_periods(["C1.SS"], ("1w",), end_date=date(2026, 9, 12), job_type="")
        info = payload["periods"]["1w"]
        assert info["planned"] == 0 and info["up_to_date"] == 1
        assert provider.calls == []
        assert info["coverage"] == {"start": "2026-09-11", "end": "2026-09-11"}


class TestDailyJobWiring:
    def test_daily_job_syncs_periods_and_summarizes(self, monkeypatch, test_db) -> None:
        """日更任务挂载周/月K：payload 只留汇总，逐标的明细不进 job_runs。"""
        from core import jobs

        class _StubService:
            def update_pool_daily(self, **kwargs):
                return {"total": 1, "success": 1, "failed": 0, "failed_symbols": [], "results": []}

            def update_pool_periods(self, symbols, periods=None, **kwargs):
                return {
                    "periods": {
                        "1w": {"planned": 1, "updated": 1, "failed": 0, "results": [{"symbol": "AAA.SS"}]},
                        "1M": {"planned": 1, "updated": 0, "failed": 0, "results": []},
                    }
                }

        monkeypatch.setattr(jobs, "_pool_symbols", lambda: ["AAA.SS"])
        monkeypatch.setattr(jobs, "get_strategy_config", lambda: {"backtest_start_primary": "2025-01-01"})
        payload = jobs.daily_market_update_job(
            MagicMock(app=MagicMock(daily_update_max_retries=1, daily_update_retry_interval_seconds=1)),
            data_service=_StubService(),
            force=True,
        )
        assert set(payload["periods"]) == {"1w", "1M"}
        assert "results" not in payload["periods"]["1w"]
        assert payload["periods"]["1w"]["updated"] == 1
