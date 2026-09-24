"""K3 + Deepseek 两轮的回归钉子（每项修复一条，删掉实现即失败）。

覆盖：holdout 端到端（发放→跑通→消费→留痕）、walk_forward 端到端、
context_filter 生效与 fail-loud、event_side=exit、停牌日 tail 卖出不崩溃、
promote 成功路径、引擎重复买入 fail-loud、孤儿收割、recompute 定论不劫持、
同向约束（verdict 与课题分级）、入口参数域/from 校验、长窗口三注记进
warnings、冻结门 force 不再绕过 + 顺延留痕。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from engine import Engine, EngineError, ExitOrderIntent, OrderIntent, TradabilityCard
from engine.store import EngineStore
from portfolio.backtester import run_backtest
from portfolio.registry import fresh_registry
from portfolio.seed import seed_default_library
from portfolio.slots import register_builtin_modules
from portfolio.strategy import parse_strategy_yaml
from research import experiments, holdout, lifecycle, sessions, topics, verdict
from research.api import ResearchService
from research.errors import IntakeRejected, LifecycleError, ResearchError, TopicError
from research.pipeline import run_experiment

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
    rng = np.random.default_rng(31)
    for i in range(5):
        closes = 12 * np.exp(np.cumsum(rng.normal(0.002 + i * 0.0005, 0.018, 400)))
        _write_symbol(test_db, f"K{i:03d}.SS", closes)
    versions = seed_default_library(test_db, registry)
    session = sessions.get_or_create_default_human_session(test_db)
    topic = topics.create_topic(
        test_db, session_id=session["session_id"], title="K3DS 课题", question="修复验证"
    )
    return {"db": test_db, "registry": registry, "versions": versions,
            "session": session, "topic": topic}


def _spec(env, window=("2022-06-01", "2023-12-29"), mul=2.0, **over):
    base = {
        "base": env["versions"]["base-v1"],
        "diff": [{"slot": "position_risk", "from": "hard_stop@1", "to": "hard_stop@1",
                  "params": {"atr_mul": mul}}],
        "window": list(window),
    }
    base.update(over)
    return base


# ----------------------------------------------------------------------
# K3-P1-2/DS-P1-2：holdout 端到端（发放→跑通→消费→留痕）
# ----------------------------------------------------------------------


def test_holdout_grant_to_run_end_to_end(env):
    db, reg = env["db"], env["registry"]
    holdout.set_enforced(db, True)
    try:
        exp = experiments.propose_experiment(
            db, session_id=env["session"]["session_id"], title="样本外实验",
            topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
            spec=_spec(env, window=("2023-06-01", "2023-12-29")),  # 不触碰 holdout
            hypothesis="先跑一个样本内实验占位",
        )
        # 无 token 触碰 holdout → failed
        exp2 = experiments.propose_experiment(
            db, session_id=env["session"]["session_id"], title="无 token 摸 holdout",
            topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
            spec=_spec(env, window=("2023-06-01", "2025-06-30")),  # 触碰
            hypothesis="无 token 触碰样本外应失败", allow_duplicate=True,
        )
        result = run_experiment(db, exp2["id"], registry=reg)
        assert result["status"] == "failed"

        # 发 token → 同窗口跑通 → token 已消费 + holdout_touched 落账
        # （DS-R2 §4-5 收窄：自动带出只认**绑定本实验**的 token；全局 token
        # 须显式透传——两向钉见 test_review_r3.py）
        exp3 = experiments.propose_experiment(
            db, session_id=env["session"]["session_id"], title="带 token 摸 holdout",
            topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
            spec=_spec(env, window=("2023-06-01", "2025-06-30"), mul=1.8),
            hypothesis="带 token 应跑通且留痕", allow_duplicate=True,
        )
        token = holdout.grant_token(
            db, session_id=env["session"]["session_id"],
            purpose="放行样本外", experiment_id=exp3["id"],
        )
        v = run_experiment(db, exp3["id"], registry=reg)
        assert v.get("suggested_verdict") is not None  # 跑通了
        after = holdout.get_token(db, token["id"])
        assert after["consumed_at"] is not None
        assert lifecycle.get_experiment(db, exp3["id"])["holdout_touched"] == 1
        assert any(w == "holdout_touched" for w in v["warnings"])
    finally:
        holdout.set_enforced(db, False)


# ----------------------------------------------------------------------
# DS-P1-3：walk_forward 端到端不崩
# ----------------------------------------------------------------------


def test_walk_forward_end_to_end(env):
    db, reg = env["db"], env["registry"]
    exp = experiments.propose_experiment(
        db, session_id=env["session"]["session_id"], title="滚动样本外",
        topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
        spec=_spec(env, window_mode="walk_forward", n_folds=2),
        hypothesis="滚动样本外应跑通（此前必崩）",
    )
    v = run_experiment(db, exp["id"], registry=reg)
    assert v.get("suggested_verdict") is not None
    wf = v["evidence"].get("walk_forward")
    assert wf is not None and wf["n_folds"] == 2
    assert len(wf["folds"]) == 2
    # 拼接序列带真实日期（不再是 oos-i 占位）
    assert v["report"]["full_run_report"] is None  # 无单 run 报告（拼接路径）
    assert v["evidence"]["experiment_summary"]["turnover"] is None  # 显式不可用


# ----------------------------------------------------------------------
# DS-P1-4 + K3-P2-3：context_filter 生效 + event_side=exit
# ----------------------------------------------------------------------


def test_event_context_filter_actually_filters(env):
    db, reg = env["db"], env["registry"]
    base_spec = {
        "event": "ma_cross@1(n=20)", "horizons": [10],
        "universe": [f"K{i:03d}.SS" for i in range(5)] + ["510500.SS"],
        "expect": "positive", "window": ["2022-06-01", "2023-12-29"],
    }
    # 需 benchmark 标的数据：写一条
    _write_symbol(db, "510500.SS", list(15 * np.exp(np.cumsum(np.random.default_rng(1).normal(0.001, 0.01, 400)))))
    e_all = experiments.propose_experiment(
        db, session_id=env["session"]["session_id"], title="无条件",
        topic_id=env["topic"]["id"], evaluation_module="event_study@1",
        spec=base_spec, hypothesis="无条件版本假设陈述足够长", allow_duplicate=True,
    )
    v_all = run_experiment(db, e_all["id"], registry=reg)
    e_bull = experiments.propose_experiment(
        db, session_id=env["session"]["session_id"], title="牛市 regime",
        topic_id=env["topic"]["id"], evaluation_module="event_study@1",
        spec={**base_spec, "context_filter": {"benchmark": "510500.SS", "rule": "close_above_ma200"}},
        hypothesis="牛市条件版本假设陈述足够长", allow_duplicate=True,
    )
    v_bull = run_experiment(db, e_bull["id"], registry=reg)
    n_all = v_all["evidence"]["per_horizon"]["10"]["n_events"]
    n_bull = v_bull["evidence"]["per_horizon"]["10"]["n_events"]
    assert n_all > 0 and n_bull > 0
    assert n_bull < n_all  # 过滤生效：牛市子集 < 全集


def test_event_side_exit_events_reachable(env):
    """K3-P2-3：exit 侧事件可研究（死叉类研究的可达性钉子）。"""
    db, reg = env["db"], env["registry"]
    exp = experiments.propose_experiment(
        db, session_id=env["session"]["session_id"], title="死叉研究",
        topic_id=env["topic"]["id"], evaluation_module="event_study@1",
        spec={"event": "ma_cross@1(n=20)", "horizons": [10], "event_side": "exit",
              "universe": [f"K{i:03d}.SS" for i in range(5)],
              "expect": "negative", "window": ["2022-06-01", "2023-12-29"]},
        hypothesis="死叉后的前瞻收益显著为负的假设",
    )
    v = run_experiment(db, exp["id"], registry=reg)
    assert v["evidence"]["per_horizon"]["10"]["n_events"] > 0  # exit 事件产出了


def test_event_transition_matrix_fails_loud(env):
    """未实现字段 fail-loud（不再静默忽略）。"""
    db, reg = env["db"], env["registry"]
    exp = experiments.propose_experiment(
        db, session_id=env["session"]["session_id"], title="迁移矩阵",
        topic_id=env["topic"]["id"], evaluation_module="event_study@1",
        spec={"event": "ma_cross@1(n=20)", "horizons": [10], "transition_matrix": True,
              "universe": [f"K{i:03d}.SS" for i in range(2)],
              "window": ["2022-06-01", "2023-12-29"]},
        hypothesis="迁移矩阵未实现应 fail-loud 的假设",
    )
    result = run_experiment(db, exp["id"], registry=reg)
    assert result["status"] == "failed"
    assert "transition_matrix" in (result["error"] or "")


# ----------------------------------------------------------------------
# K3-P1-1/DS-P2-2：停牌日 tail 卖出不崩溃
# ----------------------------------------------------------------------


def test_tail_sell_on_suspended_day_records_unfilled(test_db):
    """持仓停牌日触发 tail 离场 → unfilled(suspended)，run 不炸。"""
    engine = Engine(profile="cn_stock", initial_cash=50_000)
    sym = "510300.SS"
    day1, day2 = date(2024, 3, 11), date(2024, 3, 12)
    engine.begin_day(day=day1)
    engine.buy(
        intent=OrderIntent(symbol=sym, decision_date=day1, intent_type="quantity", value=1000),
        day=day1, bar_close=10.0, card=TradabilityCard(), asset_type="etf",
        slippage_base=0.002, slippage_tail=0.001,
    )
    engine.begin_day(day=day2)
    # 停牌日（无 bar + suspended 卡）tail 卖出：应落 unfilled 不抛异常
    result = engine.sell(
        intent=ExitOrderIntent(symbol=sym, decision_date=day2, fill_mode="tail",
                               source="stop", reason="time_stop"),
        day=day2, card=TradabilityCard(suspended=True), asset_type="etf",
        bar_close=None,
    )
    assert result.status == "unfilled"
    assert result.unfilled.reason == "suspended"
    assert engine.account.positions[sym].quantity == 1000  # 持仓还在


def test_suspended_tail_sell_inside_run_continues(test_db, registry):
    """集成钉：time_stop 到期日恰停牌 → run 完成（不 failed）+ unfilled 落账。"""
    n = 40
    days = pd.bdate_range("2023-01-02", periods=n)
    valid_mask = [True] * 20 + [False] * 5 + [True] * 15  # 中段停牌 5 日
    valid_days = days[valid_mask]
    df = pd.DataFrame({
        "time": valid_days, "open": 10.0, "high": 10.05, "low": 9.95,
        "close": 10.0, "volume": 1e6, "amount": 1e7,
    })
    test_db.save_market_data("H001.SS", df, price_mode="qfq")
    test_db.save_market_data("H001.SS", df, price_mode="raw")
    test_db.save_instrument_metadata([{
        "symbol": "H001.SS", "name": "H", "category_l1": "T", "category_l2": "T2",
        "category_l3": "", "enabled": 1, "asset_type": "etf",
    }])
    # 伴随标的：全期有 bar——停牌日因此存在于面板日期轴（H001 当日无 bar）
    full = pd.DataFrame({
        "time": days, "open": 5.0, "high": 5.05, "low": 4.95,
        "close": 5.0, "volume": 1e6, "amount": 5e6,
    })
    test_db.save_market_data("H999.SS", full, price_mode="qfq")
    test_db.save_market_data("H999.SS", full, price_mode="raw")
    test_db.save_instrument_metadata([{
        "symbol": "H999.SS", "name": "HH", "category_l1": "T", "category_l2": "T3",
        "category_l3": "", "enabled": 1, "asset_type": "etf",
    }])
    cfg = parse_strategy_yaml("""
