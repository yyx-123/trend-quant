"""Round 22 修复钉子（续）：R22B-F2 的 CLI 面（写到独立文件，避免单文件过长）。

R22B-F2（P2）：`propose-experiment --run` / `rerun --run` 跑到失败（如缺 holdout
token）时，CLI 此前**退出码恒 0**、且只打印 `{"status":…, "suggested":…}`
（失败时两项皆 null）——自动化调用方会把"失败"读成"成功"。
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
from research import sessions, topics

pytestmark = pytest.mark.integration


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
    rng = np.random.default_rng(17)
    for i in range(3):
        closes = 12 * np.exp(np.cumsum(rng.normal(0.002, 0.018, 360)))
        _write_symbol(test_db, f"W{i:03d}.SS", closes)
    return test_db


def _env(db, registry):
    versions = seed_default_library(db, registry)
    session = sessions.get_or_create_default_human_session(db)
    topic = topics.create_topic(
        db, session_id=session["session_id"], title="CLI 运行信封", question="失败退出码"
    )
    return {"versions": versions, "session": session, "topic": topic}


def _holdout_spec(base_ref):
    """窗口落到 holdout 段（默认 2025-01-01 起）→ 无 token 必失败。"""
    return {
        "base": base_ref,
        "diff": [{"slot": "position_risk", "from": "hard_stop@1", "to": "hard_stop@1",
                  "params": {"atr_mul": 2.0}}],
        "window": ["2022-06-01", "2025-06-01"],
    }


def _cli(root: Path, *args: str, timeout: int = 600):
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "research_cli.py"), *args],
        capture_output=True, text=True, cwd=str(root), timeout=timeout, check=False,
    )


def test_cli_run_failure_exit_code_is_nonzero(market, registry):
    """R22B-F2：`--run` 跑到失败 → 退出码非 0 + 信封里有 `run_status` 与原因。"""
    env = _env(market, registry)
    root = Path(__file__).resolve().parents[2]
    proc = _cli(
        root, "--db", str(market.db_path), "propose-experiment",
        "--topic", env["topic"]["id"], "--eval", "portfolio_backtest@1",
        "--spec", json.dumps(_holdout_spec(env["versions"]["base-v1"])),
        "--hypothesis", "跑到失败时退出码必须非 0（R22B-F2 钉子）", "--run",
    )
    assert proc.returncode == 1, f"失败运行必须给非 0 退出码：\n{proc.stdout}\n{proc.stderr}"
    last = json.loads(proc.stdout.strip().splitlines()[-1])
    assert last["run_status"] == "failed", last
    assert "holdout" in (last["error"] or ""), f"失败原因必须透出：{last}"


def test_cli_rerun_failure_exit_code_is_nonzero(market, registry):
    """R22B-F2（复现面）：`rerun --run` 失败同样必须非 0 且带原因。"""
    env = _env(market, registry)
    root = Path(__file__).resolve().parents[2]
    first = _cli(
        root, "--db", str(market.db_path), "propose-experiment",
        "--topic", env["topic"]["id"], "--eval", "portfolio_backtest@1",
        "--spec", json.dumps(_holdout_spec(env["versions"]["base-v1"])),
        "--hypothesis", "复现失败同样要给非 0 退出码（R22B-F2 钉子）", "--run",
    )
    exp_id = json.loads(first.stdout.strip().splitlines()[0])["experiment_id"]
    proc = _cli(root, "--db", str(market.db_path), "rerun", exp_id, "--run")
    assert proc.returncode == 1, f"复现失败必须非 0：\n{proc.stdout}\n{proc.stderr}"
    last = json.loads(proc.stdout.strip().splitlines()[-1])
    assert last["run_status"] == "failed" and ("holdout" in (last["run_error"] or ""))
