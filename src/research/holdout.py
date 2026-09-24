"""holdout 工程化（详设 §6.6.4）。

- sample/holdout 窗口全局配置：sample=[2015-01-01, 2024-12-31]、
  holdout=[2025-01-01, now)（2026-09-23 用户决策：起点前移至 2015）；
- 任何评估模块取数窗口超 sample → 需 holdout_token（人审放行）+ 计数 +
  holdout_touched 记录；高原补跑、随机对照重算同样受限；
- 约束范围（决策 C3）：holdout 只约束**实验路径**；非实验路径不拦截，
  但触碰 holdout 窗口同样记录 holdout_touched——触碰必留痕；
- 生效时点：holdout 规则自阶段 5（L4 正式化）生效——**enforced 默认开**
  （建设期/测试显式 set_enforced(False) 关闭）；本模块从阶段 0 起提供
  窗口判定与留痕。
"""

from __future__ import annotations

from research.errors import HoldoutError
from research.ledger import alloc_id, row_to_dict, rows_to_dicts
from research.sessions import require_human_session

DEFAULT_SAMPLE_START = "2015-01-01"
DEFAULT_SAMPLE_END = "2024-12-31"
DEFAULT_HOLDOUT_START = "2025-01-01"

_ENFORCE_KEY = "research.holdout_enforced"


def get_windows(db) -> dict:
    return {
        "sample_start": db.get_config("research.sample_start", DEFAULT_SAMPLE_START),
        "sample_end": db.get_config("research.sample_end", DEFAULT_SAMPLE_END),
        "holdout_start": db.get_config("research.holdout_start", DEFAULT_HOLDOUT_START),
    }


def is_enforced(db) -> bool:
    """holdout 卡控是否已生效（详设 §6.6.4：阶段 5 起生效——默认开，
    建设期/测试显式 set_enforced(False) 关闭）。"""
    return str(db.get_config(_ENFORCE_KEY, "1")).strip() in ("1", "true", "True")


def set_enforced(db, enabled: bool) -> None:
    db.set_config(_ENFORCE_KEY, "1" if enabled else "0")


def window_touches_holdout(db, start: str | None, end: str | None) -> bool:
    """窗口 [start, end] 是否与 holdout 段 [holdout_start, now) 相交。"""
    if not start or not end:
        return False
    holdout_start = str(get_windows(db)["holdout_start"])
    return str(end)[:10] >= holdout_start


def grant_token(
    db, *, session_id: str, purpose: str, experiment_id: str | None = None
) -> dict:
    """发放 holdout 放行 token（仅 human session；+ 计数留痕）。"""
    session = require_human_session(db, session_id)
    with db.connect() as conn:
        tid = alloc_id(conn, "holdout_tokens", "H", width=4)
        conn.execute(
            """INSERT INTO holdout_tokens (id, granted_by, purpose, experiment_id)
               VALUES (?, ?, ?, ?)""",
            (tid, session["session_id"], str(purpose or ""), experiment_id),
        )
    return get_token(db, tid)


def get_token(db, token_id: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM holdout_tokens WHERE id = ?", (token_id,)
        ).fetchone()
    return row_to_dict(row)


def list_tokens(db) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM holdout_tokens ORDER BY id").fetchall()
    return rows_to_dicts(rows)


def check_window(
    db,
    *,
    start: str | None,
    end: str | None,
    experiment_id: str | None = None,
    token_id: str | None = None,
) -> bool:
    """实验路径的窗口检查。返回 holdout_touched（是否触碰 holdout 段）。

    enforced 开启时：触碰 holdout 且无有效 token → HoldoutError；
    有 token 则消费之（一次性）。enforced 关闭时（建设期）只判定不拦截。
    """
    touched = window_touches_holdout(db, start, end)
    if not touched:
        return False
    if not is_enforced(db):
        return True
    if not token_id:
        raise HoldoutError(
            f"window [{start}, {end}] touches holdout; grant a holdout token first"
        )
    token = get_token(db, token_id)
    if token is None or token["consumed_at"] is not None:
        raise HoldoutError(f"invalid or consumed holdout token: {token_id}")
    if token["experiment_id"] and experiment_id and token["experiment_id"] != experiment_id:
        raise HoldoutError(
            f"holdout token {token_id} is bound to experiment {token['experiment_id']}"
        )
    # 原子消费（TOCTOU：并发下检查+更新两步会让同一 token 被用两次）
    with db.connect() as conn:
        cur = conn.execute(
            """UPDATE holdout_tokens
               SET consumed_at = datetime('now','localtime')
               WHERE id = ? AND consumed_at IS NULL""",
            (token_id,),
        )
        if cur.rowcount == 0:
            raise HoldoutError(f"invalid or consumed holdout token: {token_id}")
    return True
