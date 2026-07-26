"""批量回测服务单元测试（方案 §5.5）。

覆盖：随机指标检测、快照构建、标的解析、特征计算、格子提取、
批量执行（失败隔离 / 短数据 skipped / 取消 / 孤儿清理 / 409 兜底 / 级联删除）。
"""

from __future__ import annotations

import threading

import pandas as pd
import pytest

from data.storage.market_store import MarketStore
from rule_backtest.batch_service import (
    BatchBacktestService,
    build_strategy_snapshot,
    compute_features,
    estimate_batch_seconds,
    extract_cell,
    monthly_sampled_nav,
    resolve_batch_symbols,
    strategy_uses_random_indicator,
)
from rule_backtest.loader import StrategyLoader


def _condition(indicator: str, params: dict, op: str, right: dict, cid: str = "c1") -> dict:
    return {
        "id": cid,
        "type": "condition",
        "left": {"type": "indicator", "name": indicator, "params": params},
        "operator": op,
        "right": right,
    }


def _price_condition(op: str, right: dict, cid: str = "c2") -> dict:
    return {
        "id": cid,
        "type": "condition",
        "left": {"type": "price", "field": "close"},
        "operator": op,
        "right": right,
    }


def make_strategy(strategy_id: str = "test_sma20", entry: dict | None = None) -> dict:
    entry = entry or _condition(
        "sma", {"period": 20}, "<=", {"type": "price", "field": "close"}
    )
    return {
        "id": strategy_id,
        "name": f"测试策略 {strategy_id}",
        "schema_version": 1,
        "trade_mode": "single_symbol_all_in",
        "entry": {"type": "group", "combinator": "all", "children": [entry]},
        "exit": {
            "type": "group",
            "combinator": "any",
            "children": [
                _price_condition(
                    "<=",
                    {"type": "state_value", "name": "hard_stop", "params": {"atr_period": 20, "atr_mul": 1.5}},
                    "x1",
                )
            ],
        },
    }


def make_bars(n: int = 120, start_price: float = 10.0) -> pd.DataFrame:
    """确定性合成 bars（线性上行 + 固定波动），ISO 日期字符串与生产格式一致。"""
    base = pd.Timestamp("2024-01-02")
    records = []
    price = start_price
    for i in range(n):
        day = base + pd.Timedelta(days=i)
        close = price * 1.005
        records.append(
            {
                "time": day.date().isoformat(),
                "open": price,
                "high": close * 1.01,
                "low": price * 0.99,
                "close": close,
                "volume": 1_000_000 + i * 1000,
                "amount": (1_000_000 + i * 1000) * close,
            }
        )
        price = close
    return pd.DataFrame(records)


@pytest.fixture
def batch_db(test_db):
    """test_db + 两个标的（一长一短）+ 两个策略（一正常一随机）。"""
    db = test_db
    db.save_instrument_metadata(
        [
            {"symbol": "LONG.SS", "name": "长数据标的", "category_l1": "测试", "asset_type": "etf"},
            {"symbol": "SHORT.SS", "name": "短数据标的", "category_l1": "测试", "asset_type": "etf"},
            {"symbol": "OTHER.SS", "name": "其他类目", "category_l1": "其他", "asset_type": "etf"},
        ]
    )
    store = MarketStore(db=db)
    store.save_history("LONG.SS", make_bars(120))
    store.save_history("SHORT.SS", make_bars(30))
    store.save_history("OTHER.SS", make_bars(120))
    db.save_rule_strategy(make_strategy("sma_ok"), overwrite=True)
    db.save_rule_strategy(
        make_strategy(
            "rand_bad",
            entry=_condition("random_uniform", {}, ">=", {"type": "literal", "value": 0.5}),
        ),
        overwrite=True,
    )
    return db


# ----------------------------------------------------------------------
# 随机指标检测
# ----------------------------------------------------------------------
class TestRandomIndicatorDetection:
    def test_plain_strategy_is_clean(self) -> None:
        assert strategy_uses_random_indicator(make_strategy()) is False

    def test_random_in_entry_detected(self) -> None:
        strategy = make_strategy(
            entry=_condition("random_uniform", {}, ">=", {"type": "literal", "value": 0.5})
        )
        assert strategy_uses_random_indicator(strategy) is True

    def test_random_on_right_side_detected(self) -> None:
        strategy = make_strategy(
            entry=_price_condition(
                ">=", {"type": "indicator", "name": "random_uniform", "params": {}}, "c9"
            )
        )
        assert strategy_uses_random_indicator(strategy) is True

    def test_snapshot_rejects_random(self, batch_db) -> None:
        loader = StrategyLoader(db=batch_db)
        with pytest.raises(ValueError, match="随机指标"):
            build_strategy_snapshot(["sma_ok", "rand_bad"], loader=loader)

    def test_snapshot_builds(self, batch_db) -> None:
        snapshot = build_strategy_snapshot(["sma_ok"], loader=StrategyLoader(db=batch_db))
        assert len(snapshot) == 1
        assert snapshot[0]["id"] == "sma_ok"
        assert snapshot[0]["strategy_config"]["entry"]["type"] == "group"


