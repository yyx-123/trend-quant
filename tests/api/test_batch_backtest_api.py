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
        # blob 解析后必须是结构化的列表（而非 JSON 字符串或 None）
        assert isinstance(body["trades"], list)
        assert isinstance(body["monthly_nav"], list)
        assert isinstance(body["annual_returns"], list)
        # 单格明细的业务字段与成功回测语义
        assert body["symbol"] == "BT1.SS"
        assert body["status"] == "ok"
        assert body["bar_count"] > 0
        if body["trades"]:
            first = body["trades"][0]
            assert first["side"] in ("BUY", "SELL")
            assert "date" in first
        for point in body["monthly_nav"]:
            assert "month" in point and "equity" in point

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


class TestBatchWindow:
    def test_run_with_window(self, client, seeded_db) -> None:
        resp = client.post(
            "/batch-backtest/api/run",
            json={
                "categories": ["测试"], "strategy_ids": ["sma_ok"],
                "start_date": "2024-01-15", "end_date": "2024-03-31",
            },
        )
        assert resp.status_code == 200
        batch_id = resp.json()["batch_id"]
        final = _wait_finish(client, batch_id)
        assert final["status"] == "completed"
        assert final["ok_cells"] == 1

        # 自动命名带区间；cells 反映实际窗口
        runs = client.get("/batch-backtest/api/runs").json()["runs"]
        assert "2024-01-15~2024-03-31" in runs[0]["name"]
        cells = client.get(f"/batch-backtest/api/runs/{batch_id}/cells").json()["cells"]
        assert cells[0]["start_date"] == "2024-01-15"
        assert cells[0]["end_date"] == "2024-03-31"
        assert cells[0]["partial_window"] == 0

    def test_run_rejects_bad_window(self, client, seeded_db) -> None:
        resp = client.post(
            "/batch-backtest/api/run",
            json={"categories": ["测试"], "strategy_ids": ["sma_ok"], "start_date": "not-a-date"},
        )
        assert resp.status_code == 400
        assert "开始日期" in resp.json()["detail"]

        resp = client.post(
            "/batch-backtest/api/run",
            json={
                "categories": ["测试"], "strategy_ids": ["sma_ok"],
                "start_date": "2024-05-01", "end_date": "2024-01-01",
            },
        )
        assert resp.status_code == 400
        assert "开始日期不能晚于结束日期" in resp.json()["detail"]


class TestAnnualAggregates:
    def test_aggregates_after_run(self, client, seeded_db) -> None:
        resp = client.post(
            "/batch-backtest/api/run",
            json={"categories": ["测试"], "strategy_ids": ["sma_ok"]},
        )
        assert resp.status_code == 200
        batch_id = resp.json()["batch_id"]
        final = _wait_finish(client, batch_id)
        assert final["status"] == "completed"

        agg_resp = client.get(f"/batch-backtest/api/runs/{batch_id}/annual-aggregates")
        assert agg_resp.status_code == 200
        aggs = agg_resp.json()["aggregates"]
        # BT1.SS 120 根（2024-01-02 起）→ 单一年份 2024，n=1
        assert len(aggs) == 1
        agg = aggs[0]
        assert agg["strategy"] == "策略sma_ok"
        assert agg["year"] == 2024
        assert agg["n"] == 1
        # 与单格年度收益一致（中位数即唯一值）
        detail = client.get(
            f"/batch-backtest/api/runs/{batch_id}/cell",
            params={"symbol": "BT1.SS", "strategy_id": "sma_ok"},
        ).json()
        cell_year = detail["annual_returns"][0]
        assert agg["median_return"] == pytest.approx(cell_year["return"])
        assert agg["median_benchmark"] == pytest.approx(cell_year["benchmark_return"])

    def test_unknown_batch_404(self, client) -> None:
        assert client.get("/batch-backtest/api/runs/nope/annual-aggregates").status_code == 404


