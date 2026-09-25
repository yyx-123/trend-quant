"""Round 12 修复钉子（round12-review.md 各项的回归锚）。

覆盖两个独立代理抓到的材质性问题：
- R12A-F1 退化闸门漏了"单策略跑批"整条消费面（噪声 3680 直达前端表格）；
- R12A-F2 因子守卫的幅值带挡不住带内脏因子（f=1e3 → limit_up=0.001 且判涨停）；
- R12B-F1 批量导出跨用户泄露实盘成交；
- R12B-F4 旧栈批量回测缺运行期冻结门。
（R12B-F2/F3 的实盘清单面钉子见 tests/integration/test_live_runner.py。）
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

MACD_STRATEGY = {
    "id": "r12_probe",
    "trade_mode": "single_symbol_all_in",
    "entry": {
        "type": "group", "combinator": "all",
        "children": [{
            "id": "e1", "type": "condition",
            "left": {"type": "indicator", "name": "macd_line", "params": {}},
            "operator": "cross_above",
            "right": {"type": "indicator", "name": "macd_signal", "params": {}},
        }],
    },
    "exit": {
        "type": "group", "combinator": "any",
        "children": [{
            "id": "x1", "type": "condition",
            "left": {"type": "indicator", "name": "macd_line", "params": {}},
            "operator": "cross_below",
            "right": {"type": "indicator", "name": "macd_signal", "params": {}},
        }],
    },
}


def _bars(closes: list[float], start: str = "2023-03-03") -> pd.DataFrame:
    n = len(closes)
    arr = np.asarray(closes, dtype=float)
    return pd.DataFrame({
        "time": pd.bdate_range(start, periods=n),
        "open": arr, "high": arr, "low": arr, "close": arr,
        "volume": np.full(n, 1e6), "amount": arr * 1e6,
    })


def _run(bars: pd.DataFrame, symbol: str = "603061.SS") -> dict:
    from rule_backtest import (
        BacktestExecutionConfig,
        RuleBacktestRequest,
        SingleSymbolAllInBacktestEngine,
    )

    return SingleSymbolAllInBacktestEngine().run(RuleBacktestRequest(
        strategy=MACD_STRATEGY, symbol=symbol, bars=bars,
        execution=BacktestExecutionConfig(
            initial_capital=100_000.0, slippage=0.002, instrument_type="etf",
        ),
    ))


def test_engine_summary_exit_gates_noise_ratios():
    """R12A-F1：引擎**出口**必须过幅值闸门（单跑路径的噪声核心）。

    IPO 一字板式的 3 根 bar 窗口：买持腿年化 sharpe 实测 3680.68，旧实现把
    `benchmark_summary.sharpe` 原样塞进 HTTP 响应 → 前端表格显示 3680.677。
    闸门移到引擎出口后，single-run / 批量落库 / 导出 / 前端四条消费面一次覆盖。
    """
    short = _run(_bars([84.36, 92.80, 102.08]))
    # 策略腿零成交 → 旧栈既定语义是 0.0（R5-D-4 存量口径，出口闸门刻意不动它）
    assert short["summary"]["sharpe"] == 0.0
    # 买持腿是噪声（实测 3680.68）→ 必须被出口闸门置 None
    assert short["benchmark_summary"]["sharpe"] is None, "噪声基准 Sharpe 不得出引擎"
    assert short["annual_returns"][0]["benchmark_sharpe"] is None
    # 合法性照常保留（防过拦截）：120 根带真实波动的窗口
    rng = np.random.default_rng(7)
    closes = 10 * np.exp(np.cumsum(rng.normal(0.0004, 0.012, 120)))
    normal = _run(_bars(list(closes)))
    assert normal["summary"]["sharpe"] is not None
    assert normal["benchmark_summary"]["sharpe"] is not None
    assert abs(normal["summary"]["sharpe"]) < 50


def test_sanitize_ratio_metrics_semantics():
    """出口闸门助手：只打超闸的 sharpe/sortino，calmar 与合法值一律不动。

    注（诚实记录）：引擎出口对**基准腿**的接线由上面的窗口钉子独立钉住；
    对**策略腿**的同一条接线没有独立夹具（要构造"有成交但净值单调"的策略腿），
    只由本助手的语义断言覆盖——这条缺口记在 round12-review.md 的 backlog 里。
    """
    from rule_backtest.metrics import DEGENERATE_SHARPE_ABS_LIMIT, sanitize_ratio_metrics

    out = sanitize_ratio_metrics(
        {"sharpe": 3680.68, "sortino": 1.4e14, "calmar": 78.5,
         "total_return": 0.19, "annual_return": 3.16}
    )
    assert out["sharpe"] is None and out["sortino"] is None
    assert out["calmar"] == 78.5, "calmar 不入闸（低回撤/短窗口可合法 > 50）"
    assert out["total_return"] == 0.19 and out["annual_return"] == 3.16
    keep = sanitize_ratio_metrics({"sharpe": DEGENERATE_SHARPE_ABS_LIMIT / 2})
    assert keep["sharpe"] == DEGENERATE_SHARPE_ABS_LIMIT / 2


def test_export_live_trades_scoped_by_user(test_db, monkeypatch):
    """R12B-F1：`live_trades.csv` 必须按调用者过滤（此前是全体用户的实盘成交）。

    两层都钉：① 取数助手按 user_id 过滤；② **HTTP 端点**把调用者 id 传下去
    （直接调路由函数，覆盖"助手对了但端点忘记传"的形态）。
    """
    import asyncio

    from services.backtest_export import _live_trades_frame

    u1 = test_db.create_user("u1", "pass12345")["id"]
    u2 = test_db.create_user("u2", "pass12345")["id"]
    for uid, sym in ((u1, "AAA.SS"), (u2, "BBB.SS")):
        trade = test_db.create_manual_trade(uid, sym, "2024-03-01", 10.0, 100)
        test_db.close_manual_trade(int(trade["id"]), "2024-03-05", 11.0)
    only_u1 = _live_trades_frame(test_db, user_id=u1)
    assert list(only_u1["symbol"]) == ["AAA.SS"], "只应返回调用者自己的成交"
    assert all(only_u1["user_id"] == u1)
    # 本地脚本口径（user_id=None）仍取全部——单操作员场景
    assert len(_live_trades_frame(test_db)) == 2

    # ② 端点层：把导出入口打桩，断言端点**确实把调用者 id 传下去**
    #    （覆盖"取数助手对了、端点忘记传 id"的形态）；产物过滤本身由上一步覆盖。
    batch = {
        "batch_id": "B-r12-scope", "name": "scope", "status": "completed",
        "categories_json": "[]", "strategy_snapshot_json": "[]", "config_json": "{}",
        "total_cells": 0,
    }
    assert test_db.create_batch_run_if_idle(batch) is True
    import services.backtest_export as be
    from app.routers.batch_backtest import export_batch

    seen: list[dict] = []

    def fake_export(db, batch_id, **kw):
        seen.append(kw)
        return {"batch_id": batch_id, "dir": "stub", "files": {}}

    monkeypatch.setattr(be, "export_batch_analysis", fake_export)
    asyncio.run(export_batch(
        "B-r12-scope", compare="", live=True, live_all=False,
        user={"id": u1, "username": "u1", "is_admin": False},
    ))
    assert seen and seen[-1]["user_id"] == u1, "HTTP 导出必须按调用者过滤"
    # admin 显式要求全量时才传 None
    asyncio.run(export_batch(
        "B-r12-scope", compare="", live=True, live_all=True,
        user={"id": u2, "username": "u2", "is_admin": True},
    ))
    assert seen[-1]["user_id"] is None
    # 非 admin 传 live_all=true 不得越权
    asyncio.run(export_batch(
        "B-r12-scope", compare="", live=True, live_all=True,
        user={"id": u1, "username": "u1", "is_admin": False},
    ))
    assert seen[-1]["user_id"] == u1, "非 admin 不得越权导出全体用户"


def test_engine_runs_delete_guard_migrates_to_existing_db(tmp_path):
    """R12A backlog：禁删守卫必须能**迁移**到已存在的库（DROP+CREATE 而非 IF NOT EXISTS）。

    用当前代码建库 → 手工 DROP 掉触发器（模拟旧版代码建的库）→ 再用当前代码打开，
    守卫必须重新装上且 DELETE 被拒。
    """
    import sqlite3

    from data.storage.db import Database

    path = tmp_path / "mig" / "t.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = Database(path)
    with db.connect() as conn:
        # 既要测"守卫缺失时补装"，也要测"**定义被改弱**时被 DROP+CREATE 覆盖"
        # ——后者才是 DROP+CREATE 相对 IF NOT EXISTS 的真正差别（R13A backlog B2：
        # 只钉前者时把实现改成 IF NOT EXISTS 仍绿）
        conn.execute("DROP TRIGGER IF EXISTS trg_engine_runs_no_delete")
    reopened = Database(path)  # 重新打开 = 走一遍 DDL（含守卫的 DROP+CREATE）
    with reopened.connect() as conn:
        exists = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'"
            " AND name='trg_engine_runs_no_delete'"
        ).fetchone()[0]
        assert exists == 1, "守卫必须随 DDL 迁移到存量库"
        conn.execute(
            "INSERT INTO engine_runs (run_id, kind, strategy_ref, config_hash,"
            " resolved_config_yaml, run_params_json, data_version, engine_version, git_hash)"
            " VALUES ('R-mig','backtest','x','h','y','{}',1,'v','g')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM engine_runs WHERE run_id='R-mig'")
    # 第二段：把守卫**改弱**（只挡 status='x' 之外的删除）→ 重开库必须恢复强定义
    with reopened.connect() as conn:
        conn.execute("DROP TRIGGER trg_engine_runs_no_delete")
        conn.execute(
            "CREATE TRIGGER trg_engine_runs_no_delete BEFORE DELETE ON engine_runs"
            " WHEN OLD.status = 'never' BEGIN SELECT RAISE(ABORT, 'weak'); END"
        )
    third = Database(path)
    with third.connect() as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='trg_engine_runs_no_delete'"
        ).fetchone()[0]
        assert "is append-only" in sql, "改弱的定义必须被 DROP+CREATE 覆盖回强定义"
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM engine_runs WHERE run_id='R-mig'")


def test_batch_run_frozen_enters_freeze_gate(monkeypatch):
    """R12B-F4：批次执行必须包在运行期冻结门里（决策 A3）。"""
    from core import run_freeze
    from rule_backtest.batch_service import BatchBacktestService

    entered = []

    class _Ctx:
        def __enter__(self):
            entered.append("enter")

        def __exit__(self, *exc):
            entered.append("exit")
            return False

    monkeypatch.setattr(run_freeze, "frozen_writes", lambda: _Ctx())
    with pytest.raises(ValueError):
        # 批次不存在 → run_batch 抛错，但冻结门必须已经进入过
        BatchBacktestService().run_batch_frozen("NOPE")
    assert entered == ["enter", "exit"], "批次执行必须在冻结门内（且异常时正确退出）"
