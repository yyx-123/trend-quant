from __future__ import annotations

import unittest
from datetime import date

import pandas as pd

from data.service import DataProviderError, DataService


class FakeTickFlowProvider:
    def __init__(self, daily: pd.DataFrame | None = None, quote: dict | None = None) -> None:
        self.daily = daily if daily is not None else pd.DataFrame()
        self.quote = quote if quote is not None else {"symbol": "518850.SS", "price": None}

    def fetch_daily_history(self, symbol: str, start: date, end: date, adjust: str) -> pd.DataFrame:
        return self.daily.copy()

    def fetch_daily_histories(
        self,
        symbols: list[str],
        start: date,
        end: date,
        adjust: str,
        *,
        batch_size: int = 100,
        request_interval_seconds: float = 2.0,
    ) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
        return {symbol: self.daily.copy() for symbol in symbols}, {}

    def fetch_minute_history(
        self,
        symbol: str,
        period: str,
        count: int,
        adjust: str,
    ) -> pd.DataFrame:
        return self.daily.copy()

    def fetch_latest_quote(self, symbol: str) -> dict:
        return dict(self.quote)

    def fetch_trading_calendar(self, start: date, end: date) -> list[date]:
        return []


class FakeMarketStore:
    def __init__(self) -> None:
        self.data: dict[str, pd.DataFrame] = {}

    def load_history(self, symbol: str) -> pd.DataFrame:
        return self.data.get(symbol, pd.DataFrame()).copy()

    def save_history(self, symbol: str, df: pd.DataFrame) -> str:
        existing = self.data.get(symbol, pd.DataFrame())
        merged = pd.concat([existing, df], ignore_index=True)
        if not merged.empty:
            merged["time"] = pd.to_datetime(merged["time"], errors="coerce")
            merged = merged.dropna(subset=["time"]).drop_duplicates(subset=["time"]).sort_values("time")
            merged = merged.reset_index(drop=True)
        self.data[symbol] = merged.copy()
        return f"sqlite/qfq/{symbol}"


class FlakyBatchProvider(FakeTickFlowProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[str]] = []

    def fetch_daily_histories(
        self,
        symbols: list[str],
        start: date,
        end: date,
        adjust: str,
        *,
        batch_size: int = 100,
        request_interval_seconds: float = 2.0,
    ) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
        self.calls.append(list(symbols))
        data: dict[str, pd.DataFrame] = {}
        errors: dict[str, str] = {}
        for symbol in symbols:
            if symbol == "000002.SZ" and len(self.calls) == 1:
                errors[symbol] = "请求频率超限 (10/min)，请 1ms 后重试"
                continue
            data[symbol] = pd.DataFrame(
                [
                    {
                        "time": "2026-07-07",
                        "open": 1,
                        "high": 1,
                        "low": 1,
                        "close": 1,
                        "volume": 100,
                        "amount": 100,
                        "symbol": symbol,
                    }
                ]
            )
        return data, errors


class PermissionDeniedBatchProvider(FakeTickFlowProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[str]] = []

    def fetch_daily_histories(
        self,
        symbols: list[str],
        start: date,
        end: date,
        adjust: str,
        *,
        batch_size: int = 100,
        request_interval_seconds: float = 2.0,
    ) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
        self.calls.append(list(symbols))
        error = "无日/周/月K线查询批量查询权限"
        return {}, {symbol: error for symbol in symbols}


class DataServiceTickFlowOnlyTest(unittest.TestCase):
    def test_ignores_fallback_providers(self) -> None:
        service = DataService(provider_priority=["tickflow", "legacy_provider"])

        self.assertEqual(service.provider_priority, ["tickflow"])
        self.assertEqual(list(service.providers), ["tickflow"])

    def test_daily_history_sets_provider(self) -> None:
        service = DataService()
        service.providers["tickflow"] = FakeTickFlowProvider(
            daily=pd.DataFrame(
                [{"time": "2026-06-26", "open": 1, "high": 1, "low": 1, "close": 1}]
            )
        )

        result = service.fetch_daily_history(
            "518850.SS",
            date(2026, 6, 1),
            date(2026, 6, 26),
        )

        self.assertEqual(result.loc[0, "provider"], "tickflow")

    def test_daily_history_empty_raises(self) -> None:
        service = DataService()
        service.providers["tickflow"] = FakeTickFlowProvider()

        with self.assertRaisesRegex(DataProviderError, "TickFlow returned no daily history"):
            service.fetch_daily_history("518850.SS", date(2026, 6, 1), date(2026, 6, 26))

    def test_latest_quote_without_price_raises(self) -> None:
        service = DataService()
        service.providers["tickflow"] = FakeTickFlowProvider(quote={"symbol": "518850.SS"})

        with self.assertRaisesRegex(DataProviderError, "TickFlow returned no latest quote"):
            service.fetch_latest_quote("518850.SS")

    def test_bulk_backfill_retries_only_failed_symbols(self) -> None:
        service = DataService()
        provider = FlakyBatchProvider()
        service.providers["tickflow"] = provider
        service.market_store = FakeMarketStore()
        service.raw_store = FakeMarketStore()

        results = service.backfill_daily_histories(
            [
                {"symbol": "000001.SZ", "start_date": date(2026, 7, 7)},
                {"symbol": "000002.SZ", "start_date": date(2026, 7, 7)},
            ],
            end_date=date(2026, 7, 7),
            request_interval_seconds=0,
            retry_delay_seconds=0,
        )

        self.assertEqual(provider.calls, [["000001.SZ", "000002.SZ"], ["000002.SZ"]])
        self.assertEqual([item["ok"] for item in results], [True, True])
        self.assertEqual(results[0]["result"]["added_rows"], 1)
        self.assertEqual(results[1]["result"]["added_rows"], 1)

    def test_bulk_backfill_stops_on_non_retryable_batch_permission_error(self) -> None:
        service = DataService()
        provider = PermissionDeniedBatchProvider()
        service.providers["tickflow"] = provider
        service.market_store = FakeMarketStore()
        service.raw_store = FakeMarketStore()

        results = service.backfill_daily_histories(
            [
                {"symbol": "000001.SZ", "start_date": date(2026, 7, 7)},
                {"symbol": "000002.SZ", "start_date": date(2026, 7, 7)},
            ],
            end_date=date(2026, 7, 7),
            request_interval_seconds=0,
            retry_delay_seconds=0,
        )

        self.assertEqual(provider.calls, [["000001.SZ", "000002.SZ"]])
        self.assertEqual([item["ok"] for item in results], [False, False])
        self.assertIn("批量查询权限", results[0]["error"])


if __name__ == "__main__":
    unittest.main()
