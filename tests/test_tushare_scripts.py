"""tushare 季度脚本（fetch_etf_holdings / sync_sw_tushare / tushare_common）单元测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from data.storage.db import Database  # noqa: E402
from fetch_etf_holdings import fetch_top10  # noqa: E402
from sync_sw_tushare import fetch_sw_rows, industry_change_report  # noqa: E402
from tushare_common import prev_period, target_period  # noqa: E402


class PeriodLogicTest(unittest.TestCase):
    def test_target_period_normal(self) -> None:
        # 8 月：最近已结束季度 0630，距季末 >20 天 → 0630
        self.assertEqual(target_period(date(2026, 8, 24)), "20260630")

    def test_target_period_within_disclosure_window(self) -> None:
        # 4 月初：0331 刚过不足 20 天 → 退到上一年 1231
        self.assertEqual(target_period(date(2026, 4, 10)), "20251231")
        self.assertEqual(target_period(date(2026, 1, 5)), "20250930")

    def test_target_period_after_window(self) -> None:
        self.assertEqual(target_period(date(2026, 4, 25)), "20260331")

    def test_prev_period(self) -> None:
        self.assertEqual(prev_period("20260630"), "20260331")
        self.assertEqual(prev_period("20260331"), "20251231")
        with self.assertRaises(ValueError):
            prev_period("20240331")


class _FakePro:
    def __init__(self, portfolio_map: dict[str, pd.DataFrame] | None = None) -> None:
        self.portfolio_map = portfolio_map or {}
        self.calls: list[str] = []

    def fund_portfolio(self, ts_code: str, period: str) -> pd.DataFrame:
        self.calls.append(f"{ts_code}@{period}")
        return self.portfolio_map.get(period, pd.DataFrame())


def _portfolio(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


class FetchTop10Test(unittest.TestCase):
    def test_top10_sorted_all_markets_kept(self) -> None:
        # 港股/北交所等非 A 股行如实保留并按权重参与排序（状态由消费侧判定），
        # 不做市场过滤——过滤会让低权重行补位混进前十
        df = _portfolio(
            [
                {"symbol": f"60051{i}.SH", "stk_mkv_ratio": float(20 - i), "ann_date": "20260720"}
                for i in range(9)
            ]
            + [
                {"symbol": "00700.HK", "stk_mkv_ratio": 99.0, "ann_date": "20260720"},
                {"symbol": "830799.BJ", "stk_mkv_ratio": 98.0, "ann_date": "20260720"},
                {"symbol": "000001.SZ", "stk_mkv_ratio": 1.0, "ann_date": "20260720"},
                {"symbol": "000002.SZ", "stk_mkv_ratio": 0.5, "ann_date": "20260720"},
            ]
        )
        pro = _FakePro({"20260630": df})
        rows, actual = fetch_top10(pro, "510300.SS", "20260630")
        self.assertEqual(actual, "20260630")
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0]["stock_symbol"], "00700.HK")  # 权重最高，港股保留
        self.assertEqual(rows[0]["rank"], 1)
        self.assertEqual(rows[1]["stock_symbol"], "830799.BJ")
        self.assertEqual(rows[2]["stock_symbol"], "600510.SS")  # .SH→.SS
        self.assertEqual(rows[-1]["stock_symbol"], "600517.SS")  # 权重 13，第 11 名起被截断
        self.assertNotIn("000001.SZ", [r["stock_symbol"] for r in rows])

    def test_hk_symbol_zero_padded(self) -> None:
        # tushare 港股代码标准即 5 位，个别来源缺前导零时补齐（tickflow 查询要求 5 位）
        df = _portfolio([{"symbol": "2269.HK", "stk_mkv_ratio": 8.3, "ann_date": "20260720"}])
        pro = _FakePro({"20260630": df})
        rows, actual = fetch_top10(pro, "513120.SS", "20260630")
        self.assertEqual(actual, "20260630")
        self.assertEqual(rows[0]["stock_symbol"], "02269.HK")

    def test_fallback_to_prev_period(self) -> None:
        df = _portfolio([{"symbol": "600519.SH", "stk_mkv_ratio": 5.0, "ann_date": "20260420"}])
        pro = _FakePro({"20260331": df})
        rows, actual = fetch_top10(pro, "510300.SS", "20260630")
        self.assertEqual(actual, "20260331")
        self.assertEqual(len(rows), 1)
        self.assertEqual(pro.calls, ["510300.SH@20260630", "510300.SH@20260331"])

    def test_no_data(self) -> None:
        pro = _FakePro({})
        rows, actual = fetch_top10(pro, "511010.SS", "20260630")
        self.assertEqual(rows, [])
        self.assertIsNone(actual)


class _FakeSwPro:
    def __init__(self, members: dict[str, pd.DataFrame]) -> None:
        self.members = members

    def index_classify(self, level: str, src: str) -> pd.DataFrame:
        return pd.DataFrame({"index_code": list(self.members.keys())})

    def index_member_all(self, l1_code: str, is_new: str) -> pd.DataFrame:
        return self.members[l1_code]


class FetchSwRowsTest(unittest.TestCase):
    def test_rows_built_and_filtered(self) -> None:
        members = {
            "801080.SI": pd.DataFrame(
                [
                    {
                        "ts_code": "600519.SH",
                        "l1_name": "电子",
                        "l2_name": "半导体",
                        "l3_name": "集成电路设计",
                        "l3_code": "850811.SI",
                        "in_date": "20200101",
                    },
                    {
                        "ts_code": "830799.BJ",  # 北交所丢弃
                        "l1_name": "电子",
                        "l2_name": "半导体",
                        "l3_name": "集成电路设计",
                        "l3_code": "850811.SI",
                        "in_date": "20200101",
                    },
                ]
            ),
            "801120.SI": pd.DataFrame(
                [
                    {
                        "ts_code": "000858.SZ",
                        "l1_name": "食品饮料",
                        "l2_name": "白酒Ⅱ",  # 后缀剥离
                        "l3_name": "白酒Ⅲ",
                        "l3_code": "851251.SI",
                        "in_date": "20200101",
                    }
                ]
            ),
        }
        rows, warnings = fetch_sw_rows(_FakeSwPro(members), interval=0)
        self.assertEqual(warnings, [])
        by_symbol = {r["symbol"]: r for r in rows}
        self.assertEqual(set(by_symbol), {"600519.SS", "000858.SZ"})
        self.assertEqual(by_symbol["000858.SZ"]["sw_l2_name"], "白酒")
        self.assertEqual(by_symbol["000858.SZ"]["sw_l3_name"], "白酒Ⅲ")
        self.assertEqual(by_symbol["000858.SZ"]["sw_l3_code"], "851251.SI")

    def test_duplicate_symbol_keeps_latest_in_date(self) -> None:
        # 同一只股票多行历史调仓记录（都标 is_new='Y'）→ 最新 in_date 的归属生效
        members = {
            "801080.SI": pd.DataFrame(
                [
                    {
                        "ts_code": "000020.SZ",
                        "l1_name": "电子",
                        "l2_name": "光学光电子",
                        "l3_name": "LED",
                        "l3_code": "850831.SI",
                        "in_date": "19920428",  # 旧归属（故意放在后面，验证按 in_date 而非行序）
                    },
                    {
                        "ts_code": "000020.SZ",
                        "l1_name": "电子",
                        "l2_name": "光学光电子",
                        "l3_name": "面板",
                        "l3_code": "850832.SI",
                        "in_date": "20211213",  # 新归属
                    },
                ]
            )
        }
        rows, _ = fetch_sw_rows(_FakeSwPro(members), interval=0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "000020.SZ")
        self.assertEqual(rows[0]["sw_l3_name"], "面板")


class IndustryChangeReportTest(unittest.TestCase):
    def test_report_only_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            db.save_instrument_metadata(
                [
                    {
                        "symbol": "600519.SS",
                        "name": "贵州茅台",
                        "category_l1": "股票",
                        "category_l2": "科技硬件",  # 与官方归属不一致
                        "category_l3": "半导体-设计",
                        "asset_type": "stock",
                        "enabled": True,
                    },
                    {
                        "symbol": "000001.SZ",
                        "name": "平安银行",
                        "category_l1": "股票",
                        "category_l2": "待分类",  # 待分类不进报告（走回补）
                        "category_l3": "待分类",
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
                        "symbol": "600519.SS",
                        "sw_l1_name": "食品饮料",
                        "sw_l2_name": "白酒",
                        "sw_l3_name": "白酒Ⅲ",
                        "sw_l3_code": "340501",
                    },
                    {
                        "symbol": "000001.SZ",
                        "sw_l1_name": "银行",
                        "sw_l2_name": "股份制银行",
                        "sw_l3_name": "",
                        "sw_l3_code": "",
                    },
                ],
                "tushare_sw2021",
            )
            changes = industry_change_report(db)
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0]["symbol"], "600519.SS")
            self.assertEqual(changes[0]["current"], "科技硬件-半导体-设计")
            self.assertEqual(changes[0]["official"], "食品饮料-白酒")


if __name__ == "__main__":
    unittest.main()
