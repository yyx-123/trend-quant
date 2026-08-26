"""附录 A：db.py 关键路径（ex_factors 往返/幂等、看板历史窗口、revision 版本递增）、
condition_engine 冷却期特判、engine 边界契约、display shim 一致性。"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# db.py
# ---------------------------------------------------------------------------
class TestExFactorsRoundtrip:
    def test_save_load_roundtrip_and_idempotent(self, test_db) -> None:
        factors = [("2026-08-01", 1.05), ("2026-07-01", 1.10)]
        test_db.save_ex_factors("AAA.SS", factors, provider="test")
        first = test_db.load_ex_factors("AAA.SS")
        assert first == [("2026-07-01", 1.10), ("2026-08-01", 1.05)]  # 升序

        # 重复写幂等（INSERT OR REPLACE）
        test_db.save_ex_factors("AAA.SS", factors, provider="test")
        assert test_db.load_ex_factors("AAA.SS") == first

    def test_save_skips_invalid_factors(self, test_db) -> None:
        test_db.save_ex_factors("AAA.SS", [("2026-08-01", 0), ("2026-08-02", -1), ("2026-08-03", 1.2)])
        assert test_db.load_ex_factors("AAA.SS") == [("2026-08-03", 1.2)]


class TestLoadMarketDashboardHistory:
    def _seed(self, test_db, days: int = 40) -> None:
        start = date(2026, 7, 1)
        bars = pd.DataFrame([
            {
                "time": (start + timedelta(days=i)).isoformat(),
                "open": 1, "high": 1, "low": 1, "close": 1,
                "volume": 1, "amount": 100 + i,
            }
            for i in range(days)
        ])
        test_db.save_market_data("AAA.SS", bars, price_mode="qfq")
        test_db.save_instrument_metadata([
            {"symbol": "AAA.SS", "name": "测试", "category_l1": "L1", "category_l2": "L2",
             "category_l3": "L3", "enabled": True},
        ])

    def test_window_and_fields(self, test_db) -> None:
        self._seed(test_db, days=40)
        rows = test_db.load_market_dashboard_history(days=30)
        assert rows, "expected rows"
        times = sorted(str(r["time"])[:10] for r in rows if r["symbol"] == "AAA.SS")
        assert len(times) <= 30
        # 只含最近 30 根
        assert times == times[-30:]
        # category/amount 字段在
        row = rows[0]
        assert "category_l1" in row and "amount" in row


class TestDashboardRevisionVersion:
    def test_version_bumps_on_write(self, test_db) -> None:
        bars = pd.DataFrame([
            {"time": "2026-08-20", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "amount": 1}
        ])
        v0 = test_db.get_data_version("market_data_qfq")
        test_db.save_market_data("AAA.SS", bars, price_mode="qfq")
        v1 = test_db.get_data_version("market_data_qfq")
        assert v1 > v0

        revision = test_db.get_market_dashboard_revision()
        assert revision[2] == v1  # 第三元素为内容版本
        assert revision[0].startswith("2026-08-20")  # MAX(time) 感知 append


# ---------------------------------------------------------------------------
# condition_engine：days_since_last_exit 特判
# ---------------------------------------------------------------------------
class TestDaysSinceLastExit:
    def _engine(self):
        from rule_backtest.condition_engine import ConditionEngine
        from rule_backtest.value_resolver import ValueResolver

        return ConditionEngine(ValueResolver())

    def _group(self, op: str, right: float) -> dict:
        return {
            "combinator": "all",
            "children": [
                {
                    "left": {"type": "state_value", "name": "days_since_last_exit"},
                    "operator": op,
                    "right": {"type": "literal", "value": right},
                }
            ],
        }

    def test_none_gte_passes_lte_fails(self) -> None:
        """从未卖出（None）：>= 特判通过（首次入场不受冷却限制），<= 不通过。"""
        from rule_backtest.models import PositionState

        engine = self._engine()
        position = PositionState()  # 从未卖出：last_exit_bar_idx = None
        bars = pd.DataFrame(
            {"date": [date(2026, 1, 5)], "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]}
        )
        passed_gte, _ = engine.evaluate_group(self._group(">=", 5), bars=bars, position=position)
        assert passed_gte is True
        passed_lte, _ = engine.evaluate_group(self._group("<=", 5), bars=bars, position=position)
        assert passed_lte is False


# ---------------------------------------------------------------------------
# engine 边界契约
# ---------------------------------------------------------------------------
class TestEngineBoundaries:
    def _request(self, bars, **kw):
        from rule_backtest.models import BacktestExecutionConfig, RuleBacktestRequest

        return RuleBacktestRequest(
            strategy=kw.get("strategy") or {"entry": {"combinator": "all", "children": []}, "exit": {"combinator": "any", "children": []}},
            symbol="TEST.SS",
            bars=bars,
            start_date=kw.get("start_date"),
            end_date=kw.get("end_date"),
            execution=BacktestExecutionConfig(),
            sizer=None,
        )

    def test_empty_bars_raises(self) -> None:
        from rule_backtest.engine import SingleSymbolAllInBacktestEngine

        with pytest.raises(ValueError, match="no market data"):
            SingleSymbolAllInBacktestEngine().run(self._request(pd.DataFrame(columns=["date", "close"])))

    def test_single_bar_runs(self) -> None:
        from rule_backtest.engine import SingleSymbolAllInBacktestEngine

        bars = pd.DataFrame([
            {"date": date(2026, 1, 5), "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 1, "amount": 1}
        ])
        result = SingleSymbolAllInBacktestEngine().run(self._request(bars))
        assert result["status"] == "ok"
        # 单根 bar 必然零交易、净值曲线恰 1 个点
        assert result["summary"]["trade_count"] == 0
        assert len(result["daily_nav"]) == 1

    def test_start_after_end_raises(self) -> None:
        from rule_backtest.engine import SingleSymbolAllInBacktestEngine

        bars = pd.DataFrame([
            {"date": date(2026, 1, 5) + timedelta(days=i), "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 1, "amount": 1}
            for i in range(5)
        ])
        with pytest.raises(ValueError):
            SingleSymbolAllInBacktestEngine().run(
                self._request(bars, start_date=date(2026, 1, 10), end_date=date(2026, 1, 6))
            )


# ---------------------------------------------------------------------------
# display shim 一致性（core.display vs app.instrument_display 防漂移）
# ---------------------------------------------------------------------------
class TestDisplayShimConsistency:
    @pytest.mark.parametrize(
        "name",
        ["build_symbol_display", "format_symbol_display", "load_instrument_name_map", "strip_etf_suffix", "symbol_to_code"],
    )
    def test_shim_reexports_same_object(self, name: str) -> None:
        import app.instrument_display as shim
        import core.display as canonical

        assert getattr(shim, name) is getattr(canonical, name)


class TestMarkInterruptedJobRuns:
    def test_orphan_running_marked_interrupted(self, test_db) -> None:
        # 孤儿：running 无终态配对
        test_db.record_job_run(
            "instrument_add", {"job_id": "orphan1", "symbol": "X.SS"}, status="running"
        )
        # 善终：running + 同 job_id 终态
        test_db.record_job_run(
            "instrument_add", {"job_id": "ok1", "symbol": "Y.SS"}, status="running"
        )
        test_db.record_job_run(
            "instrument_add", {"job_id": "ok1", "status": "completed"}, status="completed"
        )
        marked = test_db.mark_interrupted_job_runs(["instrument_add"])
        assert marked == 1
        runs = test_db.list_job_runs("instrument_add", limit=10)
        statuses = [r["status"] for r in runs]
        assert statuses.count("interrupted") == 1
        assert statuses.count("completed") == 1
        assert statuses.count("running") == 1  # 善终的 running 行保持原样

    def test_instrument_jobs_sweep_entry(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        import data.storage.db as db_module

        monkeypatch.setattr(db_module, "get_db", lambda: test_db)
        monkeypatch.setattr(db_module, "_db_instance", test_db)
        test_db.record_job_run("etf_constituent_import", {"job_id": "o2"}, status="running")

        from services.instrument_jobs import mark_interrupted_at_startup

        assert mark_interrupted_at_startup() == 1
