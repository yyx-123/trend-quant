"""loop-review-ds4f Round 2 修复钉子（round2-review.md 各项的回归锚）。

Round 2 审出的问题集中在**Round 1 修复代码自身**：
- R2A-P1-1：parity 的 NAV 级联上限用"滑点上界复合乘积"，数千笔时发散
  → +100% 净值错误又被吸收（Round 1 的钉子只测了 1 笔前缀）；
- R2A-P2-1：同时，"该笔数量的 1/2"硬帽会把合法长 run 判成超纲（假报警）；
- R2A-P3-1..9：plateau 参数语义与 apply_diff 不一致、wf 探针 window_kind
  记错、heat_cap 留痕原因说谎、哨兵忙循环、holdout 文档失真、purpose 校验
  只在路由层、PBO 注释理由失实、lifespan 钉子不验 after_update。
"""

from __future__ import annotations

import time as _time
from datetime import date, datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


# ----------------------------------------------------------------------
# R2A-P1-1 / R2A-P2-1：parity 归因界必须是物理量（不是滑点上界的复合乘积）
# ----------------------------------------------------------------------


def _res(trades, nav, legacy=False):
    if legacy:
        trades = [{"date": t["date"], "side": t["side"], "qty": t["qty"],
                   "exec_price": t["price"]} for t in trades]
    return {"trades": trades, "daily_nav": nav}


def _slippage_run(n_prefix: int, *, slip: float = 0.001, qty: int = 1000,
                  price: float = 10.0, last_qty_mult: float = 1.0,
                  nav_rel: float = 0.0, equity: float = 1e6):
    """n_prefix 笔合法尾盘价差 + 末笔可注入异常。"""
    d = date(2023, 1, 4)
    new, old = [], []
    for _ in range(n_prefix):
        new.append({"date": d, "side": "BUY", "qty": qty, "price": price * (1 + slip)})
        old.append({"date": d, "side": "BUY", "qty": qty, "price": price})
    new.append({"date": d, "side": "BUY", "qty": int(qty * last_qty_mult),
                "price": price * (1 + slip)})
    old.append({"date": d, "side": "BUY", "qty": qty, "price": price})
    nav_new = [{"date": "2023-01-03", "equity": equity},
               {"date": "2023-01-04", "equity": equity * (1 + nav_rel)}]
    nav_old = [{"date": "2023-01-03", "equity": equity},
               {"date": "2023-01-04", "equity": equity}]
    return _res(new, nav_new), _res(old, nav_old, legacy=True)


def test_parity_physical_bound_rejects_absurd_nav_error_on_long_prefix():
    """**R2A-P1-1 的核心回归**：长前缀下 +100% 净值错误必须仍被判超纲。

    旧口径（滑点上界复合乘积 Π(1+slip)，300 笔时 ≈ 0.39 → ×3 = 1.17）
    会把 +100% 错误吸收；新口径（累计额外成本 ÷ 权益 × 5）在 300 笔时
    ≈ 1%，远小于 100%。
    """
    from engine.parity import attribute_diffs

    new, old = _slippage_run(300, nav_rel=1.0)
    out = attribute_diffs(new, old)
    assert any(u.get("kind") == "nav_point_diff_beyond_interest" for u in out["unexplained"]), \
        f"长前缀下的 +100% 净值错误必须判超纲（实际 {out['unexplained']}）"
    assert out["nav_cascade_bound"] < 0.2, f"上界应仍紧（实际 {out['nav_cascade_bound']}）"


def test_parity_physical_bound_rejects_absurd_qty_drift():
    """数量漂移超过"累计额外成本能买的股数"→ 必须判超纲（R2A-P2-2 收口）。"""
    from engine.parity import attribute_diffs

    # 120 笔 × 0.1% × 1000 股 ≈ 1200 元额外成本 → 只能解释 120 股
    new, old = _slippage_run(120, last_qty_mult=2.0)
    out = attribute_diffs(new, old)
    assert any(u.get("kind") == "trade_mismatch" for u in out["unexplained"]), \
        "数量 +100% 不得被当作滑点下游"
    # 反向：漂移在累计成本能解释的范围内 → 必须归类（不得假报警）
    new2, old2 = _slippage_run(600, last_qty_mult=1.05)
    out2 = attribute_diffs(new2, old2)
    assert not [u for u in out2["unexplained"] if u.get("kind") == "trade_mismatch"], \
        "累计成本能解释的漂移不得判超纲（Round 1 的 1/2 硬帽会误报）"


def test_parity_reports_saturation_flag():
    """判别力饱和必须显式标注（长 run 的 `unexplained == []` 不等于一致）。"""
    from engine.parity import attribute_diffs

    short_new, short_old = _slippage_run(30, nav_rel=0.0)
    short = attribute_diffs(short_new, short_old)
    assert short["saturated"] is False, "stage-1 验收尺度不饱和"
    assert short["attribution_note"] == ""
    assert "n_trade_diffs" in short and "nav_cascade_bound" in short

    long_new, long_old = _slippage_run(600, nav_rel=0.0)
    long_out = attribute_diffs(long_new, long_old)
    assert long_out["saturated"] is True
    assert "判别力饱和" in long_out["attribution_note"]