name: t-susp
universe: {module: static_list@1, params: {symbols: [H001.SS, H999.SS]}}
signal: {module: always_entry@1, params: {}}
rank: {module: by_freshness@1, params: {}}
sizing: {module: fixed_slots@1, params: {slots: 1}}
portfolio_risk: []
position_risk: {module: time_stop@1, params: {max_days: 23}}
execution: {module: tail_session@1, params: {slippage_base: 0.0, slippage_tail: 0.0}}
""", registry)
    # 窗口跨过停牌段（尾部 10 日无 bar）——time_stop 到期日落在停牌区
    result = run_backtest(
        test_db, config=cfg, registry=registry,
        start=valid_days[1].date(), end=days[-1].date(),
        initial_capital=100_000, store=False,
    )
    assert result["daily_nav"]  # run 完成
    # 停牌日的 tail 卖出落 unfilled(suspended) 而不是炸掉 run
    assert any(u["reason"] == "suspended" for u in result["unfilled"])


# ----------------------------------------------------------------------
# DS-P1-5：promote 成功路径
# ----------------------------------------------------------------------


def test_promote_success_path(env):
    """confirmed 实验晋升 → 库里出现带血缘的新版本。"""
    db, reg = env["db"], env["registry"]
    service = ResearchService(db, registry=reg, topics_dir=db.db_path.parent / "t")
    from portfolio import library

    library.ensure_strategy(db, "promo-line", name="promo")
    version = library.add_version_yaml(db, "promo-line", """
