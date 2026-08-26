from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


import tempfile
import unittest
from pathlib import Path

import pandas as pd

from data.storage.db import Database
from data.storage.market_store import MarketStore


class MarketDataPriceModeTest(unittest.TestCase):
    def test_raw_and_qfq_prices_are_stored_separately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            qfq_store = MarketStore(db, price_mode="qfq")
            raw_store = MarketStore(db, price_mode="raw")

            qfq_store.save_history(
                "510300.SS",
                pd.DataFrame(
                    [
                        {
                            "time": "2012-09-27",
                            "open": 1.345,
                            "high": 1.456,
                            "low": 1.344,
                            "close": 1.405,
                            "volume": 4791727,
                            "amount": 1089681314,
                            "provider": "tickflow",
                        }
                    ]
                ),
            )
            raw_store.save_history(
                "510300.SS",
                pd.DataFrame(
                    [
                        {
                            "time": "2012-09-27",
                            "open": 2.225,
                            "high": 2.336,
                            "low": 2.224,
                            "close": 2.285,
                            "volume": 4791727,
                            "amount": 1089681314,
                            "provider": "tickflow",
                        }
                    ]
                ),
            )

            default_df = db.load_market_data("510300.SS")
            raw_df = db.load_market_data("510300.SS", price_mode="raw")
            qfq_df = db.load_market_data("510300.SS", price_mode="qfq")

            self.assertEqual(float(default_df.iloc[0]["close"]), 1.405)
            self.assertEqual(float(qfq_df.iloc[0]["close"]), 1.405)
            self.assertEqual(float(raw_df.iloc[0]["close"]), 2.285)
            self.assertEqual(db.get_market_data_summary("510300.SS", "raw")["rows"], 1)
            self.assertEqual(db.get_market_data_summary("510300.SS", "qfq")["rows"], 1)


if __name__ == "__main__":
    unittest.main()
