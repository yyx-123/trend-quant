"""Round 23 修复钉子（统计面 + 报告面 + 安全面）。

对应 round23-review.md 的发现：
- R23A-F1（P1）head_to_head 曾用**未配对**单序列 PSR 当判定门；
- R23A-F2（P2）PBO 在"只剩一个可用变体"时伪造 1.0；
- R23A-F3/F4（P2）课题 FDR 的 p 口径与家族成员如实可见；
- R23A-F5/F7（P2）plateau 判据按 95% 预测区间校准（含零邻域中性）；
- R23A-F8/F10/F11（P3）dsr 的 n_trials 守卫 / moments 零方差 / 退化列判据；
- R23B-F1（P1）MCP 不得接受 caller 直传 token（4 位顺序号 = 可猜的自授权凭证）；
- R23B-F2（P2）worker 分支的运行信封；
- R23B-F3/F4（P2）报告的费用恒等式与**净额**盈亏口径；
- R23B-F5（P2）并发初始化 DDL 竞态；
- R23B-F7（P3）约束冲突只翻译唯一约束；
- R23B-F10（P3）查重与 INSERT 同事务。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from portfolio.registry import fresh_registry
from portfolio.seed import seed_default_library
from portfolio.slots import register_builtin_modules
from research import experiments, sessions, topics

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def registry():
    reg = fresh_registry()
    register_builtin_modules(reg)
    return reg


def _write_symbol(db, symbol, closes, start="2022-01-03", asset_type="etf"):
    n = len(closes)
    dates = pd.bdate_range(start, periods=n)
    arr = np.asarray(closes, dtype=float)
    df = pd.DataFrame({
        "time": dates, "open": arr, "high": arr * 1.01, "low": arr * 0.99,
        "close": arr, "volume": np.full(n, 2e6), "amount": arr * 2e6 * 5,
    })
    db.save_market_data(symbol, df, price_mode="qfq")
    db.save_market_data(symbol, df, price_mode="raw")
    db.save_instrument_metadata([{
        "symbol": symbol, "name": symbol, "category_l1": "T", "category_l2": "T2",
        "category_l3": "", "enabled": 1, "asset_type": asset_type,
    }])


@pytest.fixture
def market(test_db):
    rng = np.random.default_rng(23)
    for i in range(3):
        closes = 12 * np.exp(np.cumsum(rng.normal(0.002, 0.018, 360)))
        _write_symbol(test_db, f"W{i:03d}.SS", closes)
    return test_db


def _env(db, registry):
    versions = seed_default_library(db, registry)
    session = sessions.get_or_create_default_human_session(db)
    topic = topics.create_topic(
        db, session_id=session["session_id"], title="R23 钉子", question="?"
    )
    return {"versions": versions, "session": session, "topic": topic}


# --------------------------------------------------------------------------
# R23A-F1（P1）：配对判定门
# --------------------------------------------------------------------------


def test_paired_gate_is_the_single_source():
    """R23A-F1：判定显著性必须走配对门（backtest 与 h2h 共用同一函数）。"""
    from research.verdict_rules import paired_gate_ok

    rules = {"min_t_stat": 1.645, "min_dsr_on_diff": 0.0}
    # 高相关配对：t 大、差序列 DSR 高 → 通过
    assert paired_gate_ok({"t_stat": 6.0, "n_pairs": 2400, "dsr_on_diff": 0.9}, rules=rules)
    # t 不显著 → 不通过（DSR 再高也不行）
    assert not paired_gate_ok({"t_stat": 0.5, "n_pairs": 2400, "dsr_on_diff": 0.99}, rules=rules)
    # 小样本用 t 临界（n=5 → df=4 → 临界 ≈2.13 > 1.645）
    assert not paired_gate_ok({"t_stat": 2.0, "n_pairs": 5, "dsr_on_diff": 1.0}, rules=rules)
    assert paired_gate_ok({"t_stat": 2.5, "n_pairs": 5, "dsr_on_diff": 1.0}, rules=rules)
    # 负向（A 显著劣于 B）
    assert paired_gate_ok({"t_stat": -6.0, "n_pairs": 2400}, rules=rules, direction="negative")
    assert not paired_gate_ok({"t_stat": 6.0, "n_pairs": 2400}, rules=rules, direction="negative")
    # DSR 门生效时必须过门
    strict = {"min_t_stat": 1.645, "min_dsr_on_diff": 0.95}
    assert not paired_gate_ok({"t_stat": 6.0, "n_pairs": 2400, "dsr_on_diff": 0.5}, rules=strict)
    assert not paired_gate_ok(None, rules=rules)

    # h2h 的判定必须调用它（且不得再出现"未配对 PSR 当门"的旧形态）
    h2h = (ROOT / "src" / "research" / "evaluations" / "head_to_head.py").read_text(encoding="utf-8")
    assert "paired_gate_ok(" in h2h, "h2h 判定必须走配对门单一真源"
    assert "psr_ab >= 0.95" not in h2h and "psr_ab <= 0.05" not in h2h, (
        "未配对单序列 PSR 不得再当判定门（DS-P1-6 已点名的口径错误）"
    )
    rules_src = (ROOT / "src" / "research" / "verdict_rules.py").read_text(encoding="utf-8")
    assert "paired_gate_ok(" in rules_src, "单一真源必须落在 verdict_rules"
    backtest = (ROOT / "src" / "research" / "evaluations" / "backtest.py").read_text(encoding="utf-8")
    assert "suggest_backtest_verdict(" in backtest, "backtest 判定必须走 verdict_rules"


# --------------------------------------------------------------------------
# R23A-F2 / F12：PBO 退化与零假设对照
# --------------------------------------------------------------------------


def test_pbo_single_usable_variant_is_degenerate_not_one():
    """R23A-F2：只剩一个可用变体时不得报 PBO=1.0（伪造"必然过拟合"）。"""
    from research.stats.fdr_pbo import pbo_cscv

    rng = np.random.default_rng(7)
    T = 1024
    main = rng.normal(0.0004, 0.01, size=(T, 1))
    flat = np.full((T, 2), 1e-6)          # 全现金探针（方差不可分辨）
    out = pbo_cscv(np.hstack([main, flat]), n_blocks=8)
    assert out["pbo"] is None, f"单可用列时 PBO 无判别力，实测得到 {out['pbo']}"
    assert out["degenerate_variants"] is True
    assert out["combinations_skipped"] == out["n_combinations"]

    # 真·多可用列：正常出 PBO，且带零假设对照（N 为奇数时期望低于 0.5）
    three = np.hstack([rng.normal(0.0004, 0.01, size=(T, 3)), flat])
    out2 = pbo_cscv(three, n_blocks=8)
    assert out2["pbo"] is not None
    assert out2["n_variants"] == 3
    assert abs(out2["pbo_null_expected"] - 1 / 3) < 1e-12, (
        "N=3 的零假设期望是 1/3（中位秩恰落 0.5 不计过拟合）——只报 PBO=0.4 会被读成'不过拟合'"
    )


def test_sharpe_vec_marks_no_observation_columns_degenerate():
    """R23A-F11 连带：全 NaN / 单观测列必须记 NaN（此前带 sharpe=0.0 混进可用列）。"""
    from research.stats.fdr_pbo import _sharpe_vec

    x = np.column_stack([
        np.random.default_rng(1).normal(0.0, 0.01, 100),
        np.full(100, np.nan),
        np.concatenate([[0.001], np.full(99, np.nan)]),
    ])
    import warnings as _warnings

    with _warnings.catch_warnings():
        _warnings.simplefilter("ignore", RuntimeWarning)
        out = _sharpe_vec(x)
    assert np.isfinite(out[0])
    assert np.isnan(out[1]) and np.isnan(out[2])


# --------------------------------------------------------------------------
# R23A-F3 / F4：FDR 的 p 口径与家族可见性
# --------------------------------------------------------------------------


def test_topic_fdr_uses_paired_p_and_reports_excluded(market, registry):
    """R23A-F3/F4：p 取配对差序列口径；未产出 p 的实验数如实可见。"""
    from research.conclusion import build_conclusion_summary
    from research.pipeline import run_experiment

    env = _env(market, registry)
    spec = {"base": env["versions"]["base-v1"],
            "diff": [{"slot": "position_risk", "from": "hard_stop@1", "to": "hard_stop@1",
                      "params": {"atr_mul": 2.0}}],
            "window": ["2022-06-01", "2023-06-01"]}
    exp = experiments.propose_experiment(
        market, session_id=env["session"]["session_id"], title="FDR 口径", topic_id=env["topic"]["id"],
        evaluation_module="portfolio_backtest@1", spec=spec,
        hypothesis="课题 FDR 的 p 必须取配对差序列口径", registry=registry,
    )
    from research import verdict as verdict_mod

    run_experiment(market, exp["id"], registry=registry)
    latest0 = verdict_mod.latest_verdict(market, exp["id"])
    verdict_mod.confirm_verdict(
        market, experiment_id=exp["id"], final_verdict=latest0["suggested_verdict"],
        reasoning="定论以便进课题摘要", session_id=env["session"]["session_id"],
    )
    summary = build_conclusion_summary(market, env["topic"]["id"])
    fdr = summary["fdr"]
    assert "n_tested" in fdr and "n_excluded" in fdr, fdr
    assert fdr["n_tested"] == 1 and fdr["n_excluded"] == 0, fdr
    # 与 evidence 里的配对 p 对齐（不是 1 - stats.psr）
    latest = verdict_mod.latest_verdict(market, exp["id"])
    evidence = json.loads(latest["evidence_json"]) if isinstance(latest["evidence_json"], str) else latest["evidence_json"]
    paired = (evidence.get("stats") or {}).get("paired") or {}
    assert paired.get("psr_on_diff") is not None, "夹具必须产出配对统计"
    src = (ROOT / "src" / "research" / "conclusion.py").read_text(encoding="utf-8")
    assert 'paired.get("psr_on_diff")' in src, "课题 FDR 的 p 必须取配对差序列口径"
    assert '1.0 - float(paired["psr_on_diff"])' in src


# --------------------------------------------------------------------------
# R23A-F5 / F7：plateau 判据校准
# --------------------------------------------------------------------------


def test_plateau_rule_is_prediction_interval_calibrated():
    """R23A-F5：判据 = 95% 预测区间；真高原误判 peak 率必须落在名义附近（<15%）。"""
    from research.verdict_rules import plateau_verdict

    rng = np.random.default_rng(20260926)
    k = 3
    flagged = 0
    trials = 4000
    for _ in range(trials):
        nb = rng.normal(0.30, 0.15, k)
        if plateau_verdict(float(rng.normal(0.30, 0.15)), list(nb))["verdict"] == "peak":
            flagged += 1
    rate = flagged / trials
    assert rate < 0.15, (
        f"真高原误判 peak 率 {rate:.3f} 过高（旧判据实测 0.53；新判据名义 5%）"
    )
    # 功效：真孤峰必须能抓（k=3 实测 ~0.78）
    caught = sum(
        1 for _ in range(1000)
        if plateau_verdict(float(rng.normal(0.50, 0.10)),
                           list(rng.normal(0.0, 0.10, k)))["verdict"] == "peak"
    )
    assert caught / 1000 > 0.6, f"真孤峰漏检过多：{caught / 1000:.3f}"


def test_plateau_zero_neighbor_is_not_opposite_direction():
    """R23A-F7：邻域 Δ 恰为 0（该参数在此取值不生效）不得判成"反向"→ peak。"""
    from research.verdict_rules import plateau_verdict

    out = plateau_verdict(0.30, [0.0, 0.25])
    assert out["same_direction"] is True, out
    assert out["verdict"] == "plateau", out
    # 真孤峰（邻域贴零）仍必须是 peak
    out2 = plateau_verdict(0.90, [0.0, 0.05])
    assert out2["verdict"] == "peak", out2


# --------------------------------------------------------------------------
# R23A-F8 / F10：数值守卫
# --------------------------------------------------------------------------


def test_dsr_rejects_nonpositive_trials_and_moments_guard():
    """R23A-F8/F10：n_trials<1 显式拒绝；常量序列不得给出非物理矩。"""
    from research.stats.psr import dsr, moments

    with pytest.raises(ValueError):
        dsr(0.05, 500, 0.0, 3.0, 0)
    with pytest.raises(ValueError):
        dsr(0.05, 500, 0.0, 3.0, -3)
    # n_trials=1 仍按"首次尝试"退化为 PSR(0)
    assert 0.0 <= dsr(0.05, 500, 0.0, 3.0, 1) <= 1.0

    _mean, std, skew, kurt = moments([0.01] * 10)
    assert std == 0.0 and skew == 0.0 and kurt == 3.0, (std, skew, kurt)


# --------------------------------------------------------------------------
# R23B-F1（P1）：MCP 不得接受 caller 直传 token
# --------------------------------------------------------------------------


def test_mcp_tools_do_not_accept_caller_tokens(market, registry, monkeypatch):
    """R23B-F1：MCP 工具签名不得含 token 参数（4 位顺序号 = AI 可猜的自授权凭证）。"""
    import inspect

    from trend_mcp import research_tools as rt

    tools: dict = {}

    class FakeMCP:
        def tool(self):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn
            return deco

    rt.register_research_tools(FakeMCP())
    for name in ("research_propose_experiment", "research_rerun_experiment",
                 "research_run_experiment"):
        params = set(inspect.signature(tools[name]).parameters)
        assert not {p for p in params if "token" in p}, f"{name} 不得暴露 token 参数：{params}"

    # 匿名消费全局 token 必须被拒（这正是 AI 自授权路径）
    from research.errors import HoldoutError
    from research.holdout import check_window, grant_token, set_enforced

    env = _env(market, registry)
    set_enforced(market, True)
    try:
        token = grant_token(market, session_id=env["session"]["session_id"],
                            purpose="全局 token：不得匿名消费")
        with pytest.raises(HoldoutError, match="explicit experiment_id"):
            check_window(market, start="2025-01-01", end="2025-06-01", token_id=token["id"])
        # 具名**真实**实验（人走 CLI 的路径）仍可用；编造的 id 必须被拒
        spec = {"base": env["versions"]["base-v1"],
                "diff": [{"slot": "position_risk", "from": "hard_stop@1", "to": "hard_stop@1",
                          "params": {"atr_mul": 2.0}}],
                "window": ["2022-06-01", "2023-06-01"]}
        named = experiments.propose_experiment(
            market, session_id=env["session"]["session_id"], title="具名放行",
            topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
            spec=spec, hypothesis="具名实验才可消费 token", registry=registry,
        )
        assert check_window(market, start="2025-01-01", end="2025-06-01",
                            experiment_id=named["id"], token_id=token["id"]) is True
        with pytest.raises(HoldoutError, match="unknown experiment"):
            check_window(market, start="2025-01-01", end="2025-06-01",
                         experiment_id="E-invented", token_id=grant_token(
                             market, session_id=env["session"]["session_id"],
                             purpose="编造 id 不得消费")["id"])
    finally:
        set_enforced(market, False)


def test_reproduction_lineage_auto_backfill_spans_multiple_levels(market, registry):
    """R23B-F8：自动带出与绑定校验必须用**同一条**复现链（含二级复现）。"""
    from research.holdout import experiment_lineage, grant_token
    from research.pipeline import _pick_unconsumed_token, run_experiment

    env = _env(market, registry)
    spec = {"base": env["versions"]["base-v1"],
            "diff": [{"slot": "position_risk", "from": "hard_stop@1", "to": "hard_stop@1",
                      "params": {"atr_mul": 2.0}}],
            "window": ["2022-06-01", "2025-06-01"]}      # 触碰 holdout
    e1 = experiments.propose_experiment(
        market, session_id=env["session"]["session_id"], title="链首", topic_id=env["topic"]["id"],
        evaluation_module="portfolio_backtest@1", spec=spec,
        hypothesis="复现链上的 token 必须能被任意深度带出", registry=registry,
    )
    run_experiment(market, e1["id"], registry=registry)          # 无 token → failed（终态）
    token_id = grant_token(market, session_id=env["session"]["session_id"],
                           purpose="发给链首", experiment_id=e1["id"])["id"]
    e2 = experiments.rerun_experiment(market, experiment_id=e1["id"],
                                      session_id=env["session"]["session_id"])
    # 二级复现要能 rerun 需 e2 先到终态；这里临时关闭 enforce 让 e2 跑到 evaluating
    # 再定论（**不消费** token——enforce 关闭时窗口检查直接放行），
    # 以免测试链本身吃掉待验证的 token。
    from research.holdout import set_enforced

    set_enforced(market, False)
    try:
        run_experiment(market, e2["id"], registry=registry)
        from research import verdict as verdict_mod

        latest_e2 = verdict_mod.latest_verdict(market, e2["id"])
        verdict_mod.confirm_verdict(
            market, experiment_id=e2["id"],
            final_verdict=latest_e2["suggested_verdict"],
            reasoning="二级复现先定论", session_id=env["session"]["session_id"],
        )
    finally:
        set_enforced(market, True)
    e3 = experiments.rerun_experiment(market, experiment_id=e2["id"],
                                      session_id=env["session"]["session_id"])
    assert e3["parent_experiment_id"] == e2["id"]
    chain = experiment_lineage(market, e3["id"])
    assert chain[:3] == [e3["id"], e2["id"], e1["id"]], chain
    assert _pick_unconsumed_token(market, e3["id"]) == token_id, "二级复现必须也能带出链首 token"
    # 显式传入同一条链上的 token 也必须放行
    row = run_experiment(market, e3["id"], registry=registry, holdout_token=token_id)
    assert "suggested_verdict" in row, row


# --------------------------------------------------------------------------
# R23B-F2 / F5 / F7 / F10
# --------------------------------------------------------------------------


def test_worker_dispatch_envelope_is_explicit():
    """R23B-F2：worker（app 生产路径）分支也必须表达"未完成"，而不是让调用方猜。"""
    from research.pipeline import run_result_envelope

    class FakeWorker:
        def submit(self, experiment_id):
            return True

    class FakeService:
        worker = FakeWorker()

        def get_experiment(self, experiment_id):
            return {"status": "queued"}

    from trend_mcp.research_tools import _run_or_queue

    out = _run_or_queue(FakeService(), "E0001")
    assert out["mode"] == "queued"
    assert out["run_status"] == "queued", out
    assert out["verdict"] is None and out["error"] is None
    assert out["experiment_status"] == "queued"
    assert out.get("poll"), "必须告诉调用方去哪里取结果"
    # 同步分支的信封形态不变
    assert run_result_envelope({"suggested_verdict": "confirmed"})["run_status"] == "ran"
    assert run_result_envelope({"status": "failed", "error": "x"})["run_status"] == "failed"


def test_init_db_is_concurrency_safe(tmp_path):
    """R23B-F5：多进程同时打开同一库不得因 DDL 竞态起不来（旧实现 2~6 进程偶发必现）。"""
    db_path = tmp_path / "race.db"
    code = (
        "import sys; sys.path.insert(0, 'src');"
        "from data.storage.db import Database;"
        f"Database(r'{db_path}');"
        "print('OK')"
    )
    subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True,
                   text=True, check=False)     # 先建库
    # 10 进程 × 3 轮：实测修复前形态在此压力下 50/50 必然失败
    # （6 进程偶发、2 进程更偶发——钉子的压力必须够）
    for _ in range(3):
        procs = [subprocess.Popen([sys.executable, "-c", code], cwd=str(ROOT),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(10)]
        for proc in procs:
            out, err = proc.communicate()
            assert "OK" in out, f"并发初始化失败：{err.strip().splitlines()[-1] if err.strip() else out}"


def test_module_notnull_is_not_reported_as_duplicate(market, registry):
    """R23B-F7：NOT NULL 违例不得被误报成"模块已存在"（按唯一约束文案精确匹配）。"""
    from research import modules

    env = _env(market, registry)
    with pytest.raises(Exception) as exc:
        modules.propose_module(
            market, session_id=env["session"]["session_id"], slot="universe",
            name="probe_null", version=1, kind="python", source=None,
        )
    assert "already exists" not in str(exc.value), f"误诊：{exc.value}"


def test_concurrent_identical_specs_only_one_lands(market, registry):
    """R23B-F10：查重与 INSERT 同事务 → 并发提同一 spec 只能落一条真实验。"""
    env = _env(market, registry)
    spec = {"base": env["versions"]["base-v1"],
            "diff": [{"slot": "position_risk", "from": "hard_stop@1", "to": "hard_stop@1",
                      "params": {"atr_mul": 2.0}}],
            "window": ["2022-06-01", "2023-06-01"]}
    results: list[str] = []
    errors: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        try:
            exp = experiments.propose_experiment(
                market, session_id=env["session"]["session_id"], title="同 spec 并发",
                topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
                spec=spec, hypothesis="同一 spec 并发提交只能落一条真实验", registry=registry,
            )
            with lock:
                results.append(exp["id"])
        except Exception as exc:
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(results) == 1, f"并发同 spec 落库 {len(results)} 条（应恰 1 条）：{results} {errors}"
    assert errors and "duplicate_of" in errors[0], errors


# --------------------------------------------------------------------------
# R23B-F3 / F4：报告费用恒等式（含印花税）与净额盈亏口径
# --------------------------------------------------------------------------


def test_report_fee_identity_includes_stamp_tax():
    """R23B-F3：`cost.total_fees` 是**佣金+印花税**；同源字段是 total_trading_cost。

    股票腿有印花税时 `total_commission == total_fees` 不成立（实测差 307.49，
    近 3 倍）——R22 的钉子曾把这个错误等值固化（夹具只用 ETF，印花税为 0）。
    """
    from portfolio.reports import build_report

    nav = [{"date": f"2024-02-{d:02d}", "equity": 1_000_000.0} for d in range(1, 21)]
    fills = [
        {"symbol": "600001.SS", "side": "buy", "fill_date": "2024-02-02", "fill_price": 10.0,
         "quantity": 1000, "quantity_": 1000, "commission": 5.0, "stamp_tax": 0.0,
         "fee_total": 5.0},
        {"symbol": "600001.SS", "side": "sell", "fill_date": "2024-02-09", "fill_price": 11.0,
         "quantity": 1000, "commission": 5.0, "stamp_tax": 11.0, "fee_total": 16.0},
    ]
    rep = build_report(None, run_id="R23B-F3", nav_rows=nav, fills=fills,
                       unfilled=[], gate_log=[])
    s = rep["summary"]
    assert abs(rep["cost"]["total_fees"] - 21.0) < 1e-9, rep["cost"]
    assert abs(s["total_trading_cost"] - 21.0) < 1e-9, s["total_trading_cost"]
    assert abs(s["total_commission"] - 10.0) < 1e-9
    assert abs(s["total_stamp_tax"] - 11.0) < 1e-9
    assert abs(s["total_commission"] - rep["cost"]["total_fees"]) > 1.0, (
        "有印花税时佣金合计 ≠ 费用合计——钉子必须断言 total_trading_cost"
    )


def test_report_pnl_is_net_basis_regardless_of_round_trip_source():
    """R23B-F4：`compute_summary` 的 pnl 契约是**净额**；毛额回合不得改变胜率。

    引擎富化回合只有 `pnl = qty×(exit−entry)`（费前），报告回合是 `pnl_net`。
    此前适配器优先 pnl_net、缺失回退 pnl → 同一份 fills 因来源不同得出相反胜率
    （实测 毛 +6.0 判盈 / 净 −9.0 判亏：win_rate 1.0 vs 0.0、profit_factor 999 vs 0）。
    """
    from portfolio.reports import build_report

    nav = [{"date": f"2024-03-{d:02d}", "equity": 100_000.0} for d in range(1, 15)]
    fills = [
        {"symbol": "A.SS", "side": "buy", "fill_date": "2024-03-04", "fill_price": 10.0,
         "quantity": 1000, "commission": 8.0, "stamp_tax": 0.0, "fee_total": 8.0},
        {"symbol": "A.SS", "side": "sell", "fill_date": "2024-03-11", "fill_price": 10.006,
         "quantity": 1000, "commission": 8.0, "stamp_tax": 0.0, "fee_total": 8.0},
    ]
    gross_round_trips = [                     # 引擎富化形态：只有毛额
        {"symbol": "A.SS", "entry_date": "2024-03-04", "exit_date": "2024-03-11",
         "pnl": 6.0},
    ]
    rep = build_report(None, run_id="R23B-F4", nav_rows=nav, fills=fills,
                       unfilled=[], gate_log=[], round_trips=gross_round_trips)
    s = rep["summary"]
    # 毛 6 − 两笔费用 16 = −10 → 亏损（毛额口径会判成盈利）
    net = 6.0 - 16.0
    assert net < 0
    assert abs(s["profit_factor"] - 0.0) < 1e-12, (
        f"毛额口径会把亏损回合判成盈利（profit_factor={s['profit_factor']}，pin 应为 0）"
    )
    assert s["win_rate"] == 0.0, f"胜率必须按净额（实测 {s['win_rate']}）"
    assert s["avg_loss"] > 9.0, s["avg_loss"]     # |净亏| ≈ 10


def test_cli_run_branch_has_failure_fallback():
    """R23B-F6：CLI 的 run/`--run` 分支必须兜底异常（竞态时输出结构化信封）。"""
    src = (ROOT / "scripts" / "research_cli.py").read_text(encoding="utf-8")
    block = src[src.index('if args.cmd == "run":'):src.index('if args.cmd == "ledger":')]
    assert "except Exception as exc:" in block, "run 分支必须有异常兜底"
    assert '"run_status": "failed"' in block, "失败必须给结构化信封"
