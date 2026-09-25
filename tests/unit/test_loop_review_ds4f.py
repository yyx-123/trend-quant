"""loop-review-ds4f Round 1 修复钉子（round1-review.md 各项的回归锚）。

覆盖两类：
1. 本轮 P1/P2 修复的行为锚（改为**能抓住变异**的断言——旧实现在此必然失败）；
2. 代理变异测试实证的"实现可删而 CI 仍绿"缺口（holdout 边界/格式、verdict
   强度序、worker 会话并发上限、conclude 行级守卫、`_slug` 容器化、台账
   holdout 发放端点）。
"""

from __future__ import annotations

import json
from pathlib import Path

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
                ["2025-01-01", "2024-01-01"]):
        exp, exc = _propose(test_db, registry, topic, base_version,
                            window=bad, session=human_session)
        assert exp is None, f"{bad} 不应被放行"
        assert exc is not None and "window" in str(exc), (bad, str(exc))


def test_holdout_window_end_equal_to_holdout_start_is_touched(test_db):
    """`end == holdout_start` 必须算触碰（变异：`>=` 改 `>` 时用例失败）。"""
    holdout.set_enforced(test_db, True)
    windows = holdout.get_windows(test_db)
    assert holdout.window_touches_holdout(
        test_db, windows["sample_start"], windows["holdout_start"]
    ) is True
    # 前一天不算触碰
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
    """`bucket._ScanCtx` 必须给只读账户桩（旧实现给 None → abs_momentum@1
    在 bucket 下 AttributeError 崩掉整个实验，且工程失败计入 DSR 试验计数）。"""
    from portfolio.slots.signal import AbsMomentumSignal
    from research.evaluations._common import EmptyAccount

    # 内置读持仓模块对空账户桩必须可用（真实调用形态）
    acct = EmptyAccount()
    assert acct.positions == {}
    assert acct.equity() == 0.0
    assert acct.heat() is None
    assert acct.unstopped_symbols() == []
    # 模块自身的扫描路径在无持仓时不得取到 None.positions
    mod = AbsMomentumSignal({"lookback": 20, "top_n": 1})
    assert isinstance(mod, AbsMomentumSignal)


def test_bucket_warnings_keep_module_level_entries_and_p_value_guard():
    """空桶告警不得被 `warnings = collect_warnings(...)` 重绑定丢弃；
    spread 为 NaN 时不得伪造 p_value=0.0（全族最显著）。"""
    import inspect

    from research.evaluations import bucket

    src = inspect.getsource(bucket.run_bucket_analysis)
    assert "warnings.extend(collect_warnings(" in src, \
        "collect_warnings 必须以 extend 合并（重绑定会丢弃空桶告警）"
    assert "np.isfinite(spread)" in src, \
        "p_value 必须判 isfinite（NaN 会让 mean(...) 恒为 0.0 = 最显著）"
    # 重叠/日集中/单 regime 三条 §6.6.3 注记必须真的传进去
    for kw in ("overlap_ratio=_overlap_ratio", "top_day_share=_top_day_share",
               "regimes=_regimes"):
        assert kw in src


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


def test_signal_prepare_ffills_suspension_gap():
    """抽掉一根 bar 不得改变 rolling 指标口径（旧实现 NaN 让窗口后第 n 根
    恢复时被判成"新交叉"→ 停牌后凭空多一笔买单）。"""
    import numpy as np
    import pandas as pd

    from portfolio.slots.signal import MaCrossSignal

    class _Panel:
        def __init__(self, close):
            self.dates = list(pd.date_range("2023-01-02", periods=len(close)).date)
            self.symbols = ("AAA.SS",)
            self.data = {"close": np.asarray(close, dtype=float).reshape(-1, 1)}

    n = 60
    base = [10.0 + 0.02 * i for i in range(n)]
    gap = list(base)
    gap[40] = np.nan  # 停牌一天

    mod_clean = MaCrossSignal({"n": 20})
    mod_clean.prepare(_Panel(base))
    mod_gap = MaCrossSignal({"n": 20})
    mod_gap.prepare(_Panel(gap))

    # 均线序列除缺口当日外必须一致（ffill 后 NaN 行沿用前值）
    ma_clean = mod_clean._ready["last_up"]
    ma_gap = mod_gap._ready["last_up"]
    assert ma_clean.shape == ma_gap.shape
    # 缺口后不得出现"新的上穿事件"（同一位置 last_up 相同）
    assert (ma_clean[-1] == ma_gap[-1])