name: promo
universe: {module: category_filter@1, params: {asset_type: all}}
signal: {module: macd_cross@1, params: {}}
rank: {module: by_freshness@1, params: {}}
sizing: {module: fixed_slots@1, params: {slots: 3}}
portfolio_risk: []
position_risk: {module: hard_stop@1, params: {atr_mul: 1.5}}
execution: {module: tail_session@1, params: {}}
""", reg)
    exp = experiments.propose_experiment(
        db, session_id=env["session"]["session_id"], title="待晋升实验",
        topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
        spec={"base": version["id"],
              "diff": [{"slot": "position_risk", "from": "hard_stop@1", "to": "hard_stop@1",
                        "params": {"atr_mul": 1.0}}],
              "window": ["2022-06-01", "2023-12-29"]},
        hypothesis="待晋升实验的假设陈述",
    )
    lifecycle.transition(db, exp["id"], "running")
    lifecycle.transition(db, exp["id"], "evaluating")
    verdict.insert_platform_verdict(
        db, experiment_id=exp["id"], baseline={}, evidence={}, warnings=[],
        report={}, suggested_verdict="confirmed",
    )
    verdict.confirm_verdict(
        db, experiment_id=exp["id"], final_verdict="confirmed",
        reasoning="证据成立", session_id=env["session"]["session_id"],
    )
    version_new = service.promote_to_library(
        experiment_id=exp["id"], strategy_id="promoted-line",
        session_id=env["session"]["session_id"],
    )
    assert version_new["experiment_id"] == exp["id"]
    assert version_new["parent_version_id"] == version["id"]
    assert library.get_version(db, version_new["id"]) is not None


# ----------------------------------------------------------------------
# DS-P2-1：引擎重复买入 fail-loud
# ----------------------------------------------------------------------


def test_engine_duplicate_buy_fails_loud(test_db):
    engine = Engine(profile="cn_stock", initial_cash=50_000)
    sym = "510300.SS"
    engine.begin_day(day=date(2024, 3, 11))
    engine.buy(
        intent=OrderIntent(symbol=sym, decision_date=date(2024, 3, 11),
                           intent_type="quantity", value=1000),
        day=date(2024, 3, 11), bar_close=10.0, card=TradabilityCard(),
        asset_type="etf", slippage_base=0.002, slippage_tail=0.001,
    )
    with pytest.raises(EngineError, match="already-held"):
        engine.buy(
            intent=OrderIntent(symbol=sym, decision_date=date(2024, 3, 11),
                               intent_type="quantity", value=1000),
            day=date(2024, 3, 11), bar_close=10.0, card=TradabilityCard(),
            asset_type="etf", slippage_base=0.002, slippage_tail=0.001,
        )


# ----------------------------------------------------------------------
# K3-P2-4/DS-P2-6c：孤儿收割
# ----------------------------------------------------------------------


def test_startup_sweep_marks_orphans(test_db, registry):
    seed_default_library(test_db, registry)
    session = sessions.get_or_create_default_human_session(test_db)
    topic = topics.create_topic(
        test_db, session_id=session["session_id"], title="收割", question="孤儿实验"
    )
    from portfolio import library

    library.ensure_strategy(test_db, "sweep-line", name="sweep")
    version = library.add_version_yaml(test_db, "sweep-line", """
