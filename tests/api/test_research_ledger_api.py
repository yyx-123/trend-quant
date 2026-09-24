"""台账看板 API 测试（阶段 5：看板 UI 的页面可见性 + 人工操作路径）。"""

from __future__ import annotations

import pytest

from research import experiments, lifecycle, sessions, topics, verdict

pytestmark = pytest.mark.api


@pytest.fixture
def seeded(test_db):
    """台账里有一个课题 + 一个待确认实验（evaluating）。"""
    session = sessions.get_or_create_default_human_session(test_db)
    topic = topics.create_topic(
        test_db, session_id=session["session_id"], title="止损课题", question="硬止损选型"
    )
    exp = None
    # 直接落一个 evaluating 实验（页面测试不跑取证流水线）
    from portfolio.registry import ModuleSpec, fresh_registry

    reg = fresh_registry()
    for slot in ("universe", "signal", "rank", "sizing", "portfolio_risk", "position_risk", "execution"):
        reg.register(ModuleSpec(slot=slot, name=f"d_{slot}", version=1, factory=lambda p: p))
    from portfolio.library import add_version_yaml, ensure_strategy

    ensure_strategy(test_db, "base-x", name="x")
    version = add_version_yaml(
        test_db, "base-x",
        "name: base-x\nuniverse: {module: d_universe@1}\nsignal: {module: d_signal@1}\n"
        "rank: {module: d_rank@1}\nsizing: {module: d_sizing@1}\nportfolio_risk: []\n"
        "position_risk: {module: d_position_risk@1}\nexecution: {module: d_execution@1}\n",
        reg,
    )
    exp = experiments.propose_experiment(
        test_db, session_id=session["session_id"], title="演示实验",
        topic_id=topic["id"], evaluation_module="portfolio_backtest@1",
        spec={"base": version["id"], "diff": [{"slot": "position_risk", "to": "d_position_risk@1"}]},
        hypothesis="演示实验的假设陈述足够长", registry=reg,
    )
    lifecycle.transition(test_db, exp["id"], "running")
    lifecycle.transition(test_db, exp["id"], "evaluating")
    verdict.insert_platform_verdict(
        test_db, experiment_id=exp["id"],
        baseline={"sharpe": 0.5}, evidence={"deltas_vs_base": {"delta_sharpe": 0.2}},
        warnings=["survivorship_bias(universe 为当前池穿越历史)"],
        report={"spec": {}}, suggested_verdict="confirmed",
    )
    return {"session": session, "topic": topic, "exp": exp}


def test_ledger_board_renders(client, seeded):
    resp = client.get("/research-ledger")
    assert resp.status_code == 200
    assert "投研台账" in resp.text
    assert seeded["exp"]["id"] in resp.text
    assert "止损课题" in resp.text
    assert "confirmed" in resp.text  # 平台建议列


def test_experiment_detail_renders(client, seeded):
    resp = client.get(f"/research-ledger/experiments/{seeded['exp']['id']}")
    assert resp.status_code == 200
    assert "演示实验的假设" in resp.text
    assert "survivorship_bias" in resp.text
    assert "spec" in resp.text


def test_confirm_via_page(client, seeded):
    exp_id = seeded["exp"]["id"]
    resp = client.post(
        f"/research-ledger/experiments/{exp_id}/confirm",
        data={"final_verdict": "inconclusive", "reasoning": "页面确认降级"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # 直接读库验证（不依赖页面渲染内容）
    from data.storage import db as db_module

    latest = verdict.latest_verdict(db_module.get_db(), exp_id)
    assert latest["final_verdict"] == "inconclusive"
    assert latest["reasoning"] == "页面确认降级"


def test_conclude_via_page(client, seeded):
    # 先确认落定，再关题
    client.post(
        f"/research-ledger/experiments/{seeded['exp']['id']}/confirm",
        data={"final_verdict": "confirmed", "reasoning": "证据充分"},
        follow_redirects=False,
    )
    resp = client.post(
        "/research-ledger/topics/conclude",
        data={"topic_id": seeded["topic"]["id"],
              "conclusion": "硬止损 1.5 成立", "grade": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    from data.storage import db as db_module

    topic = topics.get_topic(db_module.get_db(), seeded["topic"]["id"])
    assert topic["status"] == "concluded"
    assert topic["conclusion_grade"]  # 平台建议分级落定


def test_experiment_detail_404(client, seeded):
    resp = client.get("/research-ledger/experiments/E9999")
    assert resp.status_code == 404


def test_confirm_rejects_cross_site_form_post(client, seeded):
    """loop-review R2-P2-1：Sec-Fetch-Site: cross-site 的表单 POST 被 403
    拒绝（confirm 不可逆——CSRF 补充防线，与 SameSite=Lax 互补）。"""
    resp = client.post(
        f"/research-ledger/experiments/{seeded['exp']['id']}/confirm",
        data={"final_verdict": "confirmed", "reasoning": "x"},
        headers={"Sec-Fetch-Site": "cross-site"},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    # 同源表单不受影响
    resp2 = client.post(
        f"/research-ledger/experiments/{seeded['exp']['id']}/confirm",
        data={"final_verdict": "confirmed", "reasoning": "证据充分"},
        headers={"Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )
    assert resp2.status_code == 303