# ----------------------------------------------------------------------
# R2A-P3-1：plateau 参数取值必须与 apply_diff 逐字一致（整体或，不是合并）
# ----------------------------------------------------------------------


def test_plateau_items_matches_apply_diff_param_semantics():
    """两处都非空且不相交时，枚举值必须是 apply_diff 实际采用的那一侧。

    用**真实内置件**（hard_stop@1 的 atr_mul/atr_period）与真实 apply_diff，
    避免桩 schema 掩盖语义差异。
    """
    from portfolio.slots import REGISTRY, ensure_builtins
    from portfolio.strategy import apply_diff, parse_strategy_yaml
    from research.evaluations.backtest import _plateau_items

    ensure_builtins()
    reg = REGISTRY
    yaml_text = (
        "name: t\n"
        "universe: {module: category_filter@1}\n"
        "signal: {module: macd_cross@1}\n"
        "rank: {module: by_freshness@1}\n"
        "sizing: {module: all_in@1}\n"
        "portfolio_risk: []\n"
        "position_risk: {module: hard_stop@1, params: {atr_mul: 1.5, atr_period: 20}}\n"
        "execution: {module: tail_session@1}\n"
    )
    cfg = parse_strategy_yaml(yaml_text, reg)
    diff = [{"slot": "position_risk", "from": "hard_stop@1",
             "to": {"module": "hard_stop@1", "params": {"atr_period": 30}},
             "params": {"atr_mul": 2.0}}]
    resolved = apply_diff(cfg, diff, reg)
    applied = resolved.slots["position_risk"].params
    # apply_diff 的语义：to.params 非空 → 整体取 to.params，item 级 params 被忽略
    assert applied["atr_period"] == 30
    assert applied["atr_mul"] == 1.5, "apply_diff 忽略了 item 级 params（应保持默认 1.5）"

    items = {i["param"]: i["value"] for i in _plateau_items(diff)}
    assert items.get("atr_period") == 30
    assert "atr_mul" not in items,         "枚举值必须与 apply_diff 一致（item 级 params 被忽略时不得当作被改动的参数）"

    # 反向形态：to 只给模块不给 params → 参数取 item 级 params
    diff2 = [{"slot": "position_risk", "from": "hard_stop@1",
              "to": {"module": "hard_stop@1"}, "params": {"atr_mul": 2.5}}]
    resolved2 = apply_diff(cfg, diff2, reg)
    assert resolved2.slots["position_risk"].params["atr_mul"] == 2.5
    items2 = {i["param"]: i["value"] for i in _plateau_items(diff2)}
    assert items2.get("atr_mul") == 2.5


# ----------------------------------------------------------------------
# R2A-P3-2：wf 高原探针的 engine_runs.window_kind 必须记 plateau_probe
# ----------------------------------------------------------------------


def test_wf_plateau_probe_records_window_kind():
    import inspect

    from research.evaluations import backtest as bt

    src = inspect.getsource(bt._run_walk_forward_exp_leg)
    assert "window_kind" in src, "探针腿必须能覆盖 window_kind"
    call_src = inspect.getsource(bt._assemble_result)
    assert 'window_kind="plateau_probe"' in call_src, \
        "wf 探针必须显式覆盖 window_kind（否则 engine_runs 记 sample，与 research_runs 矛盾）"


# ----------------------------------------------------------------------
# R2A-P3-3：heat_cap 的留痕原因必须如实（price 缺失 ≠ 无止损估计）
# ----------------------------------------------------------------------


def test_heat_cap_gate_log_reason_is_truthful():
    from engine.models import OrderIntent
    from portfolio.slots.portfolio_risk import HeatCapGate

    class _Acct:
        def equity(self):
            return 1_000_000.0

        def heat(self):
            return 0.0

        def unstopped_symbols(self):
            return []

    class _Ctx:
        def __init__(self, price):
            self.account = _Acct()
            self.panel = type("P", (), {"value": lambda self, s, f: price})()
            self.date = date(2024, 3, 11)
            self.gate_log: list = []
            self.params = {"_est_stops": {}}

    gate = HeatCapGate({"max_heat_pct": 0.06})
    intent = [OrderIntent(symbol="AAA.SS", decision_date=date(2024, 3, 11), value=1000)]

    # 仅有止损估计缺失
    ctx1 = _Ctx(price=10.0)
    ctx1.params["_est_stops"] = {"AAA.SS": None}
    gate.admit(ctx1, intent, [])
    assert "no_stop_estimate" in ctx1.gate_log[0]["reason"]

    # 仅缺价格（原因不得写成 no_stop_estimate）
    ctx2 = _Ctx(price=None)
    ctx2.params["_est_stops"] = {"AAA.SS": 9.0}
    gate.admit(ctx2, intent, [])
    assert ctx2.gate_log[0]["reason"].startswith("no_price_estimate"), ctx2.gate_log

    # 两者都缺
    ctx3 = _Ctx(price=None)
    ctx3.params["_est_stops"] = {"AAA.SS": None}
    gate.admit(ctx3, intent, [])
    assert "no_price" in ctx3.gate_log[0]["reason"]