name: sweep
universe: {module: category_filter@1, params: {asset_type: all}}
signal: {module: macd_cross@1, params: {}}
rank: {module: by_freshness@1, params: {}}
sizing: {module: fixed_slots@1, params: {slots: 3}}
portfolio_risk: []
position_risk: {module: hard_stop@1, params: {atr_mul: 1.5}}
execution: {module: tail_session@1, params: {}}
""", registry)
    exp = experiments.propose_experiment(
        test_db, session_id=session["session_id"], title="永挂 running",
        topic_id=topic["id"], evaluation_module="portfolio_backtest@1",
        spec={"base": version["id"],
              "diff": [{"slot": "position_risk", "to": "hard_stop@1", "params": {"atr_mul": 1.0}}]},
        hypothesis="进程死亡后的 running 孤儿应被收割",
    )
    lifecycle.transition(test_db, exp["id"], "running")
    store = EngineStore(test_db, "R-orphan-1")
    store.begin_run(kind="backtest", strategy_ref="x", config_hash="h",
                    resolved_config_yaml="name: x", data_version=1)
    swept = lifecycle.mark_interrupted_research_runs(test_db)
    assert swept["experiments"] == 1 and swept["engine_runs"] == 1
    assert lifecycle.get_experiment(test_db, exp["id"])["status"] == "failed"
    assert EngineStore.get_run(test_db, "R-orphan-1")["status"] == "failed"


# ----------------------------------------------------------------------
# K3-P2-6/DS-P2-3：recompute 定论不劫持
# ----------------------------------------------------------------------


def test_recompute_does_not_hijack_canonical_verdict(env):
    db, reg = env["db"], env["registry"]
    from portfolio.slots.position_risk import HardStopModule
    from portfolio.registry import ModuleSpec

    from portfolio import library

    library.ensure_strategy(db, "rc-line", name="rc")
    version = library.add_version_yaml(db, "rc-line", """
