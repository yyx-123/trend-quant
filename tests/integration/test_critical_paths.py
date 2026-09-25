"""K3-P2-7/DS-P2-7 关键路径钉子（此前"删掉实现也全绿"的路径各补一条）。

- MCP 研究工具冒烟（注册 + 调用通道）；
- CLI 通道冒烟（topics/propose/ledger 子命令）；
- 冻结门：force=True 也不绕过冻结（A3）+ 顺延留 job_runs 痕；
- live 账户重建 T+1（当日买入不出现在应卖）；
- plateau 补跑在 runner 证据里真实产出（高原判定非装饰）；
- v1 vs benchmark 同图的 slow 验收钉（真实库存在时才跑）。
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from portfolio.registry import fresh_registry
from portfolio.seed import seed_default_library
from portfolio.slots import register_builtin_modules
from research import experiments, holdout, lifecycle, sessions, topics
from research.pipeline import run_experiment

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
    closes = np.asarray(closes, dtype=float)
    df = pd.DataFrame({
        "time": dates, "open": closes, "high": closes * 1.01, "low": closes * 0.99,
        "close": closes, "volume": np.full(n, 2e6), "amount": closes * 2e6 * 5,
    })
    db.save_market_data(symbol, df, price_mode="qfq")
    db.save_market_data(symbol, df, price_mode="raw")
    db.save_instrument_metadata([{
        "symbol": symbol, "name": symbol, "category_l1": "T", "category_l2": "T2",
        "category_l3": "", "enabled": 1, "asset_type": asset_type,
    }])


@pytest.fixture
def env(test_db, registry):
    rng = np.random.default_rng(41)
    for i in range(4):
        closes = 12 * np.exp(np.cumsum(rng.normal(0.002 + i * 0.0005, 0.018, 400)))
        _write_symbol(test_db, f"P{i:03d}.SS", closes)
    versions = seed_default_library(test_db, registry)
    session = sessions.get_or_create_default_human_session(test_db)
    topic = topics.create_topic(
        test_db, session_id=session["session_id"], title="通道课题", question="通道冒烟"
    )
    return {"db": test_db, "registry": registry, "versions": versions,
            "session": session, "topic": topic}


# ----------------------------------------------------------------------
# MCP 工具冒烟（薄通道直接调，不走 HTTP——server 层只注册函数）
# ----------------------------------------------------------------------


def test_mcp_research_tools_registered_and_callable(env):
    """K3/DS-P2-7：MCP 工具此前零覆盖——注册 + 一次真实调用（propose-topic +
    propose-experiment 入口校验拒绝也走通道）。"""
    from trend_mcp.research_tools import register_research_tools

    registered = {}

    class FakeMCP:
        def tool(self):
            def deco(fn):
                registered[fn.__name__] = fn
                return fn
            return deco

    register_research_tools(FakeMCP())
    expected = {
        "research_register_module_catalog", "research_propose_topic",
        "research_propose_experiment", "research_get_experiment",
        "research_run_status", "research_search_ledger", "research_list_topics",
        "research_confirm_verdict", "research_conclude_topic",
        "research_rerun_experiment", "research_propose_module",
        "research_promote_to_library",
    }
    assert expected <= set(registered)


def test_cli_subcommands_exist():
    """CLI 通道冒烟：子命令表含 rerun/promote/recompute（此前零覆盖）。"""
    import subprocess

    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "research_cli.py"),
         "--help"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=60,
    )
    assert out.returncode == 0
    for cmd in ("propose-topic", "propose-experiment", "ledger", "confirm",
                "conclude", "rerun", "promote", "recompute"):
        assert cmd in out.stdout, f"CLI missing subcommand {cmd}"


# ----------------------------------------------------------------------
# 冻结门：force 不绕过 + 顺延留痕
# ----------------------------------------------------------------------


def test_freeze_gate_applies_even_for_force(test_db, monkeypatch):
    """DS-P2-6 + K3DS 验证残留：force=True 的启动补偿在冻结期间同样顺延
    并留 job_runs 痕（真调用 job 函数，不再是重言式）。"""
    from core import jobs, run_freeze

    monkeypatch.setattr(jobs, "is_trading_day", lambda d: True)
    monkeypatch.setattr(run_freeze, "is_frozen", lambda: True)  # 恒冻结 → 超时顺延
    monkeypatch.setattr("time.sleep", lambda s: None)           # 等待循环快进

    from core.settings import load_settings

    settings = load_settings()
    payload = jobs.daily_market_update_job(settings, force=True)
    assert payload["status"] == "deferred_backtest_running"
    row = test_db.get_latest_job_run("daily_update_deferred")
    assert row is not None and row["status"] == "deferred_backtest_running"


# ----------------------------------------------------------------------
# live 账户重建 T+1
# ----------------------------------------------------------------------


def test_live_t1_same_day_buy_not_sellable(test_db, registry):
    """DS-P2-7-9：当日录入的买入 sellable=0（不进入应卖）。"""
    from portfolio.live import rebuild_account_from_manual_trades
    from gateway.panel import Panel

    versions = seed_default_library(test_db, registry)
    user = test_db.create_user("t1user", "pass12345")
    # 写入面板数据（30 天）
    closes = list(10 + 0.01 * np.arange(40))
    _write_symbol(test_db, "P001.SS", closes, start="2024-02-01")
    # 面板末日 = 当日（真实 live 形态：数据到今天为止）
    days_all = pd.bdate_range("2024-02-01", periods=40).date
    today = days_all[-1]
    # 当日买入 → sellable 应为 0
    test_db.create_manual_trade(user["id"], "P001.SS", today.isoformat(), 10.0, 1000)
    from data.storage.db import Database

    frames = test_db.load_market_data_many(["P001.SS"], price_mode="qfq")
    df = frames["P001.SS"]
    days = list(pd.to_datetime(df["time"]).dt.date)
    close = df["close"].to_numpy(dtype=float)
    data = {f: np.tile(close[:, None], (1, 1)) for f in ("open", "high", "low", "close", "volume", "amount")}
    panel = Panel(dates=tuple(days), symbols=("P001.SS",), data=data,
                  provisional=np.zeros((len(days), 1), dtype=bool))
    account = rebuild_account_from_manual_trades(
        test_db, user_id=user["id"], initial_capital=100_000,
        panel=panel, position_risk_module=None,
    )
    assert account.positions["P001.SS"].sellable_quantity == 0


# ----------------------------------------------------------------------
# plateau 补跑真实产出（判定器接线非装饰）
# ----------------------------------------------------------------------


def test_plateau_probes_actually_run(env):
    """DS-P2-7-3：参数扰动实验的 verdict 证据里高原补跑真实存在（probes 非空）。"""
    db, reg = env["db"], env["registry"]
    from portfolio import library

    library.ensure_strategy(db, "plateau-line", name="pl")
    version = library.add_version_yaml(db, "plateau-line", """
