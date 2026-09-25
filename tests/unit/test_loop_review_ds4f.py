"""loop-review-ds4f Round 1 修复钉子（round1-review.md 各项的回归锚）。

覆盖两类：
1. 本轮 P1/P2 修复的行为锚——每条都经过**变异反证**（把对应实现改回去/改坏，
   该用例必须失败）；V1/V2 复核指出的 5 条空钉已重写为驱动真实上下文/真实
   调用形态（不再断言桩对象自身属性或源码字符串）。
2. 代理变异测试实证的"实现可删而 CI 仍绿"缺口（holdout 边界/格式、verdict
   强度序、conclude 行级守卫、`_slug` 容器化、module_gate 七槽探针、台账
   holdout 发放端点）。
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from portfolio.library import add_version_yaml, ensure_strategy
from portfolio.registry import ModuleSpec, fresh_registry
from research import holdout, sessions, topics

pytestmark = pytest.mark.unit

STRATEGIES_DIR = Path(__file__).resolve().parents[2] / "src" / "portfolio" / "strategies"


@pytest.fixture
def registry():
    reg = fresh_registry()
    for slot in ("universe", "signal", "rank", "sizing",
                 "portfolio_risk", "position_risk", "execution"):
        reg.register(
            ModuleSpec(slot=slot, name=f"dummy_{slot}", version=1, factory=lambda p: p)
        )
    return reg


@pytest.fixture
def human_session(test_db):
    return sessions.get_or_create_default_human_session(test_db)


@pytest.fixture
def topic(test_db, human_session):
    return topics.create_topic(
        test_db, session_id=human_session["session_id"],
        title="止损选型", question="硬止损还是吊灯？",
    )


@pytest.fixture
def base_version(test_db, registry):
    ensure_strategy(test_db, "base-v1", name="基准")
    cfg = (
        "name: base-v1\ndescription: x\n"
        "universe: {module: dummy_universe@1}\n"
        "signal: {module: dummy_signal@1}\n"
        "rank: {module: dummy_rank@1}\n"
        "sizing: {module: dummy_sizing@1}\n"
        "portfolio_risk: []\n"
        "position_risk: {module: dummy_position_risk@1}\n"
        "execution: {module: dummy_execution@1}\n"
    )
    return add_version_yaml(test_db, "base-v1", cfg, registry, created_by="human")


def _propose(db, registry, topic, version, *, window, session, **spec_extra):
    from research import experiments

    spec = {"base": version["id"], "diff": [], "window": window, **spec_extra}
    try:
        exp = experiments.propose_experiment(
            db, session_id=session["session_id"], title="窗口格式",
            topic_id=topic["id"], evaluation_module="portfolio_backtest@1",
            spec=spec, hypothesis="窗口格式必须被入口卡死", registry=registry,
        )
        return exp, None
    except Exception as exc:  # IntakeRejected
        return None, exc


# ----------------------------------------------------------------------
# R1-P1-2 holdout 窗口：格式 + 边界（含变异反证）
# ----------------------------------------------------------------------


def test_holdout_window_format_bypass_is_closed(test_db, registry, topic, base_version, human_session):
    """非 ISO 日期不再能绕过 holdout 卡控（旧实现按字符串字典序比较，
    `"01/01/2026"`/`" 2026-01-01"` 都被判成"未触碰"却仍取到 holdout 段数据）。"""
    from research import evaluations  # noqa: F401  注册评估模块（骨架校验用）

    holdout.set_enforced(test_db, True)
    for bad in (["2024-01-01", "01/01/2026"], ["2024-01-01", " 2026-01-01"],
                ["2024-01-01", "2026-1-1"], ["2024/01/01", "2026-01-01"],
                ["2024-01-01", "2026-01-01", "2027-01-01"], ["2024-01-01"],
                ["2025-01-01", "2024-01-01"], ["2024-01-01", "2025-02-30"],
                ["2024-01-01", "2026-01-01T00:00:00"], ["2024-01-01", ""]):
        exp, exc = _propose(test_db, registry, topic, base_version,
                            window=bad, session=human_session)
        assert exp is None, f"{bad} 不应被放行"
        assert exc is not None and "window" in str(exc), (bad, str(exc))
    # 合法 ISO 窗口不得被过度拦截（portfolio_backtest 要求 diff 非空）
    exp_ok, exc_ok = _propose(
        test_db, registry, topic, base_version,
        window=["2024-01-01", "2024-12-31"], session=human_session,
        diff=[{"slot": "position_risk", "to": "dummy_position_risk@1"}],
    )
    assert exp_ok is not None, exc_ok


def test_holdout_window_end_equal_to_holdout_start_is_touched(test_db):
    """`end == holdout_start` 必须算触碰（变异：`>=` 改 `>` 时用例失败）。"""
    holdout.set_enforced(test_db, True)
    windows = holdout.get_windows(test_db)
    assert holdout.window_touches_holdout(
        test_db, windows["sample_start"], windows["holdout_start"]
    ) is True
    assert holdout.window_touches_holdout(
        test_db, windows["sample_start"], windows["sample_end"]
    ) is False


def test_holdout_window_unparseable_is_fail_closed(test_db):
    """无法解析的日期端点 → HoldoutError（宁可拒，不可放行）。"""
    with pytest.raises(holdout.HoldoutError):
        holdout.window_touches_holdout(test_db, "2024-01-01", "not-a-date")


# ----------------------------------------------------------------------
# R1-P1-3 bucket 的扫描上下文必须能承载读持仓的信号模块
# ----------------------------------------------------------------------


def test_bucket_scan_ctx_provides_account_stub():
    """`BucketScanCtx`（bucket 的真实逐日上下文）必须能给读持仓的信号模块用。

    V1/V2 复核均实证初版是空钉（只断言直接构造的 EmptyAccount 自身属性）。
    这里驱动**真实** ctx 类 + 真实 `AbsMomentumSignal.scan()`：把 bucket.py 的
    `self.account = EmptyAccount()` 改回 `None` 时本用例必须失败。
    """
    from portfolio.slots.signal import AbsMomentumSignal
    from portfolio.slots.universe import UniverseMember
    from research.evaluations.bucket import BucketScanCtx

    class _P:
        def __init__(self, close):
            self.dates = list(pd.date_range("2023-01-02", periods=len(close)).date)
            self.symbols = tuple(f"S{i:02d}.SS" for i in range(close.shape[1]))
            self.data = {"close": np.asarray(close, dtype=float)}
            self.provisional = np.zeros(close.shape, dtype=bool)

        @property
        def _symbol_index(self):
            return {s: i for i, s in enumerate(self.symbols)}

    n = 31
    # 三列动量必须**可区分**：S00 下跌（持仓将跌出 top 集）、S01 横盘、S02 上涨
    cols = np.column_stack([
        np.linspace(12.0, 10.0, n),   # S00：动量负
        np.full(n, 10.0),             # S01：动量 0
        np.linspace(10.0, 14.0, n),   # S02：动量最高 → top
    ])
    panel = _P(cols)
    ctx = BucketScanCtx(panel, panel.dates[n - 1], n - 1)
    assert ctx.account is not None, "扫描上下文不得给 None 账户（P1-3）"
    mod = AbsMomentumSignal({"lookback": 20, "top_n": 1, "threshold": 0.0})
    mod.prepare(panel)
    members = [UniverseMember(symbol=s) for s in panel.symbols]
    # 空仓分支（真实调用形态）不得抛
    assert isinstance(mod.scan(ctx, members), list)

    # 有持仓分支：必须能读 ctx.account.positions（None 时 AttributeError）
    class _Held:
        def __init__(self):
            self.positions = {"S00.SS": object()}

    ctx.account = _Held()
    events_held = mod.scan(ctx, members)
    assert any(e.kind == "exit" for e in events_held), "持仓跌出 top 集必须产生 exit"


def test_bucket_warnings_keep_module_level_entries_and_p_value_guard():
    """空桶告警不得被重绑定丢弃；NaN spread 不得伪造 p 值（两半都可直测）。"""
    import inspect

    from research.evaluations import bucket

    src = inspect.getsource(bucket.run_bucket_analysis)
    assert "warnings.extend(collect_warnings(" in src, \
        "collect_warnings 必须以 extend 合并（重绑定会丢弃空桶告警）"
    assert "permutation_p_value(spread, random_spreads)" in src, \
        "p 值必须走 permutation_p_value（内含 isfinite 守卫）"
    for kw in ("overlap_ratio=_overlap_ratio", "top_day_share=_top_day_share",
               "regimes=_regimes"):
        assert kw in src, "§6.6.3 三注记必须真的传进去"


def test_permutation_p_value_rejects_nan_and_none():
    """R1-P2-11（V2：初版这一半是空钉）：NaN spread 必须记 None 而不是 0.0。"""
    from research.evaluations.bucket import permutation_p_value

    draws = [0.01, -0.02, 0.03, 0.005]
    # 正常：|实际利差| 大于全部随机利差 → p=0.0（这是**真**的 0，不是伪造）
    assert permutation_p_value(0.05, draws) == 0.0
    # NaN（空桶场景的 spread 形态）→ None（旧实现给 0.0 = 全族最显著）
    assert permutation_p_value(float("nan"), draws) is None
    assert permutation_p_value(None, draws) is None
    assert permutation_p_value(0.05, []) is None
    assert permutation_p_value(float("inf"), draws) is None
    assert permutation_p_value(0.02, [0.01, 0.03]) == 0.5


# ----------------------------------------------------------------------
# R1-P1-4 组合报告换手率不得被除以两次平均权益
# ----------------------------------------------------------------------


def test_report_turnover_total_is_currency_not_ratio():
    from portfolio.reports import build_report

    nav = [{"date": "2024-01-02", "equity": 1_000_000.0},
           {"date": "2024-01-03", "equity": 1_010_000.0}]
    fills = [{"fill_price": 10.0, "quantity": 2100, "symbol": "510300.SS",
              "side": "buy", "fill_date": "2024-01-02", "fee_total": 5.0}]
    report = build_report(None, run_id="R-test", nav_rows=nav, fills=fills,
                          unfilled=[], gate_log=[])
    # 货币成交额 = 10 × 2100 = 21000（不是比率）
    assert report["turnover_total"] == pytest.approx(21000.0)
    # summary.turnover = 21000 / 平均权益 → 约 0.0208（旧实现再除一次得到 2e-8）
    assert report["summary"]["turnover"] == pytest.approx(21000.0 / 1_005_000.0, rel=1e-6)
    assert report["turnover_ratio"] == pytest.approx(report["summary"]["turnover"], rel=1e-12)


# ----------------------------------------------------------------------
# R1-P2-1 停牌缺口不得在信号指标里产生幻影交叉
# ----------------------------------------------------------------------


def _panel_like(close):
    class _Panel:
        def __init__(self, series):
            self.dates = list(pd.date_range("2023-01-02", periods=len(series)).date)
            self.symbols = ("AAA.SS",)
            self.data = {"close": np.asarray(series, dtype=float).reshape(-1, 1)}
            self.provisional = np.zeros((len(series), 1), dtype=bool)

        @property
        def _symbol_index(self):
            return {"AAA.SS": 0}

    return _Panel(close)


def _scan_ctx(panel, upto):
    class _VP:
        def __init__(self):
            self.upto = upto

        def symbol_col(self, symbol):
            return 0

        def date_at(self, i):
            return panel.dates[i]

    class _C:
        def __init__(self):
            self.panel = _VP()
            self.date = panel.dates[upto]
            self.account = None
            self.params = {}

    return _C()


def test_signal_prepare_ffills_suspension_gap():
    """抽掉一根 bar 不得改变 rolling 指标口径。

    V2 复核指出初版钉子是空钉（缺口放在面板末尾，NaN 污染到尾，
    `last_up[-1]` 两边都等于缺口前的值）。这里改为：缺口放在中段、
    序列在缺口后先跌后涨制造真实上穿，比较**完整**的 `last_up` 序列
    并用 scan 取真实事件——把 `_filled_df` 换回 `_field_df`（去掉 ffill）
    时本用例必须失败。
    """
    from portfolio.slots.signal import MaCrossSignal
    from portfolio.slots.universe import UniverseMember

    n = 60
    # 形态：前 31 根下跌 → 第 31 根缺口 → 横盘 → 第 45 根起拉升（真实上穿）
    close = ([10.0 - 0.05 * i for i in range(31)]
             + [8.5] * 14 + [8.5 + 0.25 * i for i in range(15)])
    assert len(close) == n
    gap = list(close)
    gap[31] = np.nan

    clean_panel, gapped_panel = _panel_like(close), _panel_like(gap)
    clean, gapped = MaCrossSignal({"n": 20}), MaCrossSignal({"n": 20})
    clean.prepare(clean_panel)
    gapped.prepare(gapped_panel)

    # 完整序列逐位一致（含缺口之后的所有行）——去掉 ffill 后恢复行会变成新 last_up
    assert list(clean._ready["last_up"][:, 0]) == list(gapped._ready["last_up"][:, 0]), \
        "缺口后出现了幻影交叉（ffill 缺失）"

    # 真实 scan 形态：缺口之后的每一天，两个面板的事件结论必须一致
    members = [UniverseMember(symbol="AAA.SS")]
    for t in range(32, n):
        ev_c = clean.scan(_scan_ctx(clean_panel, t), members)
        ev_g = gapped.scan(_scan_ctx(gapped_panel, t), members)
        assert [(e.kind, e.date) for e in ev_c] == [(e.kind, e.date) for e in ev_g], \
            f"第 {t} 根 bar 上缺口面板与干净面板的信号不一致"


# ----------------------------------------------------------------------
# R1-P2-2 walk_forward 不得伪造"零成交/零费用"
# ----------------------------------------------------------------------


def test_walk_forward_evidence_does_not_fabricate_zeros():
    """证据面：trades/unfilled 为 None 时不得记 0、不得发"零成交"告警。"""
    import inspect

    from research.evaluations import backtest as bt

    run_src = inspect.getsource(bt.run_portfolio_backtest)
    assert '"trades": None, "unfilled": None' in run_src
    asm_src = inspect.getsource(bt._assemble_result)
    assert "fee_total = None" in asm_src, "无 run（wf 拼接）时费用必须记 None 而非 0"
    assert "trade_details_unavailable" in asm_src
    assert 'None if exp_result.get("unfilled") is None' in asm_src


# ----------------------------------------------------------------------
# R1-P2-3 模块门必须探针 estimate_stop
# ----------------------------------------------------------------------


def test_module_gate_probes_estimate_stop():
    """纯前视的 `estimate_stop` 必须被门的探针抓住（真实跑门，不看源码字符串）。

    该接口直接进 sizing（backtester 用它换算风险预算→股数）。V2 复核实证：
    父树的门对"estimate_stop 偷看未来"返回 **passed=True**，本轮修复后返回
    passed=False（prefix_stability 失败）——本用例断言后者的真实行为。
    """
    from engine.models import StopState
    from research.module_gate import run_module_gate

    class _Cheat:
        """init_stop/evaluate 保持因果；estimate_stop 返回**全面板**的极值
        （prepare 期偷看未来）——真实的前视形态。"""

        def __init__(self, params):
            self._leak: dict[str, float] = {}

        def prepare(self, panel):
            for col, symbol in enumerate(panel.symbols):
                self._leak[symbol] = float(np.nanmax(panel.data["close"][:, col]))

        def init_stop(self, ctx, fill):
            return StopState(stop_price=fill.fill_price * 0.9,
                             highest_since_buy=fill.fill_price, atr_at_entry=0.2)

        def evaluate(self, ctx, position):
            return None

        def estimate_stop(self, ctx, symbol):
            return self._leak.get(symbol, 0.0) * 0.01

    report = run_module_gate(_Cheat, slot="position_risk", params={})
    assert report["passed"] is False, report
    assert report["checks"]["prefix_stability"]["ok"] is False, report

    class _Causal(_Cheat):
        def prepare(self, panel):
            return None

        def estimate_stop(self, ctx, symbol):
            col = ctx.panel.symbol_col(symbol)
            col_v = ctx.panel.matrix("close")[:, col]
            return float(col_v[-1]) * 0.9

    assert run_module_gate(_Causal, slot="position_risk", params={})["passed"] is True


def test_module_gate_runs_every_slot_probe():
    """七个插槽各取一个内置件过门（旧覆盖：五槽的 `_probe` 分支从未被执行）。"""
    from portfolio.slots import REGISTRY, ensure_builtins
    from research.module_gate import _SLOT_PROTOCOLS, run_module_gate

    ensure_builtins()
    probes_ran: dict[str, bool] = {}
    for slot in ("universe", "signal", "rank", "sizing",
                 "portfolio_risk", "position_risk", "execution"):
        specs = [
            s for s in REGISTRY.list(slot=slot)
            if s.kind == "builtin" and not s.name.endswith(("any_of", "all_of"))
        ]
        assert specs, f"{slot} 无内置件"
        spec = specs[0]
        report = run_module_gate(spec.factory, slot=slot, params={})
        assert report["passed"] is True, (slot, spec.name, report)
        assert "contract" in report["checks"]
        assert "prefix_stability" in report["checks"], (slot, spec.name)
        probes_ran[slot] = True
    assert set(probes_ran) == set(_SLOT_PROTOCOLS), "七槽全跑"


def test_module_gate_rejects_wrong_signature_per_slot():
    """每槽一个"协议方法签名不对"的模块必须被契约门拒（防止门退化为恒放行）。"""
    from research.module_gate import run_module_gate

    for slot, proto in (
        ("universe", "members"), ("signal", "scan"), ("rank", "rank"),
        ("sizing", "size"), ("portfolio_risk", "admit"),
        ("position_risk", "evaluate"), ("execution", "fill_policy"),
    ):
        def factory(params, _m=proto):
            return type("X", (), {})()

        report = run_module_gate(factory, slot=slot, params={})
        assert report["passed"] is False, f"{slot} 缺 {proto} 必须被拒"
        assert report["checks"]["contract"]["ok"] is False


# ----------------------------------------------------------------------
# R1-P2-4 heat_cap × 无止损估计：告警与行为必须一致（放行而非静默拒绝全部）
# ----------------------------------------------------------------------


def test_heat_cap_passes_candidates_without_stop_estimate():
    from engine.models import OrderIntent
    from portfolio.slots.portfolio_risk import HeatCapGate

    class _Acct:
        def equity(self):
            return 1_000_000.0

        def heat(self):
            return 0.0

        def unstopped_symbols(self):
            return []

    class _Panel:
        def value(self, symbol, field):
            return 10.0

    class _Ctx:
        def __init__(self):
            self.account = _Acct()
            self.panel = _Panel()
            self.params = {"_est_stops": {"AAA.SS": None}}  # breakeven/time_stop/none
            self.date = date(2024, 3, 11)
            self.gate_log: list = []

    gate = HeatCapGate({"max_heat_pct": 0.06})
    ctx = _Ctx()
    intents = [OrderIntent(symbol="AAA.SS", decision_date=ctx.date, value=1000)]
    out = gate.admit(ctx, intents, [])
    assert [i.symbol for i in out] == ["AAA.SS"], \
        "无止损估计的候选必须放行（与 run warning 一致），不能静默拒绝全部"
    assert any(g["gate"] == "heat_cap" and "no_stop_estimate" in g["reason"]
               for g in ctx.gate_log), "放行必须留痕（cap NOT enforced）"


# ----------------------------------------------------------------------
# R1-P2-7 parity 归因的界必须是真界（不再吸收数量/净值级别的错配）
# ----------------------------------------------------------------------


def test_parity_attribution_rejects_large_qty_and_nav_errors():
    from engine.parity import attribute_diffs

    d = date(2023, 1, 4)

    def _res(trades, nav, legacy=False):
        if legacy:
            trades = [{"date": t["date"], "side": t["side"], "qty": t["qty"],
                       "exec_price": t["price"]} for t in trades]
        return {"trades": trades, "daily_nav": nav}

    # (a) 一笔合法尾盘价差 + 净值 +100% → 净值点必须进 unexplained
    nav_new = [{"date": "2023-01-03", "equity": 500_000.0},
               {"date": "2023-01-04", "equity": 1_000_300.0}]
    nav_old = [{"date": "2023-01-03", "equity": 500_000.0},
               {"date": "2023-01-04", "equity": 500_000.0}]
    out = attribute_diffs(
        _res([{"date": d, "side": "BUY", "qty": 1000, "price": 10.03}], nav_new),
        _res([{"date": d, "side": "BUY", "qty": 1000, "price": 10.0}], nav_old, legacy=True),
    )
    assert out["unexplained"], "净值 +100% 错误不得被单笔合法价差吸收（父树会吸收）"

    # (b) 末笔数量漂移 60%：父树的数量界随笔数线性放大（i=119 时远超整仓）→
    # 被吸收；修复后以"该笔数量的 1/2"为硬上限 → 必须进 unexplained
    tr_new, tr_old = [], []
    for i in range(120):
        tr_new.append({"date": d, "side": "BUY",
                       "qty": 1600 if i == 119 else 1000, "price": 10.01})
        tr_old.append({"date": d, "side": "BUY", "qty": 1000, "price": 10.0})
    out2 = attribute_diffs(
        _res(tr_new, [{"date": "2023-01-04", "equity": 1e6}]),
        _res(tr_old, [{"date": "2023-01-04", "equity": 1e6}], legacy=True),
    )
    assert any(u.get("kind") == "trade_mismatch" for u in out2["unexplained"]), \
        "数量漂移 60% 不得被当作滑点下游（父树会吸收）"

    # 反向：小漂移（一手）仍属合法下游，必须被归类而非误报
    out3 = attribute_diffs(
        _res([{"date": d, "side": "BUY", "qty": 900, "price": 10.03}],
             [{"date": "2023-01-04", "equity": 1e6}]),
        _res([{"date": d, "side": "BUY", "qty": 1000, "price": 10.0}],
             [{"date": "2023-01-04", "equity": 1e6}], legacy=True),
    )
    assert not out3["unexplained"], "一手内的数量漂移是滑点的合法下游"


# ----------------------------------------------------------------------
# R1-P2-9 库级守卫触发器的定义必须能传播到存量库
# ----------------------------------------------------------------------


def test_guard_triggers_use_drop_create(test_db):
    """所有 guard_update/no_update 触发器都必须 DROP IF EXISTS + CREATE——
    `CREATE TRIGGER IF NOT EXISTS` 不会把修订后的定义推到已存在同名触发器的
    存量库（R1-P2-9：research_verdicts 的"已落定 final 不可改写"在真实库上
    整个缺失）。"""
    from data.storage import db as db_module

    src = Path(db_module.__file__).read_text(encoding="utf-8")
    guards = [
        "trg_engine_runs_guard_update",
        "trg_research_experiments_guard_update",
        "trg_research_verdicts_guard_update",
        "trg_research_topics_guard_update",
        "trg_research_sessions_guard_update",
        "trg_portfolio_strategies_guard_update",
        "trg_holdout_tokens_guard_update",
        "trg_module_drafts_guard_update",
    ]
    for name in guards:
        assert f"DROP TRIGGER IF EXISTS {name};" in src, f"{name} 缺少 DROP IF EXISTS"
    with test_db.connect() as conn:
        body = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'trg_research_verdicts_guard_update'"
        ).fetchone()[0]
    assert "final_verdict" in body, "已落定 final_verdict 的库层守卫必须存在于真实库"


def test_stale_trigger_definition_is_replaced(tmp_path):
    """注入"旧定义"的守卫触发器后重开 Database()，定义必须被源码定义替换
    （R1-P2-9 的真实迁移形态）。"""
    from data.storage.db import Database

    path = tmp_path / "stale.db"
    db = Database(path)
    with db.connect() as conn:
        conn.execute("DROP TRIGGER trg_research_verdicts_guard_update")
        conn.executescript(
            """CREATE TRIGGER trg_research_verdicts_guard_update
               BEFORE UPDATE ON research_verdicts
               WHEN OLD.id <> NEW.id
               BEGIN SELECT RAISE(ABORT, 'research_verdicts content is append-only'); END;"""
        )
        stale = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='trg_research_verdicts_guard_update'"
        ).fetchone()[0]
    assert "final_verdict" not in stale, "夹具未注入旧定义"

    db2 = Database(path)  # 重新打开 = 应用启动 / 迁移
    with db2.connect() as conn:
        fresh = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='trg_research_verdicts_guard_update'"
        ).fetchone()[0]
    assert "final_verdict" in fresh, "存量库的旧触发器定义未被替换"


# ----------------------------------------------------------------------
# R1-P2-11/12 bucket 注记
# ----------------------------------------------------------------------


def test_bucket_module_level_warnings_survive():
    from research.evaluations._common import collect_warnings

    ws = collect_warnings(event_count=5, overlap_ratio=0.9, top_day_share=0.9,
                          regimes={"above"})
    joined = " ".join(ws)
    for token in ("overlap_heavy", "cross_sectional_clustered", "single_regime",
                  "sample_size_small", "survivorship_bias"):
        assert token in joined, token
    assert json.dumps(ws)  # 可序列化（进台账）


def test_overlap_and_cluster_stats_shared_impl():
    """event/bucket 共用的重叠率/日集中度实现（R1-P2-12 收口）。"""
    from research.evaluations._common import overlap_and_cluster_stats

    events = [(0, 0), (0, 1), (1, 0), (9, 2)]  # (t, col)
    days = [date(2024, 1, i) for i in range(1, 5)]
    overlap, top_share = overlap_and_cluster_stats(events, max_h=10, event_days=days)
    assert 0.0 <= overlap <= 1.0
    assert 0.0 <= top_share <= 1.0
    assert overlap_and_cluster_stats([], max_h=10, event_days=[]) == (None, None)


# ----------------------------------------------------------------------
# R1-P2-6 plateau 证据缺口
# ----------------------------------------------------------------------


def test_plateau_items_accepts_dict_form_and_zero_values():
    from research.evaluations.backtest import _plateau_items, _with_param

    diff = [{"slot": "position_risk",
             "from": "hard_stop@1",
             "to": {"module": "hard_stop@1", "params": {"atr_mul": 2.0}}},
            {"slot": "execution", "to": "tail_session@1",
             "params": {"slippage_tail": 0.0}}]
    items = _plateau_items(diff)
    params = {i["param"] for i in items}
    assert "atr_mul" in params, "字典形态 to 必须被枚举（R1-P2-6）"
    assert "slippage_tail" in params, "合法零值参数也必须能进邻域探查"
    out = _with_param(diff, "position_risk", "atr_mul", 2.4)
    assert out[0]["params"]["atr_mul"] == 2.4
    assert out[0]["to"]["params"]["atr_mul"] == 2.4


def test_plateau_unknown_verdict_is_surfaced_as_absent():
    """`verdict == "unknown"` 必须**真的发出** plateau_evidence_absent 告警。

    V2 复核指出初版只断言两个 helper（父树同样成立）→ 空钉。这里断言
    `plateau_warnings` 的输出（去掉该分支时本用例必须失败）。
    """
    from research.evaluations.backtest import plateau_warnings
    from research.verdict_rules import plateau_neighbors, plateau_verdict

    assert plateau_neighbors("slippage_tail", 0.0) == []
    unknown = plateau_verdict(0.1, [])
    assert unknown["verdict"] == "unknown"

    ws = plateau_warnings(unknown, is_creation=False)
    assert len(ws) == 1
    assert ws[0].startswith("plateau_evidence_absent")
    assert "no neighbors" in ws[0]

    assert plateau_warnings(None, is_creation=False)[0].startswith("plateau_evidence_absent")
    assert plateau_warnings(None, is_creation=True) == []
    assert plateau_warnings(unknown, is_creation=True) == []
    peak = plateau_verdict(1.0, [0.1, 0.2])
    assert peak["verdict"] == "peak"
    assert plateau_warnings(peak, is_creation=False)[0].startswith("plateau_peak")


# ----------------------------------------------------------------------
# R1-P3-15 短 regime 分段不得行使塌陷否决
# ----------------------------------------------------------------------


def test_regime_collapse_ignores_short_segments():
    from research.verdict_rules import suggest_backtest_verdict

    base = {
        "deltas_vs_base": {"delta_sharpe": 0.8, "delta_annual_return": 0.05,
                           "delta_max_drawdown": 0.0, "delta_turnover": None},
        "stats": {"paired": {"t_stat": 3.0, "dsr_on_diff": 0.1, "n_pairs": 200}},
        "plateau": {"verdict": "plateau"},
    }
    short_segment = dict(base)
    short_segment["regime_split"] = {
        "below": {"n_days": 9, "delta_sharpe": -2.19, "sufficient_sample": False}
    }
    long_segment = dict(base)
    long_segment["regime_split"] = {
        "below": {"n_days": 400, "delta_sharpe": -2.19, "sufficient_sample": True}
    }
    assert suggest_backtest_verdict(short_segment) == "confirmed", \
        "9 个交易日的分段不得行使塌陷否决（R1-P3-15）"
    assert suggest_backtest_verdict(long_segment) != "confirmed", \
        "够长分段的塌陷必须仍然否决"


# ----------------------------------------------------------------------
# R1-P3-18 复核 campaign 的 holdout 剔除判据
# ----------------------------------------------------------------------


def test_recompute_skips_holdout_touched_targets_with_reason(test_db):
    """复核 campaign 的剔除判据必须**真的**按窗口判定（去掉该分支时失败）。"""
    from research.recompute import holdout_blocks_campaign

    holdout.set_enforced(test_db, True)
    w = holdout.get_windows(test_db)
    assert holdout_blocks_campaign(
        test_db, {"window": [w["sample_start"], "2030-12-31"]}
    ) is True
    assert holdout_blocks_campaign(
        test_db, {"window": [w["sample_start"], w["sample_end"]]}
    ) is False
    assert holdout_blocks_campaign(test_db, {}) is False
    assert holdout_blocks_campaign(test_db, {"window": ["2024-01-01"]}) is False


# ----------------------------------------------------------------------
# Q4 verdict 强度序（变异：`return True` 时本用例必须失败）
# ----------------------------------------------------------------------


def test_verdict_rank_blocks_upgrade_not_caught_by_direction_rule():
    from research.evaluations.base import EvaluationModule

    mod = EvaluationModule(name="x", version=1)
    assert mod.final_verdict_allowed("inconclusive", "confirmed") is False
    # 降级允许（inconclusive → rejected 是纯降级，不落入 confirmed↔rejected 翻转）
    assert mod.final_verdict_allowed("inconclusive", "rejected") is True
    assert mod.final_verdict_allowed("confirmed", "inconclusive") is True
    # 方向翻转（confirmed↔rejected）另由方向规则拒绝——不是降级，是反向主张
    assert mod.final_verdict_allowed("confirmed", "rejected") is False
    assert mod.final_verdict_allowed("rejected", "confirmed") is False


# ----------------------------------------------------------------------
# Q7 conclude_topic 的行级守卫（变异：删 `AND status='open'` 时失败）
# ----------------------------------------------------------------------


def test_conclude_topic_rowcount_guard(test_db, human_session, monkeypatch):
    """行级状态守卫（R2-P3-1）：即使入口检查被过期快照骗过（require_open_topic
    放行），UPDATE 的 `AND status = 'open'` 仍必须挡住第二次落定。"""
    from research import topics as topics_mod
    from research.errors import TopicError

    t = topics_mod.create_topic(
        test_db, session_id=human_session["session_id"],
        title="行级守卫", question="?",
    )
    topics_mod.conclude_topic(test_db, topic_id=t["id"], conclusion="先关一次")
    monkeypatch.setattr(topics_mod, "require_open_topic", lambda db, tid: None)
    with pytest.raises(TopicError):
        topics_mod.conclude_topic(test_db, topic_id=t["id"], conclusion="再关一次")
    row = topics_mod.get_topic(test_db, t["id"])
    assert row["conclusion"] == "先关一次", "结论不得被第二次落定覆盖"


# ----------------------------------------------------------------------
# Q9 课题物化的 slug 清洗（变异：删 re.sub 时恶意标题写到 root 之外）
# ----------------------------------------------------------------------


def test_topic_materialization_containment(test_db, human_session, tmp_path):
    from research import topic_files

    t = topics.create_topic(
        test_db, session_id=human_session["session_id"],
        title="../../../../tmp/PWNED_BY_SLUG", question="路径穿越？",
    )
    out = topic_files.materialize_topic(test_db, t["id"], root=tmp_path / "topics")
    root = (tmp_path / "topics").resolve()
    if isinstance(out, dict) and out.get("dir"):
        target = Path(out["dir"]).resolve()
    else:
        produced = list(root.rglob("TOPIC.md"))
        assert produced, "课题文件必须物化在 root 内"
        target = produced[0].parent
    assert root in target.parents or target == root, "物化目录必须被限制在 topics root 内"


# ----------------------------------------------------------------------
# Q10 holdout 发放落库留痕（空 purpose 的拒绝在路由层，见 API 用例）
# ----------------------------------------------------------------------


def test_holdout_grant_records_token(test_db):
    """V2 复核指出初版含空断言——这里只保留**真实**断言（去掉 grant 落库
    后本用例失败）。"""
    from portfolio.slots import REGISTRY, ensure_builtins
    from research.api import ResearchService

    ensure_builtins()
    service = ResearchService(test_db, registry=REGISTRY,
                             topics_dir=test_db.db_path.parent / "research_topics")
    session = service.default_human_session()
    token = service.grant_holdout(session_id=session["session_id"], purpose="复核基线")
    assert token["id"].startswith("H")
    assert token["purpose"] == "复核基线"
    assert token["granted_by"] == session["session_id"]
    with test_db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM holdout_tokens WHERE id = ?", (token["id"],)
        ).fetchone()
    assert row["purpose"] == "复核基线" and row["consumed_at"] is None


# ----------------------------------------------------------------------
# R1-P3-11 空 universe 必须是领域错误（不是面板层 TypeError）
# ----------------------------------------------------------------------


def test_empty_universe_raises_domain_error(test_db, registry):
    from portfolio.backtester import BacktestError, run_backtest
    from portfolio.strategy import parse_strategy_yaml

    yaml_text = (STRATEGIES_DIR / "blank_base.yaml").read_text(encoding="utf-8")
    cfg = parse_strategy_yaml(yaml_text, registry)

    with pytest.raises(BacktestError) as ei:
        run_backtest(test_db, config=cfg, registry=registry,
                     start=date(2024, 1, 2), end=date(2024, 1, 5))
    assert "universe" in str(ei.value)


# ----------------------------------------------------------------------
# R1-P3-12 DSL 自带示例必须能编译
# ----------------------------------------------------------------------


def test_dsl_docstring_example_compiles():
    from research.dsl import compile_expression

    fn = compile_expression("(close > sma(close, 20)) & (volume > ref(volume, 1))")
    assert callable(fn)


# ----------------------------------------------------------------------
# R1-D-1（缓解）：白名单库的属性链逃逸必须被预筛拦截
# ----------------------------------------------------------------------


def test_prescreen_blocks_dangerous_attribute_chains():
    """`pd.io.common.os.system(...)` 这类"白名单库 → 模块对象 → 危险属性"的
    链路此前可过预筛（代理实证可写出 marker 文件）。命名级黑名单掐断常见
    链路；这不是真沙箱（残余风险见 R1-D-1 决策点），但常见向量不得再放行。"""
    from research.modules import _prescreen_python_source as pre

    assert pre("import pandas as pd\nx = pd.io.common.os.system('echo hi')\n")
    assert pre("import numpy as np\nx = np.ctypeslib.ctypes\n")
    assert pre("import pandas as pd\npd.read_csv('/etc/passwd')\n")
    assert pre("import pandas as pd\npd.to_csv('x')\n")
    # 正常模块源码不得被误伤；泛用属性名（remove/write/loads/to_json…）不得
    # 进黑名单（V2 复核：过宽会误杀合法件）
    assert pre(
        "import numpy as np\nimport pandas as pd\n"
        "class Module:\n"
        "    def __init__(self, params): self.n = int(params.get('n', 20))\n"
        "    def scan(self, ctx, members):\n"
        "        out = list(members)\n"
        "        out.remove(out[0])\n"
        "        close = ctx.panel.series(members[0].symbol, 'close')\n"
        "        df = pd.DataFrame({'close': close})\n"
        "        df.to_json()\n"
        "        return []\n"
    ) == []


# ----------------------------------------------------------------------
# V1 复核残留：多个除权因子落到同一根 bar 时必须**累乘**
# ----------------------------------------------------------------------


def test_tradability_multi_factor_on_one_bar_multiplies():
    """停牌跨越两个除权日时两个因子落在同一根 bar 上。

    V1 复核实证：旧写法 `f_ax[k] = f` 只留最后一个因子 → 该日基准价偏高 →
    产出假跌停（真实库 002129.SZ 有 1 例）。改为 `*=` 后必须按乘积修正。
    """
    from gateway.tradability import compute_tradability

    days = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4),
            date(2024, 1, 5), date(2024, 1, 8)]
    # 01-04 / 01-05 停牌（无 bar），两个因子分别存于两日 → 都落到 01-08 这根 bar
    # 复牌价 = 10.0 / (2.0×1.5) 的两因子复合跳水 → 落在修正后的带内
    closes = pd.Series({date(2024, 1, 2): 10.0, date(2024, 1, 3): 10.0,
                        date(2024, 1, 8): 10.0 / 3.0})
    frame = compute_tradability(
        None, symbols=["600519.SS"], dates=days, raw_closes={"600519.SS": closes},
        ex_factors={"600519.SS": [("2024-01-04", 2.0), ("2024-01-05", 1.5)]},
        listing_dates={"600519.SS": None},
    )
    row = frame[frame["date"] == date(2024, 1, 8)].iloc[0]
    # 基准 = 10.0 / (2.0 × 1.5) = 3.3333 → 涨停 3.67 / 跌停 3.0（只取 1.5 会是 7.33/6.00）
    assert row["limit_up_price"] == pytest.approx(3.67, abs=0.01)
    assert row["limit_down_price"] == pytest.approx(3.0, abs=0.01)
    assert bool(row["is_limit_down"]) is False, "累乘缺失会产出假跌停"
    assert bool(row["is_limit_up"]) is False


# ----------------------------------------------------------------------
# V1 复核残留：live_bars 不得覆盖真实 bar，受限句柄不得注入
# ----------------------------------------------------------------------


def test_live_bars_never_overwrites_a_real_bar(test_db):
    """`live_bars` 只在库里确实没有该日 bar 时生效（V1：旧写法可把真实的
    is_limit_up=True 翻成 False）。"""
    from gateway.service import Gateway

    d_prev, d_day = date(2024, 3, 11), date(2024, 3, 12)
    df = pd.DataFrame([
        {"time": f"{d_prev.isoformat()} 00:00:00", "open": 10.0, "high": 10.0,
         "low": 10.0, "close": 10.0, "volume": 1000, "amount": 10_000.0},
        # 真实 bar：收盘 11.0 顶格 → is_limit_up=True
        {"time": f"{d_day.isoformat()} 00:00:00", "open": 11.0, "high": 11.0,
         "low": 11.0, "close": 11.0, "volume": 1000, "amount": 11_000.0},
    ])
    test_db.save_market_data("600519.SS", df, price_mode="raw", period="1d")
    gw = Gateway(test_db)
    as_of = datetime.combine(d_day, time(15, 0))
    real = gw.get_tradability(symbols=["600519.SS"], dates=[d_day],
                              as_of=as_of, caller_layer="test")
    assert bool(real.iloc[0]["is_limit_up"]) is True
    # 注入一个不同的价：真实的 bar 必须仍然胜出
    patched = gw.get_tradability(symbols=["600519.SS"], dates=[d_day], as_of=as_of,
                                 caller_layer="test", live_bars={"600519.SS": 10.01})
    assert bool(patched.iloc[0]["is_limit_up"]) is True, "真实 bar 不得被 live_bars 覆盖"
    assert float(patched.iloc[0]["limit_up_price"]) == float(real.iloc[0]["limit_up_price"])


def test_bound_gateway_rejects_live_bars(test_db):
    """受限句柄是给**模块**用的：模块不得为自己伪造决策日 bar（V1 实证可经
    BoundGateway 注入并改写卡控结论）——按越权拒绝并留痕。"""
    from gateway.service import Gateway, GatewayViolation

    d_day = date(2024, 3, 12)
    gw = Gateway(test_db)
    bound = gw.bind(as_of=datetime.combine(d_day, time(15, 0)), caller_layer="portfolio",
                    run_id="R-bound")
    with pytest.raises(GatewayViolation):
        bound.get_tradability(symbols=["600519.SS"], dates=[d_day],
                              live_bars={"600519.SS": 0.01})


# ----------------------------------------------------------------------
# V1 复核残留：live_overlay 的"前一根 bar"不得取到当日自己的 bar
# ----------------------------------------------------------------------


def test_live_overlay_prev_bar_excludes_the_as_of_day(test_db, monkeypatch):
    """合成 bar 的 vol 基准必须来自 as_of **之前**的 bar。

    V1 复核实证：窗口上界曾是闭区间的 as_of → `tail(1)` 拿到当日自己的
    volume（9999）而非前一日的 1111；同时窗口只有 10 自然日，长假后取不到
    前一根（volume 退化为 0）。这里用真库 + 桩报价驱动真实 `_overlay`。
    """
    import data.intraday_service as intraday
    import data.service as data_service
    from gateway.live_overlay import default_live_overlay

    d_prev, d_day = date(2024, 3, 11), date(2024, 3, 12)
    df = pd.DataFrame([
        {"time": f"{d_prev.isoformat()} 00:00:00", "open": 10.0, "high": 10.0,
         "low": 10.0, "close": 10.0, "volume": 1111, "amount": 11_110.0},
        {"time": f"{d_day.isoformat()} 00:00:00", "open": 11.0, "high": 11.0,
         "low": 11.0, "close": 11.0, "volume": 9999, "amount": 109_989.0},
    ])
    test_db.save_market_data("600519.SS", df, price_mode="raw", period="1d")

    captured: dict = {}

    def _fake_bar(quote, prev_vol):
        captured["prev_vol"] = prev_vol
        return {"time": f"{d_day.isoformat()} 15:00:00", "open": 11.0, "high": 11.2,
                "low": 10.9, "close": 11.1, "volume": 500, "amount": 5550.0}

    class _Svc:
        def fetch_latest_quotes(self, symbols):
            return {s: {"price": 11.1, "volume": 500} for s in symbols}

    monkeypatch.setattr(intraday, "build_synthetic_bar", _fake_bar)
    monkeypatch.setattr(data_service, "get_data_service", lambda: _Svc())

    overlay = default_live_overlay(test_db)
    out = overlay(["600519.SS"], as_of=datetime.combine(d_day, time(14, 5)))
    assert "600519.SS" in out
    assert captured["prev_vol"] == 1111.0, \
        f"前一根 bar 的 volume 必须是 {d_prev} 的 1111，实际 {captured['prev_vol']}"
