"""API tests for position strategy (仓位策略) CRUD + meta exposure."""

from __future__ import annotations


class TestPositionStrategyApi:
    def test_meta_exposes_sizer_types_and_empty_list(self, client) -> None:
        resp = client.get("/rule-backtest/api/meta")
        assert resp.status_code == 200
        data = resp.json()
        assert data["position_strategies"] == []
        types = {t["type"] for t in data["sizer_types"]}
        assert types == {"fixed_pct", "risk_budget", "kelly"}
        kelly = next(t for t in data["sizer_types"] if t["type"] == "kelly")
        assert kelly["params"]["lookback"]["default"] == 10

    def test_create_list_update_delete_roundtrip(self, client) -> None:
        payload = {"id": "kz1", "name": "半仓", "sizer_type": "fixed_pct", "params": {"pct": 0.5}}
        created = client.post("/rule-backtest/api/position-strategies", json={"strategy": payload})
        assert created.status_code == 200
        assert created.json()["id"] == "kz1"

        meta = client.get("/rule-backtest/api/meta").json()
        ids = [item["id"] for item in meta["position_strategies"]]
        assert ids == ["kz1"]
        assert meta["position_strategies"][0]["sizer_type"] == "fixed_pct"

        # duplicate without overwrite -> 409; with overwrite -> 200
        dup = client.post("/rule-backtest/api/position-strategies", json={"strategy": payload})
        assert dup.status_code == 409
        updated = client.post(
            "/rule-backtest/api/position-strategies",
            json={"strategy": {**payload, "params": {"pct": 0.7}}, "overwrite": True},
        )
        assert updated.status_code == 200

        deleted = client.delete("/rule-backtest/api/position-strategies/kz1")
        assert deleted.status_code == 200
        meta = client.get("/rule-backtest/api/meta").json()
        assert meta["position_strategies"] == []
        # soft-deleted: second delete -> 404
        again = client.delete("/rule-backtest/api/position-strategies/kz1")
        assert again.status_code == 404

    def test_create_invalid_returns_400(self, client) -> None:
        bad = client.post(
            "/rule-backtest/api/position-strategies",
            json={"strategy": {"id": "bad", "sizer_type": "martingale"}},
        )
        assert bad.status_code == 400

    def test_page_renders(self, client) -> None:
        resp = client.get("/position-strategies")
        assert resp.status_code == 200
        assert "仓位策略" in resp.text