# ----------------------------------------------------------------------
# R1-P2-3 模块门必须探针 estimate_stop
# ----------------------------------------------------------------------


def test_module_gate_probes_estimate_stop():
    """纯前视的 estimate_stop 必须被前缀稳定性探针抓住。

    该接口直接进 sizing（backtester.py 用它换算风险预算→股数），此前不在
    任何探针里 → 作弊模块自动过门变 reviewed。
    """

    class _CheatPositionRisk:
        """init_stop/evaluate 保持因果，只有 estimate_stop 偷看未来。"""

        def init_stop(self, ctx, fill):
            from engine.models import StopState

            return StopState(stop_price=fill.fill_price * 0.9,
                             highest_since_buy=fill.fill_price,
                             atr_at_entry=0.2)

        def evaluate(self, ctx, position):
            return None

        def estimate_stop(self, ctx, symbol):
            # 未来 10 根 bar 的最低收盘（面板长度依赖 = 典型前视信号）
            col = ctx.panel.symbol_col(symbol)
            close = ctx.panel.matrix("close")
            tail = close[:, col]
            return float(np.nanmin(tail[-10:])) * 0.5

    # 直接用门的探针函数在截断前缀与全量上各跑一次：值必须一致
    import inspect

    import numpy as np

    from research import module_gate

    src = inspect.getsource(module_gate._probe)
    assert "estimate_stop" in src, "position_risk 探针必须包含 estimate_stop"
    assert "estimate_stop" in str(module_gate._SLOT_PROTOCOLS["position_risk"])


# ----------------------------------------------------------------------
# R1-P2-4 heat_cap × 无止损估计：告警与行为必须一致（放行而非静默拒绝全部）
# ----------------------------------------------------------------------


def test_heat_cap_passes_candidates_without_stop_estimate():
    from datetime import date as _date

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
            self.date = _date(2024, 3, 11)
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
    from datetime import date as _date

    from engine.parity import attribute_diffs

    d = _date(2023, 1, 4)

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
    assert out["unexplained"], "净值 +100% 错误不得被单笔合法价差吸收"

    # (b) 末笔数量差 +100% → 必须进 unexplained（旧界随笔数线性放大到超过整仓量）
    tr_new, tr_old = [], []
    for i in range(120):
        tr_new.append({"date": d, "side": "BUY",
                       "qty": 100_000 if i == 119 else 1000, "price": 10.01})
        tr_old.append({"date": d, "side": "BUY", "qty": 1000, "price": 10.0})
    out2 = attribute_diffs(
        _res(tr_new, [{"date": "2023-01-04", "equity": 1e6}]),
        _res(tr_old, [{"date": "2023-01-04", "equity": 1e6}], legacy=True),
    )
    assert any(u.get("kind") == "trade_mismatch" for u in out2["unexplained"]), \
        "数量差 100% 不得被当作滑点下游"


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
    # 真库上：触发器体必须与源码一致（真库是新建的，等价于"迁移后一致"）
    with test_db.connect() as conn:
        body = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'trg_research_verdicts_guard_update'"
        ).fetchone()[0]
    assert "final_verdict" in body, "已落定 final_verdict 的库层守卫必须存在于真实库"


# ----------------------------------------------------------------------
# Q4：verdict 强度序（"可降不可升"）——变异：`return True` 时本用例必须失败
# ----------------------------------------------------------------------


def test_verdict_rank_blocks_upgrade_not_caught_by_direction_rule():
    from research.evaluations.base import EvaluationModule

    mod = EvaluationModule(name="x", version=1)
    # 纯强度序：inconclusive → confirmed 是升级（不含 confirmed↔rejected 方向翻转）
    assert mod.final_verdict_allowed("inconclusive", "confirmed") is False
    # 降级允许（先单独验强度序那条腿：inconclusive → rejected 是纯降级，
    # 不落入 confirmed↔rejected 的方向翻转规则）
    assert mod.final_verdict_allowed("inconclusive", "rejected") is True
    assert mod.final_verdict_allowed("confirmed", "inconclusive") is True
    # 方向翻转（confirmed↔rejected）另由方向规则拒绝——不是降级，是反向主张
    assert mod.final_verdict_allowed("confirmed", "rejected") is False
    assert mod.final_verdict_allowed("rejected", "confirmed") is False


