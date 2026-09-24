"""DS 盲审项的回归钉子（每项 P1 一条；P2 择要）。

- test_evidence_turnover_is_not_constant（Δ换手不是恒 0 假证据）
- test_dispatcher_backs_off_when_session_capped（热自旋修复后调用次数有界）
- test_event_study_same_event_different_regime_not_duplicate（换条件不误杀）
- test_event_study_identical_spec_is_duplicate（真重复才命中）
- test_rerun_keeps_attempt_index（复现不收尝试计数）
- test_backtest_report_contains_gate_rejections_and_round_trips（报告完整性）
- test_time_stop_exits_on_nth_day（持有天数口径）
- test_heat_none_when_unstopped_positions（heat 无止损语义）
- test_creation_all_none_rejected（空创建型拒绝）
- test_promote_to_library_requires_confirmed（晋升门）
"""

from __future__ import annotations

import time
from datetime import date

import numpy as np
import pandas as pd
import pytest

from data.storage.db import Database
from portfolio.backtester import run_backtest
from portfolio.registry import fresh_registry
from portfolio.seed import seed_default_library
from portfolio.slots import register_builtin_modules
from portfolio.strategy import parse_strategy_yaml
from research import experiments, lifecycle, sessions, topics, verdict
from research.api import ResearchService
from research.errors import ResearchError
from research.pipeline import run_experiment
from research.worker import ResearchWorker

pytestmark = pytest.mark.integration


@pytest.fixture
def registry():
    reg = fresh_registry()
    register_builtin_modules(reg)
    return reg


def _write_symbol(db, symbol, closes, start="2022-01-03", asset_type="etf"):
    n = len(closes)
    dates = pd.bdate_range(start, periods=n)
    closes = np.asarray(closes, dtype=float)
    df = pd.DataFrame({
        "time": dates, "open": closes, "high": closes * 1.01, "low": closes * 0.99,
        "close": closes, "volume": np.full(n, 2e6), "amount": closes * 2e6 * 5,
    })
    db.save_market_data(symbol, df, price_mode="qfq")
    db.save_market_data(symbol, df, price_mode="raw")
    db.save_instrument_metadata([{
        "symbol": symbol, "name": symbol, "category_l1": "T", "category_l2": "T2",
        "category_l3": "", "enabled": 1, "asset_type": asset_type,
    }])


@pytest.fixture
def env(test_db, registry):
    rng = np.random.default_rng(23)
    for i in range(5):
        closes = 12 * np.exp(np.cumsum(rng.normal(0.002 + i * 0.0005, 0.018, 360)))
        _write_symbol(test_db, f"D{i:03d}.SS", closes)
    versions = seed_default_library(test_db, registry)
    session = sessions.get_or_create_default_human_session(test_db)
    topic = topics.create_topic(
        test_db, session_id=session["session_id"], title="止损研究", question="止损宽度"
    )
    return {"db": test_db, "registry": registry, "versions": versions,
            "session": session, "topic": topic}


CFG = """
name: ds-turnover
universe: {module: category_filter@1, params: {asset_type: all}}
signal: {module: macd_cross@1, params: {}}
rank: {module: by_freshness@1, params: {}}
sizing: {module: fixed_slots@1, params: {slots: 3}}
portfolio_risk:
  - {module: slot_limit@1, params: {max_positions: 3}}
position_risk: {module: hard_stop@1, params: {atr_mul: 1.5}}
execution: {module: tail_session@1, params: {slippage_base: 0.002, slippage_tail: 0.001}}
"""


