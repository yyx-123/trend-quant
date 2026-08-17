from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from app.routers.subject_market import build_subject_dashboard_payload


def _rows(symbol: str, l3: str, amount: float, step: float) -> list[dict]:
    dates = pd.date_range("2026-01-01", periods=90, freq="B")
    rows = []
    for index, date in enumerate(dates):
        close = 100.0 + step * index
        rows.append(
            {
                "symbol": symbol,
                "time": str(date),
                "open": close - 0.3,
                "high": close + 0.8,
                "low": close - 0.8,
                "close": close,
                "volume": 100000,
                "amount": amount,
                "category_l1": "A股",
                "category_l2": "生命健康",
                "category_l3": l3,
                "priority_l1": 1,
                "priority_l2": 1,
                "priority_l3": 1 if l3 == "医疗服务" else 2,
                "sort_order": 1,
            }
        )
    return rows


class SubjectMarketApiTest(unittest.TestCase):
    def test_dashboard_aggregates_instruments_by_turnover_at_l3(self) -> None:
        history_rows = [
            *_rows("AAA", "医疗服务", 100.0, 0.5),
            *_rows("BBB", "医疗服务", 300.0, 0.2),
            *_rows("CCC", "化学制药", 200.0, -0.15),
        ]

        class FakeDb:
            def load_market_dashboard_history(self, days: int) -> list[dict]:
                self.days = days
                return history_rows

            def load_market_tail(self, days: int, price_mode: str = "qfq") -> list[dict]:
                return history_rows

            def indicator_cache_info(self, symbol: str) -> dict:
                # Cold cache → indicator_store falls back to live compute.
                return {
                    "indicator_rows": 0,
                    "indicator_last": None,
                    "indicator_version": None,
                    "trend_rows": 0,
                    "trend_last": None,
                    "trend_version": None,
                }

            def get_param_set(self, param_set: str):
                # No default param set → dashboard uses per-symbol fallback.
                return None

            def get_market_data_summary(self, symbol: str) -> dict:
                return {"rows": 0, "start": None, "end": None}

            def load_market_data(self, symbol: str):
                rows = [r for r in history_rows if r["symbol"] == symbol]
                return pd.DataFrame(rows) if rows else pd.DataFrame()

        with patch("services.dashboard.get_db", return_value=FakeDb()):
            payload = build_subject_dashboard_payload()

        self.assertEqual(payload["secondary_count"], 1)
        self.assertEqual(payload["category_count"], 2)
        self.assertEqual(payload["instrument_count"], 3)
        group = payload["groups"][0]
        self.assertEqual(group["category_l1"], "A股")
        l2 = group["items"][0]
        self.assertEqual(l2["category_l2"], "生命健康")
        self.assertEqual(l2["child_count"], 2)
        service = next(item for item in l2["children"] if item["category_l3"] == "医疗服务")
        self.assertEqual(service["member_count"], 2)
        self.assertEqual(service["child_count"], 2)
        self.assertEqual({item["symbol"] for item in service["children"]}, {"AAA", "BBB"})
        self.assertEqual(service["amount"], 400.0)
        # 热力图面积字段：近20日平均成交额（标量为自身日均，聚合为成员合计日均）。
        self.assertEqual(service["amount_avg20"], 400.0)
        instrument_avg = {item["symbol"]: item["amount_avg20"] for item in service["children"]}
        self.assertEqual(instrument_avg, {"AAA": 100.0, "BBB": 300.0})
        self.assertEqual(l2["amount_avg20"], 600.0)
        self.assertEqual(len(service["trend_history"]), 61)
        self.assertAlmostEqual(service["trend_history"][-1], service["trend_ma5"])
        self.assertEqual(len(service["trend_upper_history"]), 61)
        self.assertEqual(len(service["trend_lower_history"]), 61)
        self.assertGreaterEqual(service["trend_upper_history"][-1], service["trend_history"][-1])
        self.assertLessEqual(service["trend_lower_history"][-1], service["trend_history"][-1])
        chemical = next(item for item in l2["children"] if item["category_l3"] == "化学制药")
        self.assertEqual(
            service["trend_upper_history"][-1],
            max(item["trend_history"][-1] for item in service["children"]),
        )
        self.assertEqual(
            service["trend_lower_history"][-1],
            min(item["trend_history"][-1] for item in service["children"]),
        )
        self.assertEqual(
            l2["trend_upper_history"][-1],
            max(item["trend_history"][-1] for item in l2["children"]),
        )
        self.assertEqual(
            l2["trend_lower_history"][-1],
            min(item["trend_history"][-1] for item in l2["children"]),
        )
        latest_a = (100 + 0.5 * 89) / (100 + 0.5 * 88) - 1
        latest_b = (100 + 0.2 * 89) / (100 + 0.2 * 88) - 1
        self.assertAlmostEqual(service["daily_change_pct"], (latest_a * 100 + latest_b * 300) / 4)
        self.assertEqual(service["strength"], 100)

        # MACD 金叉/死叉相位 + 近30日K线：仅具体标的级有值，类目聚合行为占位。
        instruments_by_symbol = {
            item["symbol"]: item
            for l3 in l2["children"]
            for item in l3["children"]
        }
        aaa = instruments_by_symbol["AAA"]
        ccc = instruments_by_symbol["CCC"]
        # AAA 单调上涨 → 金叉；CCC 单调下跌 → 死叉。
        self.assertEqual(aaa["macd_phase"], "golden")
        self.assertEqual(ccc["macd_phase"], "dead")
        self.assertGreaterEqual(aaa["macd_phase_days"], 1)
        self.assertIsNotNone(aaa["macd_phase_signal_date"])
        self.assertEqual(len(aaa["kline"]), 30)
        self.assertEqual(len(aaa["kline_ma5"]), 30)
        # 末根K线 = 最后一根日K；末位 MA5 = 最后 5 根收盘均值。
        last_close = 100.0 + 0.5 * 89
        self.assertAlmostEqual(aaa["kline"][-1]["c"], last_close)
        self.assertAlmostEqual(aaa["kline"][0]["c"], 100.0 + 0.5 * 60)
        self.assertAlmostEqual(
            aaa["kline_ma5"][-1],
            sum(100.0 + 0.5 * i for i in range(85, 90)) / 5,
        )
        for level_item in (l2, service):
            self.assertIsNone(level_item["macd_phase"])
            self.assertIsNone(level_item["macd_phase_days"])
            self.assertIsNone(level_item["macd_phase_change_pct"])
            self.assertEqual(level_item["kline"], [])
            self.assertEqual(level_item["kline_ma5"], [])
        # 类目行的 MACD 相位聚合 = 金叉/死叉家数：AAA/BBB 金叉、CCC 死叉。
        self.assertEqual(service["macd_golden_count"], 2)
        self.assertEqual(service["macd_dead_count"], 0)
        self.assertEqual(chemical["macd_golden_count"], 0)
        self.assertEqual(chemical["macd_dead_count"], 1)
        self.assertEqual(l2["macd_golden_count"], 2)
        self.assertEqual(l2["macd_dead_count"], 1)
        # 标的行的家数字段恒为 None。
        self.assertIsNone(aaa["macd_golden_count"])
        self.assertIsNone(aaa["macd_dead_count"])


if __name__ == "__main__":
    unittest.main()