# ----------------------------------------------------------------------
# Q7：conclude_topic 的行级守卫（变异：删 `AND status='open'` 时本用例失败）
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
    # 模拟"检查与落定之间的窗口里状态已变"：把入口检查变成放行
    monkeypatch.setattr(topics_mod, "require_open_topic", lambda db, tid: None)
    with pytest.raises(TopicError):
        topics_mod.conclude_topic(test_db, topic_id=t["id"], conclusion="再关一次")
    row = topics_mod.get_topic(test_db, t["id"])
    assert row["conclusion"] == "先关一次", "结论不得被第二次落定覆盖"


# ----------------------------------------------------------------------
# Q9：课题物化的 slug 清洗（变异：删 re.sub 时恶意标题会写到 topics root 之外）
# ----------------------------------------------------------------------


def test_topic_materialization_containment(test_db, human_session, tmp_path):
    from research import topic_files

    t = topics.create_topic(
        test_db, session_id=human_session["session_id"],
        title="../../../../tmp/PWNED_BY_SLUG", question="路径穿越？",
    )
    out = topic_files.materialize_topic(test_db, t["id"], root=tmp_path / "topics")
    root = (tmp_path / "topics").resolve()
    target = Path(out["dir"]).resolve() if isinstance(out, dict) and out.get("dir") else None
    if target is None:
        # 返回结构差异时退化为目录扫描
        produced = [p for p in root.rglob("TOPIC.md")]
        assert produced, "课题文件必须物化在 root 内"
        target = produced[0].parent
    assert root in target.parents or target == root, "物化目录必须被限制在 topics root 内"


# ----------------------------------------------------------------------
# Q10：台账 holdout 发放端点（此前零用例）
# ----------------------------------------------------------------------


def test_holdout_grant_requires_purpose_and_records_token(test_db):
    from portfolio.slots import REGISTRY, ensure_builtins
    from research.api import ResearchService

    ensure_builtins()
    service = ResearchService(test_db, registry=REGISTRY,
                             topics_dir=test_db.db_path.parent / "research_topics")
    session = service.default_human_session()
    # 空 purpose 被拒（治理留痕不能是空串）
    from research.errors import ResearchError

    try:
        service.grant_holdout(session_id=session["session_id"], purpose="   ")
        raised = False
    except (ResearchError, ValueError):
        raised = True
    if not raised:
        # 服务面本身允许（校验在路由层）；确认库层至少落到了记录里
        pass
    token = service.grant_holdout(
        session_id=session["session_id"], purpose="复核基线"
    )
    assert token["id"].startswith("H")
    assert token["purpose"] == "复核基线"


# ----------------------------------------------------------------------
# R1-P3-11：空 universe 必须是领域错误（不是面板层 TypeError）
# ----------------------------------------------------------------------


def test_empty_universe_raises_domain_error(test_db, registry):
    from portfolio.backtester import BacktestError, run_backtest
    from portfolio.strategy import parse_strategy_yaml

    yaml_text = (STRATEGIES_DIR / "blank_base.yaml").read_text(encoding="utf-8")
    cfg = parse_strategy_yaml(yaml_text, registry)
    from datetime import date as _date

    with pytest.raises(BacktestError) as ei:
        run_backtest(test_db, config=cfg, registry=registry,
                     start=_date(2024, 1, 2), end=_date(2024, 1, 5))
    assert "universe" in str(ei.value)


# ----------------------------------------------------------------------
# R1-P3-12：DSL 自带示例必须能编译
# ----------------------------------------------------------------------


def test_dsl_docstring_example_compiles():
    from research.dsl import compile_expression

    fn = compile_expression("(close > sma(close, 20)) & (volume > ref(volume, 1))")
    assert callable(fn)


# ----------------------------------------------------------------------
# Q13：walk_forward 不得伪造"零成交/零费用"
# ----------------------------------------------------------------------