def test_evidence_turnover_is_not_constant(env):
    """DS-P1-3：Δ换手必须来自真实成交（有成交的合成 run 上非平凡）。"""
    db, reg = env["db"], env["registry"]
    session, topic = env["session"], env["topic"]
    # 一条有成交的策略线
    from portfolio import library

    library.ensure_strategy(db, "ds-line", name="ds")
    version = library.add_version_yaml(db, "ds-line", CFG, reg)
    exp = experiments.propose_experiment(
        db, session_id=session["session_id"], title="换手证据",
        topic_id=topic["id"], evaluation_module="portfolio_backtest@1",
        spec={"base": version["id"],
              "diff": [{"slot": "position_risk", "from": "hard_stop@1", "to": "hard_stop@1",
                        "params": {"atr_mul": 1.0}}],
              "window": ["2022-06-01", "2023-12-29"]},
        hypothesis="止损收紧后换手上升的实验陈述",
    )
    v = run_experiment(db, exp["id"], registry=reg)
    deltas = v["evidence"]["deltas_vs_base"]
    assert deltas["delta_turnover"] is not None
    # 两个止损宽度下换手总额必须非零（有真实成交），且数值与成交总额/均权一致量级
    exp_turnover = v["evidence"]["experiment_summary"]["turnover"]
    assert exp_turnover is not None and exp_turnover > 0
    # 钉死非平凡性：紧止损（1.0）vs 松（2.0）的换手差必须非零（恒 0 的假证据
    # 形态再也过不了这条）
    assert deltas["delta_turnover"] != 0.0
    assert any("survivorship_bias" in w for w in v["warnings"])


def test_backtest_report_contains_gate_rejections_and_round_trips(env):
    """DS-P1-1：report 含 §6.5.0 明细键（round_trips / gate_rejections / 全指标表）。"""
    db, reg = env["db"], env["registry"]
    session, topic = env["session"], env["topic"]
    from portfolio import library

    library.ensure_strategy(db, "ds-line2", name="ds2")
    version = library.add_version_yaml(db, "ds-line2", CFG, reg)
    exp = experiments.propose_experiment(
        db, session_id=session["session_id"], title="报告完整性",
        topic_id=topic["id"], evaluation_module="portfolio_backtest@1",
        spec={"base": version["id"],
              "diff": [{"slot": "position_risk", "from": "hard_stop@1", "to": "hard_stop@1",
                        "params": {"atr_mul": 1.2}}],
              "window": ["2022-06-01", "2023-12-29"]},
        hypothesis="报告完整性的实验假设陈述", allow_duplicate=True,
    )
    v = run_experiment(db, exp["id"], registry=reg)
    report = v["report"]
    assert report.get("full_run_report") is not None
    fr = report["full_run_report"]
    for key in ("summary", "round_trips", "gate_rejections", "unfilled_by_reason",
                "cost", "return_distribution", "drawdown_durations"):
        assert key in fr, f"report missing {key}"
    # 风控拦截统计是真数据：该配置带 slot_limit（=3）且 5 标的 always 候选——
    # 合成 run 上 slot_limit 必有拦截记录
    assert fr["gate_rejections"].get("slot_limit", 0) > 0


def test_time_stop_exits_on_nth_day(test_db, registry):
    """DS-P2-9：max_days=N → 第 N 个交易日离场（入场日=第 1 天）。"""
    from engine.models import Position, StopState
    from portfolio.slots.position_risk import TimeStopModule

    module = TimeStopModule({"max_days": 5})
    n = 12
    days = list(pd.bdate_range("2024-01-02", periods=n).date)
    closes = [10.0] * n
    panel = _mini_panel(test_db, days, closes)

    from portfolio.context import PanelView

    entry = days[2]
    exits = []
    for upto in range(len(days)):
        ctx = _mini_ctx(panel, upto, entry)
        position = Position(symbol="GATE00.SS", quantity=1000, sellable_quantity=1000,
                            avg_cost=10.0, entry_date=entry, entry_price=10.0,
                            stop=StopState(stop_price=None, highest_since_buy=10.0,
                                           atr_at_entry=0.1, fill_mode="tail",
                                           module_state={}))
        intent = module.evaluate(ctx, position)
        if intent is not None:
            exits.append(days[upto])
    # 入场日（第 3 日）= 第 1 个交易日 → 第 5 个交易日 = days[6] 首次离场
    # （之后每日仍报离场是模块的正常行为——持仓未被成交清掉前的持续信号）
    assert exits and exits[0] == days[6]