# ----------------------------------------------------------------------
# 标的解析与耗时预估
# ----------------------------------------------------------------------
class TestResolveSymbols:
    def test_filters_by_category(self, batch_db) -> None:
        symbols = resolve_batch_symbols(batch_db, ["测试"])
        assert {s["symbol"] for s in symbols} == {"LONG.SS", "SHORT.SS"}
        by_symbol = {s["symbol"]: s for s in symbols}
        assert by_symbol["LONG.SS"]["bar_count"] == 120
        assert by_symbol["SHORT.SS"]["bar_count"] == 30

    def test_empty_category_raises(self, batch_db) -> None:
        with pytest.raises(ValueError):
            resolve_batch_symbols(batch_db, [])

    def test_estimate_scales_with_strategies(self, batch_db) -> None:
        symbols = resolve_batch_symbols(batch_db, ["测试"])
        one = estimate_batch_seconds(symbols, 1)
        assert estimate_batch_seconds(symbols, 3) == pytest.approx(one * 3)
        assert one > 0


# ----------------------------------------------------------------------
# 特征计算
# ----------------------------------------------------------------------
class TestComputeFeatures:
    def test_uptrend_features(self, batch_db) -> None:
        bars = MarketStore(db=batch_db).load_history("LONG.SS")
        features = compute_features(bars, "LONG.SS", db=batch_db)
        assert features["bar_count"] == 120
        assert features["momentum_250"] == pytest.approx(
            bars["close"].iloc[-1] / bars["close"].iloc[0] - 1, rel=1e-6
        )
        # 合成 bars 日收益恒定 → 波动率为 0 但必须可计算（非 None）
        assert features["ann_volatility"] is not None
        assert features["bh_max_drawdown"] == pytest.approx(0.0, abs=1e-9)  # 单调上行无回撤
        assert features["amount_ma20"] is not None and features["amount_ma20"] > 0
        # 测试库无 trend_daily → 可空
        assert features["trend_score_avg"] is None

    def test_short_bars_fallback(self) -> None:
        bars = make_bars(10)
        features = compute_features(bars, "X.SS", db=None)
        assert features["bar_count"] == 10
        # 不足 250 根时用全部可用数据
        expected = bars["close"].iloc[-1] / bars["close"].iloc[0] - 1
        assert features["momentum_250"] == pytest.approx(expected, rel=1e-6)

    def test_empty_bars(self) -> None:
        features = compute_features(pd.DataFrame(), "X.SS", db=None)
        assert features["bar_count"] == 0
        assert features["ann_volatility"] is None


# ----------------------------------------------------------------------
# 格子提取与月度 NAV
# ----------------------------------------------------------------------
class TestExtractCell:
    def test_monthly_nav_samples_month_ends(self) -> None:
        daily_nav = [
            {"date": "2024-01-30", "equity": 100},
            {"date": "2024-01-31", "equity": 101},
            {"date": "2024-02-01", "equity": 102},
            {"date": "2024-02-28", "equity": 103},
        ]
        nav = monthly_sampled_nav(daily_nav)
        assert nav == [
            {"month": "2024-01", "equity": 101},
            {"month": "2024-02", "equity": 103},
        ]

    def test_extract_cell_derives_excess_and_blobs(self) -> None:
        result = {
            "start_date": "2024-01-02",
            "end_date": "2024-06-30",
            "final_equity": 110000.0,
            "summary": {
                "total_return": 0.10, "annual_return": 0.20, "max_drawdown": -0.05,
                "sharpe": 1.5, "sortino": 2.0, "calmar": 4.0,
                "win_rate": 0.6, "profit_factor": 1.8, "trade_count": 10,
            },
            "benchmark_summary": {"total_return": 0.08, "annual_return": 0.15},
            "annual_returns": [{"year": 2024, "return": 0.1}],
            "monthly_heatmap": {"years": [2024]},
            "trades": [{"side": "BUY"}],
            "skipped_buys": [],
        }
        cell = extract_cell(result, [{"month": "2024-01", "equity": 100}])
        assert cell["status"] == "ok"
        assert cell["excess_annual_return"] == pytest.approx(0.05)
        assert cell["benchmark_annual_return"] == 0.15
        # blob 是 JSON 字符串，且不含大字段键
        assert "daily_nav" not in cell and "charts" not in cell and "condition_trace" not in cell
        import json

        assert json.loads(cell["trades_json"]) == [{"side": "BUY"}]
        assert json.loads(cell["monthly_nav_json"]) == [{"month": "2024-01", "equity": 100}]

    def test_extract_cell_missing_benchmark(self) -> None:
        cell = extract_cell({"summary": {"annual_return": 0.2}}, [])
        assert cell["excess_annual_return"] is None