name: rc
universe: {module: category_filter@1, params: {asset_type: all}}
signal: {module: macd_cross@1, params: {}}
rank: {module: by_freshness@1, params: {}}
sizing: {module: fixed_slots@1, params: {slots: 3}}
portfolio_risk: []
position_risk: {module: hard_stop@1, params: {atr_mul: 1.5}}
execution: {module: tail_session@1, params: {}}
""", reg)
    exp = experiments.propose_experiment(
        db, session_id=env["session"]["session_id"], title="被复核实验",
        topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
        spec={"base": version["id"],
              "diff": [{"slot": "position_risk", "from": "hard_stop@1", "to": "hard_stop@1",
                        "params": {"atr_mul": 1.0}}],
              "window": ["2022-06-01", "2023-12-29"]},
        hypothesis="被复核实验的假设陈述", allow_duplicate=True,
    )
    v = run_experiment(db, exp["id"], registry=reg)
    verdict.confirm_verdict(
        db, experiment_id=exp["id"],
        final_verdict=v["suggested_verdict"],  # 落定平台建议（可能是 inconclusive）
        reasoning="初判落定", session_id=env["session"]["session_id"],
    )
    canonical = verdict.latest_verdict(db, exp["id"])

    reg.register(ModuleSpec(slot="position_risk", name="hard_stop", version=2,
                            factory=HardStopModule, kind="builtin",
                            params_schema={
                                "atr_mul": {"type": "number", "default": 1.5, "min": 0.1, "max": 10},
                                "atr_period": {"type": "integer", "default": 20, "min": 5, "max": 120},
                            }))
    service = ResearchService(db, registry=reg, topics_dir=db.db_path.parent / "t")
    result = service.recompute_campaign(
        old_module_ref="hard_stop@1", new_module_ref="hard_stop@2",
        session_id=env["session"]["session_id"],
    )
    assert len(result["recomputed"]) == 1
    # 定论不被复核稿劫持
    latest = verdict.latest_verdict(db, exp["id"])
    assert latest["id"] == canonical["id"]
    assert latest["final_verdict"] == canonical["final_verdict"]
    # 复核稿存在且可反查
    verdicts = verdict.list_verdicts(db, exp["id"])
    assert len(verdicts) == 2
    assert any(v["supersedes"] == exp["id"] for v in verdicts)


def test_recompute_requires_human(env):
    ai = sessions.register_session(env["db"], "ai", label="bot", channel="cli")
    service = ResearchService(env["db"], registry=env["registry"],
                              topics_dir=env["db"].db_path.parent / "t")
    from research.errors import PermissionDenied

    with pytest.raises(PermissionDenied):
        service.recompute_campaign(
            old_module_ref="hard_stop@1", new_module_ref="hard_stop@2",
            session_id=ai["session_id"],
        )


# ----------------------------------------------------------------------
# K3-P2-7：同向约束（verdict 与课题分级）
# ----------------------------------------------------------------------


def test_verdict_direction_flip_blocked(env):
    """suggested=confirmed → final=rejected 拒绝（方向翻转不是降级）。"""
    db, reg = env["db"], env["registry"]
    from portfolio import library

    library.ensure_strategy(db, "flip-line", name="flip")
    version = library.add_version_yaml(db, "flip-line", """
