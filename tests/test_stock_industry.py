"""stock_industry（申万行业分类）与 etf_constituents 的单元测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from data.storage.db import Database, init_db
from services.stock_industry import (
    _build_industry_rows,
    add_missing_tree_branches,
    build_sw_tree,
    is_a_share,
    normalize_industry_name,
    parse_tickflow_universe_name,
    project_symbol_to_tushare,
    reclassify_pending_stocks,
    resolve_category,
    tickflow_symbol_to_project,
)


class MergeTreeBranchesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        self.db.set_config(
            "sw2021_tree",
            {
                "source": "tickflow_universe",
                "tree": [
                    {
                        "name": "计算机",
                        "order": 71,
                        "l2": [{"name": "计算机设备", "order": 7101}, {"name": "软件开发", "order": 7104}],
                    }
                ],
            },
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_adds_missing_l2_branch(self) -> None:
        added = add_missing_tree_branches(
            self.db,
            [{"sw_l1_name": "计算机", "sw_l2_name": "IT服务"}],
        )
        self.assertEqual(added, ["计算机/IT服务"])
        tree = self.db.get_config("sw2021_tree")["tree"]
        l2s = [c["name"] for c in tree[0]["l2"]]
        self.assertEqual(l2s, ["计算机设备", "软件开发", "IT服务"])  # 原顺序保留，新分支追加
        self.assertEqual(tree[0]["l2"][2]["order"], 7105)  # max+1

    def test_adds_missing_l1_branch(self) -> None:
        added = add_missing_tree_branches(self.db, [{"sw_l1_name": "环保", "sw_l2_name": "环境治理"}])
        self.assertEqual(added, ["L1:环保", "环保/环境治理"])
        tree = self.db.get_config("sw2021_tree")["tree"]
        self.assertEqual(tree[-1]["name"], "环保")

    def test_no_change_when_branch_exists(self) -> None:
        self.assertEqual(
            add_missing_tree_branches(self.db, [{"sw_l1_name": "计算机", "sw_l2_name": "软件开发"}]),
            [],
        )

    def test_missing_tree_warns_and_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.db")
            with self.assertLogs("services.stock_industry", level="WARNING"):
                self.assertEqual(add_missing_tree_branches(db, [{"sw_l1_name": "计算机", "sw_l2_name": "IT服务"}]), [])


class BuildSwTreeTest(unittest.TestCase):
    def test_same_l2_multiple_l3_leaves_merge_silently(self) -> None:
        # 770201/770202 都是 L2「化妆品」的 L3 叶子 —— 正常合并，非撞名
        code_names = {
            "770201": {1: "美容护理", 2: "化妆品", 3: "化妆品制造及其他"},
            "770202": {1: "美容护理", 2: "化妆品", 3: "品牌化妆品"},
            "770301": {1: "美容护理", 2: "医疗美容", 3: "医美耗材"},
        }
        with self.assertNoLogs("services.stock_industry", level="WARNING"):
            tree = build_sw_tree(code_names)
        self.assertEqual(len(tree), 1)
        self.assertEqual(tree[0]["name"], "美容护理")
        self.assertEqual([c["name"] for c in tree[0]["l2"]], ["化妆品", "医疗美容"])
        self.assertEqual(tree[0]["order"], 77)
        self.assertEqual(tree[0]["l2"][0]["order"], 7702)

    def test_true_collision_warns(self) -> None:
        # 两个原始名不同的 L2 规范化后撞名 → 保留先出现者并告警
        code_names = {
            "340501": {1: "食品饮料", 2: "白酒Ⅱ", 3: "白酒Ⅲ"},
            "340601": {1: "食品饮料", 2: "白酒II", 3: "其他白酒"},
        }
        with self.assertLogs("services.stock_industry", level="WARNING") as cm:
            tree = build_sw_tree(code_names)
        self.assertTrue(any("撞名" in line for line in cm.output))
        self.assertEqual([c["name"] for c in tree[0]["l2"]], ["白酒"])

    def test_dash_in_name_rejected(self) -> None:
        code_names = {"990101": {1: "坏-类目", 2: "某某", 3: "某某"}}
        with self.assertLogs("services.stock_industry", level="ERROR") as cm:
            tree = build_sw_tree(code_names)
        self.assertTrue(any("分隔符" in line for line in cm.output))
        self.assertEqual(tree, [])


class NormalizeIndustryNameTest(unittest.TestCase):
    def test_strips_unicode_roman_suffix(self) -> None:
        self.assertEqual(normalize_industry_name("白酒Ⅲ"), "白酒")
        self.assertEqual(normalize_industry_name("家电零部件Ⅱ"), "家电零部件")
        self.assertEqual(normalize_industry_name("综合Ⅱ"), "综合")

    def test_strips_ascii_roman_suffix(self) -> None:
        self.assertEqual(normalize_industry_name("白酒II"), "白酒")
        self.assertEqual(normalize_industry_name("游戏III"), "游戏")
        self.assertEqual(normalize_industry_name("油气开采ＩＩＩ"), "油气开采")  # 全角

    def test_keeps_names_without_suffix(self) -> None:
        self.assertEqual(normalize_industry_name("半导体"), "半导体")
        self.assertEqual(normalize_industry_name("一般零售"), "一般零售")

    def test_does_not_strip_non_cjk_prefixed_letters(self) -> None:
        # 前导非 CJK 时不剥（防误伤英文缩写结尾）
        self.assertEqual(normalize_industry_name("AI"), "AI")

    def test_whitespace_and_case(self) -> None:
        self.assertEqual(normalize_industry_name("  白酒Ⅱ "), "白酒")


class UniverseNameParseTest(unittest.TestCase):
    def test_parse(self) -> None:
        self.assertEqual(parse_tickflow_universe_name("SW3白酒Ⅲ"), (3, "白酒Ⅲ"))
        self.assertEqual(parse_tickflow_universe_name("SW1电子"), (1, "电子"))

    def test_non_sw(self) -> None:
        self.assertIsNone(parse_tickflow_universe_name("沪深ETF"))
        self.assertIsNone(parse_tickflow_universe_name(""))


class SymbolConversionTest(unittest.TestCase):
    def test_tickflow_to_project(self) -> None:
        self.assertEqual(tickflow_symbol_to_project("600519.SH"), "600519.SS")
        self.assertEqual(tickflow_symbol_to_project("000001.SZ"), "000001.SZ")

    def test_project_to_tushare(self) -> None:
        self.assertEqual(project_symbol_to_tushare("600519.SS"), "600519.SH")
        self.assertEqual(project_symbol_to_tushare("000001.SZ"), "000001.SZ")

    def test_is_a_share(self) -> None:
        self.assertTrue(is_a_share("600519.SS"))
        self.assertTrue(is_a_share("000001.SZ"))
        self.assertFalse(is_a_share("830799.BJ"))
        self.assertFalse(is_a_share("00700.HK"))


class BuildIndustryRowsTest(unittest.TestCase):
    def test_rows_from_universes(self) -> None:
        universes = [
            {"id": "CN_Equity_SW1_340501", "name": "SW1食品饮料"},
            {"id": "CN_Equity_SW2_340501", "name": "SW2白酒Ⅱ"},
            {"id": "CN_Equity_SW3_340501", "name": "SW3白酒Ⅲ"},
            {"id": "CN_Equity_A", "name": "沪深京A股"},
        ]
        details = {
            "CN_Equity_SW3_340501": {
                "symbols": ["600519.SH", "000001.SZ", "830799.BJ"],
            }
        }
        rows = _build_industry_rows(universes, details)
        by_symbol = {r["symbol"]: r for r in rows}
        self.assertNotIn("830799.BJ", by_symbol)  # 北交所过滤
        self.assertEqual(len(rows), 2)
        row = by_symbol["600519.SS"]
        self.assertEqual(row["sw_l1_name"], "食品饮料")
        self.assertEqual(row["sw_l2_name"], "白酒")  # 剥后缀
        self.assertEqual(row["sw_l3_name"], "白酒Ⅲ")  # 官方原名保留
        self.assertEqual(row["sw_l3_code"], "340501")

    def test_missing_level_names_skipped(self) -> None:
        universes = [{"id": "CN_Equity_SW3_999999", "name": "SW3某某"}]
        details = {"CN_Equity_SW3_999999": {"symbols": ["600519.SH"]}}
        self.assertEqual(_build_industry_rows(universes, details), [])


class StockIndustryDbTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _row(self, symbol: str, l1: str = "电子", l2: str = "半导体") -> dict:
        return {
            "symbol": symbol,
            "sw_l1_name": l1,
            "sw_l2_name": l2,
            "sw_l3_name": "集成电路设计",
            "sw_l3_code": "270101",
        }

    def test_upsert_and_get(self) -> None:
        self.assertEqual(self.db.upsert_stock_industry([self._row("600519.SS")], "tickflow_universe"), 1)
        row = self.db.get_stock_industry("600519.ss")  # 大小写不敏感
        self.assertIsNotNone(row)
        self.assertEqual(row["sw_l1_name"], "电子")
        self.assertEqual(row["source"], "tickflow_universe")

    def test_priority_tushare_overwrites_tickflow(self) -> None:
        self.db.upsert_stock_industry([self._row("600519.SS")], "tickflow_universe")
        self.db.upsert_stock_industry([self._row("600519.SS", l1="食品饮料", l2="白酒")], "tushare_sw2021")
        row = self.db.get_stock_industry("600519.SS")
        self.assertEqual(row["sw_l1_name"], "食品饮料")
        self.assertEqual(row["source"], "tushare_sw2021")

    def test_priority_tickflow_blocked_by_tushare(self) -> None:
        self.db.upsert_stock_industry([self._row("600519.SS", l1="食品饮料", l2="白酒")], "tushare_sw2021")
        written = self.db.upsert_stock_industry([self._row("600519.SS")], "tickflow_universe")
        self.assertEqual(written, 0)
        self.assertEqual(self.db.get_stock_industry("600519.SS")["sw_l1_name"], "食品饮料")

    def test_priority_manual_never_overwritten(self) -> None:
        self.db.upsert_stock_industry([self._row("600519.SS")], "manual")
        self.assertEqual(
            self.db.upsert_stock_industry([self._row("600519.SS", l1="食品饮料", l2="白酒")], "tushare_sw2021"),
            0,
        )
        self.assertEqual(self.db.get_stock_industry("600519.SS")["source"], "manual")

    def test_sync_never_deletes_rows(self) -> None:
        self.db.upsert_stock_industry([self._row("600519.SS"), self._row("000001.SZ")], "tickflow_universe")
        # 新一轮同步只剩一只（另一只退市/调出）——旧行必须保留
        self.db.upsert_stock_industry([self._row("600519.SS")], "tushare_sw2021")
        self.assertIsNotNone(self.db.get_stock_industry("000001.SZ"))

    def test_unknown_source_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.db.upsert_stock_industry([self._row("600519.SS")], "akshare")

    def test_list_stock_industry_filter(self) -> None:
        self.db.upsert_stock_industry(
            [self._row("600519.SS"), self._row("000001.SZ")], "tickflow_universe"
        )
        self.assertEqual(len(self.db.list_stock_industry()), 2)
        self.assertEqual(len(self.db.list_stock_industry(["600519.SS"])), 1)
        self.assertEqual(self.db.list_stock_industry([]), [])


class EtfConstituentsDbTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _rows(self, stocks: list[str], start_rank: int = 1) -> list[dict]:
        return [
            {"stock_symbol": s, "stock_name": f"名称{s}", "weight": 10.0 - i, "rank": start_rank + i}
            for i, s in enumerate(stocks)
        ]

    def test_save_and_list_current(self) -> None:
        self.db.save_etf_constituents("510300.SS", self._rows(["600519.SS", "300750.SZ"]), "20260331")
        rows = self.db.list_current_etf_constituents("510300.SS")
        self.assertEqual([r["stock_symbol"] for r in rows], ["600519.SS", "300750.SZ"])
        self.assertEqual(rows[0]["rank"], 1)

    def test_is_current_flip_between_periods(self) -> None:
        self.db.save_etf_constituents("510300.SS", self._rows(["600519.SS", "300750.SZ"]), "20260331")
        # 新一期：600519 被踢出，601318 新进
        self.db.save_etf_constituents("510300.SS", self._rows(["300750.SZ", "601318.SS"]), "20260630")
        current = self.db.list_current_etf_constituents("510300.SS")
        self.assertEqual([r["stock_symbol"] for r in current], ["300750.SZ", "601318.SS"])
        self.assertTrue(all(r["period"] == "20260630" for r in current))
        # 历史期次行保留（软失效，不删除）
        self.assertTrue(self.db.has_etf_constituents_for_period("510300.SS", "20260331"))

    def test_empty_rows_flips_all_inactive(self) -> None:
        self.db.save_etf_constituents("510300.SS", self._rows(["600519.SS"]), "20260331")
        self.db.save_etf_constituents("510300.SS", [], "20260630")
        self.assertEqual(self.db.list_current_etf_constituents("510300.SS"), [])

    def test_list_periods_and_has_period(self) -> None:
        self.db.save_etf_constituents("510300.SS", self._rows(["600519.SS"]), "20260331")
        self.db.save_etf_constituents("159915.SZ", self._rows(["300750.SZ"]), "20260331")
        periods = self.db.list_etf_constituent_periods()
        self.assertEqual(len(periods), 2)
        self.assertTrue(self.db.has_etf_constituents_for_period("159915.SZ", "20260331"))
        self.assertFalse(self.db.has_etf_constituents_for_period("159915.SZ", "20260630"))

    def test_multi_etf_same_stock(self) -> None:
        # 一只股票是多只 ETF 的重仓（功能 3 的多对多）
        self.db.save_etf_constituents("510300.SS", self._rows(["600519.SS"]), "20260331")
        self.db.save_etf_constituents("510500.SS", self._rows(["600519.SS"]), "20260331")
        self.assertEqual(len(self.db.list_current_etf_constituents("510300.SS")), 1)
        self.assertEqual(len(self.db.list_current_etf_constituents("510500.SS")), 1)


class ResolveAndReclassifyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = init_db(Path(self._tmp.name) / "test.db")  # get_db() 全局可用

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed_tree(self) -> None:
        self.db.save_instrument_categories(
            [
                {"path": "股票", "level": 1, "name": "股票", "priority": 2},
                {"path": "股票-电子", "level": 2, "name": "电子", "parent_path": "股票", "priority": 5},
                {
                    "path": "股票-电子-半导体",
                    "level": 3,
                    "name": "半导体",
                    "parent_path": "股票-电子",
                    "priority": 1,
                },
                {
                    "path": "股票-待分类",
                    "level": 2,
                    "name": "待分类",
                    "parent_path": "股票",
                    "priority": 9999,
                },
                {
                    "path": "股票-待分类-待分类",
                    "level": 3,
                    "name": "待分类",
                    "parent_path": "股票-待分类",
                    "priority": 9999,
                },
            ]
        )

    def _add_stock(self, symbol: str, l2: str, l3: str) -> None:
        self.db.save_instrument_metadata(
            [
                {
                    "symbol": symbol,
                    "name": f"测试{symbol}",
                    "category_l1": "股票",
                    "category_l2": l2,
                    "category_l3": l3,
                    "asset_type": "stock",
                    "enabled": True,
                }
            ]
        )

    def test_resolve_hit_and_miss(self) -> None:
        self.db.upsert_stock_industry(
            [
                {
                    "symbol": "600519.SS",
                    "sw_l1_name": "食品饮料",
                    "sw_l2_name": "白酒",
                    "sw_l3_name": "白酒Ⅲ",
                    "sw_l3_code": "340501",
                }
            ],
            "tickflow_universe",
        )
        hit = resolve_category("600519.SS", db=self.db)
        self.assertTrue(hit["hit"])
        self.assertEqual(
            (hit["category_l1"], hit["category_l2"], hit["category_l3"]),
            ("股票", "食品饮料", "白酒"),
        )
        self.assertEqual(hit["sw_l3_name"], "白酒Ⅲ")

        miss = resolve_category("999999.SS", db=self.db)
        self.assertFalse(miss["hit"])
        self.assertEqual(miss["category_l2"], "待分类")
        self.assertEqual(miss["category_l3"], "待分类")

    def test_reclassify_moves_pending_and_keeps_others(self) -> None:
        self._seed_tree()
        self._add_stock("600519.SS", "待分类", "待分类")
        self._add_stock("000001.SZ", "电子", "半导体")  # 非待分类，不动
        self._add_stock("688999.SS", "待分类", "待分类")  # 无行业数据，保持待分类
        self.db.upsert_stock_industry(
            [
                {
                    "symbol": "600519.SS",
                    "sw_l1_name": "电子",
                    "sw_l2_name": "半导体",
                    "sw_l3_name": "集成电路设计",
                    "sw_l3_code": "270101",
                }
            ],
            "tickflow_universe",
        )

        before = self.db.get_instrument_metadata("600519.SS")["updated_at"]
        result = reclassify_pending_stocks(db=self.db)

        self.assertEqual(result["pending"], 2)
        self.assertEqual(len(result["moved"]), 1)
        self.assertEqual(result["moved"][0]["to"], "股票-电子-半导体")
        self.assertEqual(result["still_unclassified"], ["688999.SS"])

        after = self.db.get_instrument_metadata("600519.SS")
        self.assertEqual(after["category_l2"], "电子")
        self.assertEqual(after["category_l3"], "半导体")
        self.assertEqual(after["priority_l2"], 5)
        self.assertEqual(after["priority_l3"], 1)
        # updated_at 必须刷新（看板 revision 依赖，评审 B1）
        self.assertIsNotNone(after["updated_at"])
        # 非待分类标的不动
        self.assertEqual(self.db.get_instrument_metadata("000001.SZ")["category_l2"], "电子")

    def test_reclassify_defers_when_tree_missing(self) -> None:
        # 新树尚未迁移：目标路径不存在 → deferred，不写 metadata
        self.db.save_instrument_categories(
            [
                {"path": "股票", "level": 1, "name": "股票", "priority": 2},
                {
                    "path": "股票-待分类",
                    "level": 2,
                    "name": "待分类",
                    "parent_path": "股票",
                    "priority": 9999,
                },
                {
                    "path": "股票-待分类-待分类",
                    "level": 3,
                    "name": "待分类",
                    "parent_path": "股票-待分类",
                    "priority": 9999,
                },
            ]
        )
        self._add_stock("600519.SS", "待分类", "待分类")
        self.db.upsert_stock_industry(
            [
                {
                    "symbol": "600519.SS",
                    "sw_l1_name": "电子",
                    "sw_l2_name": "半导体",
                    "sw_l3_name": "",
                    "sw_l3_code": "",
                }
            ],
            "tickflow_universe",
        )
        result = reclassify_pending_stocks(db=self.db)
        self.assertEqual(result["deferred"], 1)
        self.assertEqual(result["moved"], [])
        self.assertEqual(self.db.get_instrument_metadata("600519.SS")["category_l2"], "待分类")


class AddConstituentStockTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = init_db(Path(self._tmp.name) / "test.db")
        self.db.upsert_stock_industry(
            [
                {
                    "symbol": "600519.SS",
                    "sw_l1_name": "食品饮料",
                    "sw_l2_name": "白酒",
                    "sw_l3_name": "白酒Ⅲ",
                    "sw_l3_code": "340501",
                }
            ],
            "tickflow_universe",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_add_with_classification(self) -> None:
        from services.instrument_admin import add_constituent_stock

        outcome = add_constituent_stock("600519.SS", "贵州茅台", known_symbols=set())
        self.assertEqual(outcome["status"], "added")
        self.assertEqual(outcome["category"], "食品饮料-白酒")
        self.assertTrue(outcome["hit"])
        meta = self.db.get_instrument_metadata("600519.SS")
        self.assertEqual(meta["name"], "贵州茅台")
        self.assertEqual((meta["category_l1"], meta["category_l2"], meta["category_l3"]), ("股票", "食品饮料", "白酒"))
        self.assertEqual(meta["source"], "etf_constituent")

    def test_add_unclassified_goes_pending(self) -> None:
        from services.instrument_admin import add_constituent_stock

        outcome = add_constituent_stock("688999.SS", "未知次新", known_symbols=set())
        self.assertEqual(outcome["status"], "added")
        self.assertFalse(outcome["hit"])
        meta = self.db.get_instrument_metadata("688999.SS")
        self.assertEqual((meta["category_l2"], meta["category_l3"]), ("待分类", "待分类"))

    def test_skip_when_known_and_idempotent(self) -> None:
        from services.instrument_admin import add_constituent_stock

        self.assertEqual(
            add_constituent_stock("600519.SS", "贵州茅台", known_symbols={"600519.SS"})["status"],
            "skipped",
        )
        add_constituent_stock("600519.SS", "贵州茅台", known_symbols=set())
        # 第二次调用：不在已知集合里，但库里已存在 → 并发重复安全跳过
        outcome = add_constituent_stock("600519.SS", "贵州茅台", known_symbols=set())
        self.assertEqual(outcome["status"], "skipped")

    def test_empty_name_falls_back_to_symbol(self) -> None:
        from services.instrument_admin import add_constituent_stock

        outcome = add_constituent_stock("600519.SS", "", known_symbols=set())
        self.assertEqual(outcome["status"], "added")
        self.assertEqual(self.db.get_instrument_metadata("600519.SS")["name"], "600519.SS")


class CategoryArchiveTest(unittest.TestCase):
    def test_archive_insert_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            rows = [{"symbol": "600519.SS", "category_l2": "科技硬件", "category_l3": "半导体-设计"}]
            self.assertEqual(db.archive_stock_categories(rows, "sw2021_2026_q3"), 1)
            # 重复归档（ON CONFLICT DO NOTHING）不覆盖首次归档
            db.archive_stock_categories(
                [{"symbol": "600519.SS", "category_l2": "电子", "category_l3": "半导体"}],
                "sw2021_2026_q3",
            )
            with db._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM stock_category_archive WHERE symbol = '600519.SS'"
                ).fetchone()
            self.assertEqual(row["category_l2"], "科技硬件")


if __name__ == "__main__":
    unittest.main()
