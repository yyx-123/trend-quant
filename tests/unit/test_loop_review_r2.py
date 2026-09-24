"""loop-review-glm53f Round 2 修复钉子（round2-review.md 各项的回归锚）。"""

from __future__ import annotations

import numpy as np
import pytest

from research import sessions, topics

pytestmark = pytest.mark.unit


@pytest.fixture
def human_session(test_db):
    return sessions.get_or_create_default_human_session(test_db)


# ----------------------------------------------------------------------
# R2-P1-1 parity 归因接线
# ----------------------------------------------------------------------

def _parity_results(nav_new, nav_old):
    """构造 trades 逐位一致、NAV 给定序列的最小 results（只测 NAV 归因轴）。"""
    def _mk(nav):
        return {"trades": [], "daily_nav": [{"equity": e} for e in nav]}
    return _mk(nav_new), _mk(nav_old)


def test_parity_nav_point_diff_within_interest_bound_is_classified():
    from engine.parity import attribute_diffs

    # 1 亿权益日计息 ≈ 0.01/252 ≈ 3.97e-5 相对差——1e-5 相对差在界限内
    new, old = _parity_results(
        [1_000_000.0, 1_000_010.0], [1_000_000.0, 1_000_000.0]
    )
    report = attribute_diffs(new, old)
    assert report["classified"]["cash_interest"] == 1
    assert report["unexplained"] == []


def test_parity_nav_point_diff_beyond_bound_is_unexplained():
    from engine.parity import attribute_diffs

    # 1% NAV 差远超日计息界限——必须判负（修复前：静默 unexplained==[]）
    new, old = _parity_results(
        [1_000_000.0, 1_010_000.0], [1_000_000.0, 1_000_000.0]
    )
    report = attribute_diffs(new, old)
    assert report["classified"]["cash_interest"] == 0
    assert len(report["unexplained"]) == 1
    assert report["unexplained"][0]["kind"] == "nav_point_diff_beyond_interest"


def test_parity_normalize_bars_missing_columns_valueerror():
    import pandas as pd

    from engine.parity import _normalize_bars

    with pytest.raises(ValueError, match="date.*time"):
        _normalize_bars(pd.DataFrame({"close": [1.0, 2.0]}))


# ----------------------------------------------------------------------
# R2-P3-1 conclude 状态守卫 / R2-P3-2 rerun 关题拒绝
# ----------------------------------------------------------------------

def test_conclude_topic_rowcount_guard(test_db, human_session):
    """并发/重复 conclude：第二个写者被状态守卫拒绝，结论不被覆盖。"""
    from research import sessions, topics

    topic = topics.create_topic(
        test_db, session_id=human_session["session_id"], title="守卫", question="q?"
    )
    topics.conclude_topic(
        test_db, topic_id=topic["id"], conclusion="第一次结论",
    )
    from research.errors import TopicError

    with pytest.raises(TopicError, match="already concluded"):
        topics.conclude_topic(
            test_db, topic_id=topic["id"], conclusion="覆盖尝试",
        )
    assert topics.get_topic(test_db, topic["id"])["conclusion"] == "第一次结论"


def test_rerun_rejects_concluded_topic(test_db, human_session):
    """已 conclude 课题上的实验不可 rerun（关题不带在途实验不变式）。"""
    from research import experiments, lifecycle, sessions, topics, verdict
    from research.errors import LifecycleError, TopicError

    topic = topics.create_topic(
        test_db, session_id=human_session["session_id"], title="关题复现", question="q?"
    )
    exp = experiments.propose_experiment(
        test_db, session_id=human_session["session_id"], title="终态实验",
        topic_id=topic["id"], evaluation_module="distribution@1",
        spec={"metric": "atr_pct"}, hypothesis="假设文本足够长以过骨架校验",
    )
    lifecycle.transition(test_db, exp["id"], "running")
    lifecycle.transition(test_db, exp["id"], "evaluating")
    verdict.insert_platform_verdict(
        test_db, experiment_id=exp["id"], baseline={}, evidence={},
        warnings=[], report={}, suggested_verdict="inconclusive",
    )
    verdict.confirm_verdict(
        test_db, experiment_id=exp["id"], final_verdict="inconclusive",
        reasoning="r", session_id=human_session["session_id"],
    )
    topics.conclude_topic(
        test_db, topic_id=topic["id"], conclusion="c",
    )
    with pytest.raises(TopicError):
        experiments.rerun_experiment(
            test_db, experiment_id=exp["id"], session_id=human_session["session_id"]
        )


# ----------------------------------------------------------------------
# R2-P2-3 Sortino 推断 golden
# ----------------------------------------------------------------------