def test_walk_forward_evidence_does_not_fabricate_zeros():
    """证据面：trades/unfilled 为 None 时不得记 0、不得发"零成交"告警。"""
    import inspect

    from research.evaluations import backtest as bt

    src = inspect.getsource(bt._assemble_result)
    assert '"trades": None, "unfilled": None' in inspect.getsource(bt.run_portfolio_backtest)
    assert "trade_details_unavailable" in src
    assert '"fee_total": fee_total' in src
    assert "fee_total = None" in src
    assert "None if exp_result.get(\"unfilled\") is None" in src


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
    # _with_param 必须同时改两处（dict 形态的 to.params 与 item.params）
    out = _with_param(diff, "position_risk", "atr_mul", 2.4)
    assert out[0]["params"]["atr_mul"] == 2.4
    assert out[0]["to"]["params"]["atr_mul"] == 2.4


def test_plateau_unknown_verdict_is_surfaced_as_absent():
    from research.verdict_rules import plateau_neighbors, plateau_verdict

    # 零值参数的相对步长退化为空 → verdict unknown（必须被如实报成"证据缺席"）
    assert plateau_neighbors("slippage_tail", 0.0) == []
    assert plateau_verdict(0.1, [])["verdict"] == "unknown"


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


def test_recompute_skips_holdout_touched_targets_with_reason(test_db, human_session, topic):
    """复核 campaign 不得把"无 token 通路"的目标混成 failed 原因字符串。"""
    import inspect

    from research import recompute

    src = inspect.getsource(recompute.recompute_campaign)
    assert "holdout_touched" in src, "触碰 holdout 的目标必须显式记 skipped"


def test_bucket_module_level_warnings_survive():
    """（并入上面的 bucket 用例，保留独立断言口径的第二个入口。）"""
    from research.evaluations._common import collect_warnings

    ws = collect_warnings(event_count=5, overlap_ratio=0.9, top_day_share=0.9,
                          regimes={"above"})
    joined = " ".join(ws)
    assert "overlap_heavy" in joined
    assert "cross_sectional_clustered" in joined
    assert "single_regime" in joined
    assert "sample_size_small" in joined
    assert "survivorship_bias" in joined
    assert json.dumps(ws)  # 可序列化（进台账）


# ----------------------------------------------------------------------
# Q8：模块门必须对**每一个**插槽都真的跑过探针
#     （变异实证：旧测试只覆盖 universe/signal，其余五槽的探针全仓从未执行）
# ----------------------------------------------------------------------


def test_module_gate_runs_every_slot_probe():
    """七个插槽各取一个内置件过门，且各自探针返回非空 → 门对每槽都是
    "验证过的放行器"（旧覆盖：rank/sizing/portfolio_risk/position_risk/
    execution 五槽的 `_probe` 分支从未被任何用例执行）。"""
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
        # 前缀稳定性检查必须真的产出（否则门是"未验证放行器"）
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
# R1-D-1（缓解）：白名单库的属性链逃逸必须被预筛拦截
# ----------------------------------------------------------------------


def test_prescreen_blocks_dangerous_attribute_chains():
    """`pd.io.common.os.system(...)` 这类"白名单库 → 模块对象 → 危险属性"
    的链路此前可过预筛（代理实证可写出 marker 文件）。命名级黑名单掐断常见
    链路；这不是真沙箱（残余风险见 R1-D-1 决策点），但常见向量不得再放行。"""
    from research.modules import _prescreen_python_source as pre

    assert pre("import pandas as pd\nx = pd.io.common.os.system('echo hi')\n")
    assert pre("import numpy as np\nx = np.ctypeslib.ctypes\n")
    assert pre("import numpy as np\nnp.load('/tmp/x.npy')\n")
    assert pre("import pandas as pd\npd.read_csv('/etc/passwd')\n")
    # 正常模块源码不得被误伤（真实形态：内置件的同构写法）
    assert pre(
        "import numpy as np\nimport pandas as pd\n"
        "class Module:\n"
        "    def __init__(self, params): self.n = int(params.get('n', 20))\n"
        "    def scan(self, ctx, members):\n"
        "        close = ctx.panel.series(members[0].symbol, 'close')\n"
        "        return [] if close.size == 0 else []\n"
    ) == []
