"""批量回测执行服务（方案 docs/26-07-26-batch-backtest/2026-07-26-batch-backtest-plan.md §5.2）。

按「标的 × 策略」格子串行执行规则回测：每标的加载一次行情（多策略复用），
每格独立 try/except（失败 continue），逐格写库（per-cell commit），
批次启动时锚定数据截止日（data_anchor_date）保证批内数据一致。

钻取一致性：格子记录实际回测起止日期 + 批次快照策略 JSON，
前端钻取用快照 + 区间重跑即可复现（漂移仅剩历史数据被改写的情形）。

止损宽度诊断（2026-08-30 方案 §5）：stop_profile ∈ {default, tight, loose, sweep}
在 prepare_batch 冻结快照时覆写 exit spec 的 atr_mul（档位数字与实盘
stop_loss.py 同一把尺），批次行记 stop_profile + atr_basis=prev_close。
"""

from __future__ import annotations

import copy
import json
import math
import threading
from datetime import date, datetime
from typing import Any

import pandas as pd

from audit.app_logger import get_logger
from core.calendar import market_now
from core.strategy_config import get_strategy_config
from data.storage import db as db_module
from data.storage.db import Database
from data.storage.market_store import MarketStore
from rule_backtest.engine import SingleSymbolAllInBacktestEngine
from rule_backtest.loader import StrategyLoader
from rule_backtest.metrics import compute_roundtrip_stats
from rule_backtest.models import DEFAULT_FEE_RATE, BacktestExecutionConfig, RuleBacktestRequest

logger = get_logger(__name__)

# 低于该 K 线数的标的整标的记 skipped（指标 warmup 都不够）。
MIN_BARS = 60

# MVP 固定值（方案 §3.1）：后续接入 git hash / formula_version。
ENGINE_VERSION = "1.0"

# seed=None 时用 OS 熵源，格子结果不可复现 —— 批次拒绝含这些指标的策略。
RANDOM_INDICATORS = frozenset({"random_uniform"})

# 耗时预估（2026-07-26 分层实测，秒/格，按标的 bar 数分档）。
_ETA_TIERS: tuple[tuple[int, float], ...] = ((500, 0.1), (2000, 0.4), (5000, 0.9))
_ETA_DEFAULT = 1.8

# 止损档位定义（方案 §5.1）：tight/loose 直接复用实盘口径（stop_loss.py:185-199）。
STOP_PROFILE_TIGHT = {"hard": 1.0, "chandelier": 2.0}
# sweep 时吊灯倍数固定 = hard × 2（贴近实盘 tight 比例；方案 §10 开放问题 2 的拍板值，
# 导出 manifest 记录该约定）。
SWEEP_CHANDELIER_RATIO = 2.0
DEFAULT_SWEEP_ATR_MULS = [0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0]
STOP_PROFILES = ("default", "tight", "loose", "sweep")

# 2026-08-30 起引擎入场 ATR 为 T-1 收盘口径（方案 §7.3）。
ATR_BASIS = "prev_close"


def _exit_state_value_specs(strategy: dict):
    """遍历 exit 条件树中的 state_value spec（原地修改用）。"""
    exit_group = strategy.get("exit", {}) if isinstance(strategy.get("exit", {}), dict) else {}
    for condition in exit_group.get("children", []) or []:
        if not isinstance(condition, dict):
            continue
        for side in ("left", "right"):
            spec = condition.get(side, {})
            if isinstance(spec, dict) and spec.get("type") == "state_value":
                yield spec


def override_stop_atr_muls(
    strategy: dict,
    *,
    hard_mul: float | None = None,
    chandelier_mul: float | None = None,
) -> dict:
    """深拷贝策略并覆写 exit spec 中止损状态值的 atr_mul（不改动原对象）。"""
    out = copy.deepcopy(strategy)
    for spec in _exit_state_value_specs(out):
        name = str(spec.get("name", ""))
        params = spec.setdefault("params", {})
        if not isinstance(params, dict):
            continue
        if name == "hard_stop" and hard_mul is not None:
            params["atr_mul"] = float(hard_mul)
        elif name in ("chandelier_stop", "chandelier_stop_ratchet") and chandelier_mul is not None:
            params["atr_mul"] = float(chandelier_mul)
    return out