def _mini_panel(db, days, closes):
    from gateway.panel import Panel

    symbols = ("GATE00.SS",)
    data = {}
    for f in ("open", "high", "low", "close", "volume", "amount"):
        data[f] = np.tile(np.asarray(closes, dtype=float)[:, None], (1, 1))
    return Panel(dates=tuple(days), symbols=symbols, data=data,
                 provisional=np.zeros((len(days), 1), dtype=bool))


def _mini_ctx(panel, upto, entry_date):
    from datetime import datetime, time as _time

    from engine.models import Account
    from portfolio.context import AccountView, DayContext, PanelView

    day = panel.dates[upto]
    account = Account(cash=0.0)
    return DayContext(
        date=day, as_of=datetime.combine(day, _time(15, 0)),
        gateway=None, panel=PanelView(panel, upto),
        account=AccountView(account, {"GATE00.SS": 10.0}),
        params={}, data_version=1, history=[], gate_log=[], extras={},
    )


def test_heat_none_when_unstopped_positions():
    """DS-P2-8：任一持仓无止损价 → 组合热记 None（不是静默少计）；空仓为 0。"""
    from engine.models import Account, Position, StopState

    account = Account(cash=0.0)
    account.positions["A.SS"] = Position(
        symbol="A.SS", quantity=100, sellable_quantity=100, avg_cost=10.0,
        entry_date=date(2024, 3, 1), entry_price=10.0,
        stop=StopState(stop_price=9.0, highest_since_buy=10.0, atr_at_entry=0.2),
    )
    account.positions["B.SS"] = Position(
        symbol="B.SS", quantity=100, sellable_quantity=100, avg_cost=5.0,
        entry_date=date(2024, 3, 1), entry_price=5.0,
        stop=StopState(stop_price=None, highest_since_buy=5.0, atr_at_entry=0.1),
    )
    assert account.unstopped_symbols() == ["B.SS"]
    assert account.heat({"A.SS": 10.0, "B.SS": 5.0}) is None
    # 全有止损 → 精确值；空仓 → 0
    del account.positions["B.SS"]
    assert account.heat({"A.SS": 10.0}) == pytest.approx(100.0)
    assert Account(cash=0.0).heat({}) == 0.0


def test_creation_all_none_rejected(env):
    """DS-P3-1：七槽全 none 的"创建型"是空实验 → 拒绝。"""
    db, reg = env["db"], env["registry"]
    session, topic = env["session"], env["topic"]
    from research.errors import IntakeRejected

    with pytest.raises(IntakeRejected, match="at least one real module"):
        experiments.propose_experiment(
            db, session_id=session["session_id"], title="空创建",
            topic_id=topic["id"], evaluation_module="portfolio_backtest@1",
            spec={"base": env["versions"]["blank-base"],
                  "diff": [{"slot": s, "to": "none"} for s in
                           ("universe", "signal", "rank", "sizing",
                            "portfolio_risk", "position_risk", "execution")]},
            hypothesis="空创建型实验应被拒绝的陈述",
        )


