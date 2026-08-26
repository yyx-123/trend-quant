from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta

import pandas as pd

from audit.app_logger import get_logger
from core import env
from core.adjustment import compute_qfq, factors_equal
from core.bars import date_span
from core.calendar import is_trading_day as _calendar_is_trading_day
from core.calendar import market_now
from core.settings import TickFlowSettings, load_settings
from core.strategy_config import backfill_start_date
from core.symbols import normalize_symbol
from data.provider_tickflow import TickFlowProvider
from data.storage.db import get_db, record_job_run_safely
from data.storage.market_store import MarketStore

logger = get_logger(__name__)
_symbol_locks_guard = threading.Lock()
_symbol_locks: dict[str, threading.Lock] = {}


class DataProviderError(RuntimeError):
    """Raised when the configured market data provider cannot return usable data."""


def _symbol_lock(symbol: str) -> threading.Lock:
    key = str(symbol or "").strip().upper()
    with _symbol_locks_guard:
        lock = _symbol_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _symbol_locks[key] = lock
        return lock


# 实时报价进程级短 TTL 缓存：打开一个标的页时，日K overlay、my-trades 止损
# 等会为同一标的各拉一次 tickflow 报价（按次限流 + RTT 秒级，页面因此数秒
# 打不开）。30s 短缓存让 TTL 内重复请求零网络开销；远短于盘中快照 5 分钟
# 节奏，不影响新鲜度口径（下游 is_quote_fresh 校验照常作用于报价本身）。
_QUOTE_CACHE_TTL_SECONDS = max(
    0.0, env.quote_cache_ttl_seconds(30.0)
)
_quote_cache: dict[str, tuple[float, dict]] = {}
_quote_cache_lock = threading.Lock()


def _quote_cache_get(symbol: str) -> dict | None:
    key = str(symbol or "").strip().upper()
    if not key:
        return None
    with _quote_cache_lock:
        entry = _quote_cache.get(key)
        if entry and (time.monotonic() - entry[0]) < _QUOTE_CACHE_TTL_SECONDS:
            return dict(entry[1])
    return None


def _quote_cache_put(symbol: str, quote: dict) -> None:
    key = str(symbol or "").strip().upper()
    if not key or not quote or quote.get("error") or quote.get("price") is None:
        return
    with _quote_cache_lock:
        _quote_cache[key] = (time.monotonic(), dict(quote))


def _retry_wait_seconds(errors: dict[str, str], fallback: float) -> float:
    waits: list[float] = []
    for message in errors.values():
        match = re.search(r"请\s*(\d+)\s*ms\s*后重试", str(message))
        if match:
            waits.append(max(0.0, int(match.group(1)) / 1000.0))
    return max([float(fallback), *waits]) if waits else float(fallback)


def _non_retryable_provider_error(errors: dict[str, str]) -> str | None:
    markers = (
        "批量查询权限",
        "无日/周/月K线查询批量查询权限",
        "403 Forbidden",
        "PermissionError",
    )
    for message in errors.values():
        text = str(message or "")
        if any(marker in text for marker in markers):
            return text
    return None