def apply_stop_profile(snapshot: list[dict], profile: str, sweep_atr_muls: list[float] | None) -> list[dict]:
    """按止损档位变换策略快照（方案 §5.1）。

    - default：原样返回；
    - tight：hard=1.0 / chandelier=2.0（忽略标的级覆盖，与实盘 tight 一致）；
    - loose：hard=配置默认（1.5，标的级 stop_atr_mul 覆盖在 run_batch 逐格子应用）、
      chandelier=配置默认（2.5）；
    - sweep：每个策略 × sweep_atr_muls 每值一份快照，chandelier = hard × 2，
      id 加 @hs<mul> 后缀（格子数 ×N）。
    """
    if profile == "default":
        return snapshot
    if profile == "tight":
        return [
            {
                **entry,
                "strategy_config": override_stop_atr_muls(
                    entry["strategy_config"],
                    hard_mul=STOP_PROFILE_TIGHT["hard"],
                    chandelier_mul=STOP_PROFILE_TIGHT["chandelier"],
                ),
            }
            for entry in snapshot
        ]
    if profile == "loose":
        cfg = get_strategy_config()
        return [
            {
                **entry,
                "strategy_config": override_stop_atr_muls(
                    entry["strategy_config"],
                    hard_mul=float(cfg.get("hard_stop_atr_mul_default", 1.5)),
                    chandelier_mul=float(cfg.get("chandelier_stop_atr_mul", 2.5)),
                ),
            }
            for entry in snapshot
        ]
    if profile == "sweep":
        muls = [float(m) for m in (sweep_atr_muls or DEFAULT_SWEEP_ATR_MULS)]
        out: list[dict] = []
        for entry in snapshot:
            for mul in muls:
                out.append(
                    {
                        "id": f"{entry['id']}@hs{mul:g}",
                        "name": f"{entry.get('name', entry['id'])} [hs{mul:g}]",
                        "strategy_config": override_stop_atr_muls(
                            entry["strategy_config"],
                            hard_mul=mul,
                            chandelier_mul=mul * SWEEP_CHANDELIER_RATIO,
                        ),
                    }
                )
        return out
    raise ValueError(f"未知的止损档位: {profile}")


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
                # 标的级硬止损倍数覆盖（loose 档逐格子应用，与实盘口径一致）。
                "stop_atr_mul": item.get("stop_atr_mul"),
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
    return f"{base}-{market_now().date().isoformat()}"


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
    sharpe = summary.get("sharpe")
    bench_sharpe = bench.get("sharpe")
    excess_sharpe = (
        float(sharpe) - float(bench_sharpe)
        if sharpe is not None and bench_sharpe is not None
        else None
    )
    calmar = summary.get("calmar")
    bench_calmar = bench.get("calmar")
    excess_calmar = (
        float(calmar) - float(bench_calmar)
        if calmar is not None and bench_calmar is not None
        else None
    )
    rt_stats = compute_roundtrip_stats(result.get("round_trips") or [], result.get("daily_nav") or [])
    return {
        "status": "ok",
        "start_date": result.get("start_date"),
        "end_date": result.get("end_date"),
        "total_return": summary.get("total_return"),
        "annual_return": annual,
        "max_drawdown": summary.get("max_drawdown"),
        "sharpe": sharpe,
        "sortino": summary.get("sortino"),
        "calmar": calmar,
        "win_rate": summary.get("win_rate"),
        "profit_factor": summary.get("profit_factor"),
        "trade_count": summary.get("trade_count"),
        "avg_holding_days": summary.get("avg_holding_days"),
        "avg_flat_days": summary.get("avg_flat_days"),
        "final_equity": result.get("final_equity"),
        "benchmark_total_return": bench.get("total_return"),
        "benchmark_annual_return": bench_annual,
        "benchmark_sharpe": bench_sharpe,
        "benchmark_calmar": bench_calmar,
        "excess_annual_return": excess,
        "excess_sharpe": excess_sharpe,
        "excess_calmar": excess_calmar,
        **rt_stats,
        "annual_returns_json": json.dumps(result.get("annual_returns") or [], ensure_ascii=False),
        "monthly_heatmap_json": json.dumps(result.get("monthly_heatmap") or {}, ensure_ascii=False),
        "trades_json": json.dumps(result.get("trades") or [], ensure_ascii=False),
        "skipped_buys_json": json.dumps(result.get("skipped_buys") or [], ensure_ascii=False),
        "monthly_nav_json": json.dumps(monthly_nav, ensure_ascii=False),
        "round_trips_json": json.dumps(result.get("round_trips") or [], ensure_ascii=False),
        "round_trips_source": "engine",
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


def _mean(values: list[float]) -> float | None:
    vals = [float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(v)]
    return float(sum(vals) / len(vals)) if vals else None


# ----------------------------------------------------------------------
# 止损专项诊断聚合（方案 §4/§6.1）：全部从 round-trip 字段直接聚合
# ----------------------------------------------------------------------
LOW_CONFIDENCE_N = 30

# 分桶维度：（维度名, 取值函数）；波动率/趋势的分桶边界在批次内按分位数现算。
_STOP_DIAG_DIMS = ("vol_quintile", "asset_type", "trend_regime", "exit_year")


def _bucket_edges(values: list[float], parts: int) -> list[float]:
    """批次内分位数边界（len = parts-1）；样本不足返回空列表（整批一桶）。"""
    vals = sorted(float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(v))
    if len(vals) < parts * 2:
        return []
    import numpy as np

    return [float(np.percentile(np.asarray(vals), 100.0 * i / parts)) for i in range(1, parts)]


def _bucket_of(value: float | None, edges: list[float], prefix: str) -> str:
    if value is None:
        return "unknown"
    for i, edge in enumerate(edges):
        if value <= edge:
            return f"{prefix}{i + 1}"
    return f"{prefix}{len(edges) + 1}"


def _aggregate_trip_group(trips: list[dict]) -> dict:
    """一组 round-trips 的聚合指标（§4 五件诊断 + §3.2 分布量）。"""
    n = len(trips)
    r_vals = [float(t["r_multiple"]) for t in trips if isinstance(t.get("r_multiple"), (int, float))]
    pnls = [float(t.get("pnl") or 0.0) for t in trips]
    wins = [p for p in pnls if p > 0]
    gains = sum(wins)
    losses = abs(sum(p for p in pnls if p < 0))
    stops = [t for t in trips if t.get("exit_reason") == "hard_stop"]

    out: dict[str, Any] = {
        "n": n,
        "r_mean": _mean(r_vals),
        "r_p25": _percentile25(r_vals),
        "r_p75": _percentile75(r_vals),
        "win_rate": float(len(wins) / n) if n else None,
        "profit_factor": (gains / losses) if losses > 0 else (999.0 if gains > 0 else None),
        "avg_exit_efficiency": _mean(
            [
                max(float(t.get("pnl") or 0.0), 0.0)
                / (float(t["mfe_pct"]) / 100.0 * float(t.get("entry_price") or 0.0) * int(t.get("qty") or 0))
                for t in trips
                if isinstance(t.get("mfe_pct"), (int, float))
                and float(t.get("mfe_pct") or 0.0) > 0
                and float(t.get("entry_price") or 0.0) > 0
                and int(t.get("qty") or 0) > 0
            ]
        ),
        "stop_exit_ratio": float(len(stops) / n) if n else None,
        "trigger_within_3d_ratio": (
            float(sum(1 for t in stops if isinstance(t.get("days_to_trigger"), int) and t["days_to_trigger"] <= 3) / len(stops))
            if stops
            else None
        ),
        # 吊灯回吐（§4）：吊灯出场的 MFE 中位数 vs 实际兑现 R 中位数
        "chandelier_mfe_atr_median": _median(
            [
                t.get("mfe_atr")
                for t in trips
                if t.get("exit_reason") in ("chandelier_stop", "chandelier_stop_ratchet")
            ]
        ),
        "chandelier_r_median": _median(
            [
                t.get("r_multiple")
                for t in trips
                if t.get("exit_reason") in ("chandelier_stop", "chandelier_stop_ratchet")
            ]
        ),
        "low_confidence": n < LOW_CONFIDENCE_N,
    }
    for w in (5, 10, 20):
        rets = [t.get(f"post_exit_ret_{w}d") for t in stops]
        reentries = [t.get(f"reentry_above_entry_{w}d") for t in stops]
        known = [r for r in reentries if r is not None]
        out[f"false_stop_rate_{w}d"] = (
            float(sum(1 for r in known if r) / len(known)) if known else None
        )
        out[f"post_exit_drift_{w}d_mean"] = _mean([r for r in rets if r is not None])
        out[f"post_exit_drift_{w}d_median"] = _median([r for r in rets if r is not None])
    return out


def _percentile25(values: list[float]) -> float | None:
    return _percentile_from(sorted(values), 25)


def _percentile75(values: list[float]) -> float | None:
    return _percentile_from(sorted(values), 75)


def _percentile_from(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    import numpy as np

    return float(np.percentile(np.asarray(vals, dtype=float), q))


def aggregate_stop_diagnostics(rows: list[dict]) -> list[dict]:
    """批次级止损诊断聚合（方案 §3.2/§6.1）。

    输入：ok 格子的 round_trips_json + 标的特征行（get_batch_roundtrip_rows）。
    输出：每（strategy, dim, bucket）一行 long-format 诊断，单元格含
    r_mean / 假止损率 / post-exit 漂移 / n / low_confidence。
    """
    # 展平全部 round-trips，带上格子级语境
    flat: list[dict] = []
    for row in rows:
        try:
            trips = json.loads(row.get("round_trips_json") or "[]")
        except (ValueError, TypeError):
            continue
        for t in trips or []:
            if not isinstance(t, dict):
                continue
            flat.append(
                {
                    **t,
                    "strategy": str(row.get("strategy_name") or row.get("strategy_id") or ""),
                    "cell_asset_type": str(row.get("asset_type") or t.get("asset_type") or ""),
                    "trend_score_avg": row.get("trend_score_avg"),
                }
            )
    if not flat:
        return []

    vol_edges = _bucket_edges(
        [t.get("entry_atr_pct") for t in flat if isinstance(t.get("entry_atr_pct"), (int, float))],
        5,
    )
    trend_edges = _bucket_edges(
        [t.get("trend_score_avg") for t in flat if isinstance(t.get("trend_score_avg"), (int, float))],
        3,
    )

    def dim_value(trip: dict, dim: str) -> str:
        if dim == "vol_quintile":
            v = trip.get("entry_atr_pct")
            return _bucket_of(float(v) if isinstance(v, (int, float)) else None, vol_edges, "q")
        if dim == "asset_type":
            return trip.get("cell_asset_type") or "unknown"
        if dim == "trend_regime":
            v = trip.get("trend_score_avg")
            return _bucket_of(float(v) if isinstance(v, (int, float)) else None, trend_edges, "t")
        if dim == "exit_year":
            text = str(trip.get("exit_date") or "")[:4]
            return text if text.isdigit() else "unknown"
        return "unknown"

    groups: dict[tuple[str, str, str], list[dict]] = {}
    for t in flat:
        for dim in _STOP_DIAG_DIMS:
            groups.setdefault((t["strategy"], dim, dim_value(t, dim)), []).append(t)

    out: list[dict] = []
    for (strategy, dim, bucket), trips in sorted(groups.items()):
        out.append({"strategy": strategy, "dim": dim, "bucket": bucket, **_aggregate_trip_group(trips)})
    return out


# ----------------------------------------------------------------------
# 批次对比（方案 §6.3）：逐格差值 + 诊断并排 + 逐标的配对 bootstrap CI
# ----------------------------------------------------------------------
_COMPARE_METRICS = (
    "annual_return",
    "sharpe",
    "calmar",
    "win_rate",
    "profit_factor",
    "r_mean",
    "stop_exit_ratio",
    "exit_efficiency",
)


def bootstrap_mean_diff_ci(
    pairs: list[tuple[float, float]],
    resamples: int = 1000,
    seed: int = 42,
) -> dict | None:
    """逐标的配对的 bootstrap 95% 置信区间（方案 §6.2；固定种子可复现）。

    pairs = [(base, alt), ...]；返回 alt − base 均值的点估计与 CI。
    """
    import random

    diffs = [float(a) - float(b) for b, a in pairs]
    if len(diffs) < 2:
        return None
    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(
        sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples)
    )
    return {
        "n_pairs": n,
        "mean_diff": float(sum(diffs) / n),
        "ci95_low": float(means[int(0.025 * resamples)]),
        "ci95_high": float(means[int(0.975 * resamples)]),
        "resamples": int(resamples),
    }


def compare_batches(db: Database, base_batch_id: str, alt_batch_id: str) -> dict:
    """紧/松（或任意两批次）并排对比。两批次须同标的池同策略 —— 取
    symbol×strategy 交集，交集为空或任一批次仍在运行由路由层转 409。"""
    base_cells = db.get_batch_cells(base_batch_id)
    alt_cells = db.get_batch_cells(alt_batch_id)
    base_map = {
        (c["symbol"], c["strategy_id"]): c for c in base_cells if c.get("status") == "ok"
    }
    alt_map = {
        (c["symbol"], c["strategy_id"]): c for c in alt_cells if c.get("status") == "ok"
    }
    common = sorted(set(base_map) & set(alt_map))

    cell_diffs: list[dict] = []
    for key in common:
        b, a = base_map[key], alt_map[key]
        row: dict[str, Any] = {
            "symbol": key[0],
            "strategy_id": key[1],
            "symbol_name": b.get("symbol_name"),
            "strategy_name": b.get("strategy_name"),
        }
        for metric in _COMPARE_METRICS:
            bv, av = b.get(metric), a.get(metric)
            row[f"base_{metric}"] = bv
            row[f"alt_{metric}"] = av
            row[f"delta_{metric}"] = (
                float(av) - float(bv)
                if isinstance(av, (int, float)) and isinstance(bv, (int, float))
                else None
            )
        cell_diffs.append(row)

    # 逐（策略）配对 bootstrap：r_mean 差（alt − base）的 95% CI
    ci_rows: list[dict] = []
    strategy_of = {
        k: (base_map[k].get("strategy_name") or base_map[k].get("strategy_id")) for k in common
    }
    for strategy in sorted(set(strategy_of.values())):
        pairs = [
            (base_map[k]["r_mean"], alt_map[k]["r_mean"])
            for k in common
            if strategy_of[k] == strategy
            and isinstance(base_map[k].get("r_mean"), (int, float))
            and isinstance(alt_map[k].get("r_mean"), (int, float))
        ]
        ci = bootstrap_mean_diff_ci(pairs)
        if ci is not None:
            ci_rows.append({"strategy": strategy, "metric": "r_mean", **ci})

    return {
        "base_batch_id": base_batch_id,
        "alt_batch_id": alt_batch_id,
        "common_cells": len(common),
        "base_only_cells": len(set(base_map) - set(alt_map)),
        "alt_only_cells": len(set(alt_map) - set(base_map)),
        "cell_diffs": cell_diffs,
        "bootstrap_ci": ci_rows,
        "base_diagnostics": aggregate_stop_diagnostics(db.get_batch_roundtrip_rows(base_batch_id)),
        "alt_diagnostics": aggregate_stop_diagnostics(db.get_batch_roundtrip_rows(alt_batch_id)),
    }


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
        stop_profile: str = "default",
        sweep_atr_muls: list[float] | None = None,
    ) -> dict:
        """Build the batch row payload: snapshot strategies, resolve symbols,
        anchor the data cutoff. Raises ValueError on invalid input.

        start_date/end_date 限定回测窗口（可选）：缺省为全生命周期（上市 ~ 锚定日）。
        end_date 超过锚定日时被截到锚定日；引擎对窗口内信号用全历史做指标
        warmup（resolver 基于 all_bars），窗口起点无冷启动问题。

        stop_profile（方案 §5.1）：tight/loose 覆写快照中止损 atr_mul（与实盘同口径），
        sweep 按 sweep_atr_muls 展开快照（格子数 ×N）。
        """
        if stop_profile not in STOP_PROFILES:
            raise ValueError(f"未知的止损档位: {stop_profile}（可选：{'/'.join(STOP_PROFILES)}）")
        snapshot = build_strategy_snapshot(strategy_ids, loader=strategy_loader)
        snapshot = apply_stop_profile(snapshot, stop_profile, sweep_atr_muls)
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
            # 止损跳空成交修正（方案 §7.1，默认开；执行配置已参数化，成本压力
            # 测试走 sweep 脚本 --cost-multiplier）。
            "stop_gap_fill": True,
            "start_date": start_date.isoformat() if start_date else None,
            # 用户请求的原始区间（end 可能为 None = 最新）；run_batch 再解析成
            # 实际 window_end（= end_date or 锚定日）。展示/重跑预填用原始值。
            "end_date": end_date.isoformat() if end_date else None,
            "estimated_seconds": round(estimate_batch_seconds(eta_symbols, len(snapshot))),
            # 标的快照随批次冻结（与策略快照同理）：run_batch 直接读快照，
            # prepare 之后新增/禁用/改类目标的不影响本批次的格子集合，
            # 实际执行格子数与 total_cells 始终一致。
            "symbols": symbols,
        }
        if stop_profile == "sweep":
            config["sweep_atr_muls"] = [
                float(m) for m in (sweep_atr_muls or DEFAULT_SWEEP_ATR_MULS)
            ]
            config["sweep_chandelier_ratio"] = SWEEP_CHANDELIER_RATIO
        batch_name = name.strip() or default_batch_name(categories, len(snapshot), start_date, end_date)
        if stop_profile != "default" and f"[{stop_profile}]" not in batch_name:
            batch_name = f"{batch_name} [{stop_profile}]"
        return {
            "batch_id": batch_id,
            "name": batch_name,
            "categories_json": json.dumps(categories, ensure_ascii=False),
            "strategy_snapshot_json": json.dumps(snapshot, ensure_ascii=False),
            "config_json": json.dumps(config),
            "total_cells": len(symbols) * len(snapshot),
            "data_anchor_date": anchor.get("anchor_date"),
            "data_version": anchor.get("data_version"),
            "engine_version": ENGINE_VERSION,
            "stop_profile": stop_profile,
            "atr_basis": ATR_BASIS,
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
        stop_profile = str(batch.get("stop_profile") or "default")
        # time 列存的是 'YYYY-MM-DD 00:00:00'，date.fromisoformat 会报错。
        anchor = pd.Timestamp(str(batch["data_anchor_date"])).date()
        # 回测窗口（旧批次 config 无此字段 → 全生命周期，行为与之前一致）。
        # end 存的是用户请求值，可能超过锚定日 —— 截到锚定日，与 prepare_batch 一致。
        window_start = date.fromisoformat(config["start_date"]) if config.get("start_date") else None
        window_end_cfg = date.fromisoformat(config["end_date"]) if config.get("end_date") else None
        window_end = min(window_end_cfg, anchor) if window_end_cfg else anchor
        # 标的快照在 prepare 时已冻结进 config；旧批次（无快照字段）回退为
        # 按类目重新解析（行为与之前一致）。
        symbols = config.get("symbols") or resolve_batch_symbols(self.db, categories)

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
                    stop_gap_fill=bool(config.get("stop_gap_fill", True)),
                )

                for s in snapshot:
                    # loose 档标的级覆盖（与实盘 stop_loss.py 同口径）：快照里是配置
                    # 默认值，标的有 stop_atr_mul 时逐格子覆写硬止损倍数。
                    strategy_config = s["strategy_config"]
                    if stop_profile == "loose" and item.get("stop_atr_mul") is not None:
                        strategy_config = override_stop_atr_muls(
                            strategy_config, hard_mul=float(item["stop_atr_mul"])
                        )
                    try:
                        result = self.engine.run(
                            RuleBacktestRequest(
                                strategy=strategy_config,
                                symbol=symbol,
                                bars=bars,
                                start_date=window_start,
                                end_date=window_end,
                                execution=execution,
                                run_id=f"{batch_id}-{symbol}-{s['id']}",
                                sizer=None,
                            )
                        )
                        # 服务层补记（方案 §2.2）：引擎不关心类目归属。
                        for rt in result.get("round_trips") or []:
                            rt["category_l1"] = str(item.get("category_l1") or "")
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
            self._flush_counts(batch_id, counts, force=True)
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

    # counts 每 20 格 flush 一次（终态强制 flush）：单格 insert + counts 更新
    # 是两次事务，3000 格批次从 6000+ 次 WAL 刷盘降到约 1/20（P2-18）。
    _COUNTS_FLUSH_EVERY = 20

    def _flush_counts(self, batch_id: str, counts: dict, *, force: bool = False) -> None:
        if not force and counts["done"] % self._COUNTS_FLUSH_EVERY != 0:
            return
        self.db.update_batch_run(
            batch_id,
            done_cells=counts["done"],
            ok_cells=counts["ok"],
            failed_cells=counts["failed"],
            skipped_cells=counts["skipped"],
        )