def test_rerun_keeps_attempt_index(env):
    """DS-P1-2：复现保留原 attempt_index，且不计入后续尝试计数。"""
    db, reg = env["db"], env["registry"]
    session, topic = env["session"], env["topic"]
    from portfolio import library

    library.ensure_strategy(db, "ds-line3", name="ds3")
    version = library.add_version_yaml(db, "ds-line3", CFG, reg)
    spec = {"base": version["id"],
            "diff": [{"slot": "position_risk", "to": "hard_stop@1", "params": {"atr_mul": 1.1}}]}
    e1 = experiments.propose_experiment(
        db, session_id=session["session_id"], title="原始",
        topic_id=topic["id"], evaluation_module="portfolio_backtest@1",
        spec=spec, hypothesis="原始实验假设陈述足够长", allow_duplicate=True,
    )
    assert e1["attempt_index"] == 1
    lifecycle.transition(db, e1["id"], "running")
    lifecycle.transition(db, e1["id"], "failed", error="boom")
    r1 = experiments.rerun_experiment(db, experiment_id=e1["id"],
                                      session_id=session["session_id"])
    assert r1["attempt_index"] == e1["attempt_index"]
    assert r1["is_reproduction"] == 1
    assert r1["parent_experiment_id"] == e1["id"]
    e2 = experiments.propose_experiment(
        db, session_id=session["session_id"], title="后续不同实验",
        topic_id=topic["id"], evaluation_module="portfolio_backtest@1",
        spec={"base": version["id"],
              "diff": [{"slot": "position_risk", "to": "hard_stop@1", "params": {"atr_mul": 1.3}}]},
        hypothesis="后续不同实验的假设陈述", registry=reg,
    )
    assert e2["attempt_index"] == 2  # rerun 不计入


def test_event_study_same_event_different_regime_not_duplicate(env):
    """DS-P1-5：同事件换 context_filter/horizons 是另一个实验（详设 §6.5.2 明文）。"""
    db, reg = env["db"], env["registry"]
    session, topic = env["session"], env["topic"]
    spec = {"event": "ma_cross@1(n=20)", "horizons": [10, 20], "expect": "positive"}
    e1 = experiments.propose_experiment(
        db, session_id=session["session_id"], title="牛市跌破",
        topic_id=topic["id"], evaluation_module="event_study@1",
        spec=spec, hypothesis="牛市 regime 跌破 MA20 的演绎假设",
    )
    assert e1["status"] == "queued"
    e2 = experiments.propose_experiment(
        db, session_id=session["session_id"], title="熊市跌破",
        topic_id=topic["id"], evaluation_module="event_study@1",
        spec={**spec, "context_filter": {"benchmark": "510300.SS", "rule": "close_below_ma200"},
              "horizons": [5]},
        hypothesis="同一事件换条件应是另一个实验（不被误杀）",
    )
    assert e2["status"] == "queued"  # 不误杀
    # 真重复（完全同 spec）才命中
    with pytest.raises(Exception, match="duplicate_of"):
        experiments.propose_experiment(
            db, session_id=session["session_id"], title="真重复",
            topic_id=topic["id"], evaluation_module="event_study@1",
            spec=spec, hypothesis="完全相同的 spec 应判重复", registry=reg,
        )


def test_dispatcher_backs_off_when_session_capped(env):
    """DS-P1-4：cap 命中时调度循环退避（迭代次数有界，不再 576/s）。"""
    db, reg = env["db"], env["registry"]
    worker = ResearchWorker(db, registry=reg, max_workers=1, per_session_cap=1)
    # 塞两个同会话实验：第一个占住并发位（慢 runner），第二个反复 cap
    session = sessions.register_session(db, "ai", label="bot", channel="cli")
    topic = topics.create_topic(
        db, session_id=session["session_id"], title="并发", question="cap 退避"
    )
    from portfolio import library

    library.ensure_strategy(db, "ds-line4", name="ds4")
    version = library.add_version_yaml(db, "ds-line4", CFG, reg)

    import research.pipeline as pipeline

    def slow_runner(db_, exp_, ctx_):
        time.sleep(1.5)
        return {"baseline": {}, "evidence": {}, "warnings": [],
                "report": {}, "suggested_verdict": "inconclusive", "runs": []}

    eval_mod = pipeline.evaluations.require_evaluation("portfolio_backtest@1")
    orig_runner = eval_mod.runner
    eval_mod.runner = slow_runner
    try:
        for title, mul in (("慢实验A", 1.0), ("慢实验B", 1.5)):
            experiments.propose_experiment(
                db, session_id=session["session_id"], title=title,
                topic_id=topic["id"], evaluation_module="portfolio_backtest@1",
                spec={"base": version["id"],
                      "diff": [{"slot": "position_risk", "to": "hard_stop@1",
                                "params": {"atr_mul": mul}}]},
                hypothesis="cap 退避测试的假设陈述", allow_duplicate=True,
            )
        worker.submit_all_queued()
        worker.start()
        t0 = time.monotonic()
        time.sleep(2.5)
        worker.stop()
        elapsed = time.monotonic() - t0
        # 退避后循环节拍 ~2/s：2.5 秒内迭代应远小于热自旋量级（原实测 576/s）
        assert worker._dispatch_iterations < 30, (
            f"dispatcher iterations {worker._dispatch_iterations} in {elapsed:.1f}s — hot spin?"
        )
    finally:
        eval_mod.runner = orig_runner
        worker.stop()


