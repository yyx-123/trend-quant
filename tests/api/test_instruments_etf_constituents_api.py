"""ETF 重仓股预览/导入与类目建议接口测试（含登录墙）。"""

from __future__ import annotations

import time


def _fresh_period() -> str:
    """最近的季末日期（YYYYmmdd），保证 months_since_period 远小于 stale 阈值（>4 个月）。

    硬编码期次会随时间推移变成 stale 导致测试无故变红（时间炸弹）。
    """
    from datetime import date

    today = date.today()
    # 当季季末：3/6/9/12 月最后一天；取最近一个已过的季末
    quarter_ends = [(3, 31), (6, 30), (9, 30), (12, 31)]
    candidates = []
    for year in (today.year - 1, today.year):
        for month, day in quarter_ends:
            d = date(year, month, day)
            if d <= today:
                candidates.append(d)
    return max(candidates).strftime("%Y%m%d")


def _seed_constituents(test_db) -> None:
    test_db.save_etf_constituents(
        "510300.SS",
        [
            {"stock_symbol": "600519.SS", "stock_name": "贵州茅台", "weight": 5.1, "rank": 1},
            {"stock_symbol": "300750.SZ", "stock_name": "宁德时代", "weight": 4.2, "rank": 2},
            {"stock_symbol": "688999.SS", "stock_name": "未知次新", "weight": 1.0, "rank": 3},
            {"stock_symbol": "02269.HK", "stock_name": "药明生物", "weight": 8.3, "rank": 4},
        ],
        _fresh_period(),
    )
    test_db.upsert_stock_industry(
        [
            {
                "symbol": "600519.SS",
                "sw_l1_name": "食品饮料",
                "sw_l2_name": "白酒",
                "sw_l3_name": "白酒Ⅲ",
                "sw_l3_code": "340501",
            },
            {
                "symbol": "300750.SZ",
                "sw_l1_name": "电力设备",
                "sw_l2_name": "电池",
                "sw_l3_name": "锂电池",
                "sw_l3_code": "630701",
            },
        ],
        "tickflow_universe",
    )


class TestSuggestCategory:
    def test_hit(self, client, test_db) -> None:
        _seed_constituents(test_db)
        resp = client.get("/instruments/api/suggest-category/600519.SH")
        assert resp.status_code == 200
        data = resp.json()
        assert data["hit"] is True
        assert (data["category_l1"], data["category_l2"], data["category_l3"]) == (
            "股票",
            "食品饮料",
            "白酒",
        )
        assert data["sw_l3_name"] == "白酒Ⅲ"

    def test_miss_returns_unclassified(self, client) -> None:
        resp = client.get("/instruments/api/suggest-category/999999.SS")
        assert resp.status_code == 200
        data = resp.json()
        assert data["hit"] is False
        assert data["category_l2"] == "待分类"
        assert data["category_l3"] == "待分类"

    def test_invalid_symbol_400(self, client) -> None:
        resp = client.get("/instruments/api/suggest-category/%20")
        assert resp.status_code in (400, 422)


class TestPreviewEtfConstituents:
    def test_no_snapshot(self, client) -> None:
        resp = client.get("/instruments/api/etf-constituents/159915.SZ")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "快照" in data["message"]
        assert data["items"] == []

    def test_preview_items(self, client, test_db) -> None:
        _seed_constituents(test_db)
        # 300750.SZ 已在管理
        test_db.save_instrument_metadata(
            [
                {
                    "symbol": "300750.SZ",
                    "name": "宁德时代",
                    "category_l1": "股票",
                    "category_l2": "新能源",
                    "category_l3": "锂电池-电芯/PACK",
                    "asset_type": "stock",
                    "enabled": True,
                }
            ]
        )
        resp = client.get("/instruments/api/etf-constituents/510300.SS")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["period"] == _fresh_period()
        assert isinstance(data["months_since_period"], (int, float))
        assert data["stale"] is False  # stale 阈值 = 距今 >4 个月；动态季末期次保证常新
        items = {i["stock_symbol"]: i for i in data["items"]}
        assert items["600519.SS"]["already_managed"] is False
        assert items["600519.SS"]["manageable"] is True
        assert items["600519.SS"]["resolved_category"] == "食品饮料-白酒"
        assert items["600519.SS"]["hit"] is True
        assert items["300750.SZ"]["already_managed"] is True
        assert items["688999.SS"]["hit"] is False
        assert items["688999.SS"]["resolved_category"] == "待分类-待分类"
        # 港股如实展示但不纳入管理、不走申万归类
        hk = items["02269.HK"]
        assert hk["manageable"] is False
        assert hk["market_label"] == "港股"
        assert hk["resolved_category"] == ""
        assert hk["hit"] is False
        assert hk["already_managed"] is False


class _FakeDataService:
    def backfill_daily_histories(self, items, **kwargs):
        return [
            {"ok": True, "result": {"symbol": item["symbol"], "status": "updated", "added_rows": 100}}
            for item in items
        ]

    def close(self) -> None:
        pass


