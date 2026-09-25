"""数据请求留痕（详设 §3.3）：谁在什么时候以什么 as-of 取了什么。

用途：实验血缘（该回测当时看到的是哪一版数据）、PIT 审计复查。
写法是进程内缓冲 + 批量落库（异步不阻塞取数热路径）；worker 在 run
结束时 flush，测试可直接调 flush()。
"""

from __future__ import annotations

import threading

from audit.app_logger import get_logger

logger = get_logger(__name__)


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
        """把缓冲行写入 gateway_audit；**审计失败绝不影响取数**。

        此前 flush 失败会（a）把已取走的 rows 整批
        丢弃（缓冲被清空、无重试）、（b）异常沿 `record()` 的容量触发路径抛进
        取数热路径（一次 GET 面板因此失败）、（c）run 收尾的 flush_audit()
        把已完成的 run 弄炸——与本模块"异步不阻塞取数热路径"的声明相反。
        改为：失败时把 rows 合并回缓冲待重试（有上界，避免磁盘故障吃穿内存），
        只记日志、不抛。
        """
        with self._lock:
            rows, self._rows = self._rows, []
        if not rows:
            return 0
        try:
            with self._db.connect() as conn:
                conn.executemany(
                    """INSERT INTO gateway_audit
                       (caller_layer, run_id, method, as_of, symbols_count,
                        date_start, date_end, fields, adjust, mode, data_version)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    rows,
                )
        except Exception:
            logger.warning(
                "gateway audit flush failed; %d row(s) requeued for retry",
                len(rows), exc_info=True,
            )
            keep = rows[-max(self._capacity, 1):]
            with self._lock:
                self._rows[:0] = keep
            return 0
        return len(rows)

    def pending(self) -> int:
        with self._lock:
            return len(self._rows)
