"""P0-3 回归测试：instrument_categories 空表行为契约。

从 metadata 反推分类树的 fallback 已删除（与 instrument_categories 表
职责重叠）。空表时的行为契约：
- GET /instruments/api/categories → 200，items 为空（不炸、不 NameError）
- POST /instruments/api/add → 400「类目组合不存在」（valid_paths 为空集）
- POST /instruments/api/{symbol}/update → 400「类目组合不存在」
全新部署需先跑类目种子（迁移脚本或导出/导入），见 README 部署章节。
"""

from __future__ import annotations


def test_categories_empty_table_returns_empty_items(client, test_db):
    resp = client.get("/instruments/api/categories")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []


def test_add_instrument_empty_categories_returns_400(client, test_db):
    resp = client.post(
        "/instruments/api/add",
        json={
            "symbol": "510300.SS",
            "name": "沪深300ETF",
            "category_l1": "股票",
            "category_l2": "宽基",
            "category_l3": "沪深300",
        },
    )
    assert resp.status_code == 400
    assert "类目组合不存在" in resp.json()["detail"]


def test_update_instrument_empty_categories_returns_400(client, test_db):
    test_db.save_instrument_metadata(
        [
            {
                "symbol": "510300.SS",
                "name": "沪深300ETF",
                "category_l1": "股票",
                "category_l2": "宽基",
                "category_l3": "沪深300",
                "factor_tags": [],
                "region_tag": "",
                "priority_l1": 1,
                "priority_l2": 1,
                "priority_l3": 1,
                "sort_order": 1,
                "enabled": True,
                "stop_atr_mul": 1.5,
                "source": "test",
            }
        ]
    )
    resp = client.post(
        "/instruments/api/510300.SS/update",
        json={"category_l1": "股票", "category_l2": "宽基", "category_l3": "沪深300"},
    )
    assert resp.status_code == 400
    assert "类目组合不存在" in resp.json()["detail"]