class TestImportEtfConstituents:
    def test_import_without_snapshot_400(self, client) -> None:
        resp = client.post(
            "/instruments/api/etf-constituents/import", json={"etf_symbol": "159915.SZ"}
        )
        assert resp.status_code == 400

    def test_import_job_end_to_end(self, client, test_db, monkeypatch) -> None:
        _seed_constituents(test_db)
        test_db.save_instrument_metadata(
            [
                {
                    "symbol": "300750.SZ",
                    "name": "宁德时代",
                    "category_l1": "股票",
                    "category_l2": "新能源",
                    "category_l3": "锂电池-电芯/PACK",
                    "asset_type": "stock",
                    "enabled": True,
                }
            ]
        )
        import services.instrument_jobs as jobs_module

        monkeypatch.setattr(
            jobs_module.etf_constituent_import_manager,
            "_data_service_factory",
            lambda provider_priority: _FakeDataService(),
        )
        monkeypatch.setattr(jobs_module, "rebuild_after_backfill", lambda symbols: None)

        resp = client.post(
            "/instruments/api/etf-constituents/import", json={"etf_symbol": "510300.SS"}
        )
        assert resp.status_code == 200
        assert resp.json()["started"] is True

        deadline = time.time() + 15
        while time.time() < deadline:
            status = client.get("/instruments/api/etf-constituents/import/status").json()["job"]
            if status["status"] in ("completed", "failed"):
                break
            time.sleep(0.2)
        assert status["status"] == "completed", status.get("error")
        summary = status["summary"]
        assert {a["symbol"] for a in summary["added"]} == {"600519.SS", "688999.SS"}
        skipped = {s["symbol"]: s["reason"] for s in summary["skipped"]}
        assert skipped == {"300750.SZ": "already_managed", "02269.HK": "not_manageable"}
        assert summary["failed"] == []
        assert summary["backfill_updated"] == 2
        assert "不纳入管理 1 只" in status["message"]

        hit = test_db.get_instrument_metadata("600519.SS")
        assert (hit["category_l1"], hit["category_l2"], hit["category_l3"]) == (
            "股票",
            "食品饮料",
            "白酒",
        )
        assert hit["source"] == "etf_constituent"
        miss = test_db.get_instrument_metadata("688999.SS")
        assert (miss["category_l2"], miss["category_l3"]) == ("待分类", "待分类")

        # 幂等：重复导入全部 skipped
        resp = client.post(
            "/instruments/api/etf-constituents/import", json={"etf_symbol": "510300.SS"}
        )
        assert resp.status_code == 200
        deadline = time.time() + 15
        while time.time() < deadline:
            status = client.get("/instruments/api/etf-constituents/import/status").json()["job"]
            if status["status"] in ("completed", "failed"):
                break
            time.sleep(0.2)
        assert status["summary"]["added"] == []
        assert len(status["summary"]["skipped"]) == 4  # 3 只已管理 + 1 只港股不纳入管理


class TestAddAutoCategory:
    """新增标的类目留空时按申万自动归类（用户只需输入代码）。"""

    def _seed_tree(self, test_db) -> None:
        test_db.save_instrument_categories(
            [
                {"path": "股票", "level": 1, "name": "股票", "priority": 2},
                {"path": "股票-食品饮料", "level": 2, "name": "食品饮料", "parent_path": "股票", "priority": 8},
                {
                    "path": "股票-食品饮料-白酒",
                    "level": 3,
                    "name": "白酒",
                    "parent_path": "股票-食品饮料",
                    "priority": 1,
                },
            ]
        )

    def _capture_start(self, monkeypatch) -> dict:
        import app.routers.instruments as router_module

        captured: dict = {}
        monkeypatch.setattr(
            router_module.add_instrument_manager,
            "start",
            lambda **kw: (captured.update(kw) or (True, {})),
        )
        return captured

    def test_empty_categories_auto_classified(self, client, test_db, monkeypatch) -> None:
        _seed_constituents(test_db)
        self._seed_tree(test_db)
        captured = self._capture_start(monkeypatch)
        resp = client.post("/instruments/api/add", json={"symbol": "600519.SS", "name": "贵州茅台"})
        assert resp.status_code == 200, resp.text
        item = captured["item"]
        assert (item["category_l1"], item["category_l2"], item["category_l3"]) == (
            "股票",
            "食品饮料",
            "白酒",
        )

    def test_unclassified_requires_manual_choice(self, client, test_db, monkeypatch) -> None:
        self._seed_tree(test_db)
        self._capture_start(monkeypatch)
        resp = client.post("/instruments/api/add", json={"symbol": "688999.SS", "name": "未知次新"})
        assert resp.status_code == 400
        assert "待分类" in resp.json()["detail"]

    def test_partial_categories_rejected(self, client, test_db, monkeypatch) -> None:
        _seed_constituents(test_db)
        self._seed_tree(test_db)
        self._capture_start(monkeypatch)
        resp = client.post(
            "/instruments/api/add",
            json={"symbol": "600519.SS", "name": "贵州茅台", "category_l1": "股票"},
        )
        assert resp.status_code == 400
        assert "同时留空" in resp.json()["detail"]


class TestAuthWall:
    def test_suggest_category_requires_login(self, anon_client) -> None:
        resp = anon_client.get("/instruments/api/suggest-category/600519.SS")
        assert resp.status_code == 401

    def test_preview_requires_login(self, anon_client) -> None:
        resp = anon_client.get("/instruments/api/etf-constituents/510300.SS")
        assert resp.status_code == 401

    def test_import_requires_login(self, anon_client) -> None:
        resp = anon_client.post(
            "/instruments/api/etf-constituents/import", json={"etf_symbol": "510300.SS"}
        )
        assert resp.status_code == 401

    def test_import_status_requires_login(self, anon_client) -> None:
        resp = anon_client.get("/instruments/api/etf-constituents/import/status")
        assert resp.status_code == 401
