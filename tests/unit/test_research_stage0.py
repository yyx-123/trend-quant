"""阶段 0：台账 schema + 骨架校验 + 状态机 + 课题对象（详设 §6.3/§6.4/§6.6.1）。

验收判据（详设 §8 阶段 0）：不合法实验（无假设/多槽未标 compound/可变基准/
未注册模块/未注册评估方法）全被拒并留痕；合法实验入队；跳步操作被拒。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from portfolio.library import add_version_yaml, ensure_strategy
from portfolio.registry import ModuleSpec, fresh_registry
from research import evaluations, experiments, holdout, lifecycle, sessions, topics, verdict
from research.errors import IntakeRejected, LifecycleError, TopicError
from research.holdout import HoldoutError

pytestmark = pytest.mark.unit

STRATEGIES_DIR = Path(__file__).resolve().parents[2] / "src" / "portfolio" / "strategies"


@pytest.fixture
def registry():
    reg = fresh_registry()
    for slot in ("universe", "signal", "rank", "sizing", "portfolio_risk", "position_risk", "execution"):
        reg.register(ModuleSpec(slot=slot, name=f"dummy_{slot}", version=1, factory=lambda p: p))
    return reg


@pytest.fixture
def human_session(test_db):
    return sessions.get_or_create_default_human_session(test_db)


@pytest.fixture
def topic(test_db, human_session):
    return topics.create_topic(
        test_db, session_id=human_session["session_id"], title="止损选型", question="硬止损还是吊灯？"
    )


@pytest.fixture
def blank_base_version(test_db, registry):
    """blank-base 全 none 配置可在只含 dummy 的注册表下入库。"""
    yaml_text = (STRATEGIES_DIR / "blank_base.yaml").read_text(encoding="utf-8")
    ensure_strategy(test_db, "blank-base", name="空白基准", is_blank_base=True)
    return add_version_yaml(test_db, "blank-base", yaml_text, registry, created_by="human")


@pytest.fixture
def base_v1_version(test_db, registry):
    """一条非空白策略线（dummy 模块填充），改进型实验的合法基准。"""
    ensure_strategy(test_db, "base-v1", name="基准")
    cfg = (
        "name: base-v1\ndescription: x\n"
        "universe: {module: dummy_universe@1}\n"
        "signal: {module: dummy_signal@1}\n"
        "rank: {module: dummy_rank@1}\n"
        "sizing: {module: dummy_sizing@1}\n"
        "portfolio_risk: []\n"
        "position_risk: {module: dummy_position_risk@1}\n"
        "execution: {module: dummy_execution@1}\n"
    )
    return add_version_yaml(test_db, "base-v1", cfg, registry, created_by="human")


# ----------------------------------------------------------------------
# schema / 触发器
# ----------------------------------------------------------------------


def test_stage0_tables_exist(test_db):
    with test_db.connect() as conn:
        names = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','trigger')"
            ).fetchall()
        }
    for table in (
        "research_sessions",
        "research_topics",
        "research_experiments",
        "research_runs",
        "research_verdicts",
        "research_id_seq",
        "holdout_tokens",
        "module_drafts",
        "portfolio_strategies",
        "portfolio_strategy_versions",
        "engine_runs",
        "engine_orders",
        "engine_fills",
        "engine_unfilled",
        "engine_positions",
        "engine_daily_nav",
        "gateway_audit",
        "portfolio_live_lists",
    ):
        assert table in names, f"missing table {table}"


def test_experiments_append_only_trigger(test_db, topic, human_session, base_v1_version, registry):
    exp = _accepted_experiment(test_db, topic, human_session, base_v1_version, registry)
    with test_db.connect() as conn:
        # 内容字段禁改
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE research_experiments SET hypothesis = 'x' WHERE id = ?", (exp["id"],)
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE research_experiments SET spec_json = '{}' WHERE id = ?", (exp["id"],))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM research_experiments WHERE id = ?", (exp["id"],))
        # 白名单字段可改
        conn.execute("UPDATE research_experiments SET archived = 1 WHERE id = ?", (exp["id"],))


def test_verdicts_append_only_trigger(test_db, topic, human_session, base_v1_version, registry):
    exp = _accepted_experiment(test_db, topic, human_session, base_v1_version, registry)
    # R3C-P3-8：平台 verdict 只能挂在 evaluating/verdicted 上（新增守卫），
    # 夹具按真实状态机推进（此前夹具直接挂在 queued 上，靠"无守卫"通过）
    lifecycle.transition(test_db, exp["id"], "running")
    lifecycle.transition(test_db, exp["id"], "evaluating")
    v = verdict.insert_platform_verdict(
        test_db,
        experiment_id=exp["id"],
        baseline={},
        evidence={"d": 1},
        warnings=[],
        report={},
        suggested_verdict="confirmed",
    )
    with test_db.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE research_verdicts SET evidence_json = '{}' WHERE id = ?", (v["id"],)
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM research_verdicts WHERE id = ?", (v["id"],))


def test_strategy_versions_immutable(test_db, registry, blank_base_version):
    with test_db.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE portfolio_strategy_versions SET config_yaml = 'x' WHERE id = ?",
                (blank_base_version["id"],),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "DELETE FROM portfolio_strategy_versions WHERE id = ?",
                (blank_base_version["id"],),
            )


# ----------------------------------------------------------------------
# 骨架校验（创建卡控）
# ----------------------------------------------------------------------


def _spec(base_id, slot="signal", to="dummy_signal@1"):
    return {"base": base_id, "diff": [{"slot": slot, "to": to, "params": {}}]}


def _accepted_experiment(test_db, topic, human_session, base_v1_version, registry, **over):
    spec = over.pop("spec", _spec(base_v1_version["id"]))
    return experiments.propose_experiment(
        test_db,
        session_id=human_session["session_id"],
        title=over.pop("title", "合法实验"),
        topic_id=topic["id"],
        evaluation_module="portfolio_backtest@1",
        spec=spec,
        hypothesis=over.pop("hypothesis", "换一个信号模块后组合 Sharpe 会改善"),
        registry=registry,
        **over,
    )


def test_valid_experiment_is_queued(test_db, topic, human_session, base_v1_version, registry):
    exp = _accepted_experiment(test_db, topic, human_session, base_v1_version, registry)
    assert exp["status"] == "queued"
    assert exp["attempt_index"] == 1
    assert exp["subject_key"] == "base-v1"
    assert exp["topic_id"] == topic["id"]


def test_creation_type_requires_all_seven_slots(
    test_db, topic, human_session, blank_base_version, registry
):
    # blank-base 基准 = 创建型：七槽全填才合法
    full_diff = [
        {"slot": slot, "to": f"dummy_{slot}@1"}
        for slot in ("universe", "signal", "rank", "sizing", "position_risk", "execution")
    ] + [{"slot": "portfolio_risk", "to": ["dummy_portfolio_risk@1"]}]
    exp = experiments.propose_experiment(
        test_db,
        session_id=human_session["session_id"],
        title="创建型实验",
        topic_id=topic["id"],
        evaluation_module="portfolio_backtest@1",
        spec={"base": blank_base_version["id"], "diff": full_diff},
        hypothesis="52 周新高突破有入场 edge，策略化后跑赢买入持有",
        registry=registry,
    )
    assert exp["status"] == "queued"

    # 缺槽的创建型 → 拒
    with pytest.raises(IntakeRejected) as err:
        experiments.propose_experiment(
            test_db,
            session_id=human_session["session_id"],
            title="缺槽创建型",
            topic_id=topic["id"],
            evaluation_module="portfolio_backtest@1",
            spec={"base": blank_base_version["id"], "diff": full_diff[:-2]},
            hypothesis="缺两个槽也应被拒绝",
            registry=registry,
        )
    assert any("seven slots" in r for r in err.value.reasons)


def test_reject_empty_hypothesis(test_db, topic, human_session, base_v1_version, registry):
    with pytest.raises(IntakeRejected) as err:
        _accepted_experiment(
            test_db, topic, human_session, base_v1_version, registry, hypothesis="短"
        )
    assert any("hypothesis" in r for r in err.value.reasons)
    # 留痕：rejected_intake 也在台账里
    exp = lifecycle.get_experiment(test_db, err.value.experiment_id)
    assert exp["status"] == "rejected_intake"
    assert "hypothesis" in exp["reject_reason"]


def test_reject_unknown_evaluation_module(
    test_db, topic, human_session, base_v1_version, registry
):
    with pytest.raises(IntakeRejected) as err:
        experiments.propose_experiment(
            test_db,
            session_id=human_session["session_id"],
            title="未知评估模块",
            topic_id=topic["id"],
            evaluation_module="not_a_module@9",
            spec={},
            hypothesis="评估模块未注册应被拒绝",
            registry=registry,
        )
    assert any("evaluation module not registered" in r for r in err.value.reasons)


def test_reject_mutable_base(test_db, topic, human_session, registry):
    """base 必须是不可变版本引用 <id>@<version>（可变引用创建不了）。"""
    with pytest.raises(IntakeRejected) as err:
        experiments.propose_experiment(
            test_db,
            session_id=human_session["session_id"],
            title="可变基准",
            topic_id=topic["id"],
            evaluation_module="portfolio_backtest@1",
            spec={"base": "base-v1", "diff": [{"slot": "signal", "to": "dummy_signal@1"}]},
            hypothesis="base 不带版本号应被拒绝",
            registry=registry,
        )
    assert any("immutable version ref" in r for r in err.value.reasons)


def test_reject_base_not_in_library(test_db, topic, human_session, registry):
    with pytest.raises(IntakeRejected) as err:
        experiments.propose_experiment(
            test_db,
            session_id=human_session["session_id"],
            title="未入库基准",
            topic_id=topic["id"],
            evaluation_module="portfolio_backtest@1",
            spec={"base": "ghost@1", "diff": [{"slot": "signal", "to": "dummy_signal@1"}]},
            hypothesis="base 版本不在策略库应被拒绝",
            registry=registry,
        )
    assert any("not in library" in r for r in err.value.reasons)


def test_reject_unregistered_module(test_db, topic, human_session, base_v1_version, registry):
    with pytest.raises(IntakeRejected) as err:
        _accepted_experiment(
            test_db, topic, human_session, base_v1_version, registry,
            spec=_spec(base_v1_version["id"], to="ghost_module@1"),
        )
    assert any("module not registered" in r for r in err.value.reasons)


def test_reject_multi_slot_diff_without_compound(
    test_db, topic, human_session, base_v1_version, registry
):
    """改进型实验多槽 diff 未标 compound → 拒（单变量纪律；创建型除外）。"""
    version = base_v1_version
    multi = {
        "base": version["id"],
        "diff": [
            {"slot": "signal", "to": "dummy_signal@1"},
            {"slot": "rank", "to": "dummy_rank@1"},
        ],
    }
    with pytest.raises(IntakeRejected) as err:
        experiments.propose_experiment(
            test_db,
            session_id=human_session["session_id"],
            title="多槽未标 compound",
            topic_id=topic["id"],
            evaluation_module="portfolio_backtest@1",
            spec=multi,
            hypothesis="两个槽一起换必须显式 compound",
            registry=registry,
        )
    assert any("is_compound" in r for r in err.value.reasons)

    # 标了 compound + 原因 → 合法（置信度降档由 verdict 环节处理）
    exp = experiments.propose_experiment(
        test_db,
        session_id=human_session["session_id"],
        title="多槽标 compound",
        topic_id=topic["id"],
        evaluation_module="portfolio_backtest@1",
        spec={**multi, "is_compound": True, "compound_reason": "两槽存在交互效应"},
        hypothesis="两槽联动确有交互效应才复合",
        registry=registry,
    )
    assert exp["status"] == "queued"

    # 单槽改进型 → 合法
    exp2 = experiments.propose_experiment(
        test_db,
        session_id=human_session["session_id"],
        title="单槽改进型",
        topic_id=topic["id"],
        evaluation_module="portfolio_backtest@1",
        spec={"base": version["id"], "diff": [{"slot": "signal", "to": "dummy_signal@1"}]},
        hypothesis="单槽 diff 是标准改进型实验",
        registry=registry,
    )
    assert exp2["status"] == "queued"
    # attempt_index 沿研究线递增；rejected_intake 不计入
    assert exp2["attempt_index"] == exp["attempt_index"] + 1


def test_attempt_index_skips_rejected_intake(
    test_db, topic, human_session, base_v1_version, registry
):
    with pytest.raises(IntakeRejected):
        _accepted_experiment(
            test_db, topic, human_session, base_v1_version, registry, hypothesis="短"
        )
    exp = _accepted_experiment(test_db, topic, human_session, base_v1_version, registry)
    assert exp["attempt_index"] == 1  # 被拒的那次不计数


def test_experiment_requires_open_topic(test_db, human_session):
    with pytest.raises(TopicError):
        experiments.propose_experiment(
            test_db,
            session_id=human_session["session_id"],
            title="无课题",
            topic_id="T999",
            evaluation_module="portfolio_backtest@1",
            spec={},
            hypothesis="课题不存在应被拒绝",
        )


# ----------------------------------------------------------------------
# 状态机 + verdict
# ----------------------------------------------------------------------


def _running_experiment(test_db, topic, human_session, base_v1_version, registry):
    exp = _accepted_experiment(test_db, topic, human_session, base_v1_version, registry)
    lifecycle.transition(test_db, exp["id"], "running")
    return exp


def test_illegal_transitions_rejected(test_db, topic, human_session, base_v1_version, registry):
    exp = _accepted_experiment(test_db, topic, human_session, base_v1_version, registry)
    # 跳步：queued 不能直接 verdicted / evaluating
    with pytest.raises(LifecycleError):
        lifecycle.transition(test_db, exp["id"], "verdicted")
    with pytest.raises(LifecycleError):
        lifecycle.transition(test_db, exp["id"], "evaluating")
    # 终态不可再转移
    lifecycle.transition(test_db, exp["id"], "running")
    lifecycle.transition(test_db, exp["id"], "failed", error="boom")
    with pytest.raises(LifecycleError):
        lifecycle.transition(test_db, exp["id"], "running")
    final = lifecycle.get_experiment(test_db, exp["id"])
    assert final["status"] == "failed"
    assert final["error"] == "boom"


def test_confirm_verdict_flow(test_db, topic, human_session, base_v1_version, registry):
    exp = _running_experiment(test_db, topic, human_session, base_v1_version, registry)
    lifecycle.transition(test_db, exp["id"], "evaluating")
    v = verdict.insert_platform_verdict(
        test_db,
        experiment_id=exp["id"],
        baseline={"sharpe": 0.5},
        evidence={"delta_sharpe": 0.2},
        warnings=["single_regime"],
        report={"full": True},
        suggested_verdict="confirmed",
    )
    # reasoning 必填
    with pytest.raises(LifecycleError):
        verdict.confirm_verdict(
            test_db, experiment_id=exp["id"], final_verdict="confirmed",
            reasoning="", session_id=human_session["session_id"],
        )
    confirmed = verdict.confirm_verdict(
        test_db, experiment_id=exp["id"], final_verdict="inconclusive",  # 降级允许
        reasoning="高原判定为孤峰，降级处理", session_id=human_session["session_id"],
    )
    assert confirmed["final_verdict"] == "inconclusive"
    assert confirmed["suggested_verdict"] == "confirmed"
    exp = lifecycle.get_experiment(test_db, exp["id"])
    assert exp["status"] == "verdicted"
    # 不可重复确认
    with pytest.raises(LifecycleError):
        verdict.confirm_verdict(
            test_db, experiment_id=exp["id"], final_verdict="confirmed",
            reasoning="重复确认", session_id=human_session["session_id"],
        )
    assert v["warnings"] == ["single_regime"]


def test_confirm_cannot_upgrade(test_db, topic, human_session, base_v1_version, registry):
    exp = _running_experiment(test_db, topic, human_session, base_v1_version, registry)
    lifecycle.transition(test_db, exp["id"], "evaluating")
    verdict.insert_platform_verdict(
        test_db, experiment_id=exp["id"], baseline={}, evidence={}, warnings=[],
        report={}, suggested_verdict="rejected",
    )
    with pytest.raises(LifecycleError):
        verdict.confirm_verdict(
            test_db, experiment_id=exp["id"], final_verdict="confirmed",
            reasoning="想升级，不允许", session_id=human_session["session_id"],
        )


def test_module_allowed_finals_restriction(test_db, topic, human_session, registry):
    """评估模块 allowed_finals（如 distribution 固定 inconclusive）。"""
    restricted = evaluations.EvaluationModule(
        name="distribution", version=9, allowed_finals=("inconclusive",)
    )
    evaluations.register_evaluation(restricted)
    try:
        exp = experiments.propose_experiment(
            test_db,
            session_id=human_session["session_id"],
            title="分布实验",
            topic_id=topic["id"],
            evaluation_module="distribution@9",
            spec={"metric": "atr_pct"},
            hypothesis="ATR% 分布标定止损倍数用",
            registry=registry,
        )
        lifecycle.transition(test_db, exp["id"], "running")
        lifecycle.transition(test_db, exp["id"], "evaluating")
        verdict.insert_platform_verdict(
            test_db, experiment_id=exp["id"], baseline={}, evidence={}, warnings=[],
            report={}, suggested_verdict="inconclusive",
        )
        with pytest.raises(LifecycleError):
            verdict.confirm_verdict(
                test_db, experiment_id=exp["id"], final_verdict="confirmed",
                reasoning="distribution 不允许 confirmed", session_id=human_session["session_id"],
            )
    finally:
        evaluations.base._MODULES.pop("distribution@9", None)


def test_stale_evaluating_watchlist(test_db, topic, human_session, base_v1_version, registry):
    exp = _running_experiment(test_db, topic, human_session, base_v1_version, registry)
    lifecycle.transition(test_db, exp["id"], "evaluating")
    # started_at 是当下，7 天烂尾线扫不到；把 started_at 改老（白名单列）
    with test_db.connect() as conn:
        conn.execute(
            "UPDATE research_experiments SET started_at = '2020-01-01 00:00:00' WHERE id = ?",
            (exp["id"],),
        )
    stale = lifecycle.list_stale_evaluating(test_db, days=7)
    assert [e["id"] for e in stale] == [exp["id"]]


def test_archive_only_verdicted(test_db, topic, human_session, base_v1_version, registry):
    exp = _accepted_experiment(test_db, topic, human_session, base_v1_version, registry)
    with pytest.raises(LifecycleError):
        lifecycle.set_archived(test_db, exp["id"], True)


# ----------------------------------------------------------------------
# 课题
# ----------------------------------------------------------------------


def test_topic_conclude_requires_terminal_experiments(
    test_db, topic, human_session, base_v1_version, registry
):
    _accepted_experiment(test_db, topic, human_session, base_v1_version, registry)
    with pytest.raises(TopicError):
        topics.conclude_topic(test_db, topic_id=topic["id"], conclusion="还早")


def test_topic_conclude_reference_validation(
    test_db, topic, human_session, base_v1_version, registry
):
    exp = _running_experiment(test_db, topic, human_session, base_v1_version, registry)
    lifecycle.transition(test_db, exp["id"], "evaluating")
    verdict.insert_platform_verdict(
        test_db, experiment_id=exp["id"], baseline={}, evidence={}, warnings=[],
        report={}, suggested_verdict="confirmed",
    )
    verdict.confirm_verdict(
        test_db, experiment_id=exp["id"], final_verdict="confirmed",
        reasoning="机制成立", session_id=human_session["session_id"],
    )
    done = topics.conclude_topic(
        test_db, topic_id=topic["id"], conclusion="吊灯优于硬止损",
        experiment_ids=[exp["id"]], grade="supported",
    )
    assert done["status"] == "concluded"
    assert done["conclusion_grade"] == "supported"
    # 关题后不能再挂实验
    with pytest.raises(TopicError):
        _accepted_experiment(test_db, topic, human_session, base_v1_version, registry)


# ----------------------------------------------------------------------
# holdout
# ----------------------------------------------------------------------


def test_holdout_window_detection(test_db):
    assert holdout.window_touches_holdout(test_db, "2024-01-01", "2025-03-01") is True
    assert holdout.window_touches_holdout(test_db, "2015-01-01", "2024-12-31") is False
    assert holdout.window_touches_holdout(test_db, None, None) is False


def test_holdout_gate_enforced_requires_token(test_db, human_session):
    holdout.set_enforced(test_db, True)
    try:
        with pytest.raises(HoldoutError):
            holdout.check_window(test_db, start="2025-01-01", end="2025-06-01")
        token = holdout.grant_token(
            test_db, session_id=human_session["session_id"], purpose="首次放行"
        )
        # R23B-F1：token 必须点名实验才能消费（匿名消费 = 任何调用方都能用猜到的
        # id 自授权样本外访问，实测 MCP 侧曾可做到）。全局（未绑定）token 在
        # 明确点名实验时可用——这是人走 CLI 的路径。
        assert holdout.check_window(
            test_db, start="2025-01-01", end="2025-06-01",
            experiment_id="E-probe", token_id=token["id"],
        ) is True
        # 一次性：消费后失效
        with pytest.raises(HoldoutError):
            holdout.check_window(
                test_db, start="2025-01-01", end="2025-06-01",
                experiment_id="E-probe", token_id=token["id"],
            )
    finally:
        holdout.set_enforced(test_db, False)


def test_holdout_not_enforced_passes_through(test_db):
    holdout.set_enforced(test_db, False)  # 建设期语义；阶段 5 起默认生效
    try:
        assert holdout.check_window(test_db, start="2025-01-01", end="2025-06-01") is True
    finally:
        holdout.set_enforced(test_db, True)


def test_holdout_grant_requires_human(test_db):
    from research.errors import PermissionDenied

    ai = sessions.register_session(test_db, "ai", label="bot", channel="cli")
    with pytest.raises(PermissionDenied, match="human session"):
        holdout.grant_token(test_db, session_id=ai["session_id"], purpose="AI 越权")