# ----------------------------------------------------------------------
# R2A-P3-4：哨兵不得忙循环（deferred + 未冻结）
# ----------------------------------------------------------------------


def test_catchup_sentinel_does_not_hot_spin(monkeypatch):
    """作业返回 deferred 而冻结已解除时，哨兵必须退避并最终退出（旧写法
    不 sleep、预算永不递减 → 实测 10 秒内 15532 次调用且线程不退）。"""
    from core import jobs, settings as settings_mod

    calls: list[int] = []
    sleeps: list[float] = []

    def _fake_job(_settings, _service=None, force=False, after_update=None):
        calls.append(1)
        return {"status": "deferred_backtest_running"}

    class _FakeThread:
        def __init__(self, target=None, **kwargs):
            self._target = target
            self.daemon = kwargs.get("daemon", False)
            self.name = kwargs.get("name", "")
            self._alive = False

        def start(self):
            self._alive = True
            try:
                self._target()
            finally:
                self._alive = False

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(jobs, "daily_market_update_job", _fake_job)
    monkeypatch.setattr(jobs, "threading", type("T", (), {"Thread": _FakeThread}))
    monkeypatch.setattr(_time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(jobs.run_freeze, "is_frozen", lambda: False)
    monkeypatch.setattr(jobs, "market_now", lambda: datetime(2026, 9, 25, 17, 0))
    monkeypatch.setattr(jobs, "_catchup_sentinel", None)

    jobs._spawn_same_day_catchup(
        settings_mod.load_settings(), None, force=True, today=date(2026, 9, 25),
        after_update=None,
    )
    # 预算 7200s / 每轮 60s = 120 轮上限；必须退避（sleep 被调用）
    assert len(calls) <= 121, f"哨兵调用次数必须有界（实际 {len(calls)}）"
    assert sleeps, "deferred 且未冻结时必须退避，不得热转"
    assert all(s == 60 for s in sleeps)


# ----------------------------------------------------------------------
# R2A-P3-6：purpose 校验在源头（覆盖所有通道）
# ----------------------------------------------------------------------


def test_grant_token_rejects_empty_purpose_at_source(test_db):
    from research import holdout, sessions

    session = sessions.get_or_create_default_human_session(test_db)
    for bad in ("", "   ", None):
        with pytest.raises(holdout.HoldoutError):
            holdout.grant_token(test_db, session_id=session["session_id"], purpose=bad)
    token = holdout.grant_token(test_db, session_id=session["session_id"], purpose="ok")
    assert token["purpose"] == "ok"


# ----------------------------------------------------------------------
# R2A-P3-8：lifespan 钉子必须真的验 after_update 的传递与"恰好一次"
# ----------------------------------------------------------------------


def test_daily_update_passes_after_update_and_runs_pipeline_once(test_db, monkeypatch):
    """`_run_daily_update` 必须把 after_update 传给作业，并在成功路径
    自己调 pipeline **恰好一次**。"""
    import app.main as main

    captured: dict = {}
    pipeline_calls: list = []

    def _fake_job(_settings, force=False, after_update=None):
        captured["after_update"] = after_update
        return {"status": "ok", "total": 1, "success": 1, "failed": 0, "symbols": ["X.SS"]}

    monkeypatch.setattr(main, "daily_market_update_job", _fake_job)
    monkeypatch.setattr(
        "services.indicator_builder.run_post_update_pipeline",
        lambda *a, **k: pipeline_calls.append(1) or {"status": "ok"},
    )
    jobs = _capture_lifespan_jobs(monkeypatch, test_db)
    jobs["update_job"]()
    assert callable(captured.get("after_update")), \
        "after_update 必须传给作业（哨兵补跑时靠它收口 pipeline）"
    assert len(pipeline_calls) == 1, f"成功路径 pipeline 恰好一次（实际 {len(pipeline_calls)}）"

    # 顺延路径：不跑 pipeline，且 after_update 仍然可被哨兵调用
    pipeline_calls.clear()

    def _deferred_job(_settings, force=False, after_update=None):
        captured["after_update"] = after_update
        return {"status": "deferred_backtest_running", "results": []}

    monkeypatch.setattr(main, "daily_market_update_job", _deferred_job)
    captured["after_update"] = None
    jobs2 = _capture_lifespan_jobs(monkeypatch, test_db)
    jobs2["update_job"]()
    assert pipeline_calls == [], "顺延路径本触发不得跑 pipeline"
    assert callable(captured.get("after_update"))


def _capture_lifespan_jobs(monkeypatch, test_db):
    """复用既有 lifespan 捕获工具（与 test_main_coverage95 同形）。"""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_main_coverage95 import _capture_lifespan

    _frozen_guard = None
    _, jobs = _capture_lifespan(monkeypatch, test_db)
    return jobs
