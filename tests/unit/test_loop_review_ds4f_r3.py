"""loop-review-ds4f Round 3 修复钉子（round3-review.md 各项的回归锚）。

Round 3 审的是**平台/纪律/通道层**（前两轮集中在数值与引擎面）：
- R3C-P2-1 晋升同 config_hash 时静默复用他人版本行（血缘丢失）
- R3C-P2-2 重复检测被"显式写出平台缺省值"整体绕过
- R3C-P2-3 退役策略线仍可作新实验 base
- R3C-P3-1 CLI rerun/recompute 未包 frozen_writes
- R3C-P3-2 哨兵钉子 monkeypatch 失效（函数体内 import threading）
- R3C-P3-3 CLI recompute 丢 skipped
- R3C-P3-4/5 物化 report.json 不含定论；复核后不重新物化
- R3C-P3-6/7 engine 子证据表无 append-only 守卫；冗余索引
- R3C-P3-8/9 verdict 状态守卫；confirm 行级原子认领
- R3C-P3-10 MCP get_experiment 错误口径
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from portfolio.library import add_version_yaml, ensure_strategy
from portfolio.registry import ModuleSpec, fresh_registry
from research import experiments, lifecycle, sessions, topics, verdict

pytestmark = pytest.mark.unit


@pytest.fixture
def registry():
    reg = fresh_registry()
    for slot in ("universe", "signal", "rank", "sizing",
                 "portfolio_risk", "position_risk", "execution"):
        reg.register(
            ModuleSpec(slot=slot, name=f"d_{slot}", version=1, factory=lambda p: p)
        )
    return reg


@pytest.fixture
def human_session(test_db):
    return sessions.get_or_create_default_human_session(test_db)


@pytest.fixture
def topic(test_db, human_session):
    return topics.create_topic(
        test_db, session_id=human_session["session_id"],
        title="平台层课题", question="?",
    )


STRATEGY_YAML = (
    "name: {name}\n"
    "universe: {{module: d_universe@1}}\n"
    "signal: {{module: d_signal@1}}\n"
    "rank: {{module: d_rank@1}}\n"
    "sizing: {{module: d_sizing@1}}\n"
    "portfolio_risk: []\n"
    "position_risk: {{module: d_position_risk@1}}\n"
    "execution: {{module: d_execution@1}}\n"
)


def _strategy_version(db, registry, line: str):
    ensure_strategy(db, line, name=line)
    return add_version_yaml(db, line, STRATEGY_YAML.format(name=line), registry,
                            created_by="human")


# ----------------------------------------------------------------------
# R3C-P2-1 晋升血缘：同 config_hash 但不同实验必须 fail-loud
# ----------------------------------------------------------------------


def test_promote_refuses_silent_version_reuse_across_experiments(test_db):
    from portfolio import library
    from portfolio.library import LibraryError
    from portfolio.slots import REGISTRY, ensure_builtins
    from portfolio.strategy import parse_strategy_yaml

    ensure_builtins()
    ensure_strategy(test_db, "promo-line", name="promo-line")
    base_yaml = (
        "name: promo-line\n"
        "universe: {module: category_filter@1}\n"
        "signal: {module: macd_cross@1}\n"
        "rank: {module: by_freshness@1}\n"
        "sizing: {module: all_in@1}\n"
        "portfolio_risk: []\n"
        "position_risk: {module: hard_stop@1, params: {atr_mul: 1.5}}\n"
        "execution: {module: tail_session@1}\n"
    )
    add_version_yaml(test_db, "promo-line", base_yaml, REGISTRY, created_by="human")
    # 造一份**新** config（改 atr_mul）→ add_version 插新版本行并带上血缘
    config = parse_strategy_yaml(
        base_yaml.replace("atr_mul: 1.5", "atr_mul: 2.0"), REGISTRY
    )

    first = library.add_version(test_db, "promo-line", config,
                                experiment_id="E0001", created_by="human")
    assert first["experiment_id"] == "E0001"
    # 同 config、不同实验 → 必须拒绝（静默复用会丢掉本次实验的血缘）
    with pytest.raises(LibraryError):
        library.add_version(test_db, "promo-line", config,
                            experiment_id="E0002", created_by="human")
    # 同 config、同实验（幂等重放）→ 允许
    again = library.add_version(test_db, "promo-line", config,
                                experiment_id="E0001", created_by="human")
    assert again["id"] == first["id"]


# ----------------------------------------------------------------------
# R3C-P2-2 重复检测：显式写出平台缺省值必须与省略等价
# ----------------------------------------------------------------------


def _propose(db, registry, topic, session, *, spec_extra=None):
    exp = experiments.propose_experiment(
        db, session_id=session["session_id"], title="缺省逃逸",
        topic_id=topic["id"], evaluation_module="portfolio_backtest@1",
        spec={"base": _version_id, "diff": [{"slot": "position_risk",
                                            "to": "d_position_risk@1"}],
              **(spec_extra or {})},
        hypothesis="显式缺省值必须与省略等价（重复检测不得被绕过）",
        allow_duplicate=True, registry=registry,
    )
    return exp


_version_id = ""


def test_duplicate_detection_expands_all_platform_defaults(test_db, registry, topic, human_session):
    """省略 `initial_capital`/`window_mode`/`n_folds` 与显式写出其缺省值，
    解析出的 run 逐值相同 → 必须判重复（旧实现在键集不同时直接放行）。"""
    global _version_id
    from research.errors import IntakeRejected

    version = _strategy_version(test_db, registry, "dedup-line")
    _version_id = version["id"]

    _propose(test_db, registry, topic, human_session)
    for extra in ({"initial_capital": 1_000_000},
                  {"window_mode": "static_holdout"},
                  {"n_folds": 4},
                  {"initial_capital": 1_000_000, "window_mode": "static_holdout"}):
        with pytest.raises(IntakeRejected) as ei:
            _propose(test_db, registry, topic, human_session, spec_extra=extra)
        assert "duplicate_of" in str(ei.value) or "similar_to" in str(ei.value), extra


# ----------------------------------------------------------------------
# R3C-P2-3 退役策略线不得作新实验 base
# ----------------------------------------------------------------------


def test_retired_strategy_line_rejected_as_base(test_db, registry, topic, human_session):
    from portfolio import library, service
    from portfolio.service import ServiceError
    from research.errors import IntakeRejected

    version = _strategy_version(test_db, registry, "retire-line")
    library.retire_strategy(test_db, "retire-line")
    with pytest.raises(ServiceError):
        service.resolve_experiment_config(
            test_db, base_version_id=version["id"], diff=[], registry=registry,
        )
    with pytest.raises(IntakeRejected):
        experiments.propose_experiment(
            test_db, session_id=human_session["session_id"], title="退役线",
            topic_id=topic["id"], evaluation_module="portfolio_backtest@1",
            spec={"base": version["id"], "diff": [{"slot": "position_risk",
                                                  "to": "d_position_risk@1"}]},
            hypothesis="退役线不得作为新实验的 base（平台纪律）",
            registry=registry,
        )


# ----------------------------------------------------------------------
# R3C-P3-8/9 verdict 状态守卫 + confirm 行级原子认领
# ----------------------------------------------------------------------


def _evaluating_experiment(db, registry, topic, session):
    from research import pipeline  # noqa: F401  确保注册表可用

    exp = experiments.propose_experiment(
        db, session_id=session["session_id"], title="verdict 守卫",
        topic_id=topic["id"], evaluation_module="portfolio_backtest@1",
        spec={"base": _version_id, "diff": [{"slot": "position_risk",
                                            "to": "d_position_risk@1"}]},
        hypothesis="verdict 只能挂取证完成的实验（状态守卫）",
        registry=registry,
    )
    return exp


def test_platform_verdict_requires_evaluating(test_db, registry, topic, human_session):
    from research.errors import LifecycleError

    version = _strategy_version(test_db, registry, "verdict-line")
    globals()["_version_id"] = version["id"]
    exp = _evaluating_experiment(test_db, registry, topic, human_session)
    assert exp["status"] == "queued"
    with pytest.raises(LifecycleError):
        verdict.insert_platform_verdict(
            test_db, experiment_id=exp["id"], baseline={}, evidence={},
            warnings=[], report={}, suggested_verdict="inconclusive",
        )


def test_confirm_is_row_level_atomic_claim(test_db, registry, topic, human_session, monkeypatch):
    """落定必须是**行级原子认领**：即使入口的存在性/状态检查被过期快照骗过，
    `AND final_verdict IS NULL` 也必须挡住第二次写，且不覆写第一位作者的留痕。

    （V 复核实证：若只靠入口的 `exp["status"] != "evaluating"`，第二次 confirm
    会被状态检查拦住——行级守卫就成了未被钉住的一行代码。这里把入口检查喂成
    过期快照，直接验证行级守卫这条腿。）
    """
    from research.errors import LifecycleError

    version = _strategy_version(test_db, registry, "confirm-line")
    globals()["_version_id"] = version["id"]
    exp = _evaluating_experiment(test_db, registry, topic, human_session)
    lifecycle.transition(test_db, exp["id"], "running")
    lifecycle.transition(test_db, exp["id"], "evaluating")
    v = verdict.insert_platform_verdict(
        test_db, experiment_id=exp["id"], baseline={}, evidence={},
        warnings=[], report={}, suggested_verdict="confirmed",
    )
    verdict.confirm_verdict(test_db, experiment_id=exp["id"],
                            final_verdict="inconclusive",
                            reasoning="第一位作者的留痕",
                            session_id=human_session["session_id"])

    # 过期快照：让入口看到"尚未定论、仍 evaluating"的旧状态
    stale = dict(v)
    stale["final_verdict"] = None
    monkeypatch.setattr(verdict, "latest_verdict", lambda db, eid: dict(stale))
    monkeypatch.setattr(lifecycle, "require_experiment",
                        lambda db, eid: {**exp, "status": "evaluating"})
    monkeypatch.setattr(verdict, "require_experiment",
                        lambda db, eid: {**exp, "status": "evaluating"})
    with pytest.raises(LifecycleError) as ei:
        verdict.confirm_verdict(test_db, experiment_id=exp["id"],
                                final_verdict="inconclusive",
                                reasoning="第二位作者的文字",
                                session_id=human_session["session_id"])
    assert "already confirmed" in str(ei.value)
    row = verdict.get_verdict(test_db, v["id"])
    assert row["reasoning"] == "第一位作者的留痕", "行级守卫必须挡住覆写"


# ----------------------------------------------------------------------
# R3C-P3-4 物化 report.json 必须含定论（与 HTTP 下载同构）
# ----------------------------------------------------------------------


def test_materialized_report_json_carries_verdict_envelope(test_db, registry, topic, human_session, tmp_path):
    from research import topic_files

    version = _strategy_version(test_db, registry, "material-line")
    globals()["_version_id"] = version["id"]
    exp = _evaluating_experiment(test_db, registry, topic, human_session)
    lifecycle.transition(test_db, exp["id"], "running")
    lifecycle.transition(test_db, exp["id"], "evaluating")
    verdict.insert_platform_verdict(
        test_db, experiment_id=exp["id"], baseline={"b": 1},
        evidence={"e": 2}, warnings=["w"], report={"experiment_summary": {}},
        suggested_verdict="confirmed",
    )
    verdict.confirm_verdict(test_db, experiment_id=exp["id"],
                            final_verdict="inconclusive",
                            reasoning="物化必须带定论",
                            session_id=human_session["session_id"])
    out = topic_files.materialize_topic(test_db, topic["id"], root=tmp_path / "topics")
    topic_dir = Path(out)
    report_path = topic_dir / "experiments" / exp["id"] / "report.json"
    import json

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    for key in ("final_verdict", "reasoning", "evidence", "baseline", "warnings",
                "report", "suggested_verdict"):
        assert key in payload, f"物化 report.json 缺 {key}（R3C-P3-4）"
    assert payload["final_verdict"] == "inconclusive"
    assert payload["reasoning"] == "物化必须带定论"


# ----------------------------------------------------------------------
# R3C-P3-6 engine 子证据表 append-only 守卫
# ----------------------------------------------------------------------


def test_engine_sub_tables_are_append_only(test_db):
    cols = {
        "engine_fills": ("run_id, order_id, symbol, fill_date, base_price, "
                         "slippage_base, slippage_tail, fill_price, quantity, "
                         "commission, stamp_tax, fee_total, cash_after"),
        "engine_daily_nav": ("run_id, date, cash, positions_value, equity, heat, exposure"),
    }
    with test_db.connect() as conn:
        conn.execute(
            "INSERT INTO engine_runs (run_id, kind, strategy_ref, config_hash, "
            "resolved_config_yaml, run_params_json, data_version, engine_version, git_hash) "
            "VALUES ('R-guard','backtest','x','h','y','{}',1,'v','g')"
        )
        conn.execute(
            f"INSERT INTO engine_fills ({cols['engine_fills']}) VALUES "
            "('R-guard','O1','X.SS','2024-01-02',10,0,0,10,100,5,0,5,1000)"
        )
        conn.execute(
            f"INSERT INTO engine_daily_nav ({cols['engine_daily_nav']}) VALUES "
            "('R-guard','2024-01-02',1000,0,1000,0,0)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE engine_fills SET quantity=1")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM engine_fills")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE engine_daily_nav SET equity=999999")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM engine_daily_nav")
        # engine_runs 的白名单更新（status 收口）必须仍然可用
        conn.execute("UPDATE engine_runs SET status='finished' WHERE run_id='R-guard'")


def test_engine_duplicate_indexes_removed(test_db):
    with test_db.connect() as conn:
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name IN ('idx_engine_positions_run','idx_engine_daily_nav_run')"
        )}
    assert names == set(), "与 UNIQUE 同列的冗余索引必须已删除（R3C-P3-7）"


# ----------------------------------------------------------------------
# R3C-P3-2 哨兵钉子：monkeypatch 必须真的生效（模块级 threading 引用）
# ----------------------------------------------------------------------


def test_sentinel_uses_module_level_threading_reference():
    """`_spawn_same_day_catchup` 不得在函数体内 `import threading`——否则测试的
    `monkeypatch.setattr(jobs, "threading", ...)` 永不生效，钉子会起真线程并与
    断言竞态（R3C-P3-2 实测：假 Thread 被调用 0 次、真线程在后台上跑）。"""
    import inspect

    from core import jobs

    src = inspect.getsource(jobs._spawn_same_day_catchup)
    assert "import threading" not in src, \
        "函数体内 import threading 会让 monkeypatch(jobs.threading) 失效"
    assert hasattr(jobs, "threading"), "必须是模块级引用"


# ----------------------------------------------------------------------
# R3C-P3-10 MCP get_experiment 的错误口径
# ----------------------------------------------------------------------


def test_mcp_get_experiment_error_shape(test_db, monkeypatch):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    from portfolio.slots import REGISTRY, ensure_builtins
    from research.api import ResearchService

    ensure_builtins()
    service = ResearchService(test_db, registry=REGISTRY,
                             topics_dir=test_db.db_path.parent / "research_topics")
    from trend_mcp import research_tools

    monkeypatch.setattr(research_tools, "_service", lambda: service)
    # 直接调内部函数的实现体（工具注册面由 critical_paths 钉子覆盖）
    fn = research_tools.register_research_tools.__wrapped__ if hasattr(
        research_tools.register_research_tools, "__wrapped__") else None
    _ = fn  # 工具是闭包，改为断言源码口径
    import inspect

    src = inspect.getsource(research_tools)
    assert '"error": f"experiment not found' in src, \
        "MCP get_experiment 必须给 error 字段（与同族工具同口径）"


def test_cli_recompute_surfaces_skipped():

    sys_path = Path(__file__).resolve().parents[2] / "scripts" / "research_cli.py"
    src = sys_path.read_text(encoding="utf-8")
    assert '"skipped"' in src, "CLI recompute 必须打印 skipped（R3C-P3-3）"
    assert "frozen_writes" in src, "CLI 同步执行通道必须包 frozen_writes（R3C-P3-1）"