class TestStopProfileAndDiagnostics:
    """止损档位跑批 + 诊断/对比/导出端点（方案 2026-08-30 §5/§6/§8）。"""

    def _run(self, client, **extra) -> str:
        resp = client.post(
            "/batch-backtest/api/run",
            json={"categories": ["测试"], "strategy_ids": ["sma_ok"], **extra},
        )
        assert resp.status_code == 200, resp.text
        batch_id = resp.json()["batch_id"]
        assert _wait_finish(client, batch_id)["status"] == "completed"
        return batch_id

    def test_run_with_tight_profile(self, client, seeded_db) -> None:
        batch_id = self._run(client, stop_profile="tight")
        batch = client.get(f"/batch-backtest/api/runs/{batch_id}/cells").json()["batch"]
        assert batch["stop_profile"] == "tight"
        assert batch["atr_basis"] == "prev_close"
        assert batch["name"].endswith("[tight]")
        # 快照 atr_mul 已被覆写为实盘紧档 1.0
        snap = client.get(
            f"/batch-backtest/api/runs/{batch_id}/snapshot",
            params={"strategy_id": "sma_ok"},
        ).json()
        spec = snap["strategy_config"]["exit"]["children"][0]["right"]
        assert spec["params"]["atr_mul"] == 1.0

    def test_run_rejects_unknown_profile(self, client, seeded_db) -> None:
        resp = client.post(
            "/batch-backtest/api/run",
            json={"categories": ["测试"], "strategy_ids": ["sma_ok"], "stop_profile": "ultra"},
        )
        assert resp.status_code == 422  # pydantic Literal 校验

    def test_stop_diagnostics_endpoint(self, client, seeded_db) -> None:
        batch_id = self._run(client)
        resp = client.get(f"/batch-backtest/api/runs/{batch_id}/stop-diagnostics")
        assert resp.status_code == 200
        body = resp.json()
        assert body["stop_profile"] == "default"
        assert isinstance(body["diagnostics"], list)
        assert client.get("/batch-backtest/api/runs/nope/stop-diagnostics").status_code == 404

    def test_compare_endpoint(self, client, seeded_db) -> None:
        base_id = self._run(client, stop_profile="tight")
        alt_id = self._run(client, stop_profile="loose")
        resp = client.get(
            "/batch-backtest/api/compare",
            params={"base_batch_id": base_id, "alt_batch_id": alt_id},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["common_cells"] == 1
        assert body["cell_diffs"][0]["symbol"] == "BT1.SS"
        assert "delta_annual_return" in body["cell_diffs"][0]
        # 404 / 409 分支
        assert client.get(
            "/batch-backtest/api/compare",
            params={"base_batch_id": "nope", "alt_batch_id": alt_id},
        ).status_code == 404

    def test_compare_no_common_cells_409(self, client, seeded_db) -> None:
        from data.storage.market_store import MarketStore

        seeded_db.save_instrument_metadata(
            [{"symbol": "BT2.SS", "name": "另一类目", "category_l1": "其他", "asset_type": "etf"}]
        )
        base = pd.Timestamp("2024-01-02")
        MarketStore(db=seeded_db).save_history(
            "BT2.SS",
            pd.DataFrame(
                {
                    "time": [(base + pd.Timedelta(days=i)).date().isoformat() for i in range(120)],
                    "open": [10.0] * 120, "high": [10.1] * 120, "low": [9.9] * 120,
                    "close": [10.0] * 120, "volume": [1_000_000] * 120,
                    "amount": [10_000_000.0] * 120,
                }
            ),
        )
        base_id = self._run(client)
        resp = client.post(
            "/batch-backtest/api/run",
            json={"categories": ["其他"], "strategy_ids": ["sma_ok"]},
        )
        assert resp.status_code == 200
        other_id = resp.json()["batch_id"]
        assert _wait_finish(client, other_id)["status"] == "completed"
        resp = client.get(
            "/batch-backtest/api/compare",
            params={"base_batch_id": base_id, "alt_batch_id": other_id},
        )
        assert resp.status_code == 409

    def test_export_endpoint(self, client, seeded_db) -> None:
        batch_id = self._run(client)
        resp = client.get(f"/batch-backtest/api/runs/{batch_id}/export", params={"live": False})
        assert resp.status_code == 200
        body = resp.json()
        export_dir = body["export_dir"]
        assert f"batch_{batch_id}" in export_dir
        import os
        import shutil

        for name in ("manifest.json", "cells.csv", "round_trips.csv"):
            assert os.path.exists(os.path.join(export_dir, name)), name
        shutil.rmtree(export_dir, ignore_errors=True)  # 清理导出产物
        assert client.get("/batch-backtest/api/runs/nope/export").status_code == 404
