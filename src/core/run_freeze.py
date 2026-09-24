"""运行期数据冻结（详设 §3.1，决策 A3）。

回测运行期间冻结数据写入——调度器在 run 执行期间顺延日更/指标重建等
写任务（run 优先、日更等待），防止 qfq 原位重写导致一次 run 读到两版
数据、血缘失真。冻结判据为**计数型**：只要并发池中存在任意活跃回测 run
即冻结写任务，全部 run 结束后解冻。

进程边界（已记入开发日志 §1 决策 4）：本计数器是进程内实现，app 进程内
的调度器与 research worker 共享；独立 CLI 进程发起的 run 与 app 调度器
不共享计数（一期可接受——研究 run 统一经 research worker 发起）。
实盘运行器不冻结（它的职责就是用最新数据，14:00 执行与 16:30 日更
天然不冲突）。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

_lock = threading.Lock()
_active_backtest_runs = 0


def acquire() -> None:
    global _active_backtest_runs
    with _lock:
        _active_backtest_runs += 1


def release() -> None:
    global _active_backtest_runs
    with _lock:
        _active_backtest_runs = max(0, _active_backtest_runs - 1)


def is_frozen() -> bool:
    with _lock:
        return _active_backtest_runs > 0


def active_count() -> int:
    with _lock:
        return _active_backtest_runs


@contextmanager
def frozen_writes():
    """回测 run 的上下文管理器：进入冻结、退出解冻（异常也解冻）。"""
    acquire()
    try:
        yield
    finally:
        release()
