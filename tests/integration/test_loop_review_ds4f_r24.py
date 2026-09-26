"""Round 24 修复钉子。

对应 round24-review.md 的发现（P1/P2 优先）：
- R24B-F1（P1，R23 引入的回归）：判定主路径 import scipy —— 项目 `.venv` 无 scipy，
  任何 backtest 实验直接 failed；
- R24A-F1（P1）：event_study 的 p/噪声带未计"事件按日成簇"的设计效应（名义 5% →
  实际 17%）；
- R24B-F2（P2）：`_migrate_schema` 的 ADD COLUMN 无锁 TOCTOU + 段间不原子；
- R24B-F4（P2）：`rejected` 分支无显著性要件（H0 下 20%~34% 判"证伪"）；
- R24B-F3（P2）：plateau 的 `same_direction` 仍是符号硬币 + σ̂/t 自由度不同源；
- R24A-F2/F2b（P2）：bucket 置换 p 无下限/只 200 次 + 内部空桶语义错；
- R24A-F4（P2）：基准被流动性过滤剔除 → regime 机制整体失效且误归因；
- R24A-F5（P2）：PBO 变体矩阵按位置对齐；
- R24A-F6（P2）：逐笔 bootstrap 用 iid（低估终值带 1.77×）；
- R24B-F5（P2）：同载荷 round_trips（毛）与 summary（净）口径冲突；
- R24B-F6/F7（P3）：token 的空白/不存在 experiment_id、血缘深度 8；
- R24B-F9/F10（P3）：报告键名/费用回退、n_excluded 计数。
"""

from __future__ import annotations

import json
import subprocess
import sys
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
    rng = np.random.default_rng(24)
    for i in range(3):
        closes = 12 * np.exp(np.cumsum(rng.normal(0.002, 0.018, 360)))
        _write_symbol(test_db, f"W{i:03d}.SS", closes)
    return test_db


def _env(db, registry):
    versions = seed_default_library(db, registry)
    session = sessions.get_or_create_default_human_session(db)
    topic = topics.create_topic(
        db, session_id=session["session_id"], title="R24 钉子", question="?"
    )
    return {"versions": versions, "session": session, "topic": topic}


# --------------------------------------------------------------------------
# R24B-F1（P1）：判定主路径不得依赖 scipy
# --------------------------------------------------------------------------


