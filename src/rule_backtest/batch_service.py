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
import math
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


def default_batch_name(
    categories: list[str],
    strategy_count: int,
    start_date: date | None = None,
    end_date: date | None = None,
) -> str:
    cats = "+".join(categories[:3]) + ("..." if len(categories) > 3 else "")
    base = f"{cats}×{strategy_count}策略"
    if start_date is not None or end_date is not None:
        window = f"{start_date.isoformat() if start_date else '上市'}~{end_date.isoformat() if end_date else '最新'}"
        return f"{base}-{window}"
    return f"{base}-{date.today().isoformat()}"


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


def _partial_window_flag(bars: pd.DataFrame, start: date | None, end: date) -> int:
    """1 = 标的数据未覆盖完整回测窗口（上市晚于窗口起点，或行情止于窗口终点前）。

    跨窗口对比时这类格子的年化口径噪声更大，前端可据此过滤。用全量 bars 的
    首末交易日判断（而非窗口内首末 bar），避免窗口终点落在非交易日时误报。
    """
    if bars.empty:
        return 0
    starts_late = start is not None and bars["time"].min() > pd.Timestamp(start)
    ends_early = bars["time"].max() < pd.Timestamp(end)
    return int(bool(starts_late or ends_early))


def _median(values: list) -> float | None:
    vals = sorted(
        float(v) for v in values
        if isinstance(v, (int, float)) and v is not None and math.isfinite(v)
    )
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