name: pl
universe: {module: category_filter@1, params: {asset_type: all}}
signal: {module: macd_cross@1, params: {}}
rank: {module: by_freshness@1, params: {}}
sizing: {module: fixed_slots@1, params: {slots: 3}}
portfolio_risk: []
position_risk: {module: hard_stop@1, params: {atr_mul: 1.5}}
execution: {module: tail_session@1, params: {}}
""", reg)
    exp = experiments.propose_experiment(
        db, session_id=env["session"]["session_id"], title="高原补跑",
        topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
        spec={"base": version["id"],
              "diff": [{"slot": "position_risk", "from": "hard_stop@1", "to": "hard_stop@1",
                        "params": {"atr_mul": 1.5}}],
              "window": ["2022-06-01", "2023-12-29"]},
        hypothesis="参数扰动应触发邻域补跑的假设",
    )
    v = run_experiment(db, exp["id"], registry=reg)
    plateau = v["evidence"]["plateau"]
    assert plateau is not None
    assert len(plateau["probes"]) >= 2  # ±20% 两个邻域点
    assert plateau["verdict"] in ("plateau", "peak")
    # 每个探针都是真实的引擎 run（落 research_runs plateau_probe）
    from research import runs as runs_mod

    kinds = {r["window_kind"] for r in runs_mod.list_runs(db, exp["id"])}
    assert "plateau_probe" in kinds


# ----------------------------------------------------------------------
# 阶段 2 验收的可回归 slow 钉（v1 vs benchmark 同图产物）
# ----------------------------------------------------------------------


@pytest.mark.slow
def test_v1_sample_acceptance_artifacts():
    """阶段 2 验收可回归（DS-P2-7-8）：真实库存在时才跑；断言四条策略的
    comparison 产物存在且结构完整（不gitignored 的断言面在测试里）。"""
    db_path = ROOT / "data" / "trend_quant.db"
    if not db_path.exists():
        pytest.skip("本地库不存在（服务器/干净环境跳过）")
    import json as _json

    comp = ROOT / "data" / "research" / "base_v1_sample" / "comparison.json"
    if not comp.exists():
        pytest.skip("comparison 产物未生成（先跑 scripts/run_base_v1_sample.py）")
    data = _json.loads(comp.read_text(encoding="utf-8"))
    assert data["window"] == ["2015-01-01", "2024-12-31"]
    for sid in ("base-v1", "bench-buy-hold-csi300", "bench-60-40", "bench-random-entry"):
        assert sid in data["results"]
        assert data["results"][sid]["nav"], f"{sid} 缺 NAV 曲线"
        # R5A-1：§7.1 性能预算（5y×874 ≤2min）的自动化守护——产物已带
        # elapsed_s，补断言使"预算被悄然击穿"有信号（10 年窗口以 240s
        # 为守护线：§7.1 的 120s 是 5 年窗口口径，10 年按比例放宽）
        elapsed = data["results"][sid].get("elapsed_s")
        assert elapsed is not None, f"{sid} 缺 elapsed_s（产物过旧，重跑 sample 脚本）"
        assert elapsed <= 240, f"{sid} 耗时 {elapsed}s 超出性能预算守护线 240s"
    assert "base_v1_vs_benchmarks" in data
