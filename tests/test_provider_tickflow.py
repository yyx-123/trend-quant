from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


import os
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd

from core.settings import load_settings
from data.provider_tickflow import TickFlowProvider


class TickFlowProviderTest(unittest.TestCase):
    def test_starter_limits_are_loaded_from_application_config(self) -> None:
        # 显式临时配置（不读仓库真实 app.yaml，避免与生产配置耦合）
        import tempfile
        from pathlib import Path

        yaml_text = """
tickflow:
  api_base_url: "https://api.tickflow.org"
  daily_kline_batch_size: 100
  daily_kline_batch_requests_per_minute: 30
  daily_kline_batch_max_workers: 1
  daily_kline_single_requests_per_minute: 60
  quote_max_symbols_per_request: 50
  quote_requests_per_minute: 60
"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "app.yaml"
            cfg.write_text(yaml_text, encoding="utf-8")
            settings = load_settings(cfg).tickflow

        self.assertEqual(settings.daily_kline_batch_size, 100)
        self.assertEqual(settings.daily_kline_batch_requests_per_minute, 30)
        self.assertEqual(settings.daily_kline_single_requests_per_minute, 60)
        self.assertEqual(settings.quote_max_symbols_per_request, 50)
        self.assertEqual(settings.quote_requests_per_minute, 60)

    def test_symbol_and_adjust_mapping(self) -> None:
        self.assertEqual(TickFlowProvider._to_tickflow_symbol("518850.SS"), "518850.SH")
        self.assertEqual(TickFlowProvider._to_tickflow_symbol("159915.SZ"), "159915.SZ")
        self.assertEqual(TickFlowProvider._adjust_type("qfq"), "forward")
        self.assertEqual(TickFlowProvider._adjust_type("hfq"), "backward")
        self.assertEqual(TickFlowProvider._adjust_type("none"), "none")

    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "starter-test-key"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_daily_history_uses_starter_service_and_normalizes_schema(
        self,
        tickflow_cls: MagicMock,
    ) -> None:
        client = tickflow_cls.return_value
        client.klines.get.return_value = pd.DataFrame(
            [
                {
                    "symbol": "518850.SH",
                    "trade_date": "2026-06-25",
                    "open": 8.433,
                    "high": 8.433,
                    "low": 8.300,
                    "close": 8.364,
                    "volume": 842693,
                    "amount": 706185676.0,
                }
            ]
        )

        provider = TickFlowProvider()
        result = provider.fetch_daily_history(
            "518850.SS",
            date(2026, 6, 1),
            date(2026, 6, 25),
            "qfq",
        )

        tickflow_cls.assert_called_once_with(
            api_key="starter-test-key",
            base_url="https://api.tickflow.org",
        )
        _, kwargs = client.klines.get.call_args
        self.assertEqual(kwargs["period"], "1d")
        self.assertEqual(kwargs["adjust"], "forward")
        self.assertEqual(kwargs["start_time"], TickFlowProvider._to_milliseconds(date(2026, 5, 31)))
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["symbol"], "518850.SS")
        self.assertEqual(result.iloc[0]["amount"], 706185676.0)
        provider.close()
        client.close.assert_called_once()

    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "starter-test-key"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_daily_histories_use_batch_endpoint_and_map_symbols(
        self,
        tickflow_cls: MagicMock,
    ) -> None:
        # 真实 API（as_dataframe=False）返回 compact dict-of-arrays；
        # 该形状才会走生产的 _compact_klines_to_dataframe 分支（UTC 毫秒 → 上海墙钟）
        client = tickflow_cls.return_value
        client.klines.batch.return_value = {
            "518850.SH": {
                "timestamp": [1782316800000],  # 2026-06-24 16:00 UTC = 2026-06-25 上海
                "open": [8.433],
                "high": [8.433],
                "low": [8.300],
                "close": [8.364],
                "volume": [842693],
                "amount": [706185676.0],
            },
            "159915.SZ": {
                "timestamp": [1782316800000],
                "open": [2],
                "high": [2],
                "low": [2],
                "close": [2],
                "volume": [100],
                "amount": [200],
            },
        }

        provider = TickFlowProvider()
        data, errors = provider.fetch_daily_histories(
            ["518850.SS", "159915.SZ"],
            date(2026, 6, 1),
            date(2026, 6, 25),
            "qfq",
            batch_size=100,
            request_interval_seconds=0,
        )

        self.assertEqual(errors, {})
        _, kwargs = client.klines.batch.call_args
        self.assertEqual(client.klines.batch.call_args.args[0], ["518850.SH", "159915.SZ"])
        self.assertFalse(kwargs["as_dataframe"])
        self.assertEqual(kwargs["max_workers"], 1)
        self.assertEqual(kwargs["batch_size"], 2)
        self.assertIn("518850.SS", data)
        self.assertEqual(data["518850.SS"].iloc[0]["symbol"], "518850.SS")
        self.assertEqual(data["159915.SZ"].iloc[0]["amount"], 200)
        # compact 路径的 UTC→上海墙钟转换语义被锁定（2025-06-29 16:00 UTC → 06-30 上海）
        assert str(data["518850.SS"].iloc[0]["time"])[:10] == "2026-06-25"

    @patch.dict(os.environ, {}, clear=True)
    def test_starter_service_requires_api_key(self) -> None:
        provider = TickFlowProvider()
        with self.assertRaisesRegex(RuntimeError, "TICKFLOW_API_KEY is required"):
            provider.fetch_daily_history("518850.SS", date(2026, 6, 1), date(2026, 6, 25), "qfq")
        with self.assertRaisesRegex(RuntimeError, "TICKFLOW_API_KEY is required"):
            provider.fetch_latest_quote("518850.SS")

    
    def test_batch_throttle_enforces_starter_minimum_interval(self) -> None:
        provider = TickFlowProvider()
        # 可重复 fake：不依赖 monotonic 被调次数（side_effect 计数脆弱）
        with (
            patch("data.provider_tickflow.time_module.monotonic", lambda: 100.0),
            patch("data.provider_tickflow.time_module.sleep") as sleep,
        ):
            provider._throttle("daily_kline_batch", 2.0)
            provider._throttle("daily_kline_batch", 2.0)

        sleep.assert_called_once_with(2.0)


if __name__ == "__main__":
    unittest.main()
