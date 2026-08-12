from __future__ import annotations

import os
import threading
import time as time_module
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import pandas as pd

from audit.app_logger import get_logger
from core.settings import TickFlowSettings
from data.provider_base import IDataProvider
from data.provider_utils import safe_float, standardize_ohlcv

logger = get_logger(__name__)

try:
    from tickflow import TickFlow  # type: ignore
except Exception:  # pragma: no cover
    TickFlow = None


class TickFlowProvider(IDataProvider):
    name = "tickflow"
    paid_base_url = "https://api.tickflow.org"

    def __init__(self, settings: TickFlowSettings | None = None) -> None:
        self.settings = settings or TickFlowSettings(
            plan="starter",
            api_base_url=self.paid_base_url,
            daily_kline_batch_size=100,
            daily_kline_batch_requests_per_minute=30,
            daily_kline_batch_max_workers=1,
            daily_kline_single_requests_per_minute=60,
            quote_max_symbols_per_request=50,
            quote_requests_per_minute=60,
        )
        if self.settings.plan != "starter":
            raise ValueError(f"TickFlowProvider only supports the configured starter plan, got {self.settings.plan!r}")
        self.api_key = str(os.getenv("TICKFLOW_API_KEY", "") or "").strip()
        self._client = None
        self._rate_limit_lock = threading.Lock()
        self._next_request_at: dict[str, float] = {}

    @staticmethod
    def _to_tickflow_symbol(symbol: str) -> str:
        value = str(symbol or "").strip().upper()
        if value.endswith(".SS"):
            return f"{value[:-3]}.SH"
        return value

    @staticmethod
    def _from_tickflow_symbol(symbol: str) -> str:
        value = str(symbol or "").strip().upper()
        if value.endswith(".SH"):
            return f"{value[:-3]}.SS"
        return value

    @staticmethod
    def _to_milliseconds(day: date, *, end_of_day: bool = False) -> int:
        value = time.max if end_of_day else time.min
        dt = datetime.combine(day, value, tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)

    @staticmethod
    def _adjust_type(adjust: str) -> str:
        # qfq 必须是等比前复权（"forward"）：等差（forward_additive）会对
        # 早期低价 + 高累计分红的标的把历史价格减穿零（负价格事故 2026-07）。
        return {
            "qfq": "forward",
            "hfq": "backward",
            "none": "none",
        }.get(str(adjust or "").strip().lower(), "forward")

    def _get_client(self):
        if TickFlow is None or not self.api_key:
            return None
        if self._client is None:
            self._client = TickFlow(
                api_key=self.api_key,
                base_url=os.getenv("TICKFLOW_BASE_URL", self.settings.api_base_url),
            )
        return self._client

    def _throttle(self, operation: str, minimum_interval_seconds: float) -> None:
        """Apply a process-local minimum interval across all requests of one operation."""
        interval = max(0.0, float(minimum_interval_seconds))
        if interval <= 0:
            return
        with self._rate_limit_lock:
            now = time_module.monotonic()
            next_allowed = self._next_request_at.get(operation, now)
            wait_seconds = max(0.0, next_allowed - now)
            self._next_request_at[operation] = max(now, next_allowed) + interval
        if wait_seconds > 0:
            time_module.sleep(wait_seconds)

    @staticmethod
    def _normalize_klines(raw: Any, symbol: str) -> pd.DataFrame:
        if raw is None:
            return pd.DataFrame()
        data = raw.copy() if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        if data.empty:
            return pd.DataFrame()
        if "time" not in data.columns:
            for candidate in ("trade_time", "trade_date", "timestamp"):
                if candidate in data.columns:
                    data["time"] = data[candidate]
                    break
        return standardize_ohlcv(data, symbol)

    @staticmethod
    def _compact_klines_to_dataframe(raw: Any, symbol: str) -> pd.DataFrame:
        if not isinstance(raw, dict) or "timestamp" not in raw:
            return TickFlowProvider._normalize_klines(raw, symbol)

        timestamps = raw.get("timestamp") or []
        if not timestamps:
            return pd.DataFrame()

        trade_time = pd.to_datetime(pd.Series(timestamps), unit="ms", utc=True, errors="coerce")
        trade_time = trade_time.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
        row_count = len(timestamps)

        def _values(name: str, default: float | None = None) -> list:
            values = raw.get(name)
            if values is None:
                return [default] * row_count
            return list(values)

        return pd.DataFrame(
            {
                "symbol": symbol,
                "time": trade_time,
                "open": _values("open"),
                "high": _values("high"),
                "low": _values("low"),
                "close": _values("close"),
                "volume": _values("volume"),
                "amount": _values("amount", 0.0),
            }
        )

    def fetch_daily_history(
        self,
        symbol: str,
        start: date,
        end: date,
        adjust: str,
    ) -> pd.DataFrame:
        if end < start:
            start, end = end, start
        client = self._get_client()
        if client is None:
            raise RuntimeError("TICKFLOW_API_KEY is required for TickFlow Starter daily history")

        try:
            self._throttle(
                "daily_kline_standard",
                60.0 / self.settings.daily_kline_single_requests_per_minute,
            )
            request_start = start - timedelta(days=1)
            raw = client.klines.get(
                self._to_tickflow_symbol(symbol),
                period="1d",
                start_time=self._to_milliseconds(request_start),
                end_time=self._to_milliseconds(end, end_of_day=True),
                count=10000,
                adjust=self._adjust_type(adjust),
                as_dataframe=True,
            )
            data = self._normalize_klines(raw, symbol)
            if data.empty:
                return data
            data["time"] = pd.to_datetime(data["time"], errors="coerce")
            mask = (data["time"].dt.date >= start) & (data["time"].dt.date <= end)
            return data.loc[mask].reset_index(drop=True)
        except Exception as exc:
            logger.exception("tickflow daily fetch failed for %s", symbol)
            raise RuntimeError(f"tickflow daily fetch failed for {symbol}: {exc}") from exc

    def fetch_daily_histories(
        self,
        symbols: list[str],
        start: date,
        end: date,
        adjust: str,
        *,
        batch_size: int = 100,
        request_interval_seconds: float = 0.0,
    ) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
        if end < start:
            start, end = end, start
        normalized_symbols = [str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()]
        if not normalized_symbols:
            return {}, {}

        client = self._get_client()
        if client is None:
            raise RuntimeError("TICKFLOW_API_KEY is required for TickFlow Starter daily history")

        data_by_symbol: dict[str, pd.DataFrame] = {}
        errors: dict[str, str] = {}
        request_start = start - timedelta(days=1)
        batch_limit = max(1, min(int(batch_size or 100), self.settings.daily_kline_batch_size))
        batch_min_interval = max(
            60.0 / self.settings.daily_kline_batch_requests_per_minute,
            float(request_interval_seconds or 0.0),
        )

        chunks = [
            normalized_symbols[index : index + batch_limit]
            for index in range(0, len(normalized_symbols), batch_limit)
        ]
        for chunk in chunks:
            tickflow_to_local = {self._to_tickflow_symbol(symbol): symbol for symbol in chunk}
            try:
                self._throttle("daily_kline_batch", batch_min_interval)
                raw = client.klines.batch(
                    list(tickflow_to_local.keys()),
                    period="1d",
                    start_time=self._to_milliseconds(request_start),
                    end_time=self._to_milliseconds(end, end_of_day=True),
                    count=10000,
                    adjust=self._adjust_type(adjust),
                    as_dataframe=False,
                    show_progress=False,
                    max_workers=self.settings.daily_kline_batch_max_workers,
                    batch_size=len(chunk),
                )
                raw_map = raw if isinstance(raw, dict) else {}
                for tickflow_symbol, local_symbol in tickflow_to_local.items():
                    raw_df = raw_map.get(tickflow_symbol)
                    if raw_df is None:
                        raw_df = raw_map.get(local_symbol)
                    data = self._compact_klines_to_dataframe(raw_df, local_symbol)
                    if not data.empty:
                        data["time"] = pd.to_datetime(data["time"], errors="coerce")
                        mask = (data["time"].dt.date >= start) & (data["time"].dt.date <= end)
                        data = data.loc[mask].reset_index(drop=True)
                    data_by_symbol[local_symbol] = data
            except Exception as exc:
                logger.exception("tickflow daily batch fetch failed for %s", ",".join(chunk))
                error_text = str(exc)
                for symbol in chunk:
                    errors[symbol] = error_text

        return data_by_symbol, errors

    def fetch_ex_factors(
        self,
        symbols: list[str],
        *,
        # ex-factors 接口服务端上限 50 标的/次（与日K批量接口的 100 上限不同），
        # 超限返回 400「标的数量超限」。
        batch_size: int = 50,
    ) -> tuple[dict[str, list[tuple[date, float]]], dict[str, str]]:
        """批量获取除权因子：{symbol: [(ex_date, ex_factor)]} 升序。

        因子语义（已验证）：qfq(t) = raw(t) / Π_{ex_date >= t} f_i，见 core/adjustment.py。
        """
        normalized_symbols = [str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()]
        if not normalized_symbols:
            return {}, {}
        client = self._get_client()
        if client is None:
            raise RuntimeError("TICKFLOW_API_KEY is required for TickFlow ex-factors")

        factors_by_symbol: dict[str, list[tuple[date, float]]] = {}
        errors: dict[str, str] = {}
        batch_limit = max(1, min(int(batch_size or 50), 50))
        chunks = [
            normalized_symbols[index : index + batch_limit]
            for index in range(0, len(normalized_symbols), batch_limit)
        ]
        for chunk in chunks:
            tickflow_to_local = {self._to_tickflow_symbol(symbol): symbol for symbol in chunk}
            try:
                self._throttle("ex_factors", 60.0 / self.settings.daily_kline_batch_requests_per_minute)
                raw = client.klines.ex_factors(list(tickflow_to_local.keys()))
                raw_map = raw if isinstance(raw, dict) else {}
                for tickflow_symbol, local_symbol in tickflow_to_local.items():
                    entries = raw_map.get(tickflow_symbol)
                    if entries is None:
                        entries = raw_map.get(local_symbol) or []
                    factors: list[tuple[date, float]] = []
                    for entry in entries:
                        ts = entry.get("timestamp") if isinstance(entry, dict) else getattr(entry, "timestamp", None)
                        value = entry.get("ex_factor") if isinstance(entry, dict) else getattr(entry, "ex_factor", None)
                        if ts is None or value is None:
                            continue
                        # 与 vendor trade_date 口径一致：UTC 毫秒时间戳的 UTC 日期
                        day = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).date()
                        factors.append((day, float(value)))
                    factors.sort(key=lambda item: item[0])
                    factors_by_symbol[local_symbol] = factors
            except Exception as exc:
                logger.exception("tickflow ex-factors fetch failed for %s", ",".join(chunk))
                error_text = str(exc)
                for symbol in chunk:
                    errors[symbol] = error_text
        return factors_by_symbol, errors

    def fetch_minute_history(
        self,
        symbol: str,
        period: str,
        count: int,
        adjust: str,
    ) -> pd.DataFrame:
        del symbol, period, count, adjust
        raise RuntimeError("TickFlow Starter plan does not include minute K-line access")

    def fetch_latest_quote(self, symbol: str) -> dict:
        client = self._get_client()
        if client is None:
            raise RuntimeError("TICKFLOW_API_KEY is required for TickFlow latest quote")
        try:
            self._throttle("realtime_quote", 60.0 / self.settings.quote_requests_per_minute)
            raw = client.quotes.get(symbols=[self._to_tickflow_symbol(symbol)])
            if isinstance(raw, pd.DataFrame):
                item = raw.iloc[0].to_dict() if not raw.empty else {}
            elif isinstance(raw, list):
                item = raw[0] if raw else {}
            elif isinstance(raw, dict):
                item = raw
            else:
                item = {}
            ext = item.get("ext", {}) if isinstance(item.get("ext"), dict) else {}
            return {
                "symbol": symbol,
                "name": str(item.get("name", ext.get("name", "")) or "").strip() or None,
                "price": safe_float(item.get("last_price", item.get("price")), None),
                "open": safe_float(item.get("open"), None),
                "high": safe_float(item.get("high"), None),
                "low": safe_float(item.get("low"), None),
                "volume": safe_float(item.get("volume"), None),
                "amount": safe_float(item.get("amount"), None),
                "ts": str(
                    item.get("trade_time", item.get("timestamp", datetime.now().isoformat()))
                ),
            }
        except Exception as exc:
            logger.exception("tickflow latest quote failed for %s", symbol)
            raise RuntimeError(f"tickflow latest quote failed for {symbol}: {exc}") from exc

    def fetch_latest_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """Batch-fetch real-time quotes for multiple symbols.

        Symbols are chunked by ``quote_max_symbols_per_request`` and
        rate-limited via ``_throttle``.
        """
        normalized = [str(s or "").strip().upper() for s in symbols if str(s or "").strip()]
        if not normalized:
            return {}

        client = self._get_client()
        if client is None:
            raise RuntimeError("TICKFLOW_API_KEY is required for TickFlow batch quotes")

        chunk_size = max(1, int(self.settings.quote_max_symbols_per_request))
        result: dict[str, dict] = {}
        for i in range(0, len(normalized), chunk_size):
            chunk = normalized[i : i + chunk_size]
            tickflow_symbols = [self._to_tickflow_symbol(s) for s in chunk]
            local_by_tickflow = {
                self._to_tickflow_symbol(s): s for s in chunk
            }
            try:
                self._throttle("realtime_quote_batch", 60.0 / self.settings.quote_requests_per_minute)
                raw = client.quotes.get(symbols=tickflow_symbols)
            except Exception as exc:
                logger.exception("tickflow batch quote failed for chunk %s", chunk)
                for s in chunk:
                    result[s] = {"symbol": s, "error": str(exc)}
                continue

            if isinstance(raw, pd.DataFrame):
                items = raw.to_dict("records") if not raw.empty else []
            elif isinstance(raw, list):
                items = raw
            elif isinstance(raw, dict):
                # Single symbol returned as dict — wrap.
                items = [raw]
            else:
                items = []

            seen: set[str] = set()
            for item in items:
                if not isinstance(item, dict):
                    continue
                raw_sym = str(item.get("symbol", ""))
                local_sym = local_by_tickflow.get(raw_sym, raw_sym)
                if not local_sym or local_sym in seen:
                    continue
                seen.add(local_sym)
                ext = item.get("ext", {}) if isinstance(item.get("ext"), dict) else {}
                result[local_sym] = {
                    "symbol": local_sym,
                    "name": str(item.get("name", ext.get("name", "")) or "").strip() or None,
                    "price": safe_float(item.get("last_price", item.get("price")), None),
                    "open": safe_float(item.get("open"), None),
                    "high": safe_float(item.get("high"), None),
                    "low": safe_float(item.get("low"), None),
                    "volume": safe_float(item.get("volume"), None),
                    "amount": safe_float(item.get("amount"), None),
                    "ts": str(
                        item.get("trade_time", item.get("timestamp", datetime.now().isoformat()))
                    ),
                }

            # Any chunk symbol not returned by the API gets an error entry.
            for s in chunk:
                if s not in result:
                    result[s] = {"symbol": s, "error": "no quote returned"}

        return result

    def fetch_instrument_name(self, symbol: str) -> str | None:
        client = self._get_client()
        if client is None:
            raise RuntimeError("TICKFLOW_API_KEY is required for TickFlow instrument name")
        try:
            item = client.instruments.get(self._to_tickflow_symbol(symbol))
            if not isinstance(item, dict):
                return None
            return str(item.get("name", "") or "").strip() or None
        except Exception as exc:
            logger.exception("tickflow instrument fetch failed for %s", symbol)
            raise RuntimeError(f"tickflow instrument fetch failed for {symbol}: {exc}") from exc

    def fetch_trading_calendar(self, start: date, end: date) -> list[date]:
        return []

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception as exc:
                logger.debug("tickflow client close failed: %s", exc)
        self._client = None
