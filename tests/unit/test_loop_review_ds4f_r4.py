"""loop-review-ds4f Round 4 修复钉子（round4-review.md 各项的回归锚）。

Round 4 是**新面审查轮**（分层/数据一致/并发/UI/验收脚本），抓到 1 项 P1：
`target_weight@1.mode` 的 schema 默认值与实现的条件默认相反，`validate_params`
物化默认后**静默丢弃权重表** → `bench-60-40` 变成满仓、验收数字不可复现。

本文件的机制钉子覆盖整类：**schema 物化必须不改变任何内置模块的行为**。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

pytestmark = pytest.mark.unit


def _legal_value(rule: dict, *, avoid=None):
    """给一条 schema 规则造一个合法值（尽量避开 avoid = 该字段的默认值）。"""
    rule = rule or {}
    kind = rule.get("type")
    if kind == "dict":
        return {"PROBE.SS": 0.5}
    if kind in ("list", "array"):
        return [1, 2]
    if kind == "integer":
        lo = int(rule.get("min", 1))
        hi = int(rule.get("max", lo + 10))
        for candidate in (5, lo, hi, 7):
            if lo <= candidate <= hi and candidate != avoid:
                return candidate
        return lo
    if kind == "number":
        lo = float(rule.get("min", 0.5))
        hi = float(rule.get("max", lo + 10.0))
        for candidate in (0.5, 1.5, lo, hi):
            if lo <= candidate <= hi and candidate != avoid:
                return candidate
        return lo
    if kind == "boolean":
        return not avoid if isinstance(avoid, bool) else True
    if kind == "string":
        choices = list(rule.get("choices") or [])
        for candidate in choices:
            if candidate != avoid:
                return candidate
        for candidate in ("a", "x", "PROBE"):
            if candidate != avoid:
                return candidate
        return "a"
    return None


def _behaviour_fingerprint(instance) -> dict:
    """模块的**全部**标量实例属性（V10-ND-4：此前用手写白名单，只覆盖 66 个
    带默认值的 schema 字段中的 38 个 → 4/7 的"默认值矛盾"变异能逃逸。改为枚举
    `vars(instance)` 的标量项，整类可见）。"""
    out = {}
    for attr, value in vars(instance).items():
        if attr.startswith("__"):
            continue
        if isinstance(value, (str, int, float, bool, type(None))):
            out[attr] = value
    return out


def test_schema_defaults_do_not_change_builtin_behaviour():
    """**P1 机制守卫**（R4A-P1-1）：schema 物化（`validate_params` 的"默认值填充"）
    必须不改变任何内置模块的行为。

    对每个内置模块构造多组参数（必填-only / 必填+单个兄弟字段 / 必填+全部字段），
    比较"原样实例化"与"经 schema 物化后实例化"的行为指纹。任何"schema 默认值与
    实现隐式默认不一致"的模块都会在这里显形——`target_weight@1.mode` 的事故形态
    是 `weights` 存在时实现默认 `explicit`、而 schema 默认写着 `equal`。
    """
    from portfolio.registry import validate_params
    from portfolio.slots import REGISTRY, ensure_builtins

    ensure_builtins()
    checked = 0
    for spec in REGISTRY.list():
        schema = spec.params_schema
        if spec.kind != "builtin" or not schema:
            continue
        required = {k: _legal_value(r) for k, r in schema.items()
                    if (r or {}).get("required")}
        optional_keys = [k for k in schema if k not in required]
        variants = [dict(required)]
        for key in optional_keys:
            variants.append({**required, key: _legal_value(schema[key])})
        if optional_keys:
            variants.append({**required,
                             **{k: _legal_value(schema[k]) for k in optional_keys}})
        for params in variants:
            normalized, errors = validate_params(schema, params)
            if errors:
                continue
            try:
                raw_instance = spec.factory(dict(params))
                norm_instance = spec.factory(dict(normalized))
            except Exception:  # 缺 ctx 的模块不属本类——记日志后跳过（S112）
                import logging

                logging.getLogger(__name__).debug(
                    "skip module %s:%s (needs context to instantiate)",
                    spec.slot, spec.name,
                )
                continue
            raw_fp = _behaviour_fingerprint(raw_instance)
            norm_fp = _behaviour_fingerprint(norm_instance)
            diff = {k: (raw_fp[k], norm_fp[k]) for k in raw_fp
                    if k in norm_fp and raw_fp[k] != norm_fp[k]}
            assert not diff, (
                f"{spec.slot}:{spec.name} 在 schema 物化后行为改变 {diff}"
                f"（params={params} → normalized={normalized}）——"
                "schema 默认与实现隐式默认不一致"
            )
            checked += 1
    assert checked >= 40, f"对照组合过少（{checked}），钉子可能失效"


def test_target_weight_keeps_explicit_weights_without_mode():
    """P1 直测：写了 weights 不写 mode → 必须按显式表（不是均分）。"""
    from portfolio.registry import validate_params
    from portfolio.slots import REGISTRY, ensure_builtins

    ensure_builtins()
    spec = REGISTRY.get("target_weight@1", slot="sizing")
    normalized, errors = validate_params(spec.params_schema, {"weights": {"510300.SS": 0.6}})
    assert not errors
    assert "mode" not in normalized, "schema 不得物化 mode（条件默认由实现决定）"
    assert spec.factory(dict(normalized)).mode == "explicit"

    # 反向：不给 weights 才是均分
    norm2, _ = validate_params(spec.params_schema, {})
    assert spec.factory(dict(norm2)).mode == "equal"

    # 显式 mode 必须仍然生效
    norm3, _ = validate_params(spec.params_schema,
                               {"weights": {"510300.SS": 0.6}, "mode": "equal"})
    assert spec.factory(dict(norm3)).mode == "equal"


def test_bench_60_40_keeps_the_60_percent_weight():
    """P1 端到端：`bench-60-40` 的 sizing 必须仍是 510300 0.6（不是满仓）。"""
    from pathlib import Path

    from portfolio.slots import REGISTRY, ensure_builtins
    from portfolio.strategy import parse_strategy_yaml

    ensure_builtins()
    strategies = Path(__file__).resolve().parents[2] / "src" / "portfolio" / "strategies"
    cfg = parse_strategy_yaml(
        (strategies / "bench_bond_stock_60_40.yaml").read_text(encoding="utf-8"),
        REGISTRY,
    )
    binding = cfg.slots["sizing"]
    assert binding.module.startswith("target_weight@")
    weights = (binding.params or {}).get("weights") or {}
    assert weights.get("510300.SS") == pytest.approx(0.6), weights
    inst = REGISTRY.get(binding.module, slot="sizing").factory(dict(binding.params or {}))
    assert inst.mode == "explicit", "有 weights 时必须走显式表"


# ----------------------------------------------------------------------
# R4A-P3-3/4：worker stop() 不得丢派发/泄漏计数，也不得起第二个调度线程
# ----------------------------------------------------------------------


def _mk_worker(test_db, *, cap=4, workers=1):
    from portfolio.slots import REGISTRY, ensure_builtins
    from research.worker import ResearchWorker

    ensure_builtins()
    return ResearchWorker(test_db, registry=REGISTRY, max_workers=workers,
                          per_session_cap=cap)


@pytest.fixture
def registry():
    from portfolio.registry import ModuleSpec, fresh_registry

    reg = fresh_registry()
    for slot in ("universe", "signal", "rank", "sizing",
                 "portfolio_risk", "position_risk", "execution"):
        reg.register(
            ModuleSpec(slot=slot, name=f"d_{slot}", version=1, factory=lambda p: p)
        )
    return reg


def test_worker_stop_requeues_cancelled_dispatches(test_db, monkeypatch):
    """stop() 取消"已派发未开跑"的 future 时，必须把实验放回队列并回退会话计数
    （否则实验永远停在 queued、计数泄漏到 cap 后该会话的实验永不执行）。"""
    import time as _t

    from research import experiments, sessions, topics
    from research.errors import IntakeRejected  # noqa: F401

    session = sessions.get_or_create_default_human_session(test_db)
    topic = topics.create_topic(test_db, session_id=session["session_id"],
                                title="worker-stop", question="?")
    from portfolio.library import add_version_yaml, ensure_strategy
    from portfolio.slots import REGISTRY, ensure_builtins

    ensure_builtins()
    ensure_strategy(test_db, "wstop-line", name="wstop")
    base_yaml = (
        "name: wstop-line\n"
        "universe: {module: category_filter@1}\n"
        "signal: {module: macd_cross@1}\n"
        "rank: {module: by_freshness@1}\n"
        "sizing: {module: all_in@1}\n"
        "portfolio_risk: []\n"
        "position_risk: {module: hard_stop@1, params: {atr_mul: 1.5}}\n"
        "execution: {module: tail_session@1}\n"
    )
    ver = add_version_yaml(test_db, "wstop-line", base_yaml, REGISTRY, created_by="human")
    exp_ids = []
    for i in range(5):
        exp = experiments.propose_experiment(
            test_db, session_id=session["session_id"], title=f"w{i}",
            topic_id=topic["id"], evaluation_module="portfolio_backtest@1",
            spec={"base": ver["id"], "diff": [{"slot": "position_risk",
                                              "to": "hard_stop@1",
                                              "params": {"atr_mul": 1.5 + i * 0.1}}]},
            hypothesis=f"worker stop 语义 {i}（必须不丢派发）",
            allow_duplicate=True, registry=REGISTRY,
        )
        exp_ids.append(exp["id"])

    worker = _mk_worker(test_db)
    # 慢 runner：让 1 个在跑、多个在排队，然后在突发中 stop
    def _slow(db, exp_id, **_kw):
        _t.sleep(3)
        return {"suggested_verdict": "inconclusive", "warnings": [], "evidence": {},
                "report": {}, "baseline": {}, "runs": []}

    monkeypatch.setattr("research.worker.run_experiment", _slow)
    for exp_id in exp_ids:
        worker.submit(exp_id)  # 真实入队形态（worker 不扫库，由调用方入队）
    worker.start()
    _t.sleep(1.2)
    worker.stop()
    # 等在跑的慢 runner 收尾（3s sleep），再核对账本
    _t.sleep(3.5)
    st = worker.status()
    # (a) 会话计数不得泄漏：4 个被派发（1 完成 + 3 被取消回灌）后必须归零
    assert sum(st.get("active_by_session", {}).values()) == 0, f"会话计数泄漏（{st}）"
    # (b) 不得丢派发：5 个实验里恰好 1 个被 worker 跑过（桩 runner 不改状态机），
    # 其余 4 个必须回到**队列**（不只是回集合——R6 复核：只 `_queued_ids.add`
    # 而漏 `_queue.put` 时本断言仍会绿，而实验其实再也派发不出去）。
    assert len(worker._queued_ids) == len(exp_ids) - 1, (
        f"被取消的派发未全部回灌队列：queued_ids={sorted(worker._queued_ids)} "
        f"（status={st}）"
    )
    assert all(e in worker._queued_ids for e in exp_ids[1:]),         "除首个（在跑）外都应回到队列"
    queued_in_fifo = set()
    while not worker._queue.empty():
        queued_in_fifo.add(worker._queue.get_nowait())
    assert queued_in_fifo == set(exp_ids[1:]), (
        f"回灌必须落回 `_queue`（只进集合 = 监控说谎且再也派发不出去）："
        f"fifo={sorted(queued_in_fifo)} vs 期望 {sorted(exp_ids[1:])}"
    )
    for e in queued_in_fifo:
        worker._queue.put(e)  # 复位，避免影响后续断言"


def test_worker_start_refuses_second_live_dispatcher(test_db):
    """旧调度线程活着时 start() 不得起第二个（R4A-P3-4）。"""
    worker = _mk_worker(test_db)

    class _AliveThread:
        def is_alive(self):
            return True

        def join(self, timeout=None):
            return None

    worker._dispatcher = _AliveThread()
    worker.start()
    assert worker._dispatcher.__class__ is _AliveThread, "不得被替换成新线程"


# ----------------------------------------------------------------------
# R4A-P3-5/7：通道不写库；表单字段有上限与存在性校验
# ----------------------------------------------------------------------


def test_mcp_channel_does_not_write_the_db_directly():
    """R4A-P3-5：MCP 通道不得自己执行 SQL（会话策略归服务面）。"""
    import inspect
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2]
           / "src" / "trend_mcp" / "research_tools.py").read_text(encoding="utf-8")
    assert "INSERT " not in src.upper(), "通道内不得出现 INSERT（薄通道厚服务）"
    assert "ensure_channel_session" in src, "必须经服务面的会话装配函数"
    _ = inspect


def test_session_face_ensure_channel_session_is_idempotent(test_db):
    from research.sessions import ensure_channel_session

    first = ensure_channel_session(test_db, session_id="ai-mcp-probe",
                                   label="AI（MCP·probe）", channel="mcp")
    assert first["session_id"] == "ai-mcp-probe" and first["kind"] == "ai"
    again = ensure_channel_session(test_db, session_id="ai-mcp-probe",
                                   label="改个标签", channel="mcp")
    assert again["session_id"] == first["session_id"]
    assert again["label"] == first["label"], "幂等：已存在不覆写"


def test_grant_token_rejects_oversize_and_unknown_experiment(test_db):
    from research import holdout, sessions
    from research.holdout import HoldoutError

    session = sessions.get_or_create_default_human_session(test_db)
    with pytest.raises(HoldoutError):
        holdout.grant_token(test_db, session_id=session["session_id"],
                           purpose="x" * 201)
    with pytest.raises(HoldoutError):
        holdout.grant_token(test_db, session_id=session["session_id"],
                           purpose="ok", experiment_id="E-NOPE")


def test_confirm_rejects_oversize_reasoning(test_db, registry=None):
    """R4A-P3-7：reasoning 有上限（此前 70k 字也能落库）。"""
    import inspect

    from research import verdict

    src = inspect.getsource(verdict.confirm_verdict)
    assert "max 4000 chars" in src


# ----------------------------------------------------------------------
# V10 复核：回灌必须落回 `_queue`（只进集合不等于可再派发）
# ----------------------------------------------------------------------


def test_worker_stop_before_start_keeps_experiments_dispatchable(test_db):
    """V10-ND-1：stop() 早于 start()（或 stop 后重启）时，队列里的实验必须
    仍然**可被再次派发**——`_queued_ids` 与 `_queue` 都要有它。"""
    import time as _t

    from portfolio.library import add_version_yaml, ensure_strategy
    from portfolio.slots import REGISTRY, ensure_builtins
    from research import experiments, lifecycle, sessions, topics

    ensure_builtins()
    session = sessions.get_or_create_default_human_session(test_db)
    topic = topics.create_topic(test_db, session_id=session["session_id"],
                                title="worker-requeue", question="?")
    ensure_strategy(test_db, "requeue-line", name="requeue")
    ver = add_version_yaml(test_db, "requeue-line", (
        "name: requeue-line\n"
        "universe: {module: category_filter@1}\n"
        "signal: {module: macd_cross@1}\n"
        "rank: {module: by_freshness@1}\n"
        "sizing: {module: all_in@1}\n"
        "portfolio_risk: []\n"
        "position_risk: {module: hard_stop@1, params: {atr_mul: 1.5}}\n"
        "execution: {module: tail_session@1}\n"
    ), REGISTRY, created_by="human")
    exp = experiments.propose_experiment(
        test_db, session_id=session["session_id"], title="requeue",
        topic_id=topic["id"], evaluation_module="portfolio_backtest@1",
        spec={"base": ver["id"], "diff": [{"slot": "position_risk",
                                          "to": "hard_stop@1", "params": {"atr_mul": 2.0}}]},
        hypothesis="stop 后实验必须仍可被再次派发（回灌要落回队列）",
        allow_duplicate=True, registry=REGISTRY,
    )
    worker = _mk_worker(test_db)
    assert worker.submit(exp["id"]) is True
    worker.stop()  # 从未 start
    assert exp["id"] in worker._queued_ids
    # 关键：必须也能"再派发"——start() 后 dispatcher 能把它取走并提交
    import research.worker as _w

    seen: list[str] = []
    _orig = _w.run_experiment

    def _spy(db, exp_id, **kw):
        seen.append(exp_id)
        return {"suggested_verdict": "inconclusive", "warnings": [], "evidence": {},
                "report": {}, "baseline": {}, "runs": []}

    _w.run_experiment = _spy
    try:
        worker.start()
        _t.sleep(1.5)
        worker.stop()
    finally:
        _w.run_experiment = _orig
    assert exp["id"] in seen, (
        "stop() 之后的实验没有被重新派发（回灌只进了集合没进队列）"
        f"（seen={seen}，status={lifecycle.get_experiment(test_db, exp['id'])['status']}）"
    )


def test_worker_start_allows_restart_after_dispatcher_exits(test_db):
    """V10-ND-3：旧调度线程已退出时，start() 必须允许重启（清引用）。"""
    worker = _mk_worker(test_db)

    class _DeadThread:
        def is_alive(self):
            return False

        def join(self, timeout=None):
            return None

    worker._dispatcher = _DeadThread()
    worker.start()
    assert worker._dispatcher is not None
    assert worker._dispatcher.__class__ is not _DeadThread, "应起一个新调度线程"
    worker.stop()


def test_empty_tradability_frame_has_the_same_columns():
    """V10-ND-5：空帧与非空帧列集必须一致。"""
    from gateway.tradability import _empty_frame, compute_tradability

    empty = _empty_frame()
    non_empty = compute_tradability(
        None, symbols=["600519.SS"], dates=[date(2024, 3, 11)],
        raw_closes={"600519.SS": pd.Series({date(2024, 3, 11): 10.0})},
        ex_factors={"600519.SS": []}, listing_dates={"600519.SS": None},
    )
    assert set(empty.columns) == set(non_empty.columns), (
        f"空帧缺列：{set(non_empty.columns) - set(empty.columns)}"
    )
    assert "listing_known" in non_empty.columns


def test_tradability_listing_known_flag():
    """V10-ND-5：`listing_known` 必须如实反映"上市日是否已知"。"""
    from gateway.tradability import compute_tradability

    days = [date(2024, 3, 11), date(2024, 3, 12)]
    closes = pd.Series({date(2024, 3, 11): 10.0, date(2024, 3, 12): 11.0})
    unknown = compute_tradability(
        None, symbols=["600519.SS"], dates=days, raw_closes={"600519.SS": closes},
        ex_factors={"600519.SS": []}, listing_dates={"600519.SS": None},
    )
    assert bool(unknown.iloc[0]["listing_known"]) is False
    known = compute_tradability(
        None, symbols=["600519.SS"], dates=days, raw_closes={"600519.SS": closes},
        ex_factors={"600519.SS": []}, listing_dates={"600519.SS": "2015-01-05"},
    )
    assert bool(known.iloc[0]["listing_known"]) is True
