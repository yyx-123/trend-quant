"""Round 19 修复钉子。

- R19A-F1：离线 backfill 脚本对 `sqlite3.Row` 调 `.get()` → 一遇待回填格子即崩
  （R18 修复引入的回归，修复前可正常运行）→ 本钉子端到端跑一次真实 backfill。
- R19A-F2：plateau 判据曾一刀切成 unknown → 单参数实验的"非孤峰"条件永不阻断
  （假安全）→ 收敛为"保留阻断力 + 标注低置信"（钉子在 r18/critical_paths 文件）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd


def _load_backfill_module():
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location(
        "backfill_batch_excess_metrics", scripts / "backfill_batch_excess_metrics.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_backfill_case(db) -> None:
    n = 200
    rng = np.random.default_rng(5)
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0.0004, 0.012, n)))
    dates = pd.bdate_range("2023-01-02", periods=n)
    bars = pd.DataFrame({
        "time": dates, "open": closes, "high": closes * 1.01, "low": closes * 0.99,
        "close": closes, "volume": np.full(n, 1e6), "amount": closes * 1e6,
    })
    db.save_market_data("X.SS", bars, price_mode="qfq")
    db.create_batch_run_if_idle({
        "batch_id": "B19", "name": "b19", "categories_json": "[]",
        "strategy_snapshot_json": "[]", "config_json": "{}", "total_cells": 1,
    })
    db.insert_batch_cell({
        "batch_id": "B19", "symbol": "X.SS", "strategy_id": "s1", "status": "ok",
        "start_date": "2023-01-02", "end_date": "2023-10-06",
        "sharpe": 1.2, "calmar": 2.0, "trades_json": "[]",
    })


def test_backfill_script_runs_end_to_end(tmp_path, monkeypatch):
    """R19A-F1：离线回填脚本必须能真正跑完并写回格子列。

    回归根因：`cell.get("symbol")` 对 `sqlite3.Row` 不存在（AttributeError），
    且 SELECT 里没有 asset_type 列 → 一遇待回填格子即崩（修复前可正常运行）。
    """
    from data.storage.db import Database

    mod = _load_backfill_module()
    db = Database(tmp_path / "d" / "t.db")
    _seed_backfill_case(db)
    monkeypatch.setattr(mod, "init_db", lambda *a, **kw: db)
    monkeypatch.setattr(mod, "DB_PATH", tmp_path / "absent.db")  # 跳过自动备份分支
    mod.backfill(None, False)  # 不抛异常 = 端到端可跑
    row = db.get_batch_cells("B19")[0]
    assert row["benchmark_sharpe"] is not None, "回填必须真的写入基准夏普"
    assert row["excess_sharpe"] is not None
    # avg_flat_days 在"无成交"的格子上本就记 None（脚本语义），故不断言它


def test_all_of_exit_requires_all_legs():
    """R19B-F2：`all_of` 的离场必须是"全部子模块同时成立"（§5.2.8），不是任一。"""
    from datetime import date as _date

    from portfolio.registry import ModuleSpec, fresh_registry
    from portfolio.slots import register_builtin_modules
    from portfolio.slots.execution import AllOfSignal
    from portfolio.slots.signal import SignalEvent

    reg = fresh_registry()
    register_builtin_modules(reg)

    class _Leg:
        def __init__(self, exits):
            self._exits = exits
            self._registered_key = "leg"

        def prepare(self, panel):
            return None

        def scan(self, ctx, members):
            return [SignalEvent(symbol=s, kind="exit", date=ctx.date, meta={})
                    for s in self._exits]

    class _Ctx:
        date = _date(2024, 3, 15)

    combo = AllOfSignal({"members": [{"module": "macd_cross@1"}]}, reg)
    combo._subs = [(_Leg(["A.SS"]), "l1"), (_Leg(["B.SS"]), "l2")]
    out = [e for e in combo.scan(_Ctx(), []) if e.kind == "exit"]
    assert out == [], "只有单腿确认时 all_of 不得离场（此前实现取并集 → 会离场）"
    combo._subs = [(_Leg(["A.SS"]), "l1"), (_Leg(["A.SS"]), "l2")]
    out2 = [e for e in combo.scan(_Ctx(), []) if e.kind == "exit"]
    assert [e.symbol for e in out2] == ["A.SS"], "两腿同时确认才离场"


def test_buffered_rotation_respects_action_gate():
    """R19B-F3：轮换必须受 action_gate 门控（§5.11 缺口① 覆盖 新开仓/轮换/再平衡）。"""
    from datetime import date as _date

    from portfolio.slots.execution import BufferedRotationExecution

    mod = BufferedRotationExecution({"action_gate": {"freq": "monthly"}, "max_swaps": 1, "buffer": 0.0})

    class _Panel:
        dates = (_date(2024, 3, 14), _date(2024, 3, 15))
        upto = 1  # 前一根 03-14 与本根 03-15 同月 → monthly 门关闭

    class _GateCtx:
        date = _date(2024, 3, 15)
        panel = _Panel()
        account = type("A", (), {"equity": lambda self: 1_000_000.0})()
        params = {"_members_count": 2}

    ctx = _GateCtx()
    # 候选动量高于持仓 → 若门控缺失就会真的产生轮换卖出（否则钉子对门控不敏感）
    mod._momentum = lambda c, s: 2.0 if s == "B.SS" else 1.0  # type: ignore[assignment]
    assert mod.allows_action(ctx) is False, "非月首日门必须关闭"
    out = mod.rotation_policy(ctx, [type("E", (), {"symbol": "B.SS"})()], ["A.SS"])
    assert out == [], "门关闭时不得轮换（此前不查门 → 月中也会卖出）"


def test_heat_cap_ex_post_warning_is_honest():
    """R19B-F1：事后 heat 越线必须如实告警（cap 只是准入时刻的卡控）。"""
    from portfolio.backtester import heat_cap_ex_post_warning

    flat = [{"date": "2024-01-02", "equity": 100_000.0, "heat": 3_000.0}]
    assert heat_cap_ex_post_warning(flat, 0.06) is None
    over = flat + [{"date": "2024-01-03", "equity": 100_000.0, "heat": 24_910.0}]
    msg = heat_cap_ex_post_warning(over, 0.06)
    assert msg is not None and msg.startswith("heat_cap_exceeded(事后口径)")
    assert "1/2 个日结" in msg and "4.15×cap" in msg
    assert "不要把该 run 的敞口读作受 cap 约束" in msg
