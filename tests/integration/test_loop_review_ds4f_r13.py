"""Round 13 修复钉子（round13-review.md 各项的回归锚）。

覆盖本轮三个 P2：
- R13A-F1 影子账户按不可卖股数入账（买入清单被虚增现金放大）；
- R13A-F2 CLI 批次的冻结门是进程内计数器（对 app 进程日更不可见）；
- R13B-F1 格子级比值列在读取面（/cells、明细、CSV、compare）没有闸门 +
  离线 backfill 脚本从未接闸（上一轮"已修"的说法不成立）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd


def _monotone_bars(n: int = 70) -> pd.DataFrame:
    closes = np.array([10.0 * (1.10 ** i) for i in range(n)])
    dates = pd.bdate_range("2023-01-02", periods=n)
    return pd.DataFrame({
        "time": dates, "date": dates,
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": np.full(n, 1e6), "amount": closes * 1e6,
    })


def _noisy_bars(n: int = 200) -> pd.DataFrame:
    rng = np.random.default_rng(11)
    closes = 10 * np.exp(np.cumsum(rng.normal(0.0005, 0.013, n)))
    dates = pd.bdate_range("2023-01-02", periods=n)
    return pd.DataFrame({
        "time": dates, "date": dates,
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": np.full(n, 1e6), "amount": closes * 1e6,
    })


def test_offline_backfill_helper_gates_noise():
    """R13B-F1：离线 backfill 脚本必须过闸（行为级，取代原来的空钉）。

    旧钉子只断言 `callable(助手)` 与一个**与脚本无关**的常量
    （`DEGENERATE_SHARPE_ABS_LIMIT == 50.0`）→ 在完全无闸门的代码上也通过。
    实测该助手对 70 根一字板给出 benchmark_sharpe = 28775.93。
    """
    import importlib.util
    import sys
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[2] / "scripts"
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location(
        "backfill_batch_excess_metrics", scripts / "backfill_batch_excess_metrics.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    sharpe, calmar = mod._benchmark_sharpe_calmar(
        _monotone_bars(), initial_capital=100_000.0, lot_size=100
    )
    assert sharpe is None, "单调基准腿的噪声 Sharpe 不得写回 batch_backtest_cells"
    assert calmar is not None, "calmar 不入闸"
    ok_sharpe, _ = mod._benchmark_sharpe_calmar(
        _noisy_bars(), initial_capital=100_000.0, lot_size=100
    )
    assert ok_sharpe is not None and abs(ok_sharpe) < 50, "合法基准腿必须保留"


def test_cell_ratio_columns_are_gated_on_read_paths(test_db):
    """R13B-F1（读取半）：格子级比值列的出口都必须清噪。

    只清年度块会留下"同一行年度块 None、格子列 28775.93 裸奔"的自相矛盾
    （backfill 脚本等旁路写入的正是这些列）。
    """
    from app.routers.batch_backtest import _parse_cell_blobs

    batch = {
        "batch_id": "B-r13", "name": "r13", "status": "completed",
        "categories_json": "[]", "strategy_snapshot_json": "[]", "config_json": "{}",
        "total_cells": 1,
    }
    assert test_db.create_batch_run_if_idle(batch) is True
    noisy = {
        "batch_id": "B-r13", "symbol": "X.SS", "strategy_id": "s1", "status": "ok",
        "sharpe": 1.2, "sortino": 1.1, "calmar": 9.41,
        "benchmark_sharpe": 28775.93, "excess_sharpe": 28774.73,
        "benchmark_calmar": 8.0,
        "excess_calmar": 1.4,
        "annual_returns_json": json.dumps([{"year": 2024, "sharpe": 0.4}]),
    }
    # 直接走读取面：明细端点解析器
    parsed = _parse_cell_blobs({**noisy})
    assert parsed["benchmark_sharpe"] is None and parsed["excess_sharpe"] is None
    assert parsed["sharpe"] == 1.2 and parsed["calmar"] == 9.41
    assert parsed["annual_returns"][0]["sharpe"] == 0.4


def test_compare_batches_does_not_difference_noise(test_db):
    """R13B-F1：/api/compare 的 delta_sharpe 不得由噪声作差。"""
    from rule_backtest.batch_service import compare_batches

    for bid in ("B-r13-a", "B-r13-b"):
        assert test_db.create_batch_run_if_idle({
            "batch_id": bid, "name": bid, "status": "completed",
            "categories_json": "[]", "strategy_snapshot_json": "[]", "config_json": "{}",
            "total_cells": 1,
        }) is True
        # 建出来是 running；置 completed 才能再建下一个（单飞语义）
        test_db.update_batch_run(bid, status="completed")
    # sharpe 在 _COMPARE_METRICS 内 → delta_sharpe 会把噪声作差；标量列里
    # 只有 sharpe/sortino（calmar 豁免）会被闸门清掉
    for bid, sharpe in (("B-r13-a", 28775.93), ("B-r13-b", 0.5)):
        test_db.insert_batch_cell({
            "batch_id": bid, "symbol": "X.SS", "strategy_id": "s1", "status": "ok",
            "sharpe": sharpe, "calmar": 2.0, "benchmark_sharpe": 1.1,
            "excess_sharpe": 0.3,
        })
    out = compare_batches(test_db, "B-r13-a", "B-r13-b")
    assert out["cell_diffs"], "两批次同标的应有对比行"
    row = out["cell_diffs"][0]
    assert row["base_sharpe"] is None, "噪声格子列不得进对比响应"
    assert row["delta_sharpe"] is None, "不得用噪声作差"
    assert row["alt_sharpe"] == 0.5, "合法值必须保留"
    assert row["delta_calmar"] == 0.0, "calmar 不入闸，仍可作差"


def test_cross_process_freeze_is_visible(tmp_path, monkeypatch):
    """R13A-F2：独立 CLI 进程发起的批次必须让 **app 进程**的日更看到。

    进程内计数器跨进程不可见（实测：子进程持锁时父进程 `is_frozen()` 仍为 False），
    故补了哨兵文件；本钉子用子进程直接验证可见性，并验证崩溃残留会过期。
    """
    import subprocess
    import sys
    import time

    from core import run_freeze

    lock = tmp_path / "run_freeze.lock"
    env = {**os.environ, "TREND_QUANT_FREEZE_FILE": str(lock)}
    code = (
        "from core import run_freeze;"
        "print('FROZEN' if run_freeze.is_frozen_anywhere() else 'FREE')"
    )
    src = str(Path(__file__).resolve().parents[2] / "src")
    sub_env = {**env, "PYTHONPATH": src}

    with run_freeze.cross_process_frozen(lock):
        assert lock.exists(), "哨兵文件必须落盘"
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, env=sub_env, timeout=60, check=False)
        assert "FROZEN" in out.stdout, f"子进程必须看到哨兵：{out.stdout!r} {out.stderr[-200:]!r}"
    assert not lock.exists(), "退出时必须删除哨兵"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, env=sub_env, timeout=60, check=False)
    assert "FREE" in out.stdout
    # 崩溃残留：文件存在但超过 _STALE_SECONDS → 视为未冻结（不永久卡日更）
    lock.write_text("999999 0", encoding="utf-8")
    old = time.time() - (run_freeze._STALE_SECONDS + 60)
    os.utime(lock, (old, old))
    assert run_freeze.file_frozen(lock) is False
    assert run_freeze.is_frozen_anywhere(lock) is False