def aggregate_annual_returns(rows: list[dict]) -> list[dict]:
    """策略×年份聚合（输入为 ok 格子的 annual_returns blob 行）。

    每（策略, 年份）把各标的的年度指标取中位数 + 样本量 n。超额 = 策略年收益
    − 基准年收益，逐格子计算后再取中位数（不是中位数之差）。输出按策略、年份
    排序，供前端热力图直接消费。
    """
    by_key: dict[tuple[str, int], list[dict]] = {}
    for row in rows:
        strategy = str(row.get("strategy_name") or row.get("strategy_id") or "")
        try:
            annual = json.loads(row.get("annual_returns_json") or "[]")
        except (ValueError, TypeError):
            continue
        for entry in annual or []:
            year = entry.get("year")
            if year is None:
                continue
            ret = entry.get("return")
            bench = entry.get("benchmark_return")
            excess = (
                float(ret) - float(bench)
                if ret is not None and bench is not None
                else None
            )
            by_key.setdefault((strategy, int(year)), []).append(
                {
                    "return": ret,
                    "benchmark_return": bench,
                    "excess": excess,
                    "win_rate": entry.get("win_rate"),
                    "sharpe": entry.get("sharpe"),
                    "max_drawdown": entry.get("max_drawdown"),
                    "trade_count": int(entry.get("trade_count") or 0),
                }
            )
    out: list[dict] = []
    for (strategy, year), recs in sorted(by_key.items()):
        out.append(
            {
                "strategy": strategy,
                "year": year,
                "n": len(recs),
                "median_return": _median([r["return"] for r in recs]),
                "median_benchmark": _median([r["benchmark_return"] for r in recs]),
                "median_excess": _median([r["excess"] for r in recs]),
                "median_win_rate": _median([r["win_rate"] for r in recs]),
                "median_sharpe": _median([r["sharpe"] for r in recs]),
                "median_max_drawdown": _median([r["max_drawdown"] for r in recs]),
                "trade_count": sum(r["trade_count"] for r in recs),
            }
        )
    return out


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
        start_date: date | None = None,
        end_date: date | None = None,
        strategy_loader: StrategyLoader | None = None,
    ) -> dict:
        """Build the batch row payload: snapshot strategies, resolve symbols,
        anchor the data cutoff. Raises ValueError on invalid input.

        start_date/end_date 限定回测窗口（可选）：缺省为全生命周期（上市 ~ 锚定日）。
        end_date 超过锚定日时被截到锚定日；引擎对窗口内信号用全历史做指标
        warmup（resolver 基于 all_bars），窗口起点无冷启动问题。
        """
        snapshot = build_strategy_snapshot(strategy_ids, loader=strategy_loader)
        symbols = resolve_batch_symbols(self.db, categories)
        if not symbols:
            raise ValueError("所选类目下没有可用标的")
        anchor = self.db.get_market_data_anchor()
        anchor_day = (
            pd.Timestamp(str(anchor["anchor_date"])).date() if anchor.get("anchor_date") else None
        )
        window_end = end_date or anchor_day
        if window_end is None:
            raise ValueError("行情库为空，无法回测")
        if anchor_day is not None and window_end > anchor_day:
            window_end = anchor_day
        if start_date is not None and start_date > window_end:
            raise ValueError("回测开始日期不能晚于结束日期")

        windowed = start_date is not None or end_date is not None
        # ETA 按窗口内 bar 数估算（窗口批次通常比全周期便宜得多）。
        if windowed:
            window_counts = self.db.count_bars_by_symbol(start=start_date, end=window_end)
            eta_symbols = [{**s, "bar_count": window_counts.get(s["symbol"], 0)} for s in symbols]
        else:
            eta_symbols = symbols

        batch_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
        config = {
            "initial_capital": 100_000.0,
            "slippage": 0.002,
            "fee_rate": DEFAULT_FEE_RATE,
            "fee_min": 5.0,
            "lot_size": 100,
            "stock_stamp_tax_rate": 0.001,
            "min_bars": MIN_BARS,
            "start_date": start_date.isoformat() if start_date else None,
            # 用户请求的原始区间（end 可能为 None = 最新）；run_batch 再解析成
            # 实际 window_end（= end_date or 锚定日）。展示/重跑预填用原始值。
            "end_date": end_date.isoformat() if end_date else None,
            "estimated_seconds": round(estimate_batch_seconds(eta_symbols, len(snapshot))),
        }
        return {
            "batch_id": batch_id,
            "name": name.strip() or default_batch_name(categories, len(snapshot), start_date, end_date),
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
        # 回测窗口（旧批次 config 无此字段 → 全生命周期，行为与之前一致）。
        # end 存的是用户请求值，可能超过锚定日 —— 截到锚定日，与 prepare_batch 一致。
        window_start = date.fromisoformat(config["start_date"]) if config.get("start_date") else None
        window_end_cfg = date.fromisoformat(config["end_date"]) if config.get("end_date") else None
        window_end = min(window_end_cfg, anchor) if window_end_cfg else anchor
        symbols = resolve_batch_symbols(self.db, categories)

        counts = {"done": 0, "ok": 0, "failed": 0, "skipped": 0}
        started_at = datetime.now()
        logger.info(
            "Batch backtest started batch_id=%s symbols=%d strategies=%d anchor=%s window=%s~%s",
            batch_id, len(symbols), len(snapshot), anchor, window_start, window_end,
        )

        try:
            for item in symbols:
                if cancel_event.is_set():
                    break
                symbol = item["symbol"]
                self.db.update_batch_run(batch_id, current_symbol=symbol)
                bars = self.market_store.load_history(symbol)
                if not bars.empty:
                    end_ts = pd.Timestamp(window_end)
                    window_mask = bars["time"] <= end_ts
                    if window_start is not None:
                        window_mask &= bars["time"] >= pd.Timestamp(window_start)
                    window_bars = bars[window_mask]
                    feat_bars = bars[bars["time"] <= end_ts]
                else:
                    window_bars = feat_bars = bars
                bar_count = len(window_bars)

                if bar_count < MIN_BARS:
                    if window_start is not None:
                        reason = "窗口内无行情数据" if bar_count == 0 else f"窗口内数据不足（{bar_count} < {MIN_BARS} 根）"
                    else:
                        reason = "无行情数据" if bar_count == 0 else f"数据不足（{bar_count} < {MIN_BARS} 根）"
                    for s in snapshot:
                        self._write_cell(batch_id, item, s, {"status": "skipped", "error": reason, "bar_count": bar_count})
                        counts["done"] += 1
                        counts["skipped"] += 1
                    self._flush_counts(batch_id, counts)
                    continue

                if cancel_event.is_set():
                    break
                features = compute_features(feat_bars, symbol, db=self.db, anchor=window_end)
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
                                start_date=window_start,
                                end_date=window_end,
                                execution=execution,
                                run_id=f"{batch_id}-{symbol}-{s['id']}",
                                sizer=None,
                            )
                        )
                        cell = extract_cell(result, monthly_sampled_nav(result.get("daily_nav") or []))
                        cell["bar_count"] = bar_count
                        cell["partial_window"] = _partial_window_flag(bars, window_start, window_end)
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
