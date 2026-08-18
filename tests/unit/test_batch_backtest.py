"""批量回测服务单元测试（方案 §5.5）。

覆盖：随机指标检测、快照构建、标的解析、特征计算、格子提取、
批量执行（失败隔离 / 短数据 skipped / 取消 / 孤儿清理 / 409 兜底 / 级联删除）。
"""

from __future__ import annotations

import json
import threading
from datetime import date

import pandas as pd
import pytest

from data.storage.market_store import MarketStore
from rule_backtest.batch_service import (
    BatchBacktestService,
    aggregate_annual_returns,
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


def make_bars(n: int = 120, start_price: float = 10.0, start: str = "2024-01-02") -> pd.DataFrame:
    """确定性合成 bars（线性上行 + 固定波动），ISO 日期字符串与生产格式一致。"""
    base = pd.Timestamp(start)
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


# ----------------------------------------------------------------------
# 窗口批次（指定回测区间）
# ----------------------------------------------------------------------
def _run_window_batch(batch_db, start: date | None, end: date | None) -> str:
    service = BatchBacktestService(db=batch_db)
    batch = service.prepare_batch(
        categories=["测试"], strategy_ids=["sma_ok"],
        start_date=start, end_date=end,
        strategy_loader=StrategyLoader(db=batch_db),
    )
    assert batch_db.create_batch_run_if_idle(batch) is True
    service.run_batch(batch["batch_id"])
    return batch["batch_id"]


class TestWindowBatch:
    def test_prepare_stores_requested_window_and_names_it(self, batch_db) -> None:
        service = BatchBacktestService(db=batch_db)
        batch = service.prepare_batch(
            categories=["测试"], strategy_ids=["sma_ok"],
            start_date=date(2024, 1, 15), end_date=date(2024, 3, 31),
            strategy_loader=StrategyLoader(db=batch_db),
        )
        config = json.loads(batch["config_json"])
        assert config["start_date"] == "2024-01-15"
        assert config["end_date"] == "2024-03-31"
        assert "2024-01-15~2024-03-31" in batch["name"]

    def test_prepare_rejects_start_after_end(self, batch_db) -> None:
        service = BatchBacktestService(db=batch_db)
        with pytest.raises(ValueError, match="开始日期不能晚于结束日期"):
            service.prepare_batch(
                categories=["测试"], strategy_ids=["sma_ok"],
                start_date=date(2024, 5, 1), end_date=date(2024, 3, 1),
                strategy_loader=StrategyLoader(db=batch_db),
            )

    def test_prepare_keeps_requested_end_beyond_anchor(self, batch_db) -> None:
        """end 超过锚定日不报错（截断发生在执行侧），config 保留用户请求值。"""
        service = BatchBacktestService(db=batch_db)
        batch = service.prepare_batch(
            categories=["测试"], strategy_ids=["sma_ok"],
            end_date=date(2030, 1, 1),
            strategy_loader=StrategyLoader(db=batch_db),
        )
        config = json.loads(batch["config_json"])
        assert config["start_date"] is None
        assert config["end_date"] == "2030-01-01"

    def test_window_restricts_cells_and_skips_short(self, batch_db) -> None:
        # LONG.SS: 2024-01-02 起 120 根（连续自然日）→ 末根 2024-04-30
        batch_id = _run_window_batch(batch_db, date(2024, 1, 15), date(2024, 3, 31))
        cells = {c["symbol"]: c for c in batch_db.get_batch_cells(batch_id)}

        ok_cell = cells["LONG.SS"]
        assert ok_cell["status"] == "ok"
        assert ok_cell["start_date"] == "2024-01-15"
        assert ok_cell["end_date"] == "2024-03-31"
        assert ok_cell["bar_count"] == 77  # 01-15~01-31(17) + 02(29) + 03(31)
        # 数据完整覆盖窗口 → 非部分区间
        assert ok_cell["partial_window"] == 0

        skipped = cells["SHORT.SS"]
        assert skipped["status"] == "skipped"
        assert "窗口内数据不足" in skipped["error"]

    def test_partial_window_flagged_when_data_starts_late(self, batch_db) -> None:
        # 窗口起点早于 LONG.SS 首根 K 线（2024-01-02）→ 上市晚于窗口起点
        batch_id = _run_window_batch(batch_db, date(2023, 12, 1), date(2024, 3, 31))
        cells = {c["symbol"]: c for c in batch_db.get_batch_cells(batch_id)}
        assert cells["LONG.SS"]["status"] == "ok"
        assert cells["LONG.SS"]["partial_window"] == 1

    def test_partial_window_flagged_when_data_ends_early(self, batch_db) -> None:
        # OLD.SS 行情止于 2022-05-02，远早于窗口终点 2024-03-31 → 数据提前结束
        batch_db.save_instrument_metadata(
            [{"symbol": "OLD.SS", "name": "老标的", "category_l1": "测试", "asset_type": "etf"}]
        )
        MarketStore(db=batch_db).save_history("OLD.SS", make_bars(120, start="2022-01-03"))
        batch_id = _run_window_batch(batch_db, date(2022, 3, 1), date(2024, 3, 31))
        cells = {c["symbol"]: c for c in batch_db.get_batch_cells(batch_id)}

        old_cell = cells["OLD.SS"]
        assert old_cell["status"] == "ok"
        assert old_cell["bar_count"] == 63  # 03(31) + 04(30) + 05-01~05-02(2)
        assert old_cell["partial_window"] == 1
        # SHORT.SS 全部 30 根都在窗口内但仍不足 60 根 → 跳过
        assert cells["SHORT.SS"]["status"] == "skipped"
        assert "窗口内数据不足" in cells["SHORT.SS"]["error"]

    def test_end_beyond_anchor_capped_not_partial(self, batch_db) -> None:
        # end 请求值超过锚定日：执行截到锚定日，数据覆盖到末根 → 不应误报部分区间
        batch_id = _run_window_batch(batch_db, date(2024, 1, 15), date(2030, 1, 1))
        cells = {c["symbol"]: c for c in batch_db.get_batch_cells(batch_id)}
        ok_cell = cells["LONG.SS"]
        assert ok_cell["status"] == "ok"
        assert ok_cell["end_date"] == "2024-04-30"
        assert ok_cell["partial_window"] == 0

    def test_window_skips_all_when_range_before_any_data(self, batch_db) -> None:
        batch_id = _run_window_batch(batch_db, date(2019, 1, 1), date(2019, 12, 31))
        run = batch_db.get_batch_run(batch_id)
        assert run["status"] == "completed"
        assert run["ok_cells"] == 0
        assert run["skipped_cells"] == run["total_cells"]

    def test_count_bars_in_window(self, batch_db) -> None:
        counts = batch_db.count_bars_by_symbol(start=date(2024, 2, 1), end=date(2024, 2, 29))
        assert counts["LONG.SS"] == 29
        assert "SHORT.SS" not in counts  # 窗口内 0 根的标的不出现在结果里


# ----------------------------------------------------------------------
# 策略×年份聚合
# ----------------------------------------------------------------------
class TestAggregateAnnualReturns:
    def test_median_and_excess_per_strategy_year(self) -> None:
        rows = [
            {
                "strategy_name": "策略A", "strategy_id": "a",
                "annual_returns_json": json.dumps([
                    {"year": 2018, "return": 0.10, "benchmark_return": -0.20,
                     "win_rate": 0.6, "sharpe": 1.0, "max_drawdown": -0.10, "trade_count": 3},
                    {"year": 2019, "return": 0.20, "benchmark_return": 0.30, "trade_count": 2},
                ]),
            },
            {
                "strategy_name": "策略A", "strategy_id": "a",
                "annual_returns_json": json.dumps([
                    {"year": 2018, "return": 0.30, "benchmark_return": -0.20,
                     "win_rate": 0.4, "sharpe": 2.0, "max_drawdown": -0.20, "trade_count": 1},
                ]),
            },
            # 坏 blob 不影响其他格子
            {"strategy_name": "策略A", "strategy_id": "a", "annual_returns_json": "not-json"},
        ]
        aggs = aggregate_annual_returns(rows)
        assert len(aggs) == 2

        y2018 = next(a for a in aggs if a["year"] == 2018)
        assert y2018["n"] == 2
        assert y2018["median_return"] == pytest.approx(0.20)
        # 超额逐格子计算后取中位数：(0.10-(-0.20)) 与 (0.30-(-0.20)) → 0.40
        assert y2018["median_excess"] == pytest.approx(0.40)
        assert y2018["median_win_rate"] == pytest.approx(0.5)
        assert y2018["median_max_drawdown"] == pytest.approx(-0.15)
        assert y2018["trade_count"] == 4

        y2019 = next(a for a in aggs if a["year"] == 2019)
        assert y2019["n"] == 1
        # 缺失字段的中位数为 None 而不是 0
        assert y2019["median_win_rate"] is None

    def test_empty_input(self) -> None:
        assert aggregate_annual_returns([]) == []
        assert aggregate_annual_returns([{"annual_returns_json": None}]) == []
