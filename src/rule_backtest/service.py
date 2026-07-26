from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
import logging
from pathlib import Path
import re

import pandas as pd

from data.storage.market_store import MarketStore
from rule_backtest.engine import SingleSymbolAllInBacktestEngine
from rule_backtest.loader import StrategyLoader
from rule_backtest.models import DEFAULT_FEE_RATE, BacktestExecutionConfig, RuleBacktestRequest
from rule_backtest.registry import registry_payload
from rule_backtest.sizing import PositionStrategyLoader, build_sizer, sizer_types_payload, sizing_flags_payload

logger = logging.getLogger(__name__)

# Per-strategy fields kept when a backtest result is serialized for the
# frontend. Per-bar series (daily_nav, charts, drawdown, benchmark.series,
# condition_trace, monthly_returns) are intentionally excluded: the UI never
# reads them and they dominate the payload size (several MB per strategy on
# long ranges), which stalls delivery over the frp relay link.
_RESULT_KEEP_KEYS = (
    "run_id",
    "status",
    "strategy_id",
    "strategy_name",
    "sizer_id",
    "sizer_name",
    "symbol",
    "start_date",
    "end_date",
    "initial_capital",
    "final_equity",
    "summary",
    "trades",
    "skipped_buys",
    "annual_returns",
    "monthly_heatmap",
)

# Top-level fields kept. Note the heavy backward-compat duplicates of the
# first result (charts / daily_nav / drawdown / condition_trace / benchmark /
# monthly_returns) are dropped as well; small duplicates (summary, trades...)
# stay so the legacy single-result frontend path keeps working.
_TOP_KEEP_KEYS = (
    "results",
    "benchmark_summary",
    "multi_kline",
    "status",
    "run_id",
    "strategy_id",
    "sizer_id",
    "sizer_name",
    "symbol",
    "start_date",
    "end_date",
    "initial_capital",
    "final_equity",
    "summary",
    "trades",
    "skipped_buys",
    "annual_returns",
    "monthly_heatmap",
    "debug_log",
)


def slim_backtest_result(result: dict) -> dict:
    """Return a transport-slimmed copy of a full backtest result.

    Computation is untouched — the full result stays in the in-memory job;
    this only trims what is serialized to the browser. K-line markers come
    from ``multi_kline`` (already slim) and the K-line chart itself uses the
    market-view daily endpoint, so per-strategy candle/series payloads are
    pure waste on the wire.
    """
    out = {k: result[k] for k in _TOP_KEEP_KEYS if k in result and k != "results"}
    out["results"] = [
        {k: r[k] for k in _RESULT_KEEP_KEYS if k in r}
        for r in result.get("results", [])
    ]
    return out


