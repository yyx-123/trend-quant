"""会话登记（research_sessions）。

人与 AI 是同角色的两种客户端（架构稿 §2.1）；会话是写入归属
（owner_session）与权限判定（holdout 放行仅 human）的锚点。
"""

from __future__ import annotations

import uuid

from research.errors import SessionRequired
from research.ledger import row_to_dict, rows_to_dicts

DEFAULT_HUMAN_SESSION = "human-default"


def register_session(db, kind: str, label: str = "", channel: str = "api") -> dict:
    if kind not in ("human", "ai"):
        raise SessionRequired(f"unknown session kind: {kind}")
    channel = str(channel or "api").strip() or "api"
    session_id = f"{kind}-{channel}-{uuid.uuid4().hex[:12]}"
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO research_sessions (session_id, kind, label, channel)
               VALUES (?, ?, ?, ?)""",
            (session_id, kind, str(label or ""), channel),
        )
    return get_session(db, session_id)


def get_or_create_default_human_session(db) -> dict:
    """默认人工会话（web 台账操作 / holdout 放行的归属）。"""
    existing = get_session(db, DEFAULT_HUMAN_SESSION)
    if existing is not None:
        return existing
    with db.connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO research_sessions (session_id, kind, label, channel)
               VALUES (?, 'human', '默认人工会话', 'web')""",
            (DEFAULT_HUMAN_SESSION,),
        )
    return get_session(db, DEFAULT_HUMAN_SESSION)


def get_session(db, session_id: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM research_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
    return row_to_dict(row)


def require_session(db, session_id: str) -> dict:
    session = get_session(db, session_id)
    if session is None:
        raise SessionRequired(f"research session not found: {session_id}")
    return session


def require_human_session(db, session_id: str) -> dict:
    """holdout 放行等仅人工会话可做的操作的门。"""
    from research.errors import PermissionDenied

    session = require_session(db, session_id)
    if session["kind"] != "human":
        raise PermissionDenied(f"operation requires a human session, got {session_id}")
    return session


def list_sessions(db) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM research_sessions ORDER BY created_at, session_id"
        ).fetchall()
    return rows_to_dicts(rows)


def ensure_channel_session(
    db, *, session_id: str, label: str, channel: str
) -> dict:
    """按**指定 id** 保证一个通道会话存在（幂等）。

    MCP 通道此前为了"按 token 派生的稳定会话 id"
    **自己写库**（`INSERT OR IGNORE INTO research_sessions`），把上层策略
    （会话命名/归属）落在通道里、绕过服务面的 kind 白名单校验，与"薄通道厚服务"
    的分层声明矛盾。改为通道只调本函数。
    """
    existing = get_session(db, session_id)
    if existing is not None:
        return existing
    with db.connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO research_sessions (session_id, kind, label, channel)
               VALUES (?, 'ai', ?, ?)""",
            (session_id, label, channel),
        )
    return get_session(db, session_id)


def get_or_create_ai_session(db, channel: str = "mcp", label: str = "AI 研究助手") -> dict:
    """AI 通道默认会话（每个 channel 一个稳定会话，台账归属可读）。"""
    return ensure_channel_session(
        db, session_id=f"ai-{channel}-default", label=label, channel=channel
    )