def test_psr_sortino_golden_and_property():
    import math

    from research.stats.psr import psr

    # 固定序列手算锚：downside = 负收益子集，sortino = mean/downside_std
    rets = np.array([0.01, -0.02, 0.005, -0.01, 0.003, -0.004, 0.006, 0.002,
                     -0.008, 0.007, 0.001, -0.003, 0.004, -0.005, 0.009,
                     0.0, 0.002, -0.006, 0.008, 0.003, -0.001, 0.005,
                     -0.007, 0.004, 0.006, -0.002, 0.001, 0.003, -0.004,
                     0.007])
    downside = rets[rets < 0]
    downside_std = float(np.std(downside, ddof=1))
    mean_r = float(np.mean(rets))
    sortino_hat = mean_r / downside_std
    skew = float(pd_Skew(rets))
    kurt = float(pd_Kurt(rets)) + 3.0
    got = psr(sortino_hat, 0.0, len(rets), skew, kurt)
    # 独立公式重算（NormalDist 参考实现）
    from statistics import NormalDist

    denom = math.sqrt(max(1e-12, 1.0 - skew * sortino_hat + (kurt - 1.0) / 4.0 * sortino_hat ** 2))
    expected = 0.5 * (1.0 + math.erf((sortino_hat * math.sqrt(len(rets) - 1) / denom) / math.sqrt(2.0)))
    assert got == pytest.approx(expected, abs=1e-12)
    assert NormalDist().cdf(0.0) == 0.5  # 参考实现健康
    # 代数性质：Sortino 估计 = 基准 → PSR = 0.5
    assert psr(sortino_hat, sortino_hat, len(rets), skew, kurt) == pytest.approx(0.5)


def pd_Skew(r):
    n = len(r)
    m = r.mean()
    s = r.std(ddof=1)
    return float((n / ((n - 1) * (n - 2))) * np.sum(((r - m) / s) ** 3))


def pd_Kurt(r):
    n = len(r)
    m = r.mean()
    s = r.std(ddof=1)
    term = np.sum(((r - m) / s) ** 4)
    return float((n * (n + 1) / ((n - 1) * (n - 2) * (n - 3))) * term
                 - 3 * (n - 1) ** 2 / ((n - 2) * (n - 3)))


# ----------------------------------------------------------------------
# R2-P3-5 f-string dunder 预筛
# ----------------------------------------------------------------------

def test_module_prescreen_rejects_format_string_dunder():
    from research.modules import _prescreen_python_source

    good = "value = close.shift(1)\nresult = value.mean()\n"
    assert _prescreen_python_source(good) == []
    # 真逃逸向量：dunder 藏在字符串常量里经 .format() 触发（f-string 内
    # 表达式本就会被 Attribute 扫描抓住）
    bad = 'value = "{0.__class__}".format(0)\n'
    errs = _prescreen_python_source(bad)
    assert any("escape" in e for e in errs)


# ----------------------------------------------------------------------
# R2-P3-3 / R2-P3-4 MCP 工具错误分类与按用户会话
# ----------------------------------------------------------------------

def test_mcp_error_payload_classification():
    from research.errors import HoldoutError
    from trend_mcp.research_tools import _error_payload

    biz = _error_payload(HoldoutError("window touches holdout; grant a token first"))
    assert biz["ok"] is False and "holdout" in biz["error"]
    internal = _error_payload(RuntimeError("sqlite disk I/O error at /secret/path"))
    assert internal == {"ok": False, "error": "internal error (see server logs)"}


def test_mcp_session_per_token_user(test_db):
    from research import sessions
    from trend_mcp.research_tools import _ai_session

    class _FakeCtx:
        class request_context:  # noqa: N801 - 模拟 mcp 请求上下文
            class request:  # noqa: N801
                scope = {"state": {"mcp_user": "friend1"}}

    s = _ai_session(test_db, _FakeCtx())
    assert s["session_id"] == "ai-mcp-friend1"
    assert s["kind"] == "ai"
    # 同用户幂等；无 ctx 回退共享默认
    assert _ai_session(test_db, _FakeCtx())["session_id"] == "ai-mcp-friend1"
    assert _ai_session(test_db, None)["session_id"] == "ai-mcp-default"


# ----------------------------------------------------------------------
# R2-P3-8 sample 脚本 --db 严格化
# ----------------------------------------------------------------------

def test_sample_arg_value_requires_value():
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "run_base_v1_sample.py"
    spec = importlib.util.spec_from_file_location("r2_sample_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # __name__ != main，不触发跑批

    sys.argv = ["run_base_v1_sample.py", "--db"]
    with pytest.raises(SystemExit, match="requires a value"):
        mod._arg_value("--db")
    sys.argv = ["run_base_v1_sample.py"]
    assert mod._arg_value("--db") is None
    sys.argv = ["run_base_v1_sample.py", "--db", "/tmp/x.db"]
    assert mod._arg_value("--db") == "/tmp/x.db"


# ----------------------------------------------------------------------
# R2VB B-1 回归钉：FastMCP Context 注入只认 Context 子类注解——
# ctx 必须以 `ctx: Context = None` 形态声明（object 注解 = 死代码）
# ----------------------------------------------------------------------

def test_mcp_write_tools_annotate_ctx_as_context():
    import ast as _ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "src" / "trend_mcp"
           / "research_tools.py").read_text(encoding="utf-8")
    tree = _ast.parse(src)
    checked = 0
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and node.name.startswith("research_"):
            for arg in node.args.args:
                if arg.arg == "ctx":
                    assert isinstance(arg.annotation, _ast.Name) and arg.annotation.id == "Context", (
                        f"{node.name}: ctx 必须注解为 mcp Context（object 注解不触发注入）"
                    )
                    checked += 1
    assert checked >= 7, f"应至少 7 个写工具带 ctx 注解，实际 {checked}"
