"""批量回测 API 测试（方案 §5.5）：页面、meta、run 校验、409 并发拦截、
进度轮询到完成、cells/cell/snapshot/delete 端点。"""

from __future__ import annotations

import time

import pandas as pd
import pytest


def _seed_batch_data(db) -> None:
    db.save_instrument_metadata(
        [
            {"symbol": "BT1.SS", "name": "批量测试一", "category_l1": "测试", "asset_type": "etf"},
        ]
    )
    base = pd.Timestamp("2024-01-02")
    records = []
    price = 10.0
    for i in range(120):
        close = price * 1.005
        records.append(
            {
                "time": (base + pd.Timedelta(days=i)).date().isoformat(),
                "open": price, "high": close * 1.01, "low": price * 0.99,
                "close": close, "volume": 1_000_000, "amount": close * 1_000_000,
            }
        )
        price = close
    from data.storage.market_store import MarketStore

    MarketStore(db=db).save_history("BT1.SS", pd.DataFrame(records))

    def strategy(sid: str, entry: dict) -> dict:
        return {
            "id": sid, "name": f"策略{sid}", "schema_version": 1,
            "trade_mode": "single_symbol_all_in",
            "entry": {"type": "group", "combinator": "all", "children": [entry]},
            "exit": {
                "type": "group", "combinator": "any",
                "children": [
                    {
                        "id": "x1", "type": "condition",
                        "left": {"type": "price", "field": "close"}, "operator": "<=",
                        "right": {"type": "state_value", "name": "hard_stop",
                                  "params": {"atr_period": 20, "atr_mul": 1.5}},
                    }
                ],
            },
        }

    db.save_rule_strategy(
        strategy("sma_ok", {
            "id": "c1", "type": "condition",
            "left": {"type": "indicator", "name": "sma", "params": {"period": 20}},
            "operator": "<=", "right": {"type": "price", "field": "close"},
        }),
        overwrite=True,
    )
    db.save_rule_strategy(
        strategy("rand_bad", {
            "id": "c1", "type": "condition",
            "left": {"type": "indicator", "name": "random_uniform", "params": {}},
            "operator": ">=", "right": {"type": "literal", "value": 0.5},
        }),
        overwrite=True,
    )


@pytest.fixture
def seeded_db(test_db):
    _seed_batch_data(test_db)
    return test_db


def _wait_finish(client, batch_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/batch-backtest/api/progress/{batch_id}")
        assert resp.status_code == 200
        data = resp.json()
        if data["status"] != "running":
            return data
        time.sleep(0.2)
    raise AssertionError(f"批次未在 {timeout}s 内结束: {batch_id}")


class TestBatchPage:
    def test_page_loads(self, client) -> None:
        resp = client.get("/batch-backtest")
        assert resp.status_code == 200
        assert "批量回测" in resp.text

    def test_meta(self, client, seeded_db) -> None:
        resp = client.get("/batch-backtest/api/meta")
        assert resp.status_code == 200
        data = resp.json()
        cat = next(c for c in data["categories"] if c["name"] == "测试")
        assert cat["symbol_count"] == 1
        assert cat["estimated_seconds_per_strategy"] > 0
        strategies = {s["id"]: s for s in data["strategies"]}
        assert strategies["sma_ok"]["uses_random_indicator"] is False
        assert strategies["rand_bad"]["uses_random_indicator"] is True


class TestBatchRun:
    def test_run_validation(self, client, seeded_db) -> None:
        assert client.post("/batch-backtest/api/run", json={}).status_code == 400
        assert client.post(
            "/batch-backtest/api/run", json={"categories": ["测试"]}
        ).status_code == 400

    def test_run_rejects_random_strategy(self, client, seeded_db) -> None:
        resp = client.post(
            "/batch-backtest/api/run",
            json={"categories": ["测试"], "strategy_ids": ["sma_ok", "rand_bad"]},
        )
        assert resp.status_code == 400
        assert "随机指标" in resp.json()["detail"]

    def test_run_rejects_unknown_strategy(self, client, seeded_db) -> None:
        resp = client.post(
            "/batch-backtest/api/run",
            json={"categories": ["测试"], "strategy_ids": ["does_not_exist"]},
        )
        assert resp.status_code == 404

    def test_full_lifecycle(self, client, seeded_db) -> None:
        resp = client.post(
            "/batch-backtest/api/run",
            json={"categories": ["测试"], "strategy_ids": ["sma_ok"], "name": "API测试批次"},
        )
        assert resp.status_code == 200
        batch_id = resp.json()["batch_id"]
        assert resp.json()["total_cells"] == 1

        final = _wait_finish(client, batch_id)
        assert final["status"] == "completed"
        assert final["ok_cells"] == 1

        # 批次列表
        runs = client.get("/batch-backtest/api/runs").json()["runs"]
        assert runs[0]["batch_id"] == batch_id
        assert runs[0]["name"] == "API测试批次"

        # cells（不含 blob，含特征）
        cells_resp = client.get(f"/batch-backtest/api/runs/{batch_id}/cells")
        assert cells_resp.status_code == 200
        cells = cells_resp.json()["cells"]
        assert len(cells) == 1
        cell = cells[0]
        assert cell["symbol"] == "BT1.SS"
        assert cell["status"] == "ok"
        assert cell["excess_annual_return"] is not None
        assert cell["ann_volatility"] is not None
        assert "trades_json" not in cell  # blob 不在列表端点

        # 单格明细（含解析后的 blob）
        detail = client.get(
            f"/batch-backtest/api/runs/{batch_id}/cell",
            params={"symbol": "BT1.SS", "strategy_id": "sma_ok"},
        )
        assert detail.status_code == 200
        body = detail.json()
        assert isinstance(body["trades"], list)
        assert isinstance(body["monthly_nav"], list)
        assert isinstance(body["annual_returns"], list)

        # 钻取快照
        snap = client.get(
            f"/batch-backtest/api/runs/{batch_id}/snapshot",
            params={"strategy_id": "sma_ok", "symbol": "BT1.SS"},
        )
        assert snap.status_code == 200
        snap_body = snap.json()
        assert snap_body["strategy_config"]["id"] == "sma_ok"
        assert snap_body["start_date"] is not None
        assert snap_body["end_date"] is not None

        # 删除
        assert client.delete(f"/batch-backtest/api/runs/{batch_id}").status_code == 200
        assert client.get(f"/batch-backtest/api/progress/{batch_id}").status_code == 404

    def test_conflict_while_running(self, client, seeded_db) -> None:
        # 预置一个 running 批次 → POST 必须 409
        seeded_db.create_batch_run_if_idle(
            {
                "batch_id": "fake_running",
                "name": "占用中",
                "categories_json": '["测试"]',
                "strategy_snapshot_json": "[]",
                "config_json": "{}",
                "total_cells": 1,
            }
        )
        resp = client.post(
            "/batch-backtest/api/run",
            json={"categories": ["测试"], "strategy_ids": ["sma_ok"]},
        )
        assert resp.status_code == 409
        # running 批次禁止删除
        assert client.delete("/batch-backtest/api/runs/fake_running").status_code == 409
        # cancel 非内存中的 running 批次（无 event）不报错
        resp = client.post("/batch-backtest/api/cancel/fake_running")
        assert resp.status_code == 200

    def test_progress_unknown_batch(self, client) -> None:
        assert client.get("/batch-backtest/api/progress/nope").status_code == 404
