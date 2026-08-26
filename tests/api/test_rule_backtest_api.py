"""API tests for the async rule backtest endpoints.

POST /rule-backtest/api/run starts a background thread and returns a run_id;
GET /rule-backtest/api/progress/{run_id} reports K-line-level progress only
(never the result, so polling stays cheap); GET /rule-backtest/api/result/{run_id}
serves the slimmed result once finished. The real engine is replaced by a fake
service so tests stay fast.
"""

from __future__ import annotations

import threading

import pytest


def _fake_result(symbol: str) -> dict:
    return {
        "status": "ok",
        "run_id": "fake-run",
        "symbol": symbol,
        "summary": {"return": 0.1},
        "trades": [{"date": "2026-01-05", "side": "BUY"}],
        "results": [
            {
                "strategy_id": "s1",
                "strategy_name": "s1",
                "summary": {"return": 0.1},
                "trades": [],
                "daily_nav": [{"date": "2026-01-05", "equity": 100000.0}],
                "charts": {"kline": {"dates": ["2026-01-05"], "candles": [[1, 2, 3, 4]]}},
                "drawdown": [{"date": "2026-01-05", "drawdown": 0.0}],
                "condition_trace": [{"date": "2026-01-05"}],
            }
        ],
        # Heavy backward-compat fields that must be stripped on the wire.
        "daily_nav": [{"date": "2026-01-05", "equity": 100000.0}],
        "charts": {"kline": {"dates": ["2026-01-05"], "candles": [[1, 2, 3, 4]]}},
        "drawdown": [{"date": "2026-01-05", "drawdown": 0.0}],
        "condition_trace": [{"date": "2026-01-05"}],
        "benchmark": {"series": [{"date": "2026-01-05", "equity": 100000.0}]},
        "monthly_returns": [{"month": "2026-01", "return": 0.1}],
    }


def _install_fake_service(monkeypatch: pytest.MonkeyPatch, behavior: str) -> None:
    import app.routers.rule_backtest as rb_module

    class FakeRuleBacktestService:
        def run(self, payload: dict, progress_callback=None) -> dict:
            if behavior == "error":
                raise ValueError("symbol has no market data in range: TEST")
            if progress_callback is not None:
                progress_callback(1, 2)
                progress_callback(2, 2)
            return _fake_result(str(payload.get("symbol")))

    monkeypatch.setattr(rb_module, "RuleBacktestService", FakeRuleBacktestService)


def _poll_until_terminal(client, run_id: str, timeout_s: float = 5.0) -> dict:
    from conftest import poll_until_terminal

    return poll_until_terminal(client, f"/rule-backtest/api/progress/{run_id}", timeout_s)


class TestRuleBacktestAsyncApi:
    def test_run_then_poll_progress_then_fetch_result(self, client, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_service(monkeypatch, behavior="ok")

        start = client.post(
            "/rule-backtest/api/run",
            json={"strategy_ids": ["s1"], "symbol": "TEST", "start_date": "2026-01-01", "end_date": "2026-02-01"},
        )
        assert start.status_code == 200
        run_id = start.json()["run_id"]
        assert start.json()["status"] == "running"

        final = _poll_until_terminal(client, run_id)
        assert final["status"] == "ok"
        assert final["progress_current"] == final["progress_total"] == 2
        assert final["error"] is None
        # Progress responses never carry the result payload.
        assert "result" not in final

        result_resp = client.get(f"/rule-backtest/api/result/{run_id}")
        assert result_resp.status_code == 200
        result = result_resp.json()
        assert result["symbol"] == "TEST"
        assert result["results"][0]["strategy_id"] == "s1"
        # Fields the frontend actually uses survive slimming.
        assert result["summary"] == {"return": 0.1}
        assert result["trades"] == [{"date": "2026-01-05", "side": "BUY"}]

    def test_result_is_slimmed(self, client, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_service(monkeypatch, behavior="ok")

        start = client.post(
            "/rule-backtest/api/run",
            json={"strategy_ids": ["s1"], "symbol": "TEST"},
        )
        run_id = start.json()["run_id"]
        _poll_until_terminal(client, run_id)

        result = client.get(f"/rule-backtest/api/result/{run_id}").json()
        for heavy in ("daily_nav", "charts", "drawdown", "condition_trace", "benchmark", "monthly_returns"):
            assert heavy not in result, f"top-level {heavy} should be stripped"
            assert heavy not in result["results"][0], f"per-strategy {heavy} should be stripped"

    def test_result_while_running_returns_409(self, client, monkeypatch: pytest.MonkeyPatch) -> None:
        import app.routers.rule_backtest as rb_module

        blocker = threading.Event()

        class SlowFakeRuleBacktestService:
            def run(self, payload: dict, progress_callback=None) -> dict:
                blocker.wait(timeout=5.0)
                return _fake_result("TEST")

        monkeypatch.setattr(rb_module, "RuleBacktestService", SlowFakeRuleBacktestService)
        try:
            start = client.post(
                "/rule-backtest/api/run",
                json={"strategy_ids": ["s1"], "symbol": "TEST"},
            )
            run_id = start.json()["run_id"]

            resp = client.get(f"/rule-backtest/api/result/{run_id}")
            assert resp.status_code == 409
        finally:
            blocker.set()

    def test_result_unknown_run_id_returns_404(self, client) -> None:
        resp = client.get("/rule-backtest/api/result/does-not-exist")
        assert resp.status_code == 404

    def test_run_with_service_value_error_surfaces_as_error_status(
        self, client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_service(monkeypatch, behavior="error")

        start = client.post(
            "/rule-backtest/api/run",
            json={"strategy_ids": ["s1"], "symbol": "TEST"},
        )
        assert start.status_code == 200
        run_id = start.json()["run_id"]

        final = _poll_until_terminal(client, run_id)
        assert final["status"] == "error"
        assert "no market data in range" in final["error"]
        assert "result" not in final

        # A failed run has no result to serve.
        resp = client.get(f"/rule-backtest/api/result/{run_id}")
        assert resp.status_code == 404

    def test_progress_unknown_run_id_returns_404(self, client) -> None:
        resp = client.get("/rule-backtest/api/progress/does-not-exist")
        assert resp.status_code == 404
