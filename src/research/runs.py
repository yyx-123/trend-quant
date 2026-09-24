"""research_runs：实验 ↔ 取证运行（append-only，详设 §6.3）。"""

from __future__ import annotations

from research.ledger import alloc_id, rows_to_dicts


def insert_run(
    db,
    *,
    experiment_id: str,
    engine_run_id: str | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    window_kind: str = "sample",
    holdout_touched: bool = False,
) -> str:
    if window_kind not in ("sample", "holdout", "plateau_probe"):
        raise ValueError(f"invalid window_kind: {window_kind}")
    with db.connect() as conn:
        rid = alloc_id(conn, "research_runs", "R", width=5)
        conn.execute(
            """INSERT INTO research_runs
               (id, experiment_id, engine_run_id, window_start, window_end,
                window_kind, holdout_touched)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                rid,
                experiment_id,
                engine_run_id,
                window_start,
                window_end,
                window_kind,
                1 if holdout_touched else 0,
            ),
        )
    return rid


def list_runs(db, experiment_id: str) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM research_runs WHERE experiment_id = ? ORDER BY id",
            (experiment_id,),
        ).fetchall()
    return rows_to_dicts(rows)