class DataService:
    def __init__(
        self,
        provider_priority: list[str] | None = None,
        tickflow_settings: TickFlowSettings | None = None,
    ) -> None:
        self.tickflow_settings = tickflow_settings or load_settings().tickflow
        self.providers = {
            "tickflow": TickFlowProvider(settings=self.tickflow_settings),
        }
        requested = provider_priority or ["tickflow"]
        ignored = [name for name in requested if name != "tickflow"]
        if ignored:
            logger.warning(
                "Ignoring non-TickFlow data providers %s; TickFlow is the only active provider",
                ignored,
            )
        self.provider_priority = ["tickflow"]
        # raw（不复权）为唯一真源：历史行永不回溯改写，日更只 append；
        # qfq 表由 raw + 除权因子本地物化（core/adjustment.py），全系统读取不变。
        self.market_store = MarketStore()
        self.raw_store = MarketStore(price_mode="raw")

    def _ordered_providers(self):
        for name in self.provider_priority:
            provider = self.providers.get(name)
            if provider is not None:
                yield name, provider

    def fetch_daily_history(self, symbol: str, start: date, end: date, adjust: str = "qfq") -> pd.DataFrame:
        name, provider = self._tickflow_provider()
        data = provider.fetch_daily_history(symbol, start, end, adjust)
        if data.empty:
            raise DataProviderError(
                f"TickFlow returned no daily history for {symbol} from {start} to {end}"
            )
        data["provider"] = name
        return data

    def fetch_daily_histories(
        self,
        symbols: list[str],
        start: date,
        end: date,
        adjust: str = "qfq",
        *,
        batch_size: int = 100,
        request_interval_seconds: float = 2.0,
    ) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
        name, provider = self._tickflow_provider()
        fetcher = getattr(provider, "fetch_daily_histories", None)
        if not callable(fetcher):
            data_by_symbol: dict[str, pd.DataFrame] = {}
            errors: dict[str, str] = {}
            for symbol in symbols:
                try:
                    data_by_symbol[symbol] = provider.fetch_daily_history(symbol, start, end, adjust)
                except Exception as exc:
                    errors[symbol] = str(exc)
            for df in data_by_symbol.values():
                if not df.empty:
                    df["provider"] = name
            return data_by_symbol, errors

        data_by_symbol, errors = fetcher(
            symbols,
            start,
            end,
            adjust,
            batch_size=batch_size,
            request_interval_seconds=request_interval_seconds,
        )
        for df in data_by_symbol.values():
            if not df.empty:
                df["provider"] = name
        return data_by_symbol, errors

    def fetch_latest_quote(self, symbol: str) -> dict:
        symbol = normalize_symbol(symbol)
        cached = _quote_cache_get(symbol)
        if cached is not None:
            return cached
        name, provider = self._tickflow_provider()
        quote = provider.fetch_latest_quote(symbol)
        if quote.get("price") is None:
            raise DataProviderError(f"TickFlow returned no latest quote price for {symbol}")
        quote["provider"] = name
        _quote_cache_put(symbol, quote)
        return quote

    def fetch_latest_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """Batch-fetch real-time quotes for multiple symbols."""
        if not symbols:
            return {}
        # 入口统一归一化，保证缓存键（归一化 symbol）与调用方传参口径一致
        symbols = [normalize_symbol(s) for s in symbols]
        # TTL 内已有缓存的直接命中，只为缺失/过期的标的发网络请求
        result: dict[str, dict] = {}
        missing: list[str] = []
        for symbol in symbols:
            cached = _quote_cache_get(symbol)
            if cached is not None:
                result[symbol] = cached
            else:
                missing.append(symbol)
        if not missing:
            return result
        name, provider = self._tickflow_provider()
        batch_fetcher = getattr(provider, "fetch_latest_quotes", None)
        if not callable(batch_fetcher):
            # Fallback: call single-symbol fetch in a loop.
            for symbol in missing:
                try:
                    result[symbol] = self.fetch_latest_quote(symbol)
                except Exception as exc:
                    result[symbol] = {"symbol": symbol, "error": str(exc)}
            return result
        quotes = batch_fetcher(missing)
        for q in quotes.values():
            if "error" not in q:
                q["provider"] = name
                _quote_cache_put(q.get("symbol", ""), q)
        result.update(quotes)
        return result

    def fetch_instrument_name(self, symbol: str) -> dict:
        name, provider = self._tickflow_provider()
        provider_name_fetcher = getattr(provider, "fetch_instrument_name", None)
        if callable(provider_name_fetcher):
            instrument_name = str(provider_name_fetcher(symbol) or "").strip()
            if instrument_name:
                return {
                    "symbol": symbol,
                    "name": instrument_name,
                    "provider": name,
                    "ts": market_now().replace(tzinfo=None).isoformat(),
                }
        quote = provider.fetch_latest_quote(symbol)
        instrument_name = str(quote.get("name", "") or "").strip()
        if instrument_name:
            return {
                "symbol": symbol,
                "name": instrument_name,
                "provider": name,
                "ts": quote.get("ts"),
            }
        raise DataProviderError(f"TickFlow returned no instrument name for {symbol}")

    # ------------------------------------------------------------------
    # 除权因子同步与 qfq 本地物化（raw 真源架构）
    # ------------------------------------------------------------------

    def fetch_ex_factors(self, symbols: list[str]) -> tuple[dict[str, list], dict[str, str]]:
        """批量拉取除权因子：{symbol: [(ex_date, factor)]}。"""
        _name, provider = self._tickflow_provider()
        return provider.fetch_ex_factors(symbols)

    def sync_ex_factors(self, symbols: list[str], *, db=None) -> tuple[dict[str, list], list[str]]:
        """批量同步因子：拉取 → 与本地因子表 diff → 新因子落库。

        返回 (factors_map, changed_symbols)。除权检测的唯一判据 ——
        替代旧的「重拉近期 K 线比价」法（D9），无需重拉任何行情。
        """
        db = db or get_db()
        stored = db.load_all_ex_factors()
        fetched, errors = self.fetch_ex_factors(symbols)
        if errors:
            logger.warning("ex-factor sync partial failure (%d symbols): %s", len(errors), errors)
        changed: list[str] = []
        for symbol, factors in fetched.items():
            if not factors_equal(stored.get(symbol, []), factors):
                db.replace_ex_factors(symbol, factors, provider="tickflow")
                changed.append(symbol)
        return fetched, changed

    def rematerialize_qfq(self, symbol: str, factors: list | None = None, *, db=None) -> dict:
        """由本地 raw + 除权因子全量重写该标的的 qfq 表（纯本地操作）。

        raw 覆盖不如存量 qfq（过渡期半迁移状态）时拒绝物化并返回
        raw_incomplete —— 宁可保留旧数据也不截断历史，等迁移脚本补全 raw。
        """
        db = db or get_db()
        symbol = str(symbol or "").strip().upper()
        raw = self.raw_store.load_history(symbol)
        if raw.empty:
            return {"symbol": symbol, "status": "no_raw", "rows": 0}
        raw_start, raw_end = self._date_span(raw)
        qfq_summary = db.get_market_data_summary(symbol, price_mode="qfq")
        # 两侧时间格式不一（'YYYY-MM-DD' vs 'YYYY-MM-DD 00:00:00'），统一按前 10 位比较
        qfq_start = str(qfq_summary["start"] or "")[:10] or None
        qfq_end = str(qfq_summary["end"] or "")[:10] or None
        if qfq_summary["rows"] and (
            (raw_start and qfq_start and raw_start > qfq_start)
            or (raw_end and qfq_end and raw_end < qfq_end)
        ):
            logger.warning(
                "raw coverage %s~%s does not cover stored qfq %s~%s for %s; "
                "skip rematerialize (run the raw migration script first)",
                raw_start, raw_end, qfq_start, qfq_end, symbol,
            )
            return {"symbol": symbol, "status": "raw_incomplete", "rows": 0}
        if factors is None:
            factors = db.load_ex_factors(symbol)
        qfq = compute_qfq(raw, factors)
        qfq["provider"] = "local_raw+factors"
        rows = self.market_store.replace_history(symbol, qfq)
        return {"symbol": symbol, "status": "ok", "rows": int(rows)}

    def is_trading_day(self, day: date) -> bool:
        # Use the project-level calendar which combines weekday
        # checks with known A-share holiday exclusions.
        return _calendar_is_trading_day(day)

    def ensure_daily_history(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
        adjust: str = "qfq",
        *,
        factors: list | None = None,
        factors_changed: bool | None = None,
        db=None,
    ) -> dict:
        """日更：raw 增量 append → 因子同步 → 本地物化 qfq。

        adjust 参数仅为兼容旧调用保留；存储恒为 raw 真源 + 本地物化 qfq。
        factors/factors_changed 由 update_pool_daily 批量预同步后传入；
        未传时本方法自行单标的同步。
        """
        db = db or get_db()
        existing = self.raw_store.load_history(symbol)
        if existing.empty:
            fetch_start = start_date
        else:
            existing["time"] = pd.to_datetime(existing["time"], errors="coerce")
            max_time = existing["time"].dropna().max()
            if pd.isna(max_time):
                fetch_start = start_date
            else:
                fetch_start = max(start_date, max_time.date() + timedelta(days=1))

        raw_updated = False
        if fetch_start <= end_date:
            try:
                fetched = self.fetch_daily_history(symbol, fetch_start, end_date, adjust="none")
            except DataProviderError:
                fetched = pd.DataFrame()  # 区间无新 bar（非交易日/停牌）视为无增量
            if not fetched.empty:
                merged = pd.concat([existing, fetched], ignore_index=True)
                merged["time"] = pd.to_datetime(merged["time"], errors="coerce")
                merged = merged.dropna(subset=["time"]).drop_duplicates(subset=["time"]).sort_values("time")
                merged = merged.reset_index(drop=True)
                self.raw_store.save_history(symbol, merged)
                existing = merged
                raw_updated = True

        # 因子同步（未预取时单标兜底）
        if factors is None:
            try:
                factors_map, changed = self.sync_ex_factors([symbol], db=db)
                factors = factors_map.get(symbol, [])
                factors_changed = symbol in changed
            except Exception:
                logger.exception("ex-factor sync failed for %s; using stored factors", symbol)
                factors = db.load_ex_factors(symbol)
                factors_changed = False
        elif factors_changed is None:
            factors_changed = not factors_equal(db.load_ex_factors(symbol), factors)

        # qfq 跨度落后于 raw（含从未物化过）时也需要物化 —— 自愈特性
        qfq_summary = db.get_market_data_summary(symbol, price_mode="qfq")
        raw_start, raw_end = self._date_span(existing)
        qfq_start = str(qfq_summary["start"] or "")[:10] or None
        qfq_end = str(qfq_summary["end"] or "")[:10] or None
        qfq_behind = bool(raw_end) and (
            not qfq_summary["rows"]
            or qfq_end != raw_end
            or qfq_start != raw_start
        )

        status = "updated" if raw_updated else "up_to_date"
        remat_status = None
        if raw_updated or factors_changed or qfq_behind:
            remat = self.rematerialize_qfq(symbol, factors, db=db)
            remat_status = remat.get("status")
            if remat_status == "ok" and factors_changed:
                status = "updated"

        return {
            "symbol": symbol,
            "status": status,
            "rows": len(existing),
            "path": f"sqlite/raw/{symbol}",
            "fetched_from": fetch_start.isoformat() if raw_updated else None,
            "fetched_to": end_date.isoformat() if raw_updated else None,
            "factors_changed": bool(factors_changed),
            "qfq_rematerialized": remat_status,
        }

    @staticmethod
    def _date_span(df: pd.DataFrame) -> tuple[str | None, str | None]:
        return date_span(df)

    def backfill_daily_history(self, symbol: str, start_date: date, end_date: date, adjust: str = "qfq") -> dict:
        if end_date < start_date:
            start_date, end_date = end_date, start_date

        existing = self.raw_store.load_history(symbol)
        local_start_before, local_end_before = self._date_span(existing)

        # raw 真源：补拉的永远是不复权数据；qfq 由本地因子物化生成
        fetched = self.fetch_daily_history(symbol, start_date, end_date, adjust="none")
        result = self._save_backfill_result(
            symbol=symbol,
            requested_start=start_date,
            requested_end=end_date,
            fetched=fetched,
            local_start_before=local_start_before,
            local_end_before=local_end_before,
        )
        # 因子同步 + 本地物化 qfq（失败不拖垮 backfill 结果，等日更自愈）
        try:
            factors_map, _changed = self.sync_ex_factors([symbol])
            remat = self.rematerialize_qfq(symbol, factors_map.get(symbol))
            result["qfq_rematerialized"] = remat.get("status")
            result["qfq_rows"] = remat.get("rows")
        except Exception:
            logger.exception("qfq rematerialize after backfill failed for %s", symbol)
            result["qfq_rematerialized"] = "failed"
        return result

    def _effective_fetch_start(self, symbol: str, requested_start: date) -> tuple[date, int, str | None, str | None]:
        with _symbol_lock(symbol):
            existing = self.raw_store.load_history(symbol)
            existing_rows = len(existing)
            local_start_before, local_end_before = self._date_span(existing)
            if existing.empty:
                return requested_start, existing_rows, local_start_before, local_end_before

            existing["time"] = pd.to_datetime(existing["time"], errors="coerce")
            max_time = existing["time"].dropna().max()
            if pd.isna(max_time):
                return requested_start, existing_rows, local_start_before, local_end_before
            return (
                max(requested_start, max_time.date() + timedelta(days=1)),
                existing_rows,
                local_start_before,
                local_end_before,
            )

    def _save_backfill_result(
        self,
        *,
        symbol: str,
        requested_start: date,
        requested_end: date,
        fetched: pd.DataFrame,
        local_start_before: str | None,
        local_end_before: str | None,
    ) -> dict:
        if requested_end < requested_start:
            requested_start, requested_end = requested_end, requested_start

        store = self.raw_store  # backfill 一律写 raw 真源
        fetched_rows = len(fetched)
        fetched_start, fetched_end = self._date_span(fetched)
        if fetched.empty:
            with _symbol_lock(symbol):
                existing = store.load_history(symbol)
                existing_rows = len(existing)
                local_start_after, local_end_after = self._date_span(existing)
            return {
                "symbol": symbol,
                "status": "no_data",
                "requested_start": requested_start.isoformat(),
                "requested_end": requested_end.isoformat(),
                "rows_before": existing_rows,
                "rows_after": existing_rows,
                "added_rows": 0,
                "fetched_rows": 0,
                "fetched_start": None,
                "fetched_end": None,
                "local_start_before": local_start_before,
                "local_end_before": local_end_before,
                "local_start_after": local_start_after,
                "local_end_after": local_end_after,
                "path": f"sqlite/raw/{symbol}",
            }

        with _symbol_lock(symbol):
            existing = store.load_history(symbol)
            existing_rows = len(existing)
            current_local_start, current_local_end = self._date_span(existing)
            local_start_before = local_start_before or current_local_start
            local_end_before = local_end_before or current_local_end
            to_save = fetched.copy()
            to_save["time"] = pd.to_datetime(to_save["time"], errors="coerce")
            to_save = to_save.dropna(subset=["time"]).drop_duplicates(subset=["time"]).sort_values("time")
            to_save = to_save.reset_index(drop=True)
            path = store.save_history(symbol, to_save)
            saved = store.load_history(symbol)
            rows_after = len(saved)
            local_start_after, local_end_after = self._date_span(saved)
        return {
            "symbol": symbol,
            "status": "updated",
            "requested_start": requested_start.isoformat(),
            "requested_end": requested_end.isoformat(),
            "rows_before": existing_rows,
            "rows_after": rows_after,
            "added_rows": rows_after - existing_rows,
            "fetched_rows": fetched_rows,
            "fetched_start": fetched_start,
            "fetched_end": fetched_end,
            "local_start_before": local_start_before,
            "local_end_before": local_end_before,
            "local_start_after": local_start_after,
            "local_end_after": local_end_after,
            "path": str(path),
        }

    def _up_to_date_result(
        self,
        *,
        symbol: str,
        requested_start: date,
        requested_end: date,
        rows: int,
        local_start: str | None,
        local_end: str | None,
    ) -> dict:
        return {
            "symbol": symbol,
            "status": "up_to_date",
            "requested_start": requested_start.isoformat(),
            "requested_end": requested_end.isoformat(),
            "rows_before": rows,
            "rows_after": rows,
            "added_rows": 0,
            "fetched_rows": 0,
            "fetched_start": None,
            "fetched_end": None,
            "local_start_before": local_start,
            "local_end_before": local_end,
            "local_start_after": local_start,
            "local_end_after": local_end,
            "path": f"sqlite/raw/{symbol}",
        }

    def backfill_daily_histories(
        self,
        items: list[dict],
        end_date: date,
        adjust: str = "qfq",
        *,
        batch_size: int = 100,
        max_retries: int = 3,
        request_interval_seconds: float = 2.0,
        retry_delay_seconds: float = 2.0,
        progress_callback: Callable[[dict], None] | None = None,
    ) -> list[dict]:
        normalized_items: dict[str, date] = {}
        for item in items:
            symbol = str(item.get("symbol") or "").strip().upper()
            if not symbol or symbol in normalized_items:
                continue
            raw_start = item.get("start_date") or backfill_start_date()
            start = raw_start if isinstance(raw_start, date) else datetime.strptime(str(raw_start), "%Y-%m-%d").date()
            start = min(start, end_date)
            normalized_items[symbol] = start

        results: dict[str, dict] = {}
        remaining = list(normalized_items.keys())
        total = len(remaining)

        for attempt in range(max_retries + 1):
            if not remaining:
                break
            if progress_callback:
                progress_callback(
                    {
                        "event": "attempt_start",
                        "attempt": attempt + 1,
                        "max_attempts": max_retries + 1,
                        "remaining": len(remaining),
                        "finished": len(results),
                        "total": total,
                    }
                )

            work: list[dict] = []
            for symbol in remaining:
                requested_start = normalized_items[symbol]
                fetch_start, rows, local_start, local_end = self._effective_fetch_start(symbol, requested_start)
                if fetch_start > end_date:
                    results[symbol] = {
                        "ok": True,
                        "result": self._up_to_date_result(
                            symbol=symbol,
                            requested_start=requested_start,
                            requested_end=end_date,
                            rows=rows,
                            local_start=local_start,
                            local_end=local_end,
                        ),
                    }
                    if progress_callback:
                        progress_callback(
                            {
                                "event": "item_done",
                                "symbol": symbol,
                                "attempt": attempt + 1,
                                "finished": len(results),
                                "total": total,
                            }
                        )
                    continue
                work.append(
                    {
                        "symbol": symbol,
                        "requested_start": requested_start,
                        "fetch_start": fetch_start,
                        "local_start_before": local_start,
                        "local_end_before": local_end,
                    }
                )

            attempt_errors: dict[str, str] = {}
            non_retryable_error: str | None = None
            batch_limit = max(1, min(int(batch_size or 100), 100))
            chunks = [work[index : index + batch_limit] for index in range(0, len(work), batch_limit)]
            for chunk_index, chunk in enumerate(chunks, start=1):
                if not chunk:
                    continue
                symbols = [item["symbol"] for item in chunk]
                fetch_start = min(item["fetch_start"] for item in chunk)
                if progress_callback:
                    progress_callback(
                        {
                            "event": "request_start",
                            "symbols": symbols,
                            "attempt": attempt + 1,
                            "chunk_index": chunk_index,
                            "chunk_total": len(chunks),
                            "finished": len(results),
                            "total": total,
                        }
                    )
                data_by_symbol, errors = self.fetch_daily_histories(
                    symbols,
                    fetch_start,
                    end_date,
                    adjust="none",
                    batch_size=batch_limit,
                    request_interval_seconds=0,
                )
                attempt_errors.update(errors)
                chunk_non_retryable_error = _non_retryable_provider_error(errors)
                if chunk_non_retryable_error:
                    non_retryable_error = chunk_non_retryable_error
                    for remaining_item in work:
                        attempt_errors.setdefault(remaining_item["symbol"], non_retryable_error)
                    break
                for item in chunk:
                    symbol = item["symbol"]
                    if symbol in errors:
                        continue
                    fetched = data_by_symbol.get(symbol, pd.DataFrame())
                    if not fetched.empty:
                        fetched = fetched.copy()
                        fetched["time"] = pd.to_datetime(fetched["time"], errors="coerce")
                        mask = (
                            (fetched["time"].dt.date >= item["fetch_start"])
                            & (fetched["time"].dt.date <= end_date)
                        )
                        fetched = fetched.loc[mask].reset_index(drop=True)
                    try:
                        result = self._save_backfill_result(
                            symbol=symbol,
                            requested_start=item["requested_start"],
                            requested_end=end_date,
                            fetched=fetched,
                            local_start_before=item["local_start_before"],
                            local_end_before=item["local_end_before"],
                        )
                    except Exception as exc:
                        attempt_errors[symbol] = str(exc)
                        logger.exception("saving backfilled data failed for %s", symbol)
                        continue
                    results[symbol] = {"ok": True, "result": result}
                    if progress_callback:
                        progress_callback(
                            {
                                "event": "item_done",
                                "symbol": symbol,
                                "attempt": attempt + 1,
                                "finished": len(results),
                                "total": total,
                            }
                        )

                if request_interval_seconds > 0 and chunk_index < len(chunks):
                    time.sleep(float(request_interval_seconds))

            remaining = [symbol for symbol in remaining if symbol not in results]
            if remaining and non_retryable_error:
                for symbol in remaining:
                    results[symbol] = {
                        "ok": False,
                        "symbol": symbol,
                        "error": non_retryable_error,
                    }
                break
            if remaining and attempt < max_retries:
                wait_seconds = _retry_wait_seconds(attempt_errors, retry_delay_seconds)
                if progress_callback:
                    progress_callback(
                        {
                            "event": "retry_sleep",
                            "attempt": attempt + 1,
                            "next_attempt": attempt + 2,
                            "remaining": len(remaining),
                            "wait_seconds": wait_seconds,
                            "finished": len(results),
                            "total": total,
                            "errors": {symbol: attempt_errors.get(symbol, "") for symbol in remaining},
                        }
                    )
                time.sleep(wait_seconds)
            elif remaining:
                for symbol in remaining:
                    results[symbol] = {
                        "ok": False,
                        "symbol": symbol,
                        "error": attempt_errors.get(symbol) or "补齐失败，已达到最大重试次数",
                    }

        # raw 落库完成后：批量因子同步 + 逐标的本重物化 qfq
        ok_symbols = [symbol for symbol, payload in results.items() if payload.get("ok")]
        if ok_symbols:
            factors_map: dict[str, list] = {}
            try:
                factors_map, _changed = self.sync_ex_factors(ok_symbols)
            except Exception:
                logger.exception("batch ex-factor sync after backfill failed; using stored factors")
            if progress_callback:
                progress_callback({"event": "rematerialize_start", "total": len(ok_symbols)})
            for index, symbol in enumerate(ok_symbols, start=1):
                try:
                    remat = self.rematerialize_qfq(symbol, factors_map.get(symbol))
                    results[symbol]["result"]["qfq_rematerialized"] = remat.get("status")
                except Exception:
                    logger.exception("qfq rematerialize after backfill failed for %s", symbol)
                    results[symbol]["result"]["qfq_rematerialized"] = "failed"
                if progress_callback:
                    progress_callback(
                        {"event": "item_done", "symbol": symbol, "finished": index, "total": len(ok_symbols)}
                    )

        return [results[symbol] for symbol in normalized_items if symbol in results]

    def update_pool_daily(
        self,
        symbols: list[str],
        start_date: date,
        end_date: date,
        adjust: str = "qfq",
        max_retries: int = 2,
        retry_interval_seconds: float = 5.0,
    ) -> dict:
        # 先批量预同步除权因子（~N/100 次请求），避免逐标的各拉一次；
        # 失败时兜底为 ensure_daily_history 内部单标同步。
        factors_map: dict[str, list] = {}
        factor_changed: set[str] = set()
        try:
            factors_map, changed = self.sync_ex_factors(symbols)
            factor_changed = set(changed)
            if factor_changed:
                logger.info("ex-factor changes detected for %d symbols: %s", len(changed), changed)
        except Exception:
            logger.exception("batch ex-factor pre-sync failed; falling back to per-symbol sync")

        results: list[dict] = []
        failed_symbols: list[str] = []

        for symbol in symbols:
            result: dict | None = None
            last_error: str | None = None
            for attempt in range(max_retries + 1):
                try:
                    result = self.ensure_daily_history(
                        symbol,
                        start_date,
                        end_date,
                        adjust=adjust,
                        factors=factors_map.get(symbol),
                        factors_changed=(symbol in factor_changed) if symbol in factors_map else None,
                    )
                    break
                except Exception as exc:
                    last_error = str(exc)
                    if attempt < max_retries:
                        logger.warning(
                            "update_pool_daily retry %s/%s for %s: %s",
                            attempt + 1,
                            max_retries,
                            symbol,
                            last_error,
                        )
                        time.sleep(float(retry_interval_seconds))
            if result is None:
                result = {"symbol": symbol, "status": "error", "error": last_error}
                failed_symbols.append(symbol)
            results.append(result)

        success_count = sum(1 for r in results if r.get("status") not in ("error", "no_data"))
        failed_count = len(failed_symbols)

        payload = {
            "ts": market_now().replace(tzinfo=None).isoformat(),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "total": len(symbols),
            "success": success_count,
            "failed": failed_count,
            "failed_symbols": failed_symbols,
            "results": results,
        }
        day = end_date.isoformat()
        status = "completed" if failed_count == 0 else "partial"
        record_job_run_safely("daily_update", payload, run_date=day, status=status)
        if failed_symbols:
            logger.warning(
                "update_pool_daily finished with %s/%s failed symbols: %s",
                failed_count,
                len(symbols),
                failed_symbols,
            )

        return payload

    def close(self) -> None:
        for provider in self.providers.values():
            close = getattr(provider, "close", None)
            if callable(close):
                close()

    def _tickflow_provider(self):
        provider = self.providers.get("tickflow")
        if provider is None:
            raise DataProviderError("TickFlow provider is not configured")
        return "tickflow", provider


# ---------------------------------------------------------------------------
# 进程级单例（P2-11）
# ---------------------------------------------------------------------------
# DataService 随处 new 会让实例级资源被无限分身（每个实例一个 TickFlow/
# httpx client）；进程内共享一个实例即可——provider 限流状态已模块级化，
# 多入口并发不会再稀释 vendor 预算。内部调用方一律不再 close() 共享实例
# （close 保留给显式生命周期管理的场景，如测试）。
_data_service_lock = threading.Lock()
_data_service_instance: DataService | None = None


def get_data_service() -> DataService:
    """返回进程级共享的 DataService（双检锁单飞构造）。"""
    global _data_service_instance
    if _data_service_instance is None:
        with _data_service_lock:
            if _data_service_instance is None:
                _data_service_instance = DataService()
    return _data_service_instance
