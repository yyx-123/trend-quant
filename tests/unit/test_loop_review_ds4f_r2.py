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


def _full_in_run(n_rounds: int, *, slip: float = 0.001, price: float = 10.0,
                 equity: float = 100_000.0, lot: int = 100,
                 position_pct: float = 1.0,
                 last_qty_mult: float = 1.0, nav_rel: float = 0.0):
    """复刻真实引擎形态的**满仓**往返（每轮买卖各一笔，仓位 ≈ 全额权益）。

    真实引擎的合法数量漂移是"整手取整的量化随机游走"（每轮至多差一手），
    满仓形态才复现这一点；小额订单的合成形态会高估界的相对松紧。
    """
    d = date(2023, 1, 4)
    new, old = [], []
    for _ in range(n_rounds):
        q = int(equity * position_pct / price / lot) * lot
        # 真实引擎的滑点方向：买入更贵、卖出更便宜（两侧都对我方不利）
        new.append({"date": d, "side": "BUY", "qty": q, "price": price * (1 + slip)})
        old.append({"date": d, "side": "BUY", "qty": q, "price": price})
        new.append({"date": d, "side": "SELL", "qty": q, "price": price * (1 - slip)})
        old.append({"date": d, "side": "SELL", "qty": q, "price": price})
    if last_qty_mult != 1.0:
        new[-1]["qty"] = int(new[-1]["qty"] * last_qty_mult)
    # nav 必须是**引擎形态**（cash + 持仓市值 + close）：否则恒等式两条腿都是
    # 空转（V6 复核实证：nav 精简时只剩启发式界在防守，把界删掉就失效）
    def _nav(trades, rel=0.0):
        held = 0
        for t in trades:
            held += t["qty"] if t["side"] == "BUY" else -t["qty"]
        cash = equity - sum(
            t["qty"] * t["price"] for t in trades if t["side"] == "BUY"
        ) + sum(t["qty"] * t["price"] for t in trades if t["side"] == "SELL")
        # 注入的净值偏离落在 cash 上（保持 equity = cash + 市值 的恒等式成立，
        # 即"自洽地重写 nav"——恒等式故意不抓这一类，由白名单界/饱和标注处理）
        cash += equity * rel
        pv = held * price
        return [
            {"date": "2023-01-03", "cash": equity, "positions_value": 0.0,
             "market_value": 0.0, "qty": 0, "close": price, "equity": equity},
            {"date": "2023-01-04", "cash": cash, "positions_value": pv,
             "market_value": pv, "qty": held, "close": price,
             "equity": cash + pv},
        ]

    return (_res(new, _nav(new, nav_rel)),
            _res(old, _nav(old, 0.0), legacy=True))


def test_parity_no_false_positives_on_long_legitimate_runs():
    """**R2A-P2-1 / V3-P1-1 的核心回归**：合法（纯尾滑点）长 run 必须零假报警。

    R1 的"数量 1/2 硬帽"在 4000~6000 日给出 76~229 笔假报警、R2 的"纯现金项"
    在 260 日就给出 7 笔——合法漂移是整手量化的随机游走（每轮至多一手），
    界必须同时含**现金项**与**量化项**。这里同时断言两端：
    短 run 与长 run 的 unexplained 都必须为空。
    """
    from engine.parity import attribute_diffs

    for rounds in (10, 130, 300, 500, 1000):
        new, old = _full_in_run(rounds)
        out = attribute_diffs(new, old)
        assert out["unexplained"] == [],             f"{rounds} 轮合法运行被误判：{out['unexplained'][:2]}"
        # 2×rounds 笔成交差异全部归类（另有 nav 级联点也归入同类）
        assert out["classified"]["tail_slippage"] >= 2 * rounds


