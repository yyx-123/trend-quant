"""止损宽度诊断：格子级分布指标 / stop_profile 快照 / 诊断聚合 / 批次对比
（方案 2026-08-30 §3-§6）。"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from data.storage.market_store import MarketStore
from rule_backtest.batch_service import (
    DEFAULT_SWEEP_ATR_MULS,
    BatchBacktestService,
    aggregate_stop_diagnostics,
    apply_stop_profile,
    bootstrap_mean_diff_ci,
    compare_batches,
    override_stop_atr_muls,
)
from rule_backtest.loader import StrategyLoader
from rule_backtest.metrics import compute_roundtrip_stats

from tests.unit.test_batch_backtest import make_bars, make_strategy


def make_wave_bars(start_price: float = 10.0) -> pd.DataFrame:
    """跌 10 天 → 涨 50 天 → 跌 60 天：close 对 SMA20 在 day20 上穿、day63 下穿，
    产生一笔完整 round-trip（参数经数值验证）。

    开头必须有一段下跌 —— SMA warmup 期被 mask，若从第一天就上行，close 始终
    在 SMA 上方，cross_above 永远没有「前一日在下方」的初态。
    """
    base = pd.Timestamp("2024-01-02")
    segments = [(0.98, 10), (1.005, 50), (0.99, 60)]
    records = []
    price = start_price
    i = 0
    for rate, count in segments:
        for _ in range(count):
            day = base + pd.Timedelta(days=i)
            close = price * rate
            records.append(
                {
                    "time": day.date().isoformat(),
                    "open": price,
                    "high": max(price, close) * 1.005,
                    "low": min(price, close) * 0.995,
                    "close": close,
                    "volume": 1_000_000 + i * 1000,
                    "amount": (1_000_000 + i * 1000) * close,
                }
            )
            price = close
            i += 1
    return pd.DataFrame(records)


def make_cross_strategy(strategy_id: str = "sma_cross") -> dict:
    return {
        "id": strategy_id,
        "name": f"均线穿越 {strategy_id}",
        "schema_version": 1,
        "trade_mode": "single_symbol_all_in",
        "entry": {
            "type": "group",
            "combinator": "all",
            "children": [
                {
                    "id": "c1",
                    "type": "condition",
                    "left": {"type": "price", "field": "close"},
                    "operator": "cross_above",
                    "right": {"type": "indicator", "name": "sma", "params": {"period": 20}},
                }
            ],
        },
        "exit": {
            "type": "group",
            "combinator": "any",
            "children": [
                {
                    "id": "x1",
                    "type": "condition",
                    "left": {"type": "price", "field": "close"},
                    "operator": "cross_below",
                    "right": {"type": "indicator", "name": "sma", "params": {"period": 20}},
                },
                {
                    "id": "x2",
                    "type": "condition",
                    "left": {"type": "price", "field": "close"},
                    "operator": "<=",
                    "right": {"type": "state_value", "name": "hard_stop", "params": {"atr_period": 20, "atr_mul": 1.5}},
                },
            ],
        },
    }


@pytest.fixture
def batch_db(test_db):
    db = test_db
    db.save_instrument_metadata(
        [
            {"symbol": "LONG.SS", "name": "长数据标的", "category_l1": "测试", "asset_type": "etf"},
            {"symbol": "SHORT.SS", "name": "短数据标的", "category_l1": "测试", "asset_type": "etf"},
        ]
    )
    store = MarketStore(db=db)
    store.save_history("LONG.SS", make_wave_bars())
    store.save_history("SHORT.SS", make_bars(30))
    db.save_rule_strategy(make_cross_strategy("sma_ok"), overwrite=True)
    return db


# ----------------------------------------------------------------------
# compute_roundtrip_stats（§3.1）
# ----------------------------------------------------------------------
def _trip(r=None, pnl=0.0, reason="hard_stop", mfe_pct=None, exit_date="2026-01-10", qty=100, entry_price=10.0):
    return {
        "r_multiple": r,
        "pnl": pnl,
        "exit_reason": reason,
        "mfe_pct": mfe_pct,
        "exit_date": exit_date,
        "qty": qty,
        "entry_price": entry_price,
    }


class TestRoundtripStats:
    def test_known_r_sequence(self) -> None:
        trips = [
            _trip(r=-1.0, pnl=-100.0, exit_date="2026-01-05"),
            _trip(r=-0.5, pnl=-50.0, exit_date="2026-01-06"),
            _trip(r=1.0, pnl=100.0, exit_date="2026-01-07"),
            _trip(r=3.0, pnl=300.0, exit_date="2026-01-08"),
        ]
        stats = compute_roundtrip_stats(trips, [])
        assert stats["r_mean"] == pytest.approx(0.625)
        assert stats["r_p25"] == pytest.approx(-0.625)
        assert stats["r_p75"] == pytest.approx(1.5)
        assert stats["r_p5"] == pytest.approx(-0.925)
        assert stats["r_p95"] == pytest.approx(2.7)
        assert stats["tail_ratio"] == pytest.approx(2.7 / 0.925)
        assert stats["r_skew"] is not None
        assert stats["stop_exit_ratio"] == pytest.approx(1.0)
        assert stats["chandelier_exit_ratio"] == pytest.approx(0.0)
        assert stats["max_losing_streak"] == 2

    def test_exit_reason_ratios(self) -> None:
        trips = [
            _trip(reason="hard_stop"),
            _trip(reason="chandelier_stop"),
            _trip(reason="chandelier_stop_ratchet"),
            _trip(reason="exit_conditions_passed"),
        ]
        stats = compute_roundtrip_stats(trips, [])
        assert stats["stop_exit_ratio"] == pytest.approx(0.25)
        assert stats["chandelier_exit_ratio"] == pytest.approx(0.5)

    def test_exit_efficiency(self) -> None:
        # MFE 金额 = 10% × 10 × 100 = 100；pnl=50 → 效率 0.5
        trips = [_trip(pnl=50.0, mfe_pct=10.0)]
        stats = compute_roundtrip_stats(trips, [])
        assert stats["exit_efficiency"] == pytest.approx(0.5)

    def test_nav_based_metrics(self) -> None:
        nav = [{"date": f"2026-01-{i + 1:02d}", "equity": v} for i, v in enumerate([100, 110, 99, 99, 120])]
        stats = compute_roundtrip_stats([], nav)
        assert stats["cvar_5"] is not None and stats["cvar_5"] < 0
        assert stats["ulcer_index"] is not None and stats["ulcer_index"] > 0
        assert stats["max_dd_duration_days"] == 2  # 99, 99 两段水下
        # 无交易时 round-trip 维度全 None
        assert stats["r_mean"] is None
        assert stats["stop_exit_ratio"] is None

    def test_empty(self) -> None:
        stats = compute_roundtrip_stats([], [])
        assert stats["r_mean"] is None
        assert stats["cvar_5"] is None
        assert stats["max_dd_duration_days"] is None


# ----------------------------------------------------------------------
# stop_profile 快照覆写（§5.1）
# ----------------------------------------------------------------------
def _stop_muls(strategy_config: dict) -> dict:
    out = {}
    for cond in strategy_config["exit"]["children"]:
        for side in ("left", "right"):
            spec = cond.get(side, {})
            if isinstance(spec, dict) and spec.get("type") == "state_value":
                out[spec["name"]] = spec["params"]["atr_mul"]
    return out


class TestStopProfile:
    def test_override_does_not_mutate_original(self) -> None:
        strategy = make_strategy("s1")
        out = override_stop_atr_muls(strategy, hard_mul=1.0, chandelier_mul=2.0)
        assert _stop_muls(out) == {"hard_stop": 1.0}
        assert _stop_muls(strategy) == {"hard_stop": 1.5}

    def test_tight_profile(self) -> None:
        snapshot = [{"id": "s1", "name": "策略1", "strategy_config": make_strategy("s1")}]
        out = apply_stop_profile(snapshot, "tight", None)
        assert _stop_muls(out[0]["strategy_config"]) == {"hard_stop": 1.0}
        assert _stop_muls(snapshot[0]["strategy_config"]) == {"hard_stop": 1.5}

    def test_sweep_expands_snapshot(self) -> None:
        strategy = make_strategy("s1")
        strategy["exit"]["children"].append(
            {
                "id": "x2",
                "type": "condition",
                "left": {"type": "price", "field": "close"},
                "operator": "<=",
                "right": {"type": "state_value", "name": "chandelier_stop", "params": {"atr_period": 20, "atr_mul": 2.5}},
            }
        )
        snapshot = [{"id": "s1", "name": "策略1", "strategy_config": strategy}]
        out = apply_stop_profile(snapshot, "sweep", None)
        assert len(out) == len(DEFAULT_SWEEP_ATR_MULS)
        first = out[0]
        assert first["id"] == f"s1@hs{DEFAULT_SWEEP_ATR_MULS[0]:g}"
        muls = _stop_muls(first["strategy_config"])
        assert muls["hard_stop"] == pytest.approx(DEFAULT_SWEEP_ATR_MULS[0])
        # sweep 吊灯固定 = hard × 2（方案 §10 拍板）
        assert muls["chandelier_stop"] == pytest.approx(DEFAULT_SWEEP_ATR_MULS[0] * 2)

    def test_unknown_profile_raises(self) -> None:
        with pytest.raises(ValueError):
            apply_stop_profile([], "ultra", None)

    def test_prepare_batch_tight(self, batch_db) -> None:
        service = BatchBacktestService(db=batch_db)
        batch = service.prepare_batch(
            categories=["测试"],
            strategy_ids=["sma_ok"],
            strategy_loader=StrategyLoader(db=batch_db),
            stop_profile="tight",
        )
        assert batch["stop_profile"] == "tight"
        assert batch["atr_basis"] == "prev_close"
        assert batch["name"].endswith("[tight]")
        snapshot = json.loads(batch["strategy_snapshot_json"])
        assert _stop_muls(snapshot[0]["strategy_config"]) == {"hard_stop": 1.0}


# ----------------------------------------------------------------------
# 端到端：紧/松两批 × 2 标的冒烟（§8.3 任务 5 验证）
# ----------------------------------------------------------------------
def _run_batch(batch_db, stop_profile="default"):
    service = BatchBacktestService(db=batch_db)
    batch = service.prepare_batch(
        categories=["测试"],
        strategy_ids=["sma_ok"],
        strategy_loader=StrategyLoader(db=batch_db),
        stop_profile=stop_profile,
    )
    assert batch_db.create_batch_run_if_idle(batch) is True
    service.run_batch(batch["batch_id"])
    return batch["batch_id"]


class TestBatchWithRoundTrips:
    def test_cells_carry_round_trips_and_stats(self, batch_db) -> None:
        batch_id = _run_batch(batch_db)
        run = batch_db.get_batch_run(batch_id)
        assert run["stop_profile"] == "default"
        assert run["atr_basis"] == "prev_close"
        cell = batch_db.get_batch_cell_detail(batch_id, "LONG.SS", "sma_ok")
        assert cell["round_trips_source"] == "engine"
        trips = json.loads(cell["round_trips_json"] or "[]")
        assert trips, "LONG.SS 上行行情应产生 round-trips"
        rt = trips[0]
        for field in (
            "entry_date", "exit_date", "exit_reason", "r_multiple",
            "mae_pct", "mfe_pct", "holding_days", "entry_atr", "entry_atr_pct",
            "asset_type", "category_l1", "hard_stop_atr_mul",
        ):
            assert field in rt
        assert rt["category_l1"] == "测试"
        # 平铺分布列随行落库
        assert cell["r_mean"] is not None
        # 格子列表端点也带新列
        listed = batch_db.get_batch_cells(batch_id)
        assert "r_mean" in listed[0] and "round_trips_source" in listed[0]

    def test_tight_and_loose_batches_comparable(self, batch_db) -> None:
        base_id = _run_batch(batch_db, "tight")
        alt_id = _run_batch(batch_db, "loose")
        result = compare_batches(batch_db, base_id, alt_id)
        assert result["common_cells"] >= 1
        assert result["cell_diffs"][0]["symbol"] == "LONG.SS"
        assert "delta_annual_return" in result["cell_diffs"][0]
        assert isinstance(result["base_diagnostics"], list)

    def test_stop_diagnostics_aggregation(self, batch_db) -> None:
        batch_id = _run_batch(batch_db)
        rows = batch_db.get_batch_roundtrip_rows(batch_id)
        diags = aggregate_stop_diagnostics(rows)
        assert diags
        dims = {d["dim"] for d in diags}
        assert {"vol_quintile", "asset_type", "trend_regime", "exit_year"} <= dims
        for d in diags:
            assert d["n"] >= 1
            assert d["low_confidence"] is (d["n"] < 30)


# ----------------------------------------------------------------------
# bootstrap CI（§6.2）
# ----------------------------------------------------------------------
class TestBootstrap:
    def test_known_pairs(self) -> None:
        pairs = [(1.0, 1.5), (2.0, 2.5), (3.0, 3.5)] * 10
        ci = bootstrap_mean_diff_ci(pairs, resamples=200)
        assert ci["n_pairs"] == 30
        assert ci["mean_diff"] == pytest.approx(0.5)
        assert ci["ci95_low"] <= ci["mean_diff"] <= ci["ci95_high"]

    def test_insufficient_pairs(self) -> None:
        assert bootstrap_mean_diff_ci([(1.0, 2.0)]) is None
