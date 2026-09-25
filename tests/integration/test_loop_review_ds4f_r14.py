"""Round 14 修复钉子（round14-review.md 各项的回归锚）。

本轮两条 P2 都是"同源只改一半"的继续：
- R14B-F1 冻结门只覆盖调度面与批次侧，**写入侧**（HTTP 触发的补齐/qfq 物化/指标重建）没闸；
- R14B-F2 `portfolio_live_lists` 无用户维度：跨用户可读 + 两用户同日互相覆盖。

另按 R14A 的自检更正补两条钉子：
- 日更"看哨兵"的接线钉子此前打在空转目标（`jobs.DataService` 仅注解引用）→ 改用真入口；
- 策略腿出口闸门可构造夹具（恒入场 + 单调 bars → 原值 9899.52）。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import date

import numpy as np
import pandas as pd
import pytest


def test_write_side_freeze_guard_blocks_backfill_and_rebuild(test_db):
    """R14B-F1：冻结中 HTTP 触发的补齐/物化/指标重建必须被拒（写入侧守卫）。"""
    from core import run_freeze
    from data.service import FrozenWritesError, assert_writes_unfrozen

    assert assert_writes_unfrozen("x") is None  # 未冻结 → 放行
    old = os.environ.get(run_freeze._FREEZE_FILE_ENV)
    fd, lock = tempfile.mkstemp(suffix=".lock")
    os.close(fd)
    try:
        os.environ[run_freeze._FREEZE_FILE_ENV] = lock
        with run_freeze.cross_process_frozen(lock):
            assert run_freeze.is_frozen_anywhere()
            with pytest.raises(FrozenWritesError):
                assert_writes_unfrozen("历史补齐")
            # 三个整段重写入口都必须被拒（此前可重写 qfq / 指标缓存）
            from services.indicator_builder import rebuild_after_backfill

            with pytest.raises(FrozenWritesError):
                rebuild_after_backfill(["X.SS"], db=test_db)
            from data.service import get_data_service

            svc = get_data_service()
            with pytest.raises(FrozenWritesError):
                svc.backfill_daily_history("X.SS", date(2024, 1, 2), date(2024, 1, 5))
            with pytest.raises(FrozenWritesError):
                svc.rematerialize_qfq("X.SS", [], db=test_db)
    finally:
        if old is None:
            os.environ.pop(run_freeze._FREEZE_FILE_ENV, None)
        else:
            os.environ[run_freeze._FREEZE_FILE_ENV] = old
        if lock:
            os.unlink(lock) if os.path.exists(lock) else None


def test_sentinel_degrades_instead_of_crashing(tmp_path):
    """R14A-B1：哨兵路径不可建时降级为进程内冻结，**不得**崩批次。"""
    from core import run_freeze

    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")  # 父路径是文件
    target = blocker / "run_freeze.lock"
    with run_freeze.cross_process_frozen(target):
        assert run_freeze.file_frozen(target) is False, "写不进哨兵 → 视为未冻结（降级）"
    assert run_freeze.is_frozen() is False, "退出后进程内计数也必须归零"


def test_sentinel_staleness_boundary(tmp_path):
    """R14A-B3：过期阈值边界（44 分钟批次有 8 倍余量；崩溃残留不永久卡日更）。"""
    from core import run_freeze

    lock = tmp_path / "run_freeze.lock"
    lock.write_text("1 0", encoding="utf-8")
    fresh = time.time() - max(1.0, run_freeze._STALE_SECONDS * 0.5)
    os.utime(lock, (fresh, fresh))
    assert run_freeze.is_frozen_anywhere(lock) is True
    old = time.time() - (run_freeze._STALE_SECONDS + 60)
    os.utime(lock, (old, old))
    assert run_freeze.is_frozen_anywhere(lock) is False


def test_daily_update_defers_on_cross_process_sentinel(monkeypatch, tmp_path):
    """日更"看哨兵"的接线钉子（取代此前打在空转目标的那条）。

    取数**真入口**（`jobs.get_data_service` / `_pool_symbols` / `get_strategy_config`）
    被设成绊线：哨兵存在时它们一次都不能被触达。变异回 `is_frozen()` 会立刻踩线。
    """
    import time as _time

    from core import jobs, run_freeze

    lock = tmp_path / "run_freeze.lock"
    lock.write_text("999999 0", encoding="utf-8")
    monkeypatch.setenv(run_freeze._FREEZE_FILE_ENV, str(lock))
    monkeypatch.setattr(_time, "sleep", lambda _s: None)  # 不真等 30 分钟
    # 前置：进程内计数必须为 0——否则"顺延"可能来自别处泄漏的计数而掩盖哨兵未接线
    assert run_freeze.is_frozen() is False, "本钉子必须只靠哨兵触发冻结"
    assert run_freeze.is_frozen_anywhere() is True

    def _tripwire(*_a, **_kw):
        raise AssertionError("冻结期间日更不得触达取数入口（哨兵没被读？）")

    for name in ("get_data_service", "_pool_symbols", "get_strategy_config"):
        if hasattr(jobs, name):
            monkeypatch.setattr(jobs, name, _tripwire)
    recorded: list = []
    monkeypatch.setattr(
        jobs, "record_job_run_safely", lambda *a, **kw: recorded.append((a, kw))
    )

    payload = jobs._daily_market_update_job_locked(None, None, force=True)
    assert payload.get("status") == "deferred_backtest_running", payload
    assert any("daily_update_defer" in str(a[0]) for a in recorded), recorded


def test_strategy_leg_ratio_gate_is_pinned():
    """R14A 提供的夹具：**策略腿**出口同样过闸（原值 9899.52 → None）。

    此前只有基准腿的夹具能触发闸门，策略腿因"零成交 → 0.0"绕过了幅值判定。
    恒入场策略 + 单调 20%/日 的 70 根 bars → 有成交且净值单调 → 原值超闸。
    """
    from rule_backtest import (
        BacktestExecutionConfig,
        RuleBacktestRequest,
        SingleSymbolAllInBacktestEngine,
    )
    from rule_backtest.metrics import compute_summary

    always_in = {
        "schema_version": 1, "id": "always_in", "name": "恒入场夹具",
        "trade_mode": "single_symbol_all_in",
        "entry": {"type": "group", "combinator": "all", "children": [
            {"left": {"type": "literal", "value": 1}, "operator": ">=",
             "right": {"type": "literal", "value": 0}}]},
        "exit": {"type": "group", "combinator": "all", "children": [
            {"left": {"type": "literal", "value": 0}, "operator": ">=",
             "right": {"type": "literal", "value": 1}}]},
    }
    closes = np.array([10.0 * (1.20 ** i) for i in range(70)])
    bars = pd.DataFrame({
        "date": pd.bdate_range("2023-01-02", periods=70),
        "time": pd.bdate_range("2023-01-02", periods=70),
        "open": closes, "high": closes * 1.001, "low": closes * 0.999,
        "close": closes, "volume": np.full(70, 1e6), "amount": closes * 1e6,
    })
    res = SingleSymbolAllInBacktestEngine().run(RuleBacktestRequest(
        strategy=always_in, symbol="MONO.SS", bars=bars,
        execution=BacktestExecutionConfig(),
    ))
    raw = compute_summary(daily_nav=res["daily_nav"], trades=res["trades"],
                          turnover_total=0.0)
    assert res["trades"], "夹具必须有成交（否则退化为零成交腿，与基准腿夹具同构）"
    assert abs(raw["sharpe"]) > 50, f"夹具原值必须超闸，实际 {raw['sharpe']}"
    assert res["summary"]["sharpe"] is None, "策略腿出口必须过闸"
    assert res["summary"]["calmar"] is not None, "calmar 不入闸"


def test_live_lists_are_per_user(test_db):
    """R14B-F2：清单必须逐用户隔离，且两用户同日同策略不得互相覆盖。"""
    from app.routers.research_ledger import _recent_live_lists

    rows = [
        {"list_date": "2026-09-24", "strategy_version_id": "s@1", "user_id": uid,
         "as_of": "2026-09-24 14:00:00", "engine_run_id": f"L-{uid}",
         "target_json": json.dumps({"buys": [{"symbol": f"X{uid}.SS"}], "sells": []})}
        for uid in (1, 2)
    ]
    with test_db.connect() as conn:
        for row in rows:
            conn.execute(
                "INSERT INTO portfolio_live_lists (list_date, strategy_version_id,"
                " user_id, as_of, engine_run_id, target_json)"
                " VALUES (?,?,?,?,?,?)",
                (row["list_date"], row["strategy_version_id"], row["user_id"],
                 row["as_of"], row["engine_run_id"], row["target_json"]),
            )
    u1 = _recent_live_lists(user_id=1)
    u2 = _recent_live_lists(user_id=2)
    assert [r["engine_run_id"] for r in u1] == ["L-1"], "只应看到自己的清单"
    assert [r["engine_run_id"] for r in u2] == ["L-2"], "两用户同日不得互相覆盖"
    assert len(_recent_live_lists(user_id=None)) == 2, "admin 口径看全量"


def test_live_lists_schema_migrates_old_table(tmp_path):
    """R14B-F2：旧库（无 user_id、旧 UNIQUE）重开时必须被重建为逐用户 schema。"""
    from data.storage.db import Database

    path = tmp_path / "mig" / "t.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = Database(path)  # 新库 = 新 schema
    with db.connect() as conn:
        # 退化成旧 schema（含一行历史数据）→ 重开必须重建且保留数据（user_id 归 1）
        conn.execute("DROP TABLE portfolio_live_lists")
        conn.execute(
            """CREATE TABLE portfolio_live_lists (
                   id INTEGER PRIMARY KEY AUTOINCREMENT, list_date TEXT NOT NULL,
                   strategy_version_id TEXT NOT NULL, as_of TEXT NOT NULL,
                   engine_run_id TEXT, target_json TEXT NOT NULL DEFAULT '{}',
                   reconcile_json TEXT, reconciled_at TEXT,
                   created_at TEXT DEFAULT (datetime('now','localtime')),
                   UNIQUE(list_date, strategy_version_id))"""
        )
        conn.execute(
            "INSERT INTO portfolio_live_lists (list_date, strategy_version_id, as_of,"
            " target_json) VALUES ('2026-09-24','s@1','2026-09-24 14:00:00','{}')"
        )
    reopened = Database(path)
    with reopened.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(portfolio_live_lists)")}
        assert "user_id" in cols, "旧表必须被重建出 user_id 列"
        row = conn.execute("SELECT user_id FROM portfolio_live_lists").fetchone()
        assert row["user_id"] == 1, "历史行归默认操作者（user_id=1）"
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='portfolio_live_lists'"
        ).fetchone()["sql"]
        assert "UNIQUE(user_id, list_date, strategy_version_id)" in sql