class RuleBacktestService:
    def __init__(
        self,
        strategy_loader: StrategyLoader | None = None,
        market_store: MarketStore | None = None,
        position_loader: PositionStrategyLoader | None = None,
    ) -> None:
        self.strategy_loader = strategy_loader or StrategyLoader()
        self.market_store = market_store or MarketStore()
        self.position_loader = position_loader or PositionStrategyLoader()
        self.engine = SingleSymbolAllInBacktestEngine()

    def list_strategies(self) -> list[dict]:
        return self.strategy_loader.list_strategies()

    def list_position_strategies(self) -> list[dict]:
        return self.position_loader.list_strategies()

    def list_sizer_types(self) -> list[dict]:
        return sizer_types_payload()

    def list_sizing_flags(self) -> dict:
        return sizing_flags_payload()

    def save_position_strategy(self, strategy: dict, overwrite: bool = False) -> dict:
        if not str(strategy.get("id", "")).strip():
            strategy = dict(strategy)
            strategy["id"] = self._generate_strategy_id(str(strategy.get("name", "") or "position"))
        return self.position_loader.save(strategy=strategy, overwrite=overwrite)

    def delete_position_strategy(self, strategy_id: str) -> dict:
        return self.position_loader.delete(strategy_id)

    def list_indicators(self) -> list[dict]:
        return registry_payload()

    def save_strategy(self, strategy: dict, overwrite: bool = False) -> dict:
        if not str(strategy.get("id", "")).strip():
            strategy = dict(strategy)
            strategy["id"] = self._generate_strategy_id(str(strategy.get("name", "") or "strategy"))
        return self.strategy_loader.save(strategy=strategy, overwrite=overwrite)

    def delete_strategy(self, strategy_id: str) -> dict:
        return self.strategy_loader.delete(strategy_id)

    def list_instruments(self) -> list[dict]:
        import sqlite3

        from data.storage.db import get_db

        try:
            instruments = get_db().list_instrument_metadata()
        except (RuntimeError, sqlite3.Error) as exc:
            logger.warning("Instrument metadata unavailable; instrument list is empty: %s", exc)
            instruments = []  # database unavailable (bare test/script context)
        rows: list[dict] = []
        try:
            stored_symbols = set(self.market_store.list_stored_symbols())
        except Exception as exc:
            logger.warning("Stored market symbols unavailable: %s", exc)
            stored_symbols = set()
        for item in instruments:
            symbol = str(item.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "name": str(item.get("name", "") or ""),
                    "enabled": bool(item.get("enabled", True)),
                    "asset_type": str(item.get("asset_type") or "etf"),
                    "has_market_data": symbol in stored_symbols,
                }
            )
        return rows

    def run(
        self,
        payload: dict,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict:
        strategy_ids = self._normalize_strategy_ids(payload)
        symbol = str(payload.get("symbol", "")).strip().upper()

        # Batch-drill snapshot mode: run a frozen strategy payload (from a
        # batch snapshot) instead of loading the current DB version, so
        # drill-down re-runs reproduce the batch-time configuration.
        snapshot_strategy = payload.get("strategy_config")
        if isinstance(snapshot_strategy, dict) and snapshot_strategy:
            from rule_backtest.validators import StrategyConfigValidator

            validation = StrategyConfigValidator().validate_and_normalize(snapshot_strategy)
            if not validation.ok:
                raise ValueError("; ".join(validation.errors))
            snapshot_strategy = validation.normalized or snapshot_strategy
            strategy_ids = [str(snapshot_strategy.get("id") or "snapshot")]
        else:
            snapshot_strategy = None

        if not strategy_ids:
            raise ValueError("strategy_ids is required")
        if not symbol:
            raise ValueError("symbol is required")

        start_date = self._parse_date(payload.get("start_date"))
        end_date = self._parse_date(payload.get("end_date"))
        if start_date is not None and end_date is not None and start_date > end_date:
            raise ValueError("start_date cannot be later than end_date")

        bars = self.market_store.load_history(symbol)
        trading_bars = self._filter_bars(
            bars=bars,
            start_date=start_date,
            end_date=end_date,
        )
        if trading_bars.empty:
            raise ValueError(f"symbol has no market data in range: {symbol}")

        results: list[dict] = []
        debug_enabled_for_first = False
        sizers = self._resolve_sizers(payload)
        # Cartesian product: trade strategies x position sizers.
        combos = [(sid, sizer) for sid in strategy_ids for sizer in sizers]
        days_per_combo = len(trading_bars)
        total_days = days_per_combo * len(combos)
        if progress_callback is not None:
            # Report the real total up front: the UI shows 0/N during
            # preparation (e.g. instrument-type resolution) instead of the
            # meaningless 0/1 placeholder.
            progress_callback(0, total_days)

        instrument_type = str(payload.get("instrument_type", "") or "").strip().lower()
        if instrument_type not in {"etf", "stock"}:
            instrument_type = self._resolve_instrument_type(symbol)

        execution = BacktestExecutionConfig(
            initial_capital=float(payload.get("initial_capital", 100_000.0) or 100_000.0),
            fee_rate=float(payload.get("fee_rate", DEFAULT_FEE_RATE) or DEFAULT_FEE_RATE),
            fee_min=float(payload.get("fee_min", 5.0) or 5.0),
            slippage=float(payload.get("slippage", 0.002) or 0.002),
            lot_size=int(payload.get("lot_size", 100) or 100),
            instrument_type="stock" if instrument_type == "stock" else "etf",
            stock_stamp_tax_rate=float(payload.get("stock_stamp_tax_rate", 0.001) or 0.001),
            debug_log_enabled=self._parse_debug_flag(payload.get("debug_log_enabled")),
        )

        for c_idx, (sid, sizer) in enumerate(combos):
            strategy = snapshot_strategy if snapshot_strategy is not None else self.strategy_loader.load(sid)
            if progress_callback is not None:
                day_offset = c_idx * days_per_combo

                def engine_progress(day_cur: int, day_total: int, *, _offset: int = day_offset) -> None:
                    progress_callback(min(_offset + day_cur, total_days), total_days)
            else:
                engine_progress = None
            request = RuleBacktestRequest(
                strategy=strategy,
                symbol=symbol,
                bars=bars,
                start_date=start_date,
                end_date=end_date,
                execution=execution,
                run_id=datetime.now().strftime("%Y%m%d%H%M%S%f"),
                sizer=sizer,
                progress_callback=engine_progress,
            )
            result = self.engine.run(request)
            result["strategy_name"] = str(strategy.get("name", "") or sid)
            results.append(result)
            if not debug_enabled_for_first and result.get("debug_log"):
                debug_enabled_for_first = True

        if not results:
            raise ValueError("no strategy results produced")

        multi_kline = []
        for r in results:
            kline = r.get("charts", {}).get("kline", {})
            multi_kline.append({
                "strategy_id": r.get("strategy_id", ""),
                "strategy_name": r.get("strategy_name", ""),
                "sizer_id": r.get("sizer_id", ""),
                "sizer_name": r.get("sizer_name", ""),
                "buy_points": kline.get("buy_points", []),
                "sell_points": kline.get("sell_points", []),
                "skipped_buy_points": kline.get("skipped_buy_points", []),
            })

        first = results[0]
        return {
            "results": results,
            "benchmark_summary": first.get("benchmark_summary", {}),
            "multi_kline": multi_kline,
            # backward-compat: first result's fields for charts / trades / debug
            "status": first.get("status", "ok"),
            "run_id": first.get("run_id", ""),
            "strategy_id": first.get("strategy_id", ""),
            "sizer_id": first.get("sizer_id", ""),
            "sizer_name": first.get("sizer_name", ""),
            "symbol": symbol,
            "start_date": first.get("start_date"),
            "end_date": first.get("end_date"),
            "initial_capital": float(execution.initial_capital),
            "final_equity": first.get("final_equity"),
            "summary": first.get("summary", {}),
            "trades": first.get("trades", []),
            "skipped_buys": first.get("skipped_buys", []),
            "daily_nav": first.get("daily_nav", []),
            "condition_trace": first.get("condition_trace", []),
            "debug_log": first.get("debug_log", []),
            "drawdown": first.get("drawdown", []),
            "annual_returns": first.get("annual_returns", []),
            "monthly_returns": first.get("monthly_returns", []),
            "monthly_heatmap": first.get("monthly_heatmap", {}),
            "benchmark": first.get("benchmark", {}),
            "charts": first.get("charts", {}),
        }

    @staticmethod
    def _parse_date(value: object) -> date | None:
        text = str(value or "").strip()
        if not text:
            return None
        return datetime.strptime(text, "%Y-%m-%d").date()

    @staticmethod
    def _normalize_strategy_ids(payload: dict) -> list[str]:
        ids = payload.get("strategy_ids", [])
        if isinstance(ids, str):
            ids = [ids]
        # backward compat: single strategy_id field
        single = str(payload.get("strategy_id", "") or "").strip()
        if single and single not in ids:
            ids = list(ids) + [single]
        return [s for s in ids if s]

    def _resolve_sizers(self, payload: dict) -> list:
        """Position sizers for the cartesian product. Empty selection means
        the built-in all-in behavior (sizer=None), fully backward compatible."""
        ids = payload.get("position_strategy_ids", [])
        if isinstance(ids, str):
            ids = [ids]
        ids = [s for s in ids if s]
        if not ids:
            return [None]
        return [build_sizer(self.position_loader.load(psid)) for psid in ids]

    @staticmethod
    def _filter_bars(bars: pd.DataFrame, start_date: date | None, end_date: date | None) -> pd.DataFrame:
        if bars.empty:
            return bars
        df = bars.copy()
        if "date" not in df.columns and "time" in df.columns:
            df["date"] = pd.to_datetime(df["time"], errors="coerce").dt.date
        else:
            df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
        if start_date is not None:
            df = df[df["date"] >= start_date]
        if end_date is not None:
            df = df[df["date"] <= end_date]
        return df

    def _resolve_instrument_type(self, symbol: str) -> str:
        for item in self.list_instruments():
            if item["symbol"] == symbol:
                asset_type = str(item.get("asset_type", "etf")).lower()
                return "stock" if asset_type == "stock" else "etf"
        return "etf"

    @staticmethod
    def _parse_debug_flag(value: object) -> bool | None:
        text = str(value or "").strip().lower()
        if text == "":
            return None
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return None

    @staticmethod
    def _generate_strategy_id(name: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")[:32]
        suffix = datetime.now().strftime("%Y%m%d%H%M%S%f")
        return f"{slug or 'strategy'}_{suffix}"
