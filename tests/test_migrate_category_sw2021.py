"""scripts/migrate_category_sw2021.py 单元测试（小型临时库）。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import migrate_category_sw2021 as mig

from data.storage.db import Database

TREE = [
    {
        "name": "电子",
        "order": 27,
        "l2": [{"name": "半导体", "order": 2701}, {"name": "消费电子", "order": 2705}],
    },
    {"name": "银行", "order": 48, "l2": [{"name": "股份制银行", "order": 4802}]},
]


def _run_main(db_path: str, *extra: str) -> int:
    argv = ["migrate_category_sw2021.py", "--db", db_path, *extra]
    with mock.patch.object(sys, "argv", argv):
        return mig.main()


class MigrateCategoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "test.db")
        db = Database(self.db_path)
        # 旧树：股票-科技硬件-半导体-设计 + ETF 子树（不应被动）
        db.save_instrument_categories(
            [
                {"path": "ETF", "level": 1, "name": "ETF", "priority": 1},
                {"path": "ETF-宽基", "level": 2, "name": "宽基", "parent_path": "ETF", "priority": 1},
                {"path": "股票", "level": 1, "name": "股票", "priority": 2},
                {"path": "股票-科技硬件", "level": 2, "name": "科技硬件", "parent_path": "股票", "priority": 1},
                {
                    "path": "股票-科技硬件-半导体-设计",
                    "level": 3,
                    "name": "半导体-设计",
                    "parent_path": "股票-科技硬件",
                    "priority": 1,
                },
            ]
        )
        db.save_instrument_metadata(
            [
                {
                    "symbol": "002475.SZ",
                    "name": "立讯精密",
                    "category_l1": "股票",
                    "category_l2": "科技硬件",
                    "category_l3": "半导体-设计",
                    "asset_type": "stock",
                    "enabled": True,
                },
                {
                    "symbol": "688999.SS",
                    "name": "未知次新",
                    "category_l1": "股票",
                    "category_l2": "科技硬件",
                    "category_l3": "半导体-设计",
                    "asset_type": "stock",
                    "enabled": True,
                },
                {
                    "symbol": "510300.SS",
                    "name": "沪深300ETF",
                    "category_l1": "ETF",
                    "category_l2": "宽基",
                    "category_l3": "大盘宽基",
                    "asset_type": "etf",
                    "enabled": True,
                },
            ]
        )
        db.upsert_stock_industry(
            [
                {
                    "symbol": "002475.SZ",
                    "sw_l1_name": "电子",
                    "sw_l2_name": "消费电子",
                    "sw_l3_name": "消费电子零部件及组装",
                    "sw_l3_code": "270504",
                }
            ],
            "tickflow_universe",
        )
        db.set_config("sw2021_tree", {"source": "tickflow_universe", "tree": TREE})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_validate_tree(self) -> None:
        self.assertEqual(mig.validate_tree(TREE), [])
        bad = [{"name": "坏-类目", "order": 1, "l2": []}]
        self.assertTrue(mig.validate_tree(bad))
        dup = [{"name": "电子", "order": 1, "l2": [{"name": "半导体", "order": 1}, {"name": "半导体", "order": 2}]}]
        self.assertTrue(mig.validate_tree(dup))

    def test_dry_run_writes_nothing(self) -> None:
        rc = _run_main(self.db_path, "--dry-run")
        self.assertEqual(rc, 0)
        db = Database(self.db_path)
        item = db.get_instrument_metadata("002475.SZ")
        self.assertEqual(item["category_l2"], "科技硬件")  # 未变
        paths = {r["path"] for r in db.list_instrument_categories()}
        self.assertIn("股票-科技硬件", paths)  # 旧树还在

    def test_full_migration(self) -> None:
        rc = _run_main(self.db_path)
        self.assertEqual(rc, 0)
        db = Database(self.db_path)

        hit = db.get_instrument_metadata("002475.SZ")
        self.assertEqual((hit["category_l2"], hit["category_l3"]), ("电子", "消费电子"))
        self.assertEqual(hit["priority_l2"], 1)  # 电子是树里第一个 L2
        self.assertEqual(hit["priority_l3"], 2)  # 消费电子是电子下第二个 L3

        miss = db.get_instrument_metadata("688999.SS")
        self.assertEqual((miss["category_l2"], miss["category_l3"]), ("待分类", "待分类"))
        self.assertEqual(miss["priority_l2"], 9999)

        etf = db.get_instrument_metadata("510300.SS")
        self.assertEqual((etf["category_l1"], etf["category_l2"]), ("ETF", "宽基"))

        paths = {r["path"] for r in db.list_instrument_categories()}
        self.assertNotIn("股票-科技硬件", paths)  # 旧子树已删
        self.assertIn("股票-电子-半导体", paths)
        self.assertIn("股票-待分类-待分类", paths)
        self.assertIn("ETF-宽基", paths)  # ETF 子树未动

        with db._connect() as conn:
            archived = conn.execute(
                "SELECT * FROM stock_category_archive WHERE symbol = '002475.SZ'"
            ).fetchone()
        self.assertEqual(archived["category_l3"], "半导体-设计")
        self.assertEqual(archived["migration"], mig.MIGRATION_TAG)

        # 幂等：再跑一遍仍然通过（重归类结果相同）
        rc = _run_main(self.db_path)
        self.assertEqual(rc, 0)

    def test_missing_tree_config_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            empty = str(Path(tmp) / "empty.db")
            Database(empty)
            rc = _run_main(empty, "--dry-run")
            self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