# ----------------------------------------------------------------------
# 批量执行（端到端，测试库）
# ----------------------------------------------------------------------
def _run_batch(batch_db, cancel_event=None, engine=None):
    service = BatchBacktestService(db=batch_db, engine=engine)
    batch = service.prepare_batch(
        categories=["测试"], strategy_ids=["sma_ok"],
        strategy_loader=StrategyLoader(db=batch_db),
    )
    assert batch_db.create_batch_run_if_idle(batch) is True
    service.run_batch(batch["batch_id"], cancel_event=cancel_event)
    return batch["batch_id"]


class TestRunBatch:
    def test_completes_with_ok_and_skipped(self, batch_db) -> None:
        batch_id = _run_batch(batch_db)
        run = batch_db.get_batch_run(batch_id)
        assert run["status"] == "completed"
        # LONG 120 根 ok；SHORT 30 根 skipped；各 1 格
        assert run["total_cells"] == 2
        assert run["done_cells"] == 2
        assert run["ok_cells"] == 1
        assert run["skipped_cells"] == 1
        assert run["failed_cells"] == 0
        assert run["data_anchor_date"] is not None

        cells = batch_db.get_batch_cells(batch_id)
        by_symbol = {c["symbol"]: c for c in cells}
        ok_cell = by_symbol["LONG.SS"]
        assert ok_cell["status"] == "ok"
        assert ok_cell["annual_return"] is not None
        assert ok_cell["excess_annual_return"] is not None
        assert ok_cell["bar_count"] == 120
        assert ok_cell["strategy_name"].startswith("测试策略")
        # skipped 格带原因
        skipped = by_symbol["SHORT.SS"]
        assert skipped["status"] == "skipped"
        assert "数据不足" in skipped["error"]
        # 特征只为非 skipped 标的写
        assert ok_cell["ann_volatility"] is not None
        assert skipped["ann_volatility"] is None
        # blob 明细
        detail = batch_db.get_batch_cell_detail(batch_id, "LONG.SS", "sma_ok")
        assert detail["trades_json"] is not None
        assert detail["monthly_nav_json"] is not None

    def test_cell_failure_does_not_abort(self, batch_db) -> None:
        class FlakyEngine:
            def run(self, request):
                if request.symbol == "LONG.SS":
                    raise RuntimeError("boom")
                from rule_backtest.engine import SingleSymbolAllInBacktestEngine

                return SingleSymbolAllInBacktestEngine().run(request)

        batch_id = _run_batch(batch_db, engine=FlakyEngine())
        run = batch_db.get_batch_run(batch_id)
        assert run["status"] == "completed"
        assert run["failed_cells"] == 1
        cells = {c["symbol"]: c for c in batch_db.get_batch_cells(batch_id)}
        assert cells["LONG.SS"]["status"] == "failed"
        assert "boom" in cells["LONG.SS"]["error"]

    def test_cancel_before_start(self, batch_db) -> None:
        event = threading.Event()
        event.set()
        batch_id = _run_batch(batch_db, cancel_event=event)
        run = batch_db.get_batch_run(batch_id)
        assert run["status"] == "cancelled"
        assert run["done_cells"] == 0

    def test_second_run_blocked_while_running(self, batch_db) -> None:
        service = BatchBacktestService(db=batch_db)
        batch = service.prepare_batch(
            categories=["测试"], strategy_ids=["sma_ok"],
            strategy_loader=StrategyLoader(db=batch_db),
        )
        assert batch_db.create_batch_run_if_idle(batch) is True
        # 仍是 running：第二个批次必须被拒绝（409 的 DB 兜底）
        assert batch_db.create_batch_run_if_idle(batch) is False

    def test_orphan_cleanup_marks_interrupted(self, batch_db) -> None:
        service = BatchBacktestService(db=batch_db)
        batch = service.prepare_batch(
            categories=["测试"], strategy_ids=["sma_ok"],
            strategy_loader=StrategyLoader(db=batch_db),
        )
        batch_db.create_batch_run_if_idle(batch)
        assert batch_db.mark_interrupted_batch_runs() == 1
        run = batch_db.get_batch_run(batch["batch_id"])
        assert run["status"] == "interrupted"
        assert "重启" in run["error"]

    def test_delete_cascade_and_running_protection(self, batch_db) -> None:
        batch_id = _run_batch(batch_db)
        assert batch_db.get_batch_cells(batch_id)  # 有格子
        assert batch_db.delete_batch_run(batch_id) is True
        assert batch_db.get_batch_run(batch_id) is None
        assert batch_db.get_batch_cells(batch_id) == []

        # running 批次禁止删除
        service = BatchBacktestService(db=batch_db)
        batch = service.prepare_batch(
            categories=["测试"], strategy_ids=["sma_ok"],
            strategy_loader=StrategyLoader(db=batch_db),
        )
        batch_db.create_batch_run_if_idle(batch)
        assert batch_db.delete_batch_run(batch["batch_id"]) is False