def test_parity_acceptance_scale_is_discriminating():
    """stage-1 验收尺度（少笔数、紧界）必须**未饱和**且能抓住荒谬错误。

    验收判据的适用前提就是"未饱和"：此时 `unexplained == []` 才等价于
    "引擎力学一致"。三种注入（+100% 净值 / +9% 净值 / ×2 数量）都必须落在
    `unexplained`。
    """
    from engine.parity import attribute_diffs

    # 10 轮 = 20 笔差异（≤ 饱和阈值），满仓形态
    base_new, base_old = _full_in_run(10)
    base = attribute_diffs(base_new, base_old)
    assert base["saturated"] is False
    assert base["unexplained"] == []
    assert base["nav_cascade_bound"] < 0.05

    for nav_rel, qty_mult, expected in ((1.0, 1.0, "nav"), (0.09, 1.0, "nav"),
                                        (0.0, 2.0, "trade")):
        import copy

        new = copy.deepcopy(base_new)
        if nav_rel:
            new["daily_nav"][-1]["equity"] *= (1 + nav_rel)
        if qty_mult != 1.0:
            new["trades"][-1]["qty"] = int(new["trades"][-1]["qty"] * qty_mult)
        out = attribute_diffs(new, base_old)
        assert out["unexplained"], f"nav_rel={nav_rel} qty={qty_mult} 必须判超纲"
        assert out["saturated"] is False, "注入不改变饱和状态（尺度未变）"
        kinds = {u.get("kind") for u in out["unexplained"]}
        assert any(expected in str(k) for k in kinds), kinds


def test_parity_reports_saturation_flag():
    """判别力饱和必须显式标注（长 run 的 `unexplained == []` 不等于一致）。"""
    from engine.parity import attribute_diffs

    short_new, short_old = _full_in_run(10)
    short = attribute_diffs(short_new, short_old)
    assert short["saturated"] is False
    assert short["attribution_note"] == ""
    assert "n_trade_diffs" in short and "nav_cascade_bound" in short

    long_new, long_old = _full_in_run(300)
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
    from core import jobs
    from core import settings as settings_mod

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
    from app import main

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


def test_plateau_neighbor_probe_changes_one_param_only():
    """邻域点必须只改被探查的那一个参数（V3-P2-2 复核）。

    字典形态 `to` 无 params + 多个 item 级参数时，邻域注入必须写回
    **生效位置**（item.params），不得新建 `to.params`——后者会让
    `apply_diff` 的"to.params 整体接管"生效并静默丢掉其余参数（实测
    `atr_period` 被重置为默认），高原/孤峰判定因此对着错误基准计算。
    """
    from portfolio.slots import REGISTRY, ensure_builtins
    from portfolio.strategy import apply_diff, parse_strategy_yaml
    from research.evaluations.backtest import _plateau_items, _with_param

    ensure_builtins()
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
    # 字典形态 to 只给模块；两个 item 级参数都生效
    diff = [{"slot": "position_risk", "from": "hard_stop@1",
             "to": {"module": "hard_stop@1"},
             "params": {"atr_mul": 2.0, "atr_period": 30}}]
    cfg = parse_strategy_yaml(yaml_text, REGISTRY)
    center = apply_diff(cfg, diff, REGISTRY).slots["position_risk"].params
    assert center["atr_mul"] == 2.0 and center["atr_period"] == 30

    items = {i["param"] for i in _plateau_items(diff)}
    assert items == {"atr_mul", "atr_period"}

    probe = _with_param(diff, "position_risk", "atr_mul", 2.4)
    resolved = apply_diff(cfg, probe, REGISTRY).slots["position_risk"].params
    assert resolved["atr_mul"] == 2.4
    assert resolved["atr_period"] == 30, \
        f"邻域点改了第二个参数（实际 {resolved}）——高原判定基准错误"

    probe2 = _with_param(diff, "position_risk", "atr_period", 36)
    resolved2 = apply_diff(cfg, probe2, REGISTRY).slots["position_risk"].params
    assert resolved2["atr_period"] == 36
    assert resolved2["atr_mul"] == 2.0, f"邻域点改了第二个参数（实际 {resolved2}）"


