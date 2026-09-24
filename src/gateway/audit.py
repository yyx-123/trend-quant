"""数据请求留痕（详设 §3.3）：谁在什么时候以什么 as-of 取了什么。

用途：实验血缘（该回测当时看到的是哪一版数据）、PIT 审计复查。
写法是进程内缓冲 + 批量落库（异步不阻塞取数热路径）；worker 在 run
结束时 flush，测试可直接调 flush()。
"""

from __future__ import annotations

import threading


class AuditBuffer:
    def __init__(self, db, capacity: int = 200) -> None:
        self._db = db
        self._capacity = int(capacity)
        self._lock = threading.Lock()
        self._rows: list[tuple] = []

    def record(
        self,
        *,
        caller_layer: str,
        method: str,
        as_of: str,
        run_id: str | None = None,
        symbols_count: int = 0,
        date_start: str | None = None,
        date_end: str | None = None,
        fields: str | None = None,
        adjust: str | None = None,
        mode: str | None = None,
        data_version: int = 0,
    ) -> None:
        row = (
            caller_layer,
            run_id,
            method,
            str(as_of),
            int(symbols_count),
            date_start,
            date_end,
            fields,
            adjust,
            mode,
            int(data_version),
        )
        with self._lock:
            self._rows.append(row)
            pending = len(self._rows)
        if pending >= self._capacity:
            self.flush()

    def flush(self) -> int:
        with self._lock:
            rows, self._rows = self._rows, []
        if not rows:
            return 0
        with self._db.connect() as conn:
            conn.executemany(
                """INSERT INTO gateway_audit
                   (caller_layer, run_id, method, as_of, symbols_count,
                    date_start, date_end, fields, adjust, mode, data_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        return len(rows)

    def pending(self) -> int:
        with self._lock:
            return len(self._rows)
