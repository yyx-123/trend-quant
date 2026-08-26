from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


import tempfile
import unittest
from pathlib import Path

from data.storage.db import Database


class InstrumentMetadataTest(unittest.TestCase):
    def test_metadata_is_saved_with_category_path_and_priority_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            db.save_instrument_metadata(
                [
                    {
                        "symbol": "510500.SS",
                        "name": "中证500ETF南方",
                        "category_l1": "宽基",
                        "category_l2": "中盘宽基",
                        "category_l3": "中证500",
                        "factor_tags": ["低波"],
                        "priority_l1": 1,
                        "priority_l2": 2,
                        "priority_l3": 1,
                        "sort_order": 20,
                    },
                    {
                        "symbol": "510300.SS",
                        "name": "沪深300ETF华泰柏瑞",
                        "category_l1": "宽基",
                        "category_l2": "大盘宽基",
                        "category_l3": "沪深300",
                        "priority_l1": 1,
                        "priority_l2": 1,
                        "priority_l3": 1,
                        "sort_order": 1,
                    },
                ]
            )
            db.save_instrument_categories(
                [
                    {"path": "宽基", "level": 1, "name": "宽基", "priority": 1},
                    {
                        "path": "宽基-大盘宽基",
                        "level": 2,
                        "name": "大盘宽基",
                        "parent_path": "宽基",
                        "priority": 1,
                    },
                ]
            )

            items = db.list_instrument_metadata()
            categories = db.list_instrument_categories()

        self.assertEqual([item["symbol"] for item in items], ["510300.SS", "510500.SS"])
        self.assertEqual(items[0]["category_path"], "宽基-大盘宽基-沪深300")
        self.assertEqual(items[1]["factor_tags"], ["低波"])
        self.assertEqual(categories[0]["path"], "宽基")
        self.assertEqual(categories[1]["path"], "宽基-大盘宽基")


if __name__ == "__main__":
    unittest.main()
