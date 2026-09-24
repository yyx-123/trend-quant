"""阶段 2∥：event_study / bucket_analysis / distribution 评估模块端到端测试。

合成数据上验证：事件提取 → 前瞻收益 vs 无条件分布 → verdict 建议；
单变量/对照/警告（§6.6.3）；pipeline 全链路（queued→verdicted）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from portfolio.registry import fresh_registry
from portfolio.slots import register_builtin_modules
from research import experiments, lifecycle, sessions, topics, verdict
from research.pipeline import run_experiment

pytestmark = pytest.mark.integration


@pytest.fixture
def registry():
    reg = fresh_registry()
    register_builtin_modules(reg)
    return reg


def _write_symbol(db, symbol, closes, start="2021-01-04", asset_type="etf", amount=2e8):
    n = len(closes)
    dates = pd.bdate_range(start, periods=n)
    closes = np.asarray(closes, dtype=float)
    df = pd.DataFrame({
        "time": dates, "open": closes, "high": closes * 1.01, "low": closes * 0.99,
        "close": closes, "volume": np.full(n, 2e6), "amount": closes * amount / 10,
    })
    db.save_market_data(symbol, df, price_mode="qfq")
    db.save_market_data(symbol, df, price_mode="raw")
    db.save_instrument_metadata([{
        "symbol": symbol, "name": symbol, "category_l1": "T", "category_l2": "T2",
        "category_l3": "", "enabled": 1, "asset_type": asset_type,
    }])


@pytest.fixture
def market(test_db):
    """40 只合成标的：一半趋势（金叉后涨）、一半随机。"""
    rng = np.random.default_rng(5)
    for i in range(40):
        drift = 0.004 if i % 2 == 0 else 0.0
        closes = 20 * np.exp(np.cumsum(rng.normal(drift, 0.015, 420)))
        _write_symbol(test_db, f"TES{i:03d}.SS", closes)
    return test_db


@pytest.fixture
def session_topic(market):
    session = sessions.get_or_create_default_human_session(market)
    topic = topics.create_topic(
        market, session_id=session["session_id"], title="MA20 研究", question="MA20 交叉有料吗"
    )
    return session, topic


def test_event_study_end_to_end(market, session_topic, registry):
    session, topic = session_topic
    exp = experiments.propose_experiment(
        market,
        session_id=session["session_id"],
        title="MA20 金叉快筛",
        topic_id=topic["id"],
        evaluation_module="event_study@1",
        spec={
            "event": "ma_cross@1(n=20)",
            "universe": [f"TES{i:03d}.SS" for i in range(40)],
            "horizons": [5, 10, 20],
            "expect": "positive",
        },
        hypothesis="MA20 金叉后 10 日前瞻收益显著高于无条件分布",
        registry=registry,
    )
    assert exp["status"] == "queued"
    assert exp["subject_key"] == "ma_cross@1"

    v = run_experiment(market, exp["id"], registry=registry)
    # 合成夹具（固定种子）的确定性回归锚：混合池金叉前瞻 10 日 delta 显著为负
    # → rejected（mean-reversion 噪声中的金叉没有正向 edge）
    assert v["suggested_verdict"] == "rejected"
    ev = v["evidence"]
    assert set(ev["per_horizon"]) == {"5", "10", "20"}
    # 去重+事件日起算后的新锚（K3-P2-2 修复生效：候选期重复报到不再膨胀事件数）
    assert ev["per_horizon"]["10"]["n_events"] == 798
    assert ev["per_horizon"]["10"]["delta_mean"] == pytest.approx(-0.00744, abs=1e-4)
    assert ev["noise_band"]["low"] is not None
    # 警告必带（当前池穿越历史 → 幸存者偏差警告，§6.6.3）
    assert any("survivorship_bias" in w for w in v["warnings"])
    # 台账：runs 留痕 + 实验到 evaluating
    exp_after = lifecycle.get_experiment(market, exp["id"])
    assert exp_after["status"] == "evaluating"
    from research import runs as runs_mod

    run_rows = runs_mod.list_runs(market, exp["id"])
    assert len(run_rows) == 1 and run_rows[0]["window_kind"] == "sample"

    # confirm → verdicted
    final = verdict.confirm_verdict(
        market, experiment_id=exp["id"], final_verdict=v["suggested_verdict"],
        reasoning="合成趋势数据上金叉前瞻收益为正", session_id=session["session_id"],
    )
    assert final["final_verdict"] == v["suggested_verdict"]
    assert lifecycle.get_experiment(market, exp["id"])["status"] == "verdicted"


def test_event_study_reject_unknown_signal_module(market, session_topic, registry):
    session, topic = session_topic
    from research.errors import IntakeRejected

    with pytest.raises(IntakeRejected, match="event module not registered"):
        experiments.propose_experiment(
            market, session_id=session["session_id"], title="坏模块",
            topic_id=topic["id"], evaluation_module="event_study@1",
            spec={"event": "ghost@9", "horizons": [10]},
            hypothesis="事件模块未注册应被拒绝",
            registry=registry,
        )


def test_bucket_analysis_monotonicity(market, session_topic, registry):
    session, topic = session_topic
    exp = experiments.propose_experiment(
        market,
        session_id=session["session_id"],
        title="ATR% 分桶",
        topic_id=topic["id"],
        evaluation_module="bucket_analysis@1",
        spec={
            "signal_module": "ma_cross@1(n=20)",
            "feature": "atr_pct",
            "buckets": 5,
            "horizons": [10],
            "universe": [f"TES{i:03d}.SS" for i in range(40)],
        },
        hypothesis="ATR% 分桶对前瞻收益有单调排序力",
        registry=registry,
    )
    assert exp["status"] == "queued"
    v = run_experiment(market, exp["id"], registry=registry)
    # 合成夹具回归锚：ATR% 无排序力 → inconclusive；单调性/利差/随机带钉死
    assert v["suggested_verdict"] == "inconclusive"
    ev = v["evidence"]
    assert len(ev["bucket_table"]) == 5
    assert ev["monotonicity"] == pytest.approx(0.5)  # 5 桶 4 个相邻对，2 个同向（去重后锚点）
    assert ev["q_spread"] == pytest.approx(-0.00907, abs=1e-3)
    assert ev["random_band_abs95"] is not None and ev["random_band_abs95"] > 0
    assert v["report"]["events"] == 825  # (symbol,事件日) 去重后的事件数（K3-R2 残留修复锚点）


def test_distribution_fixed_inconclusive(market, session_topic, registry):
    session, topic = session_topic
    exp = experiments.propose_experiment(
        market,
        session_id=session["session_id"],
        title="ATR% 分布",
        topic_id=topic["id"],
        evaluation_module="distribution@1",
        spec={"metric": "atr_pct", "universe": [f"TES{i:03d}.SS" for i in range(40)]},
        hypothesis="ATR% 池化分布用于止损倍数标定",
        registry=registry,
    )
    v = run_experiment(market, exp["id"], registry=registry)
    assert v["suggested_verdict"] == "inconclusive"  # 固定档
    # distribution 不允许 confirmed（模块级 allowed_finals）
    with pytest.raises(Exception):
        verdict.confirm_verdict(
            market, experiment_id=exp["id"], final_verdict="confirmed",
            reasoning="想升级", session_id=session["session_id"],
        )
    final = verdict.confirm_verdict(
        market, experiment_id=exp["id"], final_verdict="inconclusive",
        reasoning="标定建议：见 quantiles", session_id=session["session_id"],
    )
    assert final["final_verdict"] == "inconclusive"
    assert final["evidence"]["quantiles"]["p50"] > 0


def test_holdout_window_blocked_when_enforced(market, session_topic, registry):
    """enforced 开启后，触碰 holdout 的实验被拦（生效时点前的建设期不拦）。"""
    from research import holdout

    session, topic = session_topic
    holdout.set_enforced(market, True)
    try:
        exp = experiments.propose_experiment(
            market, session_id=session["session_id"], title="摸 holdout",
            topic_id=topic["id"], evaluation_module="event_study@1",
            spec={"event": "ma_cross@1(n=20)", "horizons": [10],
                  "window": ["2025-01-01", "2025-06-30"],
                  "universe": ["TES001.SS"]},
            hypothesis="窗口触碰 holdout 应被拦截",
            registry=registry,
        )
        result = run_experiment(market, exp["id"], registry=registry)
        assert result["status"] == "failed"  # HoldoutError → 工程失败入表
        assert "holdout" in (result["error"] or "").lower()
    finally:
        holdout.set_enforced(market, False)