def test_promote_to_library_requires_confirmed(env):
    """DS-P2-1：晋升门在入库——未确认/非 confirmed 的实验不得晋升。"""
    db, reg = env["db"], env["registry"]
    session, topic = env["session"], env["topic"]
    service = ResearchService(db, registry=reg, topics_dir=db.db_path.parent / "t")
    from portfolio import library

    library.ensure_strategy(db, "ds-line5", name="ds5")
    version = library.add_version_yaml(db, "ds-line5", CFG, reg)
    exp = experiments.propose_experiment(
        db, session_id=session["session_id"], title="待晋升",
        topic_id=topic["id"], evaluation_module="portfolio_backtest@1",
        spec={"base": version["id"],
              "diff": [{"slot": "position_risk", "from": "hard_stop@1", "to": "hard_stop@1",
                        "params": {"atr_mul": 1.0}}]},
        hypothesis="待晋升实验的假设陈述", allow_duplicate=True,
    )
    # 未跑 → 拒
    with pytest.raises(ResearchError, match="verdicted"):
        service.promote_to_library(
            experiment_id=exp["id"], strategy_id="promoted-x",
            session_id=session["session_id"],
        )
    # 到终态但非 confirmed → 拒
    lifecycle.transition(db, exp["id"], "running")
    lifecycle.transition(db, exp["id"], "evaluating")
    verdict.insert_platform_verdict(
        db, experiment_id=exp["id"], baseline={}, evidence={}, warnings=[],
        report={}, suggested_verdict="inconclusive",
    )
    verdict.confirm_verdict(
        db, experiment_id=exp["id"], final_verdict="inconclusive",
        reasoning="未证实", session_id=session["session_id"],
    )
    with pytest.raises(ResearchError, match="confirmed"):
        service.promote_to_library(
            experiment_id=exp["id"], strategy_id="promoted-x",
            session_id=session["session_id"],
        )


def test_rerun_error_branches(env):
    """验证复审残留：rerun 不存在/非终态实验抛 LifecycleError（不是 NameError）。"""
    db = env["db"]
    session = env["session"]
    from research.errors import LifecycleError

    with pytest.raises(LifecycleError, match="not found"):
        experiments.rerun_experiment(db, experiment_id="E9999",
                                     session_id=session["session_id"])
    from portfolio import library

    library.ensure_strategy(db, "ds-line6", name="ds6")
    version = library.add_version_yaml(db, "ds-line6", CFG, env["registry"])
    exp = experiments.propose_experiment(
        db, session_id=session["session_id"], title="未终态",
        topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
        spec={"base": version["id"],
              "diff": [{"slot": "position_risk", "to": "hard_stop@1", "params": {"atr_mul": 1.0}}]},
        hypothesis="未终态实验不可复现的假设陈述", allow_duplicate=True,
    )
    with pytest.raises(LifecycleError, match="only terminal"):
        experiments.rerun_experiment(db, experiment_id=exp["id"],
                                     session_id=session["session_id"])


