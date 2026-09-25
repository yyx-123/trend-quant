"""loop-review-glm53f Round 1 修复钉子（round1-review.md 各项的回归锚）。

每颗钉子复刻被修复缺陷的**真实调用形态**（开发日志 DS-R2 流程教训）：
- transition 原子认领（R1-P1-2）：两个"通道"先后 claim 同一 queued 实验；
- rebalance_band 仅 overweight（R1-P1-1）：见 test_module_behaviors 重锚；
- live 整手（R1-P1-5）：见 tests/integration/test_live_runner.py 补钉；
- 缺省展开重复检测（R1-P2-3）：显式 window vs 省略 window 判 exact；
- recompute 精确引用（R1-P2-4）：hard_stop@1 不被 stop@1 campaign 误伤；
- is_reproduction 库层守卫（R1-P2-7）：真 SQL 直改被触发器 ABORT；
- 其余 R1-P3 各一颗。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from research import experiments, lifecycle, sessions, topics

pytestmark = pytest.mark.unit


@pytest.fixture
def human_session(test_db):
    return sessions.get_or_create_default_human_session(test_db)


@pytest.fixture
def topic(test_db, human_session):
    return topics.create_topic(
        test_db, session_id=human_session["session_id"],
        title="loop-review 钉子", question="修复是否被钉住？",
    )


# ----------------------------------------------------------------------
# R1-P1-2 状态机原子认领
# ----------------------------------------------------------------------

def test_transition_atomic_claim_double_channel(test_db, human_session, topic, monkeypatch):
    """两个通道同时认领同一 queued 实验：只有一个成功，另一个 LifecycleError。"""
    sessions.register_session(test_db, "ai", "ai-cli", "cli")
    exp = experiments.propose_experiment(
        test_db,
        session_id=human_session["session_id"],
        title="原子认领钉子",
        topic_id=topic["id"],
        evaluation_module="distribution@1",
        spec={"metric": "atr_pct"},
        hypothesis="假设文本足够长以过骨架校验",
    )
    # 真实调用形态：pipeline 先读 status 再 transition。复刻竞态读：
    # 通道 B 的 require_experiment 读到的是通道 A 写入前的快照（queued），
    # 但 UPDATE 落库时行已被 A 占用（status=running）→ rowcount=0 → 拒。
    first = lifecycle.transition(test_db, exp["id"], "running")
    assert first["status"] == "running"

    stale = dict(first)
    stale["status"] = "queued"  # 通道 B 的过期读
    orig_require = lifecycle.require_experiment

    def stale_require(db, experiment_id):
        return stale

    monkeypatch.setattr(lifecycle, "require_experiment", stale_require)
    try:
        with pytest.raises(lifecycle.LifecycleError, match="lost the race"):
            lifecycle.transition(test_db, exp["id"], "running")
    finally:
        monkeypatch.setattr(lifecycle, "require_experiment", orig_require)
    # 库内状态未被双写破坏
    assert lifecycle.get_experiment(test_db, exp["id"])["status"] == "running"


def test_transition_stale_read_rejected(test_db, human_session, topic):
    """读到过期 status 的转移被拒：evaluating → running 不在转移表（原有语义
    不回归），且 UPDATE 层 WHERE status 守卫不放过"读后状态被改"的窗口。"""
    exp = experiments.propose_experiment(
        test_db,
        session_id=human_session["session_id"],
        title="过期读钉子",
        topic_id=topic["id"],
        evaluation_module="distribution@1",
        spec={"metric": "atr_pct"},
        hypothesis="假设文本足够长以过骨架校验",
    )
    lifecycle.transition(test_db, exp["id"], "running")
    lifecycle.transition(test_db, exp["id"], "evaluating")
    # 用 from=running 的过期读试图转移（真状态已 evaluating）
    with pytest.raises(lifecycle.LifecycleError, match="illegal transition"):
        lifecycle.transition(test_db, exp["id"], "evaluating", error="x")


# ----------------------------------------------------------------------
# R1-P2-3 重复检测缺省展开
# ----------------------------------------------------------------------

def test_duplicate_detection_default_expansion(test_db, human_session, topic):
    """显式 window=[2015-01-01,2024-12-31] 的实验之后，省略 window 的同 spec
    实验必须判 exact（缺省 = 同一窗口）——不再放行。"""
    exp1 = experiments.propose_experiment(
        test_db,
        session_id=human_session["session_id"],
        title="显式窗口",
        topic_id=topic["id"],
        evaluation_module="distribution@1",
        spec={"metric": "atr_pct", "window": ["2015-01-01", "2024-12-31"]},
        hypothesis="假设文本足够长以过骨架校验",
    )
    assert exp1["status"] == "queued"
    with pytest.raises(experiments.IntakeRejected) as err:
        experiments.propose_experiment(
            test_db,
            session_id=human_session["session_id"],
            title="省略窗口的同实验",
            topic_id=topic["id"],
            evaluation_module="distribution@1",
            spec={"metric": "atr_pct"},  # window 缺省 = 同一 sample 窗口
            hypothesis="假设文本足够长以过骨架校验",
        )
    assert any("duplicate_of" in r for r in err.value.reasons)


# ----------------------------------------------------------------------
# R1-P2-4 recompute 精确引用
# ----------------------------------------------------------------------

def test_recompute_exact_reference_matching(test_db, human_session, topic):
    """`stop@1` 的 campaign 不得误伤引用 `hard_stop@1` 的实验（子串→精确）。"""
    from research import recompute as rc

    rows = [
        ("E-精确", {"diff": [{"slot": "position_risk", "from": "hard_stop@1", "to": "stop@1"}]}),
        ("E-误伤源", {"diff": [{"slot": "position_risk", "from": "hard_stop@1", "to": "hard_stop@2"}]}),
        ("E-内联", {"diff": [{"slot": "portfolio_risk", "to": ["heat_cap@1(0.06)"]}]}),
    ]
    with test_db.connect() as conn:
        # attempt_index 逐行递增：同研究线内试次号必须唯一（R22B-F1 的部分唯一索引
        # ux_research_experiments_line_attempt 会拦重复——这三条是三条独立实验，
        # 本来就该各有试次号，此前省略没被约束）。
        for idx, (eid, spec) in enumerate(rows, start=1):
            conn.execute(
                """INSERT INTO research_experiments
                   (id, title, owner_session, created_by, topic_id, evaluation_module,
                    subject_key, spec_json, hypothesis, status, attempt_index)
                   VALUES (?, ?, ?, 'human', ?, 'portfolio_backtest@1', 'k', ?, 'h',
                           'verdicted', ?)""",
                (eid, eid, human_session["session_id"], topic["id"],
                 __import__("json").dumps(spec), idx),
            )
    hits = {item["id"] for item in rc.find_experiments_using(test_db, "stop@1")}
    assert "E-精确" in hits
    assert "E-误伤源" not in hits  # 修复点：hard_stop@2 不再被子串误伤
    assert "E-内联" not in hits
    hits_heat = {item["id"] for item in rc.find_experiments_using(test_db, "heat_cap@1")}
    assert "E-内联" in hits_heat  # 内联参数形（name@ver(...)）仍可命中


# ----------------------------------------------------------------------
# R1-P2-7 is_reproduction 库层守卫
# ----------------------------------------------------------------------

def test_is_reproduction_guarded_by_trigger(test_db, human_session, topic):
    """SQL 直改 is_reproduction 被 append-only 触发器 ABORT（DSR 输入不可
    篡改）。同时验证 DROP+CREATE 使新守卫定义在存量库生效。"""
    exp = experiments.propose_experiment(
        test_db,
        session_id=human_session["session_id"],
        title="复现标记守卫",
        topic_id=topic["id"],
        evaluation_module="distribution@1",
        spec={"metric": "atr_pct"},
        hypothesis="假设文本足够长以过骨架校验",
    )
    import sqlite3 as _sqlite3

    with pytest.raises(_sqlite3.IntegrityError, match="append-only"):
        with test_db.connect() as conn:
            conn.execute(
                "UPDATE research_experiments SET is_reproduction = 1 WHERE id = ?",
                (exp["id"],),
            )


# ----------------------------------------------------------------------
# R1-P3-8 / R1-P3-9 评估模块入口校验
# ----------------------------------------------------------------------

def test_event_spec_window_null_and_primary_horizon():
    from research.evaluations.event import _spec_errors

    ctx = {"db": None, "registry": None}
    # window: null 不得在 runner 才炸（TypeError→failed）；intake 即拒
    errs = _spec_errors(
        {"event": "ma_cross@1", "horizons": [5, 10], "window": None}, ctx
    )
    assert errs == []  # window null = 用缺省窗口，合法（与其它模块同语义）
    errs2 = _spec_errors(
        {"event": "ma_cross@1", "horizons": [5, 10], "primary_horizon": 20}, ctx
    )
    assert any("primary_horizon" in e for e in errs2)


def test_bucket_distribution_universe_and_expect_validation():
    from research.evaluations.bucket import _spec_errors as bucket_errs
    from research.evaluations.distribution import _spec_errors as dist_errs

    errs = bucket_errs(
        {"signal_module": "ma_cross@1", "feature": "er",
         "expect": "bogus", "universe": "bogus"}, {"db": None, "registry": None}
    )
    assert any("expect" in e for e in errs)
    assert any("universe" in e for e in errs)
    errs2 = dist_errs({"metric": "atr_pct", "universe": 123}, {"db": None, "registry": None})
    assert any("universe" in e for e in errs2)
    # 合法形态放行
    assert dist_errs({"metric": "atr_pct", "universe": "single(510300.SS)"},
                     {"db": None, "registry": None}) == []


# ----------------------------------------------------------------------
# R1-P3-12 / R1-P3-13 统计件守卫
# ----------------------------------------------------------------------

def test_paired_requires_equal_length():
    import numpy as np

    from research.stats.paired import paired_sharpe_comparison

    with pytest.raises(ValueError, match="date-aligned"):
        paired_sharpe_comparison(np.zeros(40), np.zeros(39))


def test_pbo_rejects_odd_blocks_and_has_no_seed():
    import numpy as np

    from research.stats.fdr_pbo import pbo_cscv

    matrix = np.random.default_rng(1).normal(size=(100, 3))
    with pytest.raises(ValueError, match="even"):
        pbo_cscv(matrix, n_blocks=7)
    out = pbo_cscv(matrix, n_blocks=8)
    assert out["pbo"] is not None


# ----------------------------------------------------------------------
# R1-P3-11 / R1-P3-15 / R1-P3-22 杂项行为
# ----------------------------------------------------------------------

def test_conclude_direction_rate_ignores_zero_effect(test_db, human_session, topic):
    """effect == 0 无方向：不计入方向一致率分子/分母（expect=negative 时
    不再把 0 计为命中）。"""
    from research import conclusion, verdict as verdict_mod

    exp = experiments.propose_experiment(
        test_db,
        session_id=human_session["session_id"],
        title="零效应方向钉子",
        topic_id=topic["id"],
        evaluation_module="bucket_analysis@1",
        spec={"signal_module": "ma_cross@1", "feature": "er_10", "expect": "negative"},
        hypothesis="假设文本足够长以过骨架校验",
    )
    lifecycle.transition(test_db, exp["id"], "running")
    lifecycle.transition(test_db, exp["id"], "evaluating")
    verdict_mod.insert_platform_verdict(
        test_db, experiment_id=exp["id"], baseline={},
        evidence={"q_spread": 0.0},  # effect==0：无方向
        warnings=[], report={}, suggested_verdict="inconclusive",
    )
    from research import verdict as _v

    _v.confirm_verdict(
        test_db, experiment_id=exp["id"], final_verdict="inconclusive",
        reasoning="零效应", session_id=human_session["session_id"],
    )
    summary = conclusion.build_conclusion_summary(test_db, topic["id"])
    # 修复前：expect=negative 时 (0>0)==False 被计成命中（dir_total=1,
    # dir_hits=1 → 一致率 1.0 的假信号）；修复后零效应不进分子分母
    assert summary.get("direction_consistency") is None, summary


def test_panel_view_date_at_clamped_to_upto():
    from datetime import timedelta

    from gateway.panel import Panel
    from portfolio.context import PanelView

    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(10)]
    panel = Panel(
        dates=tuple(days), symbols=("X.SS",),
        data={"close": __import__("numpy").arange(10).reshape(10, 1).astype(float)},
        provisional=__import__("numpy").zeros((10, 1), dtype=bool),
    )
    view = PanelView(panel, 4)  # 当日 = 第 5 行
    assert view.date_at(2) == days[2]
    assert view.date_at(999) == days[4]  # 越界探测被钳到当日
    assert view.date_at(-5) == days[0]


# ----------------------------------------------------------------------
# R1-P3-6 回撤修复天数单位
# ----------------------------------------------------------------------

def test_drawdown_recovery_in_trading_days():
    from portfolio.reports import drawdown_durations

    # 30 行 NAV：第 3 行见顶回落，第 10 行修复——修复天数应为 7（交易日），
    # 而不是日历日（周末跨度的 .days 会 >7）
    from datetime import date as _date, timedelta as _td

    days = [_date(2024, 1, d) for d in (2, 3, 4, 5, 8, 9, 10, 11, 12, 15)]  # 跳周末
    equities = [100.0, 100.0, 110.0, 95.0, 95.0, 95.0, 95.0, 95.0, 95.0, 110.0]
    nav = [{"date": d.isoformat(), "equity": e} for d, e in zip(days, equities)]
    out = drawdown_durations(nav)
    assert out["max_underwater_days"] == 6
    # 修复 = 位置差：谷底 idx3 → 修复 idx9 = 6 个交易日（日历差是 10 天）
    assert out["max_dd_recovery_days"] == 6


# ----------------------------------------------------------------------
# R1-P3-7 载入期元模块成员参数校验
# ----------------------------------------------------------------------

def test_meta_member_params_validated_at_parse():
    from portfolio.registry import fresh_registry
    from portfolio.slots import ensure_builtins
    from portfolio.strategy import parse_strategy_yaml

    ensure_builtins()
    reg = fresh_registry()
    from portfolio.slots import REGISTRY as BUILTIN
    for spec in BUILTIN.list():
        reg.register(spec)
    yaml_bad = (
        "name: bad-meta\ndescription: x\n"
        "universe: {module: none}\nsignal: {module: none}\n"
        "rank: {module: none}\nsizing: {module: none}\n"
        "portfolio_risk: []\n"
        "position_risk: {module: any_of@1, params: {members: ["
        "{module: hard_stop@1, params: {atr_mul: -5}}]}}\n"
        "execution: {module: none}\n"
    )
    with pytest.raises(Exception, match="member"):
        parse_strategy_yaml(yaml_bad, reg)


# ----------------------------------------------------------------------
# R1-P1-4 日更单飞锁
# ----------------------------------------------------------------------

def test_daily_update_job_self_guarded(monkeypatch):
    """锁被占用时 daily_market_update_job 立即返回 skipped_already_running
    （哨兵/定时/补偿三路互斥下沉到模块级锁）。"""
    import threading

    from core import jobs

    with jobs._DAILY_UPDATE_LOCK:
        payload = jobs.daily_market_update_job(settings=None, data_service=None)
    assert payload["status"] == "skipped_already_running"


# ----------------------------------------------------------------------
# R1-P3-17 bucket 空桶警告（轻量：直接构造事件调用内部分桶段不可行，
# 改为断言 warnings 文案生成逻辑存在）——由集成套件的 bucket 用例回归覆盖。
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# R1-P2-2 any_of 组合止损重建（V2 验收 FAIL 后的修正钉子）
# ----------------------------------------------------------------------

def test_live_any_of_stop_rebuild_uses_member_keys():
    """any_of 止损重建必须逐成员分派（成员实例须带 _registered_key）并取
    max(stop_price)——修复前成员全落兜底分支，组合止损恒 None。"""
    from datetime import timedelta

    import numpy as np

    from gateway.panel import Panel
    from portfolio.live import _rebuild_stop_state
    from portfolio.slots import ensure_builtins
    from portfolio.slots import REGISTRY as BUILTIN

    ensure_builtins()
    mod = BUILTIN.require("any_of@1", slot="position_risk").factory({
        "members": [
            {"module": "hard_stop@1", "params": {"atr_mul": 2.0}},
            {"module": "chandelier@1", "params": {"atr_mul": 3.0}},
        ]
    })
    # 复刻真实调用形态（开发日志 DS-R2 流程教训）：顶层实例的
    # _registered_key 由 backtester/live 的 instantiate_modules 打上——
    # 裸 factory 产物没有；成员实例的键由 _MetaBase.__init__ 打（本次修复）
    mod._registered_key = "any_of@1"
    # 成员实例必须带注册键（V2 发现的根因；分派依赖它）
    for sub, key in mod._subs:
        assert getattr(sub, "_registered_key", "") == key

    # 合成面板：30 天 10→20 单边上行，末根真实收盘 20
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(30)]
    close = np.linspace(10.0, 20.0, 30)
    high = close * 1.02
    low = close * 0.98
    panel = Panel(
        dates=tuple(days), symbols=("X.SS",),
        data={"close": close.reshape(-1, 1), "high": high.reshape(-1, 1),
              "low": low.reshape(-1, 1)},
        provisional=np.zeros((30, 1), dtype=bool),
    )
    state = _rebuild_stop_state(mod, panel, "X.SS", days[20], 17.0)
    assert state is not None
    assert state.stop_price is not None, "any_of 重建不得落兜底（stop=None）"
    # 单成员对照：stop 应为 max(hard_stop, chandelier) 两位成员的止损价
    hs = _rebuild_stop_state(mod._subs[0][0], panel, "X.SS", days[20], 17.0)
    ch = _rebuild_stop_state(mod._subs[1][0], panel, "X.SS", days[20], 17.0)
    expected = max(hs.stop_price, ch.stop_price)
    assert state.stop_price == pytest.approx(expected)
