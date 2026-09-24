"""台账底层读写助手：id 分配、行转换、JSON 编解码。

表结构与 append-only 触发器见 data/storage/db.py 的 _RESEARCH_STACK_DDL。
本模块不做业务校验——骨架校验在 experiments.py，状态机在 lifecycle.py。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def alloc_id(conn: sqlite3.Connection, name: str, prefix: str, width: int = 4) -> str:
    """在既有事务内分配递增 id（research_id_seq）。调用方负责提交事务。"""
    conn.execute(
        """INSERT INTO research_id_seq (name, value) VALUES (?, 1)
           ON CONFLICT(name) DO UPDATE SET value = value + 1""",
        (name,),
    )
    row = conn.execute("SELECT value FROM research_id_seq WHERE name = ?", (name,)).fetchone()
    return f"{prefix}{int(row['value']):0{width}d}"


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}  # noqa: SIM118 —— sqlite3.Row.keys() 才是列名来源（迭代 Row 得到的是值）


def rows_to_dicts(rows) -> list[dict]:
    return [row_to_dict(r) for r in rows]


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def loads(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default