def test_manifest_data_version_from_audit(env, tmp_path):
    """DS-P2-2 残留：event 类实验的 manifest.data_version 从 gateway_audit 补齐。"""
    db, reg = env["db"], env["registry"]
    session, topic = env["session"], env["topic"]
    exp = experiments.propose_experiment(
        db, session_id=session["session_id"], title="事件快筛",
        topic_id=topic["id"], evaluation_module="event_study@1",
        spec={"event": "ma_cross@1(n=20)", "horizons": [10],
              "universe": [f"D{i:03d}.SS" for i in range(5)]},
        hypothesis="事件快筛的假设陈述足够长", allow_duplicate=True,
    )
    run_experiment(db, exp["id"], registry=reg)
    from research import runs as runs_mod

    rows = runs_mod.list_runs(db, exp["id"])
    assert rows and all(r["engine_run_id"] is None for r in rows)
    from research.topic_files import _build_manifest
    from research.verdict import latest_verdict

    manifest = _build_manifest(db, exp, latest_verdict(db, exp["id"]))
    assert manifest["data_version"] is not None and manifest["data_version"] > 0


def test_duplicate_two_tier_and_field_escape(env):
    """DS-R2 P2：完全一致→硬拒；挪窗口/改数值=相似须确认；加未知字段=相似须确认；
    换声明字段（horizons/context_filter）=另一个实验放行。"""
    db, reg = env["db"], env["registry"]
    session, topic = env["session"], env["topic"]
    from research.errors import IntakeRejected

    base_spec = {"event": "ma_cross@1(n=20)", "horizons": [10, 20], "expect": "positive",
                 "window": ["2022-06-01", "2023-12-29"]}
    experiments.propose_experiment(
        db, session_id=session["session_id"], title="基准",
        topic_id=topic["id"], evaluation_module="event_study@1",
        spec=base_spec, hypothesis="重复检测两档的基准实验假设", allow_duplicate=True,
    )
    # 挪窗口一天 → similar（须确认，不再完全绕过）
    with pytest.raises(IntakeRejected, match="similar_to"):
        experiments.propose_experiment(
            db, session_id=session["session_id"], title="挪一天",
            topic_id=topic["id"], evaluation_module="event_study@1",
            spec={**base_spec, "window": ["2022-06-02", "2023-12-29"]},
            hypothesis="挪窗口一天须确认的假设陈述",
        )
    # 加未知字段 → similar（逃逸被堵）
    with pytest.raises(IntakeRejected, match="similar_to"):
        experiments.propose_experiment(
            db, session_id=session["session_id"], title="加字段",
            topic_id=topic["id"], evaluation_module="event_study@1",
            spec={**base_spec, "some_unused_field": 1},
            hypothesis="加未知字段须确认的假设陈述",
        )
    # 换声明字段（horizons/context_filter）→ 另一个实验，放行
    ok = experiments.propose_experiment(
        db, session_id=session["session_id"], title="换条件",
        topic_id=topic["id"], evaluation_module="event_study@1",
        spec={**base_spec, "horizons": [5],
              "context_filter": {"benchmark": "510300.SS", "rule": "close_below_ma200"}},
        hypothesis="换声明字段是另一个实验应放行", allow_duplicate=True,
    )
    assert ok["status"] == "queued"


def test_module_gate_noarg_metadata_form(env):
    """DS-R2 P2-7：复刻内置件真实调用形态——无参 `instruments()` 的 universe
    模块必须过自动测试门（stub 的 TypeError 修复钉子）。"""
    from research import module_gate

    src = """
class Module:
    def __init__(self, params):
        pass

    def members(self, ctx):
        meta = ctx.gateway.metadata.instruments()  # 无参调用（与内置 category_filter 同形）
        from portfolio.slots.universe import UniverseMember

        return [UniverseMember(symbol=s) for s in sorted(meta)]
"""
    from research.modules import _load_python_module

    factory = _load_python_module(src)
    result = module_gate.run_module_gate(factory, slot="universe", params={})
    assert result["passed"], result["checks"]