def test_parity_saturates_on_many_diffs_even_with_a_tight_bound():
    """饱和判据必须包含"差异笔数"这一条：小额订单形态下 `drag/equity` 很小
    （上界紧），但差异笔数已远超 stage-1 尺度——逐笔位置对齐同样不可靠。
    """
    from engine.parity import attribute_diffs

    # 1000 股订单、100 万权益 → 仓位仅 1%，drag 相对权益极小；但差异 300 笔
    new, old = _full_in_run(150, equity=1_000_000.0, price=10.0, lot=100,
                            position_pct=0.01)
    out = attribute_diffs(new, old)
    assert out["n_trade_diffs"] == 300
    assert out["nav_cascade_bound"] <= 0.05, "该形态的上界应当很紧"
    assert out["saturated"] is True, "笔数超尺度必须判饱和（判据含 n_trade_diffs）"


def test_parity_nonfinite_and_shape_guards():
    """V5 复核的边界：NaN/inf 不得静默通过恒等式；旧侧形状异常不得抛异常穿出；
    且恒等式与仓位恒等式的**覆盖计数**必须如实报告（否则可能静默空转）。"""
    import math

    from engine.parity import attribute_diffs

    new, old = _engine_shaped_run_local(5)
    base = attribute_diffs(new, old)
    assert base["unexplained"] == []
    assert base["nav_identity_checked_days"] == 4  # 两侧 × 2 天
    assert base["nav_position_identity_checked_days"] == 2

    # (a) NaN 塞进 cash：不得被判"恒等式通过"
    for field in ("cash", "positions_value"):
        import copy

        inj = copy.deepcopy(new)
        inj["daily_nav"][-1][field] = float("nan")
        out = attribute_diffs(inj, old)
        kinds = {u.get("kind") for u in out["unexplained"]}
        assert "nav_identity_nonfinite" in kinds or "nav_identity_broken" in kinds, (field, kinds)

    # (b) inf 同理
    import copy

    inj_inf = copy.deepcopy(new)
    inj_inf["daily_nav"][-1]["equity"] = float("inf")
    out_inf = attribute_diffs(inj_inf, old)
    assert any(u.get("kind") in ("nav_identity_nonfinite", "nav_identity_broken")
               for u in out_inf["unexplained"])

    # (c) 旧侧形状异常（qty=None / date 垃圾）不得抛异常穿出
    bad = {
        "trades": [{"date": "garbage", "side": "BUY", "qty": None, "exec_price": 10.0}],
        "daily_nav": [{"date": "2023-01-03", "cash": 1e6, "market_value": 0.0,
                       "equity": 1e6, "qty": 0, "close": 10.0}],
    }
    out_bad = attribute_diffs(new, bad)
    assert out_bad["unexplained"], "形状异常必须判超纲而不是静默通过"

    # (d) 合法性数值不被误判
    assert math.isclose(base["nav_identity_residual"], 0.0, abs_tol=1e-12)


def _engine_shaped_run_local(n_buys: int, *, slip: float = 0.001, qty: int = 1000,
                             price: float = 10.0, equity: float = 1_000_000.0):
    """引擎形态的两侧结果（nav 带 cash + 持仓市值 + close）。

    本文件自带一份，避免依赖别的测试模块的 sys.path 副作用（V6 复核：跨文件
    import 会让用例单独运行时 ModuleNotFoundError）。
    """
    buy_n = [{"date": date(2023, 1, 4), "side": "BUY", "qty": qty,
              "price": price * (1 + slip)} for _ in range(n_buys)]
    buy_o = [{"date": date(2023, 1, 4), "side": "BUY", "qty": qty,
              "price": price} for _ in range(n_buys)]

    def _nav(trades):
        held = sum(t["qty"] for t in trades)
        spent = sum(t["qty"] * t["price"] for t in trades)
        cash = equity - spent
        pv = held * price
        return [
            {"date": "2023-01-03", "cash": equity, "positions_value": 0.0,
             "market_value": 0.0, "qty": 0, "close": price, "equity": equity},
            {"date": "2023-01-04", "cash": cash, "positions_value": pv,
             "market_value": pv, "qty": held, "close": price,
             "equity": cash + pv},
        ]

    return (
        {"trades": buy_n, "daily_nav": _nav(buy_n)},
        {"trades": [{"date": t["date"], "side": t["side"], "qty": t["qty"],
                     "exec_price": t["price"]} for t in buy_o],
         "daily_nav": _nav(buy_o)},
    )