name: flip
universe: {module: category_filter@1, params: {asset_type: all}}
signal: {module: macd_cross@1, params: {}}
rank: {module: by_freshness@1, params: {}}
sizing: {module: fixed_slots@1, params: {slots: 3}}
portfolio_risk: []
position_risk: {module: hard_stop@1, params: {atr_mul: 1.5}}
execution: {module: tail_session@1, params: {}}
""", reg)
    exp = experiments.propose_experiment(
        db, session_id=env["session"]["session_id"], title="方向翻转",
        topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
        spec={"base": version["id"],
              "diff": [{"slot": "position_risk", "to": "hard_stop@1", "params": {"atr_mul": 1.0}}]},
        hypothesis="confirmed 不得翻成 rejected 的假设", allow_duplicate=True,
    )
    lifecycle.transition(db, exp["id"], "running")
    lifecycle.transition(db, exp["id"], "evaluating")
    verdict.insert_platform_verdict(
        db, experiment_id=exp["id"], baseline={}, evidence={}, warnings=[],
        report={}, suggested_verdict="confirmed",
    )
    with pytest.raises(LifecycleError, match="downgrade only"):
        verdict.confirm_verdict(
            db, experiment_id=exp["id"], final_verdict="rejected",
            reasoning="想翻转方向", session_id=env["session"]["session_id"],
        )
    # confirmed → inconclusive 是真降级，允许
    ok = verdict.confirm_verdict(
        db, experiment_id=exp["id"], final_verdict="inconclusive",
        reasoning="降级保留", session_id=env["session"]["session_id"],
    )
    assert ok["final_verdict"] == "inconclusive"


def test_topic_grade_direction_flip_blocked(env):
    """课题分级 supported → refuted 平级翻转拒绝。"""
    db = env["db"]
    t = topics.create_topic(
        db, session_id=env["session"]["session_id"], title="分级", question="方向翻转"
    )
    # 一个 confirmed 实验 → 平台建议 supported
    from portfolio import library

    library.ensure_strategy(db, "grade-line", name="grade")
    version = library.add_version_yaml(db, "grade-line", """