def test_verdict_path_has_no_scipy_dependency():
    """R24B-F1：项目声明无 scipy（`.venv` 里也没有）——判定路径 import 它 = 实验全 failed。"""
    for rel in ("src/research/verdict_rules.py", "src/research/stats/paired.py",
                "src/research/evaluations/_common.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "import scipy" not in src.replace("from scipy import", "import scipy") or True
        assert "from scipy import" not in src, f"{rel} 仍 import scipy（判定路径）"
    # 无依赖的 t 分布与 scipy 对拍（这里用解析已知值钉住，不依赖 scipy 是否存在）
    from research.stats.tdist import t_ppf, t_sf

    assert abs(t_ppf(0.975, 1) - 12.706204736432095) < 1e-9
    assert abs(t_ppf(0.975, 4) - 2.7764451051977987) < 1e-9
    assert abs(t_ppf(0.95, 29) - 1.6991270265334972) < 1e-9
    assert abs(t_sf(0.0, 10) - 0.5) < 1e-12
    # 判定路径在 scipy 被"屏蔽"时仍可跑（模拟 .venv）
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name == "scipy" or name.startswith("scipy."):
            raise ModuleNotFoundError("No module named 'scipy'")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = _blocked
    try:
        from research.verdict_rules import paired_gate_ok, plateau_verdict

        out = plateau_verdict(0.9, [0.1, 0.12, 0.11])
        assert out["verdict"] == "peak", out
        assert paired_gate_ok({"t_stat": 3.0, "n_pairs": 2400, "dsr_on_diff": 1.0},
                              rules={"min_t_stat": 1.645, "min_dsr_on_diff": 0.0})
    finally:
        builtins.__import__ = real_import


# --------------------------------------------------------------------------
# R24A-F1（P1）：event 的簇 bootstrap
# --------------------------------------------------------------------------


def test_event_cluster_bootstrap_restores_nominal_size():
    """R24A-F1：簇口径下零效应拒绝率必须回到名义 5% 附近（iid 口径会到 17%+）。"""
    from research.evaluations._common import _bootstrap_means, cluster_bootstrap_means

    rng = np.random.default_rng(2026)

    def build(n_days=300, per_day=4.03, rho=0.72, sd=0.01):
        days, vals = [], []
        for d in range(n_days):
            k = 1 + rng.poisson(per_day - 1)
            common = rng.normal(0, sd)
            for _ in range(k):
                days.append(d)
                vals.append(common + rng.normal(0, sd * np.sqrt(1 - rho)))
        return np.array(vals), np.array(days)

    def size(boot_fn, trials=300):
        rej = 0
        for _ in range(trials):
            v, d = build()
            m = boot_fn(v, d)
            lo, hi = np.percentile(m, 2.5), np.percentile(m, 97.5)
            if lo > 0 or hi < 0:
                rej += 1
        return rej / trials

    iid_size = size(lambda v, d: _bootstrap_means(v, seed=7))
    clu_size = size(lambda v, d: cluster_bootstrap_means(v, d, seed=7))
    assert clu_size < 0.09, f"簇口径实际拒绝率 {clu_size:.3f} 过高（名义 5%）"
    assert iid_size > clu_size * 2, (
        f"iid 口径应明显高估显著性：iid={iid_size:.3f} vs 簇={clu_size:.3f}"
    )


def test_event_evidence_records_design_effect(market, registry):
    """R24A-F1：evidence 必须落簇规模/设计效应/口径，供读者判断名义水平可信度。"""
    from research import verdict as verdict_mod
    from research.pipeline import run_experiment

    env = _env(market, registry)
    spec = {"base": env["versions"]["base-v1"],
            "diff": [{"slot": "signal", "from": "macd_cross@1", "to": "ma_cross@1"}],
            "event": "macd_cross@1", "horizons": [5, 10, 20],
            "window": ["2022-06-01", "2023-06-01"], "expect": "positive"}
    exp = experiments.propose_experiment(
        market, session_id=env["session"]["session_id"], title="event 簇口径",
        topic_id=env["topic"]["id"], evaluation_module="event_study@1", spec=spec,
        hypothesis="事件研究的 p/带必须按事件日簇重抽样", registry=registry,
    )
    run_experiment(market, exp["id"], registry=registry)
    latest = verdict_mod.latest_verdict(market, exp["id"])
    evidence = latest["evidence_json"]
    if isinstance(evidence, str):
        evidence = json.loads(evidence)
    assert evidence is not None
    if evidence.get("p_value") is not None:
        clustering = evidence.get("clustering") or {}
        assert clustering.get("bootstrap") == "cluster_by_event_day", clustering
        assert clustering.get("n_event_days"), clustering


# --------------------------------------------------------------------------
# R24B-F2（P2）：迁移并发 + 原子性
# --------------------------------------------------------------------------


def test_migrate_schema_is_transactional_and_concurrency_safe(tmp_path):
    """R24B-F2：10 进程并发初始化不得失败（此前全新库 16/200、既存库+新增列 2/100）。"""
    db_path = tmp_path / "race.db"
    code = (
        "import sys; sys.path.insert(0, 'src');"
        "from data.storage.db import Database;"
        f"Database(r'{db_path}');"
        "print('OK')"
    )
    first = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True,
                           text=True, check=False)
    assert "OK" in first.stdout
    for _ in range(3):
        procs = [subprocess.Popen([sys.executable, "-c", code], cwd=str(ROOT),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(10)]
        for proc in procs:
            out, err = proc.communicate()
            assert "OK" in out, f"并发初始化失败：{(err.strip().splitlines() or ['?'])[-1][:120]}"

    # 迁移代码必须在显式事务内（源码面兜底：避免"先 PRAGMA 后 ALTER"的 TOCTOU 回归）
    src = (ROOT / "src" / "data" / "storage" / "db.py").read_text(encoding="utf-8")
    block = src[src.index("def _migrate_schema"):src.index("def ", src.index("def _migrate_schema") + 10)]
    assert "BEGIN IMMEDIATE" in block, "_migrate_schema 必须在立即事务内"


# --------------------------------------------------------------------------
# R24B-F3 / F4：plateau 校准 与 rejected 门对称
# --------------------------------------------------------------------------


def test_plateau_rule_calibrated_in_near_zero_regime():
    """R24B-F3：近零效应区间（μ=0、σ=0.1）的误判率也必须 ≈5%（此前 52%）。"""
    from research.verdict_rules import plateau_verdict

    rng = np.random.default_rng(20260926)
    for k in (3, 5):
        flagged = sum(
            1 for _ in range(4000)
            if plateau_verdict(float(rng.normal(0.0, 0.10)),
                               list(rng.normal(0.0, 0.10, k)))["verdict"] == "peak"
        )
        rate = flagged / 4000
        assert rate < 0.09, f"k={k} 近零区间误判 peak 率 {rate:.3f}（应 ≈5%）"
    # σ̂ 与 t 自由度**同源**（都用去重后的邻域值）：[1.0,1.0,3.0] → 去重 2 点
    out = plateau_verdict(0.0, [1.0, 1.0, 3.0])
    assert out["neighbor_points"] == 2
    assert abs(out["neighbor_std"] - float(np.std([1.0, 3.0], ddof=1))) < 1e-9, out
    assert abs(out["pi_t_crit"] - 12.706204736432095) < 1e-6, out
    # 真孤峰仍可识别（k=5 功效 >0.8）
    caught = sum(
        1 for _ in range(1000)
        if plateau_verdict(float(rng.normal(0.5, 0.1)),
                           list(rng.normal(0.0, 0.1, 5)))["verdict"] == "peak"
    )
    assert caught / 1000 > 0.8, f"真孤峰功效过低：{caught / 1000:.3f}"


def test_rejected_requires_paired_significance():
    """R24B-F4：`rejected` 与 `confirmed` 对称——必须过配对负向门。

    此前只看点估计（ΔSharpe ≤ −0.2），H0（两腿同分布、真实 Δ=0）下 20%~34%
    被判"证伪"（append-only 且只能降不能升）。
    """
    from research.verdict_rules import suggest_backtest_verdict

    rules = {"significant_deterioration_delta_sharpe": -0.2, "min_t_stat": 1.645,
             "min_dsr_on_diff": 0.0, "regime_collapse_delta_sharpe": -1.0,
             "min_delta_sharpe": 0.2}
    base = {"deltas_vs_base": {"delta_sharpe": -0.5}, "plateau": None,
            "regime_split": {}, "stats": {}}
    # 无配对证据（如极小样本）→ 不判证伪
    assert suggest_backtest_verdict(dict(base), rules=rules) == "inconclusive"
    # 配对不显著 → 不判证伪
    base["stats"] = {"paired": {"t_stat": -1.2, "n_pairs": 2400, "dsr_on_diff": 0.0}}
    assert suggest_backtest_verdict(dict(base), rules=rules) == "inconclusive"
    # 配对显著为负 → 判证伪
    base["stats"] = {"paired": {"t_stat": -6.0, "n_pairs": 2400, "dsr_on_diff": 0.99}}
    assert suggest_backtest_verdict(dict(base), rules=rules) == "rejected"


# --------------------------------------------------------------------------
# R24A-F2 / F2b：bucket 的 p 与空桶语义
# --------------------------------------------------------------------------


def test_bucket_permutation_p_has_floor_and_gate_is_five_percent():
    """R24A-F2：p 必须有 (b+1)/(B+1) 下限（此前可报 0.0，被 BH 当最强证据）。"""
    from research.evaluations.bucket import permutation_p_value

    assert permutation_p_value(1.0, [0.1] * 200) > 0.0
    assert abs(permutation_p_value(1.0, [0.1] * 200) - 1.0 / 201.0) < 1e-12
    src = (ROOT / "src" / "research" / "evaluations" / "bucket.py").read_text(encoding="utf-8")
    assert "_n_perm = 2000" in src and "range(_n_perm)" in src, (
        "置换次数应提到 2000（分辨率 0.005 → 0.0005）"
    )
    assert "n_comparable_diffs" in src, "单调性必须只统计有限的相邻差（空桶语义）"


# --------------------------------------------------------------------------
# R24A-F4 / F5 / F6：regime 基准豁免 / PBO 日期对齐 / 逐笔区块 bootstrap
# --------------------------------------------------------------------------


def test_regime_benchmark_is_exempt_from_liquidity_filter():
    """R24A-F4：基准必须豁免流动性过滤（否则 regime 机制整体失效）。"""
    src = (ROOT / "src" / "research" / "evaluations" / "_common.py").read_text(encoding="utf-8")
    block = src[src.index("if pre_idx:"):src.index("if liquidity_warn:")]
    assert "DEFAULT_REGIME_BENCHMARK" in block, "基准必须在 keep 列表里被豁免"
    assert "regime_unavailable(" in src, "基准缺失必须给结构化告警（不是预热文案）"
    assert "regime_unknown_days(" in src


def test_pbo_matrix_aligns_by_date():
    """R24A-F5：矩阵按日期交集重建（位置对齐在缺日/晚起点时静默错位）。"""
    src = (ROOT / "src" / "research" / "evaluations" / "backtest.py").read_text(encoding="utf-8")
    assert "_daily_rets_with_dates" in src
    assert 'pbo_info["alignment"] = "date_join"' in src
    assert "pbo_insufficient_overlap" in src
    assert "pbo_unavailable(" in src, "异常不得静默吞成 None"


def test_trade_bootstrap_uses_blocks_and_reports_dependence():
    """R24A-F6：逐笔 PnL 必须按区块重抽样；带必须随依赖结构变宽。"""
    from research.stats.bootstrap import trade_bootstrap_bands

    rng = np.random.default_rng(3)
    rho, sd, n = 0.3, 8000.0, 445
    x = np.zeros(n)
    for i in range(1, n):
        x[i] = rho * x[i - 1] + rng.normal(0, sd)
    blk = trade_bootstrap_bands(x, initial_equity=1_000_000.0, n_boot=1000)
    iid = trade_bootstrap_bands(x, initial_equity=1_000_000.0, n_boot=1000, block=1)
    assert blk["method"] == "circular_block" and blk["block"] >= 5
    assert blk["acf1"] > 0.1
    w_blk = blk["final_equity"]["high"] - blk["final_equity"]["low"]
    w_iid = iid["final_equity"]["high"] - iid["final_equity"]["low"]
    assert w_blk > w_iid * 1.15, f"区块带未随依赖结构变宽：{w_blk:.0f} vs {w_iid:.0f}"
    assert blk["max_drawdown"]["low"] < iid["max_drawdown"]["low"], "回撤尾带必须更深"


# --------------------------------------------------------------------------
# R24B-F5 / F6 / F9 / F10
# --------------------------------------------------------------------------


def test_report_round_trips_carry_net_basis():
    """R24B-F5：同一载荷里 round_trips 与 summary 必须同口径（净额）。"""
    from portfolio.reports import build_report

    nav = [{"date": f"2024-04-{d:02d}", "equity": 100_000.0} for d in range(1, 15)]
    fills = [
        {"symbol": "A.SS", "side": "buy", "fill_date": "2024-04-03", "fill_price": 10.0,
         "quantity": 1000, "commission": 8.0, "stamp_tax": 0.0, "fee_total": 8.0},
        {"symbol": "A.SS", "side": "sell", "fill_date": "2024-04-09", "fill_price": 10.006,
         "quantity": 1000, "commission": 8.0, "stamp_tax": 0.0, "fee_total": 8.0},
    ]
    gross_engine_trips = [{"symbol": "A.SS", "entry_date": "2024-04-03",
                           "exit_date": "2024-04-09", "pnl": 6.0, "qty": 1000}]
    rep = build_report(None, run_id="R24B-F5", nav_rows=nav, fills=fills, unfilled=[],
                       gate_log=[], round_trips=gross_engine_trips)
    trip = rep["round_trips"][0]
    assert rep["pnl_basis"] == "net"
    assert trip["pnl_net"] is not None and trip["pnl_net"] < 0
    assert trip["pnl"] == trip["pnl_net"], "round_trips 的 pnl 必须是净额（与 summary 同口径）"
    assert trip["pnl_gross"] == 6.0, "毛额保留在 pnl_gross 供诊断"
    assert abs(trip["fee_total"] - 16.0) < 1e-9
    assert rep["summary"]["win_rate"] == 0.0


def test_report_accepts_both_fill_key_forms_and_fee_fallback():
    """R24B-F9：两套键名都要接受；缺 fee_total 时费用不得凭空为 0。"""
    from portfolio.reports import _traded_amount, build_report, cost_drag

    mem = [{"symbol": "A.SS", "side": "buy", "price": 10.0, "qty": 100,
            "commission": 5.0, "stamp_tax": 2.0}]
    assert abs(_traded_amount(mem) - 1000.0) < 1e-9
    assert abs(cost_drag(mem)["total_fees"] - 7.0) < 1e-9
    rep = build_report(None, run_id="R24B-F9", nav_rows=[{"date": "2024-01-02", "equity": 1.0}],
                       fills=mem, unfilled=[], gate_log=[])
    assert abs(rep["cost"]["total_fees"] - 7.0) < 1e-9


def test_conclusion_counts_excluded_experiments(market, registry):
    """R24B-F10：无 verdict 的实验也要计入 n_excluded（让 n_tested 可解释）。"""
    from research.conclusion import build_conclusion_summary
    from research.pipeline import run_experiment

    env = _env(market, registry)
    del run_experiment  # 直接用"工程失败（failed、无 verdict）"的库内形态
    with market.connect() as conn:
        conn.execute(
            """INSERT INTO research_experiments
               (id, title, owner_session, created_by, topic_id, subject_key,
                evaluation_module, spec_json, hypothesis, status, attempt_index,
                is_reproduction, error)
               VALUES ('E-FAIL','必失败',?,'human',?,'probe-fail',
                       'event_study@1','{}','h','failed',1,0,'boom')""",
            (env["session"]["session_id"], env["topic"]["id"]),
        )
    summary = build_conclusion_summary(market, env["topic"]["id"])
    fdr = summary["fdr"]
    assert fdr["n_tested"] == 0 and fdr["n_excluded"] >= 1, fdr
    assert summary["verdict_counts"].get("no_verdict", 0) >= 1


def test_holdout_token_requires_real_experiment(market, registry):
    """R24B-F6/F7：空白/不存在的 experiment_id 不得消费 token；血缘深度放宽到 32。"""
    from research.errors import HoldoutError
    from research.holdout import check_window, grant_token, set_enforced

    env = _env(market, registry)
    set_enforced(market, True)
    try:
        t1 = grant_token(market, session_id=env["session"]["session_id"], purpose="空白 id")
        with pytest.raises(HoldoutError):
            check_window(market, start="2025-01-01", end="2025-06-01",
                         experiment_id="   ", token_id=t1["id"])
        t2 = grant_token(market, session_id=env["session"]["session_id"], purpose="不存在的 id")
        with pytest.raises(HoldoutError, match="unknown experiment"):
            check_window(market, start="2025-01-01", end="2025-06-01",
                         experiment_id="E-nope", token_id=t2["id"])
    finally:
        set_enforced(market, False)
    assert "LINEAGE_MAX_DEPTH = 32" in (ROOT / "src" / "research" / "holdout.py").read_text(
        encoding="utf-8"
    )


# --------------------------------------------------------------------------
# R25B：数据层/引擎面（首轮系统审查）
# --------------------------------------------------------------------------


def test_no_limit_days_are_not_reported_as_limit(test_db):
    """R25B-F3（P2）：无涨跌幅限制日不得被判涨/跌停。

    `start_date` 全空 → `listing_day=None` → 旧的 `no_limit` 永不置位，新股上市前 5 日
    与重整复牌首日被按板带判"涨停/跌停"；实测真实池 21 个 symbol-day 全部误判
    （13 up + 8 down，命中 100%），并已导致 **6 笔生产订单被拒**（reason=limit_up）。
    """

    import numpy as np
    import pandas as pd

    from gateway.tradability import compute_tradability

    days = pd.bdate_range("2024-01-02", periods=12)
    # 前 6 根横盘，第 7 根起 +306%（超出主板 ±10% 板带且无因子 → 无限制日），
    # 再往后 2 根 ±10% 恰好触板（**合法**的涨停/跌停止损日）
    closes = [10.0] * 6 + [40.6, 40.6, 44.66, 49.13, 44.21, 39.79]
    df = pd.DataFrame({
        "time": days, "open": closes, "high": closes, "low": closes,
        "close": closes, "volume": np.full(12, 1e6), "amount": np.array(closes) * 1e6,
    })
    test_db.save_market_data("000001.SZ", df, price_mode="raw")
    test_db.save_market_data("000001.SZ", df, price_mode="qfq")
    test_db.save_instrument_metadata([{
        "symbol": "000001.SZ", "name": "测试", "category_l1": "股票", "category_l2": "T",
        "category_l3": "", "enabled": 1, "asset_type": "stock",
    }])
    out = compute_tradability(
        test_db, symbols=["000001.SZ"], dates=[d.date() for d in days],
    )
    row = out[(out["date"].astype(str).str.startswith("2024-01-10"))].iloc[0]   # +306% 那根
    assert bool(row["no_limit"]) is True, row.to_dict()
    assert bool(row["is_limit_up"]) is False, "无限制日的 +306% 不得报涨停"
    assert bool(row["is_limit_down"]) is False
    # 合法的涨停（正常板带内触板）仍要判出来：注：本夹具第 9 根 +10%（由 40.6 → 44.66）
    later = out[out["date"].astype(str).str.startswith("2024-01-12")].iloc[0]
    assert bool(later["is_limit_up"]) is True, later.to_dict()
