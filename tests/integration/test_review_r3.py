"""K3-R2 / DS-R2 / GLM53F 三轮复审的集成级回归钉子（删掉实现即失败）。

覆盖：
- DS-R2 新 P1：启动收割不误伤"evaluating + 已生成 verdict"的实验
  （有 verdict 存活可确认 / 无 verdict 收割两向）；
- K3-R2 残留：bucket_analysis 同一信号在 valid_days 内只计一次；
- DS-R2 §4-5 / K3-R2-注记-2：全局 holdout token 不被自动带出，
  绑定实验的 token 自动带出；
- K3-R2-注记-3 → 用户决策反转（2026-09-24）：promote 无人工门，AI 可全流程
  闭环自动晋升（血缘门 verdicted+confirmed 是唯一门）；
- GLM53F-P1-4：课题摘要定论优先——复核稿不劫持 verdict 计数与分级；
- GLM53F-P2-2：方向一致率 = 与假设（spec.expect）同向的占比；
- GLM53F-P2-6：strategy_line attempt_count 排除复现 run；
- DS-R2 §4-1：长窗口三注记下发生到 event/bucket/distribution；
- GLM53F-P2-3：regime 基准不可用时禁用拆分（不回退 base NAV 假证据）；
- GLM53F-P2-9：worker 调度循环异常守卫（瞬时异常不杀死调度线程）；
- GLM53F-P2-11：MCP 无 worker 同步路径包冻结写；
- GLM53F-P2-12：reviewed 草稿装载失败不拖垮启动；
- GLM53F-P2-13：冻结顺延挂当日一次性补跑哨兵；
- GLM53F-P2-18：live 对账分母 = 收盘基准价 + qty 核对。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from portfolio.registry import ModuleSpec, fresh_registry
from portfolio.seed import seed_default_library
from portfolio.slots import register_builtin_modules
from research import experiments, holdout, lifecycle, sessions, topics, verdict
from research.api import ResearchService
from research.errors import ResearchError
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
        test_db, session_id=session["session_id"], title="R3 复审课题", question="修复验证"
    )
    return {"db": test_db, "registry": registry, "versions": versions,
            "session": session, "topic": topic}


def _spec(env, window=("2022-06-01", "2023-06-30"), mul=2.0, **over):
    base = {
        "base": env["versions"]["base-v1"],
        "diff": [{"slot": "position_risk", "from": "hard_stop@1", "to": "hard_stop@1",
                  "params": {"atr_mul": mul}}],
        "window": list(window),
    }
    base.update(over)
    return base


def _propose(env, title="实验", **spec_over):
    return experiments.propose_experiment(
        env["db"], session_id=env["session"]["session_id"], title=title,
        topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
        spec=_spec(env, **spec_over), hypothesis=f"{title}的假设陈述（长度满足入口校验）",
        allow_duplicate=True,
    )


# ----------------------------------------------------------------------
# DS-R2 新 P1：启动收割谓词收窄（两向）
# ----------------------------------------------------------------------


def test_startup_sweep_preserves_evaluating_with_verdict(env):
    """evaluating + 平台 verdict 已生成 = 合法长驻态——重启收割不得判死，
    且 confirm_verdict 仍可成功。"""
    db = env["db"]
    exp = _propose(env, "待确认实验")
    lifecycle.transition(db, exp["id"], "running")
    lifecycle.transition(db, exp["id"], "evaluating")
    verdict.insert_platform_verdict(
        db, experiment_id=exp["id"], baseline={}, evidence={}, warnings=[],
        report={}, suggested_verdict="inconclusive",
    )

    lifecycle.mark_interrupted_research_runs(db)
    after = lifecycle.get_experiment(db, exp["id"])
    assert after["status"] == "evaluating"  # 旧代码：被判 failed 且永远不可确认

    v = verdict.confirm_verdict(
        db, experiment_id=exp["id"], final_verdict="inconclusive",
        reasoning="重启后人工确认", session_id=env["session"]["session_id"],
    )
    assert v["final_verdict"] == "inconclusive"


def test_startup_sweep_reaps_orphans_only(env):
    """running 一律收割；evaluating 无 verdict（pipeline 取证途中死亡）收割。"""
    db = env["db"]
    exp_run = _propose(env, "跑中死亡")
    lifecycle.transition(db, exp_run["id"], "running")
    exp_eval = _propose(env, "取证途中死亡", mul=1.9)
    lifecycle.transition(db, exp_eval["id"], "running")
    lifecycle.transition(db, exp_eval["id"], "evaluating")  # 无 verdict

    swept = lifecycle.mark_interrupted_research_runs(db)
    assert swept["experiments"] == 2
    assert lifecycle.get_experiment(db, exp_run["id"])["status"] == "failed"
    assert lifecycle.get_experiment(db, exp_eval["id"])["status"] == "failed"


# ----------------------------------------------------------------------
# K3-R2 残留：bucket_analysis 事件去重 + 事件日起算
# ----------------------------------------------------------------------


class _RepeatEventSignal:
    """valid_days=3 语义探针：触发日后连续 3 个 scan 日重复上报同一事件日。"""

    TARGET = date(2022, 3, 1)

    def __init__(self, params):
        self.valid_days = int((params or {}).get("valid_days", 3))

    def scan(self, ctx, members):
        from portfolio.slots.signal import SignalEvent

        if self.TARGET <= ctx.date <= date(2022, 3, 3):
            return [SignalEvent(symbol=m.symbol, kind="entry", date=self.TARGET, meta={})
                    for m in members]
        return []


def test_bucket_dedupes_event_within_valid_days(env):
    """同一 (symbol, 事件日) 在 valid_days 内重复报到只计一次（旧代码计 3 次）。"""
    db, reg = env["db"], env["registry"]
    reg.register(ModuleSpec(
        slot="signal", name="repeat_event", version=1, factory=_RepeatEventSignal,
        params_schema={"valid_days": {"type": "integer", "default": 3}},
        kind="builtin", description="probe",
    ))
    symbols = [f"K{i:03d}.SS" for i in range(5)]
    exp = experiments.propose_experiment(
        db, session_id=env["session"]["session_id"], title="分桶去重钉",
        topic_id=env["topic"]["id"], evaluation_module="bucket_analysis@1",
        spec={"signal_module": "repeat_event@1", "feature": "momentum_20",
              "buckets": 2, "horizons": [5], "universe": symbols,
              "window": ["2022-02-01", "2022-06-30"]},
        hypothesis="同一信号在有效期内只计一次的假设陈述",
    )
    v = run_experiment(db, exp["id"], registry=reg)
    assert v["report"]["events"] == 5  # 5 标的 × 1 次（不是 × 3 个 scan 日）


# ----------------------------------------------------------------------
# DS-R2 §4-5 / K3-R2-注记-2：holdout token 绑定收窄
# ----------------------------------------------------------------------


def test_global_token_not_auto_picked_but_explicit_pass_works(env):
    db, reg = env["db"], env["registry"]
    holdout.set_enforced(db, True)
    try:
        exp = _propose(env, "全局 token 不该被自动吃", window=("2023-01-03", "2025-06-30"))
        holdout.grant_token(
            db, session_id=env["session"]["session_id"],
            purpose="全局用途", experiment_id=None,
        )
        result = run_experiment(db, exp["id"], registry=reg)
        assert result["status"] == "failed"  # 全局 token 不再被自动带出
        assert "holdout" in str(result.get("error", "")).lower()

        # 显式透传 → 跑通
        exp2 = _propose(env, "显式透传全局 token", window=("2023-01-03", "2025-06-30"), mul=1.8)
        token2 = holdout.grant_token(
            db, session_id=env["session"]["session_id"],
            purpose="显式用", experiment_id=None,
        )
        v = run_experiment(db, exp2["id"], registry=reg, holdout_token=token2["id"])
        assert v.get("suggested_verdict") is not None
        assert holdout.get_token(db, token2["id"])["consumed_at"] is not None
    finally:
        holdout.set_enforced(db, False)


def test_bound_token_auto_picked(env):
    db, reg = env["db"], env["registry"]
    holdout.set_enforced(db, True)
    try:
        exp = _propose(env, "绑定 token 自动带出", window=("2023-01-03", "2025-06-30"))
        token = holdout.grant_token(
            db, session_id=env["session"]["session_id"],
            purpose="绑定该实验", experiment_id=exp["id"],
        )
        v = run_experiment(db, exp["id"], registry=reg)  # 未显式传 token
        assert v.get("suggested_verdict") is not None
        assert holdout.get_token(db, token["id"])["consumed_at"] is not None
    finally:
        holdout.set_enforced(db, False)


# ----------------------------------------------------------------------
# K3-R2-注记-3：promote 入库门仅 human session
# ----------------------------------------------------------------------


def _verdicted_confirmed_experiment(env):
    db = env["db"]
    exp = _propose(env, "待晋升实验")
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
    return exp


def test_promote_allows_ai_full_loop(env):
    """2026-09-24 用户决策：AI 可全流程闭环自动晋升（提议→跑→confirm→晋升）。
    唯一的门 = 血缘门（verdicted + final=confirmed）——未确认实验 AI 晋升被拒。"""
    db = env["db"]
    service = ResearchService(db, registry=env["registry"],
                              topics_dir=db.db_path.parent / "t")
    ai = sessions.get_or_create_ai_session(db, channel="mcp")
    # 未确认（evaluating）实验：血缘门拦（AI 也好人工也好都拦）
    exp_open = _propose(env, "未确认不可晋升", mul=1.6)
    lifecycle.transition(db, exp_open["id"], "running")
    lifecycle.transition(db, exp_open["id"], "evaluating")
    with pytest.raises(ResearchError):
        service.promote_to_library(
            experiment_id=exp_open["id"], strategy_id="premature-line",
            session_id=ai["session_id"],
        )
    # confirmed 实验：AI 会话直接晋升成功（带血缘）
    exp = _verdicted_confirmed_experiment(env)
    version = service.promote_to_library(
        experiment_id=exp["id"], strategy_id="ai-full-loop-line",
        session_id=ai["session_id"],
    )
    assert version["experiment_id"] == exp["id"]
    assert version["parent_version_id"]  # 血缘仍在（diff 基准）


# ----------------------------------------------------------------------
# GLM53F-P1-4 + P2-2：课题摘要定论优先 + 方向一致率与假设同向
# ----------------------------------------------------------------------


def test_conclusion_canonical_verdict_and_hypothesis_direction(env):
    db = env["db"]
    # 三个实验：expect 全 positive；效应量 [−0.2, −0.1, +0.05]
    # → 与假设同向占比 = 1/3（旧口径"与多数方向一致"= 2/3，可区分）
    effects = [-0.2, -0.1, 0.05]
    exp_ids = []
    for i, eff in enumerate(effects):
        exp = _propose(env, f"方向实验{i}", mul=1.5 + 0.1 * i, expect="positive")
        lifecycle.transition(db, exp["id"], "running")
        lifecycle.transition(db, exp["id"], "evaluating")
        verdict.insert_platform_verdict(
            db, experiment_id=exp["id"], baseline={},
            evidence={"deltas_vs_base": {"delta_sharpe": eff}},
            warnings=[], report={}, suggested_verdict="confirmed",
        )
        verdict.confirm_verdict(
            db, experiment_id=exp["id"], final_verdict="confirmed",
            reasoning="落定", session_id=env["session"]["session_id"],
        )
        exp_ids.append(exp["id"])

    # 对实验 0 追加一条复核 verdict（平台自动落定 final=rejected，带 supersedes）
    v_re = verdict.insert_platform_verdict(
        db, experiment_id=exp_ids[0], baseline={},
        evidence={"deltas_vs_base": {"delta_sharpe": -0.9}},
        warnings=[], report={}, suggested_verdict="rejected",
        supersedes=exp_ids[0],
    )
    with db.connect() as conn:
        conn.execute(
            """UPDATE research_verdicts
               SET final_verdict = 'rejected', confirmed_by = 'platform',
                   confirmed_at = datetime('now','localtime')
               WHERE id = ?""",
            (v_re["id"],),
        )

    from research.conclusion import build_conclusion_summary

    summary = build_conclusion_summary(db, env["topic"]["id"])
    # 复核稿不劫持：三个原生 confirmed 计数，rejected 不出现
    assert summary["verdict_counts"].get("confirmed", 0) == 3
    assert summary["verdict_counts"].get("rejected", 0) == 0
    # 方向一致率 = 与假设同向（1/3），不是多数方向占比（2/3）
    assert summary["effect_sizes"]["direction_consistency"] == pytest.approx(1 / 3)
    # dc=1/3 < 0.7 → 不得升 supported（分级卡控消费同口径）
    assert summary["suggested_grade"] == "insufficient-evidence"


# ----------------------------------------------------------------------
# GLM53F-P2-6：attempt_count 排除复现 run
# ----------------------------------------------------------------------


def test_strategy_line_attempt_count_excludes_reproductions(env):
    db = env["db"]
    service = ResearchService(db, registry=env["registry"],
                              topics_dir=db.db_path.parent / "t")
    exp = _propose(env, "主线实验")
    lifecycle.transition(db, exp["id"], "running")
    lifecycle.transition(db, exp["id"], "failed", error="工程失败占位")
    service.rerun_experiment(
        experiment_id=exp["id"], session_id=env["session"]["session_id"],
        auto_queue=False,
    )
    line = service.strategy_line(exp["subject_key"])
    assert line["attempt_count"] == 1  # 复现不计入（旧口径 = 2）
    assert len(line["experiments"]) == 2  # 复现记录仍在清单（一个不许藏）


# ----------------------------------------------------------------------
# DS-R2 §4-1：长窗口三注记下发生到 event/bucket/distribution
# ----------------------------------------------------------------------


def _assert_three_annotations(warnings):
    assert any(w.startswith("coverage_note") for w in warnings)
    assert any(w.startswith("fee_era_mismatch") for w in warnings)
    assert any(w.startswith("survivorship_weighting") for w in warnings)


def test_long_window_annotations_reach_event_and_distribution(env):
    db, reg = env["db"], env["registry"]
    symbols = [f"K{i:03d}.SS" for i in range(5)]
    exp_ev = experiments.propose_experiment(
        db, session_id=env["session"]["session_id"], title="事件长窗口",
        topic_id=env["topic"]["id"], evaluation_module="event_study@1",
        spec={"event": "ma_cross@1", "horizons": [5], "universe": symbols,
              "window": ["2019-06-03", "2022-12-30"]},
        hypothesis="长窗口事件研究应带三注记的假设",
    )
    v_ev = run_experiment(db, exp_ev["id"], registry=reg)
    _assert_three_annotations(v_ev["warnings"])

    exp_di = experiments.propose_experiment(
        db, session_id=env["session"]["session_id"], title="分布长窗口",
        topic_id=env["topic"]["id"], evaluation_module="distribution@1",
        spec={"metric": "atr_pct", "universe": symbols,
              "window": ["2019-06-03", "2022-12-30"]},
        hypothesis="长窗口分布标定应带三注记的假设",
    )
    v_di = run_experiment(db, exp_di["id"], registry=reg)
    _assert_three_annotations(v_di["warnings"])


def test_long_window_annotations_reach_bucket(env):
    db, reg = env["db"], env["registry"]
    reg.register(ModuleSpec(
        slot="signal", name="repeat_event", version=1, factory=_RepeatEventSignal,
        params_schema={"valid_days": {"type": "integer", "default": 3}},
        kind="builtin", description="probe",
    ))
    symbols = [f"K{i:03d}.SS" for i in range(5)]
    exp = experiments.propose_experiment(
        db, session_id=env["session"]["session_id"], title="分桶长窗口",
        topic_id=env["topic"]["id"], evaluation_module="bucket_analysis@1",
        spec={"signal_module": "repeat_event@1", "feature": "momentum_20",
              "buckets": 2, "horizons": [5], "universe": symbols,
              "window": ["2019-06-03", "2022-12-30"]},
        hypothesis="长窗口分桶应带三注记的假设",
    )
    v = run_experiment(db, exp["id"], registry=reg)
    _assert_three_annotations(v["warnings"])


# ----------------------------------------------------------------------
# GLM53F-P2-3：regime 基准不可用时禁用拆分
# ----------------------------------------------------------------------


def test_regime_split_disabled_when_benchmark_unavailable(env):
    """测试库无 510300 数据 → regime 拆分为空 + 显式警告（不回退 base NAV）。"""
    db, reg = env["db"], env["registry"]
    exp = _propose(env, "regime 禁用钉")
    v = run_experiment(db, exp["id"], registry=reg)
    assert v["evidence"]["regime_split"] == {}
    assert any(w.startswith("regime_benchmark_unavailable") for w in v["warnings"])


# ----------------------------------------------------------------------
# GLM53F-P2-9：worker 调度循环异常守卫
# ----------------------------------------------------------------------


def test_dispatch_loop_survives_probe_exception(env, monkeypatch):
    import threading
    import time

    from research.worker import ResearchWorker

    worker = ResearchWorker(env["db"], registry=env["registry"], max_workers=1)
    monkeypatch.setattr(
        lifecycle, "get_experiment",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sqlite busy")),
    )
    worker._queue.put("E-DUMMY")
    worker._stop.clear()
    t = threading.Thread(target=worker._dispatch_loop, daemon=True)
    t.start()
    time.sleep(1.2)  # 第一次探测异常 → 退避 0.5s → 继续空转
    assert t.is_alive()  # 旧代码：调度线程带异常死亡，队列永久卡死
    worker._stop.set()
    t.join(timeout=5)
    assert not t.is_alive()


# ----------------------------------------------------------------------
# GLM53F-P2-11：MCP 无 worker 同步路径包冻结写
# ----------------------------------------------------------------------


def test_mcp_sync_run_wraps_freeze(test_db, registry, monkeypatch):
    from core import run_freeze
    from trend_mcp import research_tools

    service = ResearchService(test_db, registry=registry)  # worker=None → 同步路径
    entered = []
    orig = run_freeze.frozen_writes

    def _recording():
        entered.append(True)
        return orig()

    monkeypatch.setattr(run_freeze, "frozen_writes", _recording)
    monkeypatch.setattr(
        "research.pipeline.run_experiment",
        lambda db, eid, registry=None, holdout_token=None: {"status": "evaluating"},
    )
    out = research_tools._run_or_queue(service, "E-DUMMY")
    assert out["mode"] == "sync"
    assert entered == [True]  # 冻结写上下文确实进入过


# ----------------------------------------------------------------------
# GLM53F-P2-12：reviewed 草稿装载失败不拖垮启动
# ----------------------------------------------------------------------


def test_load_reviewed_modules_isolates_bad_draft(test_db, registry):
    from research.modules import load_reviewed_modules

    with test_db.connect() as conn:
        conn.execute(
            """INSERT INTO module_drafts
               (id, slot, name, version, kind, source, params_schema_json, status)
               VALUES ('M9001', 'signal', 'broken_mod', 1, 'python',
                       'raise RuntimeError("dependency drift")', '{}', 'reviewed')"""
        )
    loaded = load_reviewed_modules(test_db, registry)  # 旧代码：exec 抛错 → 全站起不来
    assert loaded == 0
    assert not registry.has("broken_mod@1", slot="signal")


# ----------------------------------------------------------------------
# GLM53F-P2-13：冻结顺延挂当日补跑哨兵
# ----------------------------------------------------------------------


def test_freeze_defer_spawns_same_day_catchup(test_db, monkeypatch):
    from core import jobs, run_freeze

    monkeypatch.setattr(jobs, "is_trading_day", lambda d: True)
    monkeypatch.setattr(run_freeze, "is_frozen", lambda: True)  # 恒冻结
    monkeypatch.setattr("time.sleep", lambda s: None)           # 快进

    from core.settings import load_settings

    payload = jobs.daily_market_update_job(load_settings(), force=True)
    assert payload["status"] == "deferred_backtest_running"
    sentinel = jobs._catchup_sentinel
    assert sentinel is not None  # 顺延 ≠ 饿一整天：当日补跑哨兵已挂
    sentinel.join(timeout=10)    # 快进下哨兵耗尽 2h 预算后自行退出
    assert not sentinel.is_alive()


# ----------------------------------------------------------------------
# GLM53F-P2-18：live 对账分母 = 收盘基准价 + qty 核对
# ----------------------------------------------------------------------


def test_live_reconcile_uses_ref_price_and_checks_qty(test_db, registry):
    import json

    from portfolio.live import reconcile_daily_list

    versions = seed_default_library(test_db, registry)
    user = test_db.create_user("r3user", "pass12345")
    # 清单：买 P001（qty 1000，ref 10.00，est 10.03 含模型滑点）
    target = {"buys": [{"symbol": "P001.SS", "qty": 1000,
                        "ref_price": 10.00, "est_price": 10.03}],
              "sells": []}
    with test_db.connect() as conn:
        conn.execute(
            """INSERT INTO portfolio_live_lists
               (list_date, strategy_version_id, as_of, target_json)
               VALUES ('2024-03-05', ?, '2024-03-05 14:00:00', ?)""",
            (versions["base-v1"], json.dumps(target)),
        )
    # 实际：买在 10.10、900 股（数量也与清单不符）
    test_db.create_manual_trade(user["id"], "P001.SS", "2024-03-05", 10.10, 900)

    out = reconcile_daily_list(
        test_db, list_date="2024-03-05",
        strategy_version_id=versions["base-v1"], user_id=user["id"],
    )
    assert out["price_diffs"][0]["diff_pct"] == pytest.approx(0.01)   # vs 收盘基准 10.00
    assert out["price_diffs"][0]["bench_price"] == 10.00              # 不是 est 10.03
    assert out["qty_mismatches"] == [
        {"symbol": "P001.SS", "side": "buy", "target_qty": 1000, "actual_qty": 900}
    ]
