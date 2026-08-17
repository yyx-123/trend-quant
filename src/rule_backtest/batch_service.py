"""批量回测执行服务（方案 docs/batch-backtest/2026-07-26-batch-backtest-plan.md §5.2）。

按「标的 × 策略」格子串行执行规则回测：每标的加载一次行情（多策略复用），
每格独立 try/except（失败 continue），逐格写库（per-cell commit），
批次启动时锚定数据截止日（data_anchor_date）保证批内数据一致。

钻取一致性：格子记录实际回测起止日期 + 批次快照策略 JSON，
前端钻取用快照 + 区间重跑即可复现（漂移仅剩历史数据被改写的情形）。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime
from typing import Any

import pandas as pd

from data.storage import db as db_module
from data.storage.db import Database
from data.storage.market_store import MarketStore
from rule_backtest.engine import SingleSymbolAllInBacktestEngine
from rule_backtest.loader import StrategyLoader
from rule_backtest.models import DEFAULT_FEE_RATE, BacktestExecutionConfig, RuleBacktestRequest

logger = logging.getLogger(__name__)

# 低于该 K 线数的标的整标的记 skipped（指标 warmup 都不够）。
MIN_BARS = 60

# MVP 固定值（方案 §3.1）：后续接入 git hash / formula_version。
ENGINE_VERSION = "1.0"

# seed=None 时用 OS 熵源，格子结果不可复现 —— 批次拒绝含这些指标的策略。
RANDOM_INDICATORS = frozenset({"random_uniform"})

# 耗时预估（2026-07-26 分层实测，秒/格，按标的 bar 数分档）。
_ETA_TIERS: tuple[tuple[int, float], ...] = ((500, 0.1), (2000, 0.4), (5000, 0.9))
_ETA_DEFAULT = 1.8


def estimate_cell_seconds(bar_count: int) -> float:
    for upper, secs in _ETA_TIERS:
        if bar_count < upper:
            return secs
    return _ETA_DEFAULT


def strategy_uses_random_indicator(strategy: dict) -> bool:
    """Walk the entry/exit condition tree; True if any value spec references
    a non-deterministic indicator (seedless random_uniform)."""

    def value_uses_random(spec: Any) -> bool:
        if not isinstance(spec, dict):
            return False
        return spec.get("type") == "indicator" and spec.get("name") in RANDOM_INDICATORS

    def walk(node: Any) -> bool:
        if not isinstance(node, dict):
            return False
        if value_uses_random(node.get("left")) or value_uses_random(node.get("right")):
            return True
        return any(walk(child) for child in node.get("children") or [])

    return walk(strategy.get("entry")) or walk(strategy.get("exit"))


def build_strategy_snapshot(strategy_ids: list[str], loader: StrategyLoader | None = None) -> list[dict]:
    """Load strategies and freeze them as [{id, name, strategy_config}].

    Raises ValueError listing the strategies that use random indicators —
    the caller (POST /run validation layer) turns this into a 400.
    """
    loader = loader or StrategyLoader()
    snapshot: list[dict] = []
    random_named: list[str] = []
    for sid in strategy_ids:
        strategy = loader.load(sid)  # FileNotFoundError → 404 at the router
        name = str(strategy.get("name", "") or sid)
        if strategy_uses_random_indicator(strategy):
            random_named.append(name)
            continue
        snapshot.append({"id": sid, "name": name, "strategy_config": strategy})
    if random_named:
        raise ValueError(
            "以下策略含随机指标（结果不可复现），不支持批量回测：" + "、".join(random_named)
        )
    if not snapshot:
        raise ValueError("至少需要选择一个策略")
    return snapshot


def resolve_batch_symbols(db: Database, categories: list[str]) -> list[dict]:
    """Enabled instruments under the selected L1 categories, with bar counts."""
    wanted = {c for c in categories if c}
    if not wanted:
        raise ValueError("至少需要选择一个一级类目")
    bar_counts = db.count_bars_by_symbol()
    symbols: list[dict] = []
    for item in db.list_instrument_metadata():
        if not item.get("enabled", True):
            continue
        if str(item.get("category_l1") or "") not in wanted:
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        symbols.append(
            {
                "symbol": symbol,
                "name": str(item.get("name") or ""),
                "category_l1": str(item.get("category_l1") or ""),
                "category_l2": str(item.get("category_l2") or ""),
                "category_l3": str(item.get("category_l3") or ""),
                "asset_type": str(item.get("asset_type") or "etf"),
                "bar_count": bar_counts.get(symbol, 0),
            }
        )
    return symbols


def estimate_batch_seconds(symbols: list[dict], strategy_count: int) -> float:
    return sum(estimate_cell_seconds(int(s.get("bar_count", 0))) for s in symbols) * strategy_count


def default_batch_name(categories: list[str], strategy_count: int) -> str:
    cats = "+".join(categories[:3]) + ("..." if len(categories) > 3 else "")
    return f"{cats}×{strategy_count}策略-{date.today().isoformat()}"


def compute_features(
    feat_bars: pd.DataFrame,
    symbol: str,
    db: Database | None = None,
    anchor: date | None = None,
) -> dict:
    """标的特征（方案 §5.1，全部基于锚定日截断后的 bars，与回测区间一致）：

    - ann_volatility: 日收益率标准差 × sqrt(252)，全周期
    - momentum_250:   锚定日前 250 个交易日的价格收益率 close[-1]/close[-251]-1；
                      不足 250 根时用全部可用数据（close[-1]/close[0]-1）
    - bh_max_drawdown: 全周期买入持有最大回撤（收盘价口径，负值）
    - trend_score_avg: 锚定日前 250 个 trend_daily 行（param_set='default'）的
                      trend_score 均值；trend_daily 无行时为 None（可空）
    - amount_ma20:    SMA(amount, 20) 在锚定日的末值（流动性代理）
    - bar_count:      feat_bars 行数
    """
    features: dict[str, Any] = {
        "ann_volatility": None,
        "momentum_250": None,
        "bh_max_drawdown": None,
        "trend_score_avg": None,
        "amount_ma20": None,
        "bar_count": len(feat_bars),
    }
    if feat_bars.empty:
        return features

    closes = pd.to_numeric(feat_bars["close"], errors="coerce").dropna()
    # 防御：非正价格（复权事故/脏数据）会污染收益率特征 —— 整列作废，特征记缺失
    if not closes.empty and bool((closes <= 0).any()):
        logger.warning("compute_features: %s has non-positive closes; features set to None", symbol)
        closes = closes.iloc[0:0]
    if len(closes) >= 2:
        returns = closes.pct_change().replace([float("inf"), float("-inf")], pd.NA).dropna()
        if len(returns) >= 2:
            features["ann_volatility"] = float(returns.std() * (252 ** 0.5))
        base = closes.iloc[-251] if len(closes) > 250 else closes.iloc[0]
        if base:
            features["momentum_250"] = float(closes.iloc[-1] / base - 1.0)
        features["bh_max_drawdown"] = float((closes / closes.cummax() - 1.0).min())

    amounts = pd.to_numeric(feat_bars["amount"], errors="coerce").dropna()
    if not amounts.empty:
        features["amount_ma20"] = float(amounts.tail(20).mean())

    if db is not None:
        try:
            trend = db.load_trend_daily(symbol, param_set="default")
            if not trend.empty:
                # time 可能是 'YYYY-MM-DD' 或 'YYYY-MM-DD 00:00:00'，统一按前 10 位比较
                anchor_text = (anchor.isoformat() if anchor else "9999-12-31")[:10]
                trend = trend[trend["time"].astype(str).str[:10] <= anchor_text]
                scores = pd.to_numeric(trend["trend_score"], errors="coerce").dropna().tail(250)
                if not scores.empty:
                    features["trend_score_avg"] = float(scores.mean())
        except Exception as exc:  # 特征缺失不应拖垮格子 —— 记日志留 None
            logger.warning("trend feature unavailable for %s: %s", symbol, exc)
    return features


def extract_cell(result: dict, monthly_nav: list[dict]) -> dict:
    """从引擎完整结果中提取格子字段：指标平铺 + 服务层派生超额 + 分层 blob。

    只保留方案 §3.1 约定的分层字段；daily_nav / charts / condition_trace /
    debug_log 等大字段在此被丢弃（调用方随后 del result）。
    """
    summary = result.get("summary") or {}
    bench = result.get("benchmark_summary") or {}
    annual = summary.get("annual_return")
    bench_annual = bench.get("annual_return")
    excess = (
        float(annual) - float(bench_annual)
        if annual is not None and bench_annual is not None
        else None
    )
    return {
        "status": "ok",
        "start_date": result.get("start_date"),
        "end_date": result.get("end_date"),
        "total_return": summary.get("total_return"),
        "annual_return": annual,
        "max_drawdown": summary.get("max_drawdown"),
        "sharpe": summary.get("sharpe"),
        "sortino": summary.get("sortino"),
        "calmar": summary.get("calmar"),
        "win_rate": summary.get("win_rate"),
        "profit_factor": summary.get("profit_factor"),
        "trade_count": summary.get("trade_count"),
        "avg_holding_days": summary.get("avg_holding_days"),
        "final_equity": result.get("final_equity"),
        "benchmark_total_return": bench.get("total_return"),
        "benchmark_annual_return": bench_annual,
        "excess_annual_return": excess,
        "annual_returns_json": json.dumps(result.get("annual_returns") or [], ensure_ascii=False),
        "monthly_heatmap_json": json.dumps(result.get("monthly_heatmap") or {}, ensure_ascii=False),
        "trades_json": json.dumps(result.get("trades") or [], ensure_ascii=False),
        "skipped_buys_json": json.dumps(result.get("skipped_buys") or [], ensure_ascii=False),
        "monthly_nav_json": json.dumps(monthly_nav, ensure_ascii=False),
    }


def monthly_sampled_nav(daily_nav: list[dict]) -> list[dict]:
    """月度采样 NAV：每个自然月最后一个交易日的净值（方案 §5.1 monthly_nav_json）。"""
    by_month: dict[str, dict] = {}
    for row in daily_nav:
        month = str(row.get("date", ""))[:7]
        if month:
            by_month[month] = {"month": month, "equity": row.get("equity")}
    return list(by_month.values())


class BatchBacktestService:
    def __init__(
        self,
        db: Database | None = None,
        market_store: MarketStore | None = None,
        engine: SingleSymbolAllInBacktestEngine | None = None,
    ) -> None:
        # Lazy attribute lookup: API tests monkeypatch db_module.get_db, and a
        # top-level `from data.storage.db import get_db` binding could capture
        # a stale test double at import time.
        self.db = db or db_module.get_db()
        self.market_store = market_store or MarketStore(db=self.db)
        self.engine = engine or SingleSymbolAllInBacktestEngine()

    # ------------------------------------------------------------------
    # batch preparation (called by the router before starting the thread)
    # ------------------------------------------------------------------
    def prepare_batch(
        self,
        categories: list[str],
        strategy_ids: list[str],
        name: str = "",
        strategy_loader: StrategyLoader | None = None,
    ) -> dict:
        """Build the batch row payload: snapshot strategies, resolve symbols,
        anchor the data cutoff. Raises ValueError on invalid input."""
        snapshot = build_strategy_snapshot(strategy_ids, loader=strategy_loader)
        symbols = resolve_batch_symbols(self.db, categories)
        if not symbols:
            raise ValueError("所选类目下没有可用标的")
        anchor = self.db.get_market_data_anchor()
        batch_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
        config = {
            "initial_capital": 100_000.0,
            "slippage": 0.002,
            "fee_rate": DEFAULT_FEE_RATE,
            "fee_min": 5.0,
            "lot_size": 100,
            "stock_stamp_tax_rate": 0.001,
            "min_bars": MIN_BARS,
            "estimated_seconds": round(estimate_batch_seconds(symbols, len(snapshot))),
        }
        return {
            "batch_id": batch_id,
            "name": name.strip() or default_batch_name(categories, len(snapshot)),
            "categories_json": json.dumps(categories, ensure_ascii=False),
            "strategy_snapshot_json": json.dumps(snapshot, ensure_ascii=False),
            "config_json": json.dumps(config),
            "total_cells": len(symbols) * len(snapshot),
            "data_anchor_date": anchor.get("anchor_date"),
            "data_version": anchor.get("data_version"),
            "engine_version": ENGINE_VERSION,
        }

    # ------------------------------------------------------------------
    # execution (background thread)
    # ------------------------------------------------------------------
    def run_batch(self, batch_id: str, cancel_event: threading.Event | None = None) -> None:
        cancel_event = cancel_event or threading.Event()
        batch = self.db.get_batch_run(batch_id)
        if batch is None:
            raise ValueError(f"batch not found: {batch_id}")

        categories = json.loads(batch["categories_json"])
        snapshot = json.loads(batch["strategy_snapshot_json"])
        config = json.loads(batch["config_json"])
        # time 列存的是 'YYYY-MM-DD 00:00:00'，date.fromisoformat 会报错。
        anchor = pd.Timestamp(str(batch["data_anchor_date"])).date()
        symbols = resolve_batch_symbols(self.db, categories)

        counts = {"done": 0, "ok": 0, "failed": 0, "skipped": 0}
        started_at = datetime.now()
        logger.info(
            "Batch backtest started batch_id=%s symbols=%d strategies=%d anchor=%s",
            batch_id, len(symbols), len(snapshot), anchor,
        )

        try:
            for item in symbols:
                if cancel_event.is_set():
                    break
                symbol = item["symbol"]
                self.db.update_batch_run(batch_id, current_symbol=symbol)
                bars = self.market_store.load_history(symbol)
                if not bars.empty:
                    feat_bars = bars[bars["time"] <= pd.Timestamp(anchor)]
                else:
                    feat_bars = bars
                bar_count = len(feat_bars)

                if bar_count < MIN_BARS:
                    reason = "无行情数据" if bar_count == 0 else f"数据不足（{bar_count} < {MIN_BARS} 根）"
                    for s in snapshot:
                        self._write_cell(batch_id, item, s, {"status": "skipped", "error": reason, "bar_count": bar_count})
                        counts["done"] += 1
                        counts["skipped"] += 1
                    self._flush_counts(batch_id, counts)
                    continue

                if cancel_event.is_set():
                    break
                features = compute_features(feat_bars, symbol, db=self.db, anchor=anchor)
                self.db.insert_batch_symbol_features(batch_id, symbol, features)

                execution = BacktestExecutionConfig(
                    initial_capital=float(config.get("initial_capital", 100_000.0)),
                    fee_rate=float(config.get("fee_rate", DEFAULT_FEE_RATE)),
                    fee_min=float(config.get("fee_min", 5.0)),
                    slippage=float(config.get("slippage", 0.002)),
                    lot_size=int(config.get("lot_size", 100)),
                    instrument_type="stock" if item["asset_type"] == "stock" else "etf",
                    stock_stamp_tax_rate=float(config.get("stock_stamp_tax_rate", 0.001)),
                )

                for s in snapshot:
                    try:
                        result = self.engine.run(
                            RuleBacktestRequest(
                                strategy=s["strategy_config"],
                                symbol=symbol,
                                bars=bars,
                                start_date=None,
                                end_date=anchor,
                                execution=execution,
                                run_id=f"{batch_id}-{symbol}-{s['id']}",
                                sizer=None,
                            )
                        )
                        cell = extract_cell(result, monthly_sampled_nav(result.get("daily_nav") or []))
                        cell["bar_count"] = bar_count
                        counts["ok"] += 1
                        del result  # 大字段（daily_nav/charts/condition_trace）到此释放
                    except Exception as exc:  # 单格失败不中断批次
                        logger.warning("batch cell failed %s %s %s: %s", batch_id, symbol, s["id"], exc)
                        cell = {"status": "failed", "error": str(exc), "bar_count": bar_count}
                        counts["failed"] += 1
                    self._write_cell(batch_id, item, s, cell)
                    counts["done"] += 1
                    self._flush_counts(batch_id, counts)
                    if cancel_event.is_set():
                        break

            cancelled = cancel_event.is_set()
            self.db.update_batch_run(
                batch_id,
                status="cancelled" if cancelled else "completed",
                current_symbol=None,
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
            logger.info(
                "Batch backtest %s batch_id=%s ok=%d failed=%d skipped=%d elapsed=%.1fs",
                "cancelled" if cancelled else "completed",
                batch_id, counts["ok"], counts["failed"], counts["skipped"],
                (datetime.now() - started_at).total_seconds(),
            )
        except Exception as exc:
            logger.exception("Batch backtest failed batch_id=%s", batch_id)
            self.db.update_batch_run(
                batch_id,
                status="error",
                error=str(exc),
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )

    # ------------------------------------------------------------------
    def _write_cell(self, batch_id: str, item: dict, snapshot_entry: dict, cell: dict) -> None:
        self.db.insert_batch_cell(
            {
                "batch_id": batch_id,
                "symbol": item["symbol"],
                "strategy_id": snapshot_entry["id"],
                "symbol_name": item.get("name", ""),
                "strategy_name": snapshot_entry.get("name", ""),
                "category_l1": item.get("category_l1"),
                "category_l2": item.get("category_l2"),
                "category_l3": item.get("category_l3"),
                "asset_type": item.get("asset_type"),
                **cell,
            }
        )

    def _flush_counts(self, batch_id: str, counts: dict) -> None:
        self.db.update_batch_run(
            batch_id,
            done_cells=counts["done"],
            ok_cells=counts["ok"],
            failed_cells=counts["failed"],
            skipped_cells=counts["skipped"],
        )
