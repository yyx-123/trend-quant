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
        # 五张子表各插入一行，逐表验证 UPDATE/DELETE 都被拒（R6 复核：此前只钉
        # 了 fills/daily_nav 两张，orders/unfilled/positions 的守卫可被删而测试仍绿）
        conn.execute(
            "INSERT INTO engine_orders (run_id, order_id, decision_date, "
            "target_fill_date, symbol, side, order_type, intent_type, intent_value, "
            "source, status) VALUES ('R-guard','O-guard','2024-01-02','2024-01-02',"
            "'X.SS','buy','tail_market','quantity',100,'signal','filled')"
        )
        conn.execute(
            "INSERT INTO engine_unfilled (run_id, order_id, symbol, decision_date, "
            "reason, intent_snapshot_json) VALUES ('R-guard','O-u','X.SS','2024-01-02',"
            "'limit_up','{}')"
        )
        conn.execute(
            "INSERT INTO engine_positions (run_id, date, symbol, quantity, "
            "sellable_quantity, avg_cost, entry_price, stop_price, highest_since_buy, "
            "atr_at_entry) VALUES ('R-guard','2024-01-02','X.SS',100,0,10.0,10.0,9.0,10.0,0.2)"
        )
        for table, update_sql in (
            ("engine_orders", "UPDATE engine_orders SET status='x'"),
            ("engine_fills", "UPDATE engine_fills SET quantity=1"),
            ("engine_unfilled", "UPDATE engine_unfilled SET reason='x'"),
            ("engine_positions", "UPDATE engine_positions SET quantity=1"),
            ("engine_daily_nav", "UPDATE engine_daily_nav SET equity=999999"),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(update_sql)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(f"DELETE FROM {table}")
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


# ----------------------------------------------------------------------
# V7 复核后的补钉（ND-1..ND-6）
# ----------------------------------------------------------------------


def test_recompute_rematerializes_with_the_right_root(test_db, registry, topic, human_session, tmp_path):
    """ND-1：复核 campaign 的重新物化必须**真的跑起来**（root 参数不能缺）。

    V7 复核实证：初版调用漏了 keyword-only 的 `root` → TypeError 被本地 except
    吞掉、每个 campaign 都往 stderr 打 traceback、且从不物化。
    """
    version = _strategy_version(test_db, registry, "remat-line")
    globals()["_version_id"] = version["id"]
    exp = _evaluating_experiment(test_db, registry, topic, human_session)
    lifecycle.transition(test_db, exp["id"], "running")
    lifecycle.transition(test_db, exp["id"], "evaluating")
    verdict.insert_platform_verdict(
        test_db, experiment_id=exp["id"], baseline={}, evidence={},
        warnings=[], report={}, suggested_verdict="confirmed",
    )
    verdict.confirm_verdict(test_db, experiment_id=exp["id"],
                            final_verdict="confirmed", reasoning="准入复核",
                            session_id=human_session["session_id"])
    from research.recompute import recompute_campaign

    out = recompute_campaign(
        test_db, old_module_ref="d_position_risk@1", new_module_ref="d_position_risk@1",
        registry=registry, topics_dir=tmp_path / "topics_dir",
    )
    assert out["rematerialized_topics"] == [topic["id"]], out
    produced = list((tmp_path / "topics_dir").rglob("report.json"))
    assert produced, "复核后必须产出物化文件（ND-1）"


def test_materialized_report_spec_matches_http_envelope(test_db, registry, topic, human_session, tmp_path):
    """ND-2：物化 report.json 的 `spec` 必须与 HTTP 下载一致（不能是 null）。"""
    from research import topic_files

    version = _strategy_version(test_db, registry, "spec-env-line")
    globals()["_version_id"] = version["id"]
    exp = _evaluating_experiment(test_db, registry, topic, human_session)
    lifecycle.transition(test_db, exp["id"], "running")
    lifecycle.transition(test_db, exp["id"], "evaluating")
    verdict.insert_platform_verdict(
        test_db, experiment_id=exp["id"], baseline={}, evidence={},
        warnings=[], report={}, suggested_verdict="confirmed",
    )
    verdict.confirm_verdict(test_db, experiment_id=exp["id"],
                            final_verdict="inconclusive", reasoning="信封一致",
                            session_id=human_session["session_id"])
    out = topic_files.materialize_topic(test_db, topic["id"], root=tmp_path / "t2")
    import json

    payload = json.loads(
        (Path(out) / "experiments" / exp["id"] / "report.json").read_text(encoding="utf-8")
    )
    assert payload["spec"] is not None, "物化信封的 spec 不得为 null（ND-2）"
    assert payload["spec"].get("base") == version["id"]


def test_platform_defaults_table_covers_all_declared_fields():
    """ND-3：缺省值表必须覆盖**全部**声明字段（否则"显式缺省即逃逸"会复发）。

    这是机制性守卫：新增声明字段而不登记缺省值 → 本用例失败。
    """
    from research.experiments import _DECLARED_SPEC_FIELDS, _MODULE_SPEC_DEFAULTS

    # `base`/`diff`/`event` 是**必填**字段（无缺省），其余声明字段必须有缺省登记
    required = {"base", "diff", "event", "signal_module", "feature", "metric", "ref"}
    for module_key, declared in _DECLARED_SPEC_FIELDS.items():
        table = _MODULE_SPEC_DEFAULTS.get(module_key, {})
        missing = sorted(declared - set(table) - required)
        assert not missing, (
            f"{module_key} 的声明字段缺省值未登记：{missing}"
            "（显式写出缺省值会绕过重复检测——见 R3C-P2-2/ND-3）"
        )


def test_explicit_defaults_of_all_fields_collide_with_omission(test_db, registry, topic, human_session):
    """ND-3 行为面：把声明字段逐个显式写成缺省值，都必须判重复。"""
    global _version_id
    from research.errors import IntakeRejected

    version = _strategy_version(test_db, registry, "allextra-line")
    _version_id = version["id"]
    _propose(test_db, registry, topic, human_session)
    for extra in ({"expect": "positive"}, {"mc_bands": True},
                  {"is_compound": False}, {"compound_reason": ""}):
        with pytest.raises(IntakeRejected) as ei:
            _propose(test_db, registry, topic, human_session, spec_extra=extra)
        assert "duplicate_of" in str(ei.value) or "similar_to" in str(ei.value), extra


def test_retired_line_existing_experiment_still_runnable(test_db, registry, topic, human_session):
    """ND-4：退役线只禁**新引用**——既有实验的复现/复核必须仍可跑。"""
    from portfolio import library, service
    from portfolio.service import ServiceError

    version = _strategy_version(test_db, registry, "rerun-retired-line")
    library.retire_strategy(test_db, "rerun-retired-line")
    with pytest.raises(ServiceError):
        service.resolve_experiment_config(
            test_db, base_version_id=version["id"], diff=[], registry=registry,
        )
    # 复现路径：allow_retired=True → 可解析
    config, _yaml = service.resolve_experiment_config(
        test_db, base_version_id=version["id"], diff=[], registry=registry,
        allow_retired=True,
    )
    assert config is not None


def test_promote_refuses_version_without_lineage(test_db, registry):
    """ND-5：既有版本行**无血缘**（种子/人工版本）时同样不得静默复用。"""
    from portfolio import library
    from portfolio.library import LibraryError
    from portfolio.slots import REGISTRY, ensure_builtins
    from portfolio.strategy import parse_strategy_yaml

    ensure_builtins()
    ensure_strategy(test_db, "seed-line", name="seed-line")
    yaml_text = (
        "name: seed-line\n"
        "universe: {module: category_filter@1}\n"
        "signal: {module: macd_cross@1}\n"
        "rank: {module: by_freshness@1}\n"
        "sizing: {module: all_in@1}\n"
        "portfolio_risk: []\n"
        "position_risk: {module: hard_stop@1, params: {atr_mul: 1.5}}\n"
        "execution: {module: tail_session@1}\n"
    )
    add_version_yaml(test_db, "seed-line", yaml_text, REGISTRY, created_by="human")
    config = parse_strategy_yaml(yaml_text, REGISTRY)
    with pytest.raises(LibraryError):
        library.add_version(test_db, "seed-line", config,
                            experiment_id="E0009", created_by="experiment")


def test_mcp_error_payload_classifies_library_errors():
    """ND-6：业务异常（LibraryError/ServiceError）不得落进"internal error"。"""
    import sys

    from portfolio.library import LibraryError
    from portfolio.service import ServiceError

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    from trend_mcp.research_tools import _error_payload

    for exc in (LibraryError("config already exists as x@1"), ServiceError("base not found")):
        payload = _error_payload(exc)
        assert payload["ok"] is False
        assert "internal error" not in payload["error"], payload


# ----------------------------------------------------------------------
# V8 复核后的补钉
# ----------------------------------------------------------------------


def test_recompute_campaign_uses_the_service_topics_dir(test_db, registry, topic, human_session, tmp_path):
    """V8 阻断 1：服务面必须把 `topics_dir` 透传给 campaign（否则产物落到仓库默认目录）。"""
    import inspect

    from research import api as research_api

    monkeypatch_src = inspect.getsource(research_api.ResearchService.recompute_campaign)
    assert "topics_dir=self.topics_dir" in monkeypatch_src, \
        "ResearchService.recompute_campaign 必须透传 self.topics_dir"


def test_primary_horizon_default_is_module_aware(test_db, registry, topic, human_session):
    """V8 阻断 2：只有**声明了** `primary_horizon` 的模块才现算 `horizons[0]`。

    给未声明该字段的 bucket_analysis 注入它，会让键集与省略形态不同 →
    同 subject_key 的第二个分桶实验被判 similar_to（整类实验被误杀）。
    """
    from research.experiments import _expand_platform_defaults

    bucket = _expand_platform_defaults(
        {"signal_module": "macd_cross@1", "feature": "atr_pct", "horizons": [10, 20]},
        "bucket_analysis@1",
    )
    assert "primary_horizon" not in bucket, "bucket_analysis 未声明 primary_horizon"

    event = _expand_platform_defaults(
        {"event": "macd_cross@1", "horizons": [5, 10, 20]}, "event_study@1",
    )
    assert event["primary_horizon"] == 5, "event_study 的缺省 = horizons[0]（runner 语义）"


def test_falsy_defaults_are_normalized(test_db, registry, topic, human_session):
    """V8 残留：runner 用 `or default` 的字段，falsy 值等价于省略。"""
    from research.experiments import _expand_platform_defaults

    for spec, module, key, expected in (
        ({"n_folds": 0}, "portfolio_backtest@1", "n_folds", 4),
        ({"window_mode": ""}, "portfolio_backtest@1", "window_mode", "static_holdout"),
    ):
        out = _expand_platform_defaults(
            {"base": "b@1", "diff": [], **spec}, module,
        )
        assert out[key] == expected, (key, out)


# ----------------------------------------------------------------------
# V9 复核 R1：退役种子线不得被 seed 流程复活（否则所有回测都 failed）
# ----------------------------------------------------------------------


def test_retired_seed_line_does_not_break_every_backtest(test_db, registry):
    """V9-R1：退役任一种子线后，seed 流程必须跳过它而不是抛错——
    `add_version` 对退役线显式拒绝，而 seed 每次 run 前都跑（幂等），
    此前会让**所有** portfolio_backtest 在 seed 处失败（含既有实验复现）。"""
    from portfolio import library
    from portfolio.seed import seed_default_library
    from portfolio.slots import REGISTRY, ensure_builtins

    ensure_builtins()
    seeded = seed_default_library(test_db, REGISTRY)
    assert seeded, "种子库必须可入库"
    victim = "bench-60-40"
    assert victim in seeded
    library.retire_strategy(test_db, victim)
    # 再次 seed（= 每次 run 前的幂等步骤）不得抛错，且不复活退役线
    again = seed_default_library(test_db, REGISTRY)
    assert victim not in again, "退役的种子线不得被 seed 复活"
    assert "base-v1" in again, "其余种子线照常"
    line = library.get_strategy(test_db, victim)
    assert line.get("retired_at"), "退役标记必须保留"
    assert library.list_versions(test_db, victim), "已发布的版本行必须仍在（历史可复现）"


def test_settled_final_verdict_is_immutable_at_db_level(test_db):
    """R6 复核（M24 survivor）：`已落定 final_verdict 不可改写`这条**库层**守卫
    必须由**行为**钉住——此前只有源码注释里出现 "final_verdict" 字样，删掉 WHEN
    子句后 3 条相关钉子仍绿（实测可改写已定论行）。"""
    from research import sessions, verdict

    session = sessions.get_or_create_default_human_session(test_db)
    topic = topics.create_topic(test_db, session_id=session["session_id"],
                                title="定论不可改写", question="?")
    with test_db.connect() as conn:
        conn.execute(
            """INSERT INTO research_experiments
               (id, title, owner_session, created_by, topic_id, subject_key,
                evaluation_module, spec_json, hypothesis, status, attempt_index,
                is_reproduction)
               VALUES ('E-IMM','定论','human-default','human',?,'imm',
                       'portfolio_backtest@1','{}','h','verdicted',1,0)""",
            (topic["id"],),
        )
    row = verdict.insert_platform_verdict(
        test_db, experiment_id="E-IMM", baseline={}, evidence={}, warnings=[],
        report={}, suggested_verdict="confirmed",
    )
    with test_db.connect() as conn:
        conn.execute(
            "UPDATE research_verdicts SET final_verdict='inconclusive' WHERE id=?",
            (row["id"],),
        )
    # 已落定后：直改 final_verdict 必须被库层拒绝
    with test_db.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE research_verdicts SET final_verdict='rejected' WHERE id=?",
                (row["id"],),
            )
        # 白名单列（reasoning/confirmed_by/时间戳）仍可写
        conn.execute(
            "UPDATE research_verdicts SET reasoning='改理由' WHERE id=?", (row["id"],)
        )