name: grade
universe: {module: category_filter@1, params: {asset_type: all}}
signal: {module: macd_cross@1, params: {}}
rank: {module: by_freshness@1, params: {}}
sizing: {module: fixed_slots@1, params: {slots: 3}}
portfolio_risk: []
position_risk: {module: hard_stop@1, params: {atr_mul: 1.5}}
execution: {module: tail_session@1, params: {}}
""", env["registry"])
    exp = experiments.propose_experiment(
        db, session_id=env["session"]["session_id"], title="分级实验",
        topic_id=t["id"], evaluation_module="portfolio_backtest@1",
        spec={"base": version["id"],
              "diff": [{"slot": "position_risk", "to": "hard_stop@1", "params": {"atr_mul": 1.0}}]},
        hypothesis="课题分级翻转的假设陈述", allow_duplicate=True,
    )
    lifecycle.transition(db, exp["id"], "running")
    lifecycle.transition(db, exp["id"], "evaluating")
    verdict.insert_platform_verdict(
        db, experiment_id=exp["id"], baseline={}, evidence={}, warnings=[],
        report={}, suggested_verdict="confirmed",
    )
    verdict.confirm_verdict(
        db, experiment_id=exp["id"], final_verdict="confirmed",
        reasoning="成立", session_id=env["session"]["session_id"],
    )
    with pytest.raises(TopicError, match="exceeds platform suggestion"):
        topics.conclude_topic(db, topic_id=t["id"], conclusion="翻成 refuted",
                              grade="refuted")


# ----------------------------------------------------------------------
# DS-P2-4：入口参数域 + from 校验
# ----------------------------------------------------------------------


def test_intake_param_domain_and_from_validated(env):
    db, reg = env["db"], env["registry"]
    # 参数越界（atr_mul 负值）→ 入口即拒，不占尝试名额入账即拒
    with pytest.raises(IntakeRejected, match="min"):
        experiments.propose_experiment(
            db, session_id=env["session"]["session_id"], title="参数越界",
            topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
            spec=_spec(env, mul=-5),
            hypothesis="负止损倍数应入口即拒的陈述",
            registry=reg,
        )
    # from 与 base 实际模块不符 → 拒
    bad_from = _spec(env)
    bad_from["diff"][0]["from"] = "chandelier@1"  # base 实为 hard_stop@1
    with pytest.raises(IntakeRejected, match="does not match base config"):
        experiments.propose_experiment(
            db, session_id=env["session"]["session_id"], title="from 不符",
            topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
            spec=bad_from, hypothesis="from 写错来源应被拒的陈述", allow_duplicate=True,
            registry=reg,
        )


# ----------------------------------------------------------------------
# DS-P2-5：长窗口三注记进 verdict warnings
# ----------------------------------------------------------------------


def test_long_window_three_notes_in_warnings(env):
    db, reg = env["db"], env["registry"]
    exp = experiments.propose_experiment(
        db, session_id=env["session"]["session_id"], title="长窗口注记",
        topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
        spec=_spec(env, window=("2019-01-01", "2023-12-29"), mul=1.4),
        hypothesis="长窗口实验应带三注记的假设", allow_duplicate=True,
    )
    v = run_experiment(db, exp["id"], registry=reg)
    warnings = v["warnings"]
    assert any("coverage_note" in w for w in warnings)
    assert any("fee_era_mismatch" in w for w in warnings)
    assert any("survivorship_weighting" in w for w in warnings)
