"""运行状态与成交流水落库（详设 §4.1）。

层级关系：一次运行 = 一行 engine_runs（血缘锚点：策略/config_hash/
data_version/engine_version/git_hash）；其余各表全部是 run_id 的子记录。

写入策略：run 开始时插入 engine_runs(running)；订单/成交/未成交/持仓
快照/净值在内存缓冲，run 结束时单事务批量落库并置 finished；异常置
failed。缓冲不上限——5 年 × 800 标的单次回测子记录量级 ≈ 数万行，
单机内存可承（存储预算：每 run ≈ 2 万行 ≈ 2MB，详设 §7.1）。
"""

from __future__ import annotations

import json
import subprocess

from engine.models import Account, Fill, Unfilled

ENGINE_VERSION = "1.0.0"


def current_git_hash() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


class EngineStore:
    """一个 run 的落库上下文（缓冲 + 结束批量写）。"""

    def __init__(self, db, run_id: str) -> None:
        self._db = db
        self.run_id = run_id
        self._orders: list[tuple] = []
        self._fills: list[tuple] = []
        self._unfilled: list[tuple] = []
        self._positions: list[tuple] = []
        self._nav: list[tuple] = []

    # ------------------------------------------------------------------
    def begin_run(
        self,
        *,
        kind: str,
        strategy_ref: str,
        config_hash: str,
        resolved_config_yaml: str,
        run_params: dict | None = None,
        data_version: int = 0,
    ) -> None:
        with self._db.connect() as conn:
            conn.execute(
                """INSERT INTO engine_runs
                   (run_id, kind, strategy_ref, config_hash, resolved_config_yaml,
                    run_params_json, data_version, engine_version, git_hash, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')""",
                (
                    self.run_id,
                    kind,
                    strategy_ref,
                    config_hash,
                    resolved_config_yaml,
                    json.dumps(run_params or {}, ensure_ascii=False, sort_keys=True),
                    int(data_version),
                    ENGINE_VERSION,
                    current_git_hash(),
                ),
            )

    # ------------------------------------------------------------------
    def record_order(
        self,
        *,
        order_id: str,
        decision_date: str,
        target_fill_date: str,
        symbol: str,
        side: str,
        order_type: str,
        intent_type: str,
        intent_value: float,
        source: str,
        status: str,
    ) -> None:
        self._orders.append(
            (self.run_id, order_id, decision_date, target_fill_date, symbol, side,
             order_type, intent_type, float(intent_value), source, status)
        )

    def record_fill(self, fill: Fill, *, order_id: str | None = None) -> None:
        self._fills.append(
            (
                self.run_id,
                order_id or fill.order_id,
                fill.symbol,
                fill.fill_date.isoformat(),
                float(fill.base_price),
                float(fill.slippage_base),
                float(fill.slippage_tail),
                float(fill.fill_price),
                int(fill.quantity),
                float(fill.commission),
                float(fill.stamp_tax),
                float(fill.fee_total),
                float(fill.cash_after),
            )
        )

    def record_unfilled(self, unfilled: Unfilled) -> None:
        self._unfilled.append(
            (
                self.run_id,
                unfilled.order_id,
                unfilled.symbol,
                unfilled.decision_date.isoformat(),
                unfilled.reason,
                json.dumps(unfilled.intent_snapshot, ensure_ascii=False, sort_keys=True),
            )
        )

    def record_positions(self, *, day, account: Account) -> None:
        """逐持仓当日快照（engine_positions 是逐日快照表）。"""
        day_str = day.isoformat() if hasattr(day, "isoformat") else str(day)
        for symbol, pos in account.positions.items():
            stop = pos.stop
            self._positions.append(
                (
                    self.run_id, day_str, symbol,
                    int(pos.quantity), int(pos.sellable_quantity),
                    float(pos.avg_cost),
                    pos.entry_date.isoformat() if pos.entry_date else None,
                    float(pos.entry_price),
                    None if stop is None or stop.stop_price is None else float(stop.stop_price),
                    0.0 if stop is None else float(stop.highest_since_buy),
                    0.0 if stop is None else float(stop.atr_at_entry),
                    json.dumps({} if stop is None else stop.module_state,
                               ensure_ascii=False, sort_keys=True),
                )
            )

    def record_nav(self, nav_row: dict) -> None:
        self._nav.append(
            (
                self.run_id,
                str(nav_row["date"]),
                float(nav_row["cash"]),
                float(nav_row["positions_value"]),
                float(nav_row["equity"]),
                None if nav_row.get("heat") is None else float(nav_row["heat"]),
                float(nav_row.get("exposure", 0.0)),
            )
        )

    # ------------------------------------------------------------------
    def finish_run(self, *, status: str = "finished", error: str | None = None) -> None:
        """缓冲子记录单事务落库 + 运行状态收口。"""
        with self._db.connect() as conn:
            if self._orders:
                conn.executemany(
                    """INSERT INTO engine_orders
                       (run_id, order_id, decision_date, target_fill_date, symbol,
                        side, order_type, intent_type, intent_value, source, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    self._orders,
                )
            if self._fills:
                conn.executemany(
                    """INSERT INTO engine_fills
                       (run_id, order_id, symbol, fill_date, base_price,
                        slippage_base, slippage_tail, fill_price, quantity,
                        commission, stamp_tax, fee_total, cash_after)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    self._fills,
                )
            if self._unfilled:
                conn.executemany(
                    """INSERT INTO engine_unfilled
                       (run_id, order_id, symbol, decision_date, reason, intent_snapshot_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    self._unfilled,
                )
            if self._positions:
                conn.executemany(
                    """INSERT INTO engine_positions
                       (run_id, date, symbol, quantity, sellable_quantity, avg_cost,
                        entry_date, entry_price, stop_price, highest_since_buy,
                        atr_at_entry, module_state_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    self._positions,
                )
            if self._nav:
                conn.executemany(
                    """INSERT INTO engine_daily_nav
                       (run_id, date, cash, positions_value, equity, heat, exposure)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    self._nav,
                )
            conn.execute(
                """UPDATE engine_runs
                   SET status = ?, finished_at = datetime('now','localtime'), error = ?
                   WHERE run_id = ?""",
                (status, (str(error)[:4000] if error else None), self.run_id),
            )

    # ------------------------------------------------------------------
    # 读取侧（报告/对账/测试）
    # ------------------------------------------------------------------
    @staticmethod
    def load_nav(db, run_id: str) -> list[dict]:
        with db.connect() as conn:
            rows = conn.execute(
                """SELECT date, cash, positions_value, equity, heat, exposure
                   FROM engine_daily_nav WHERE run_id = ? ORDER BY date""",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def load_fills(db, run_id: str) -> list[dict]:
        """成交 + 方向（联 engine_orders 取 side；fills 表自身不冗余方向）。"""
        with db.connect() as conn:
            rows = conn.execute(
                """SELECT f.*, o.side AS side
                   FROM engine_fills f
                   JOIN engine_orders o ON o.run_id = f.run_id AND o.order_id = f.order_id
                   WHERE f.run_id = ? ORDER BY f.id""",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def load_unfilled(db, run_id: str) -> list[dict]:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM engine_unfilled WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def load_orders(db, run_id: str) -> list[dict]:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM engine_orders WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def load_positions(db, run_id: str, day: str | None = None) -> list[dict]:
        sql = "SELECT * FROM engine_positions WHERE run_id = ?"
        params: list = [run_id]
        if day:
            sql += " AND date = ?"
            params.append(day)
        sql += " ORDER BY date, symbol"
        with db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_run(db, run_id: str) -> dict | None:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM engine_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return dict(row) if row else None
