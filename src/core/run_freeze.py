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

import os
import threading
import time
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


# ----------------------------------------------------------------------
# 跨进程哨兵（R13A-F2）：进程内计数器对**独立 CLI 进程**发起的批次不可见，
# 于是 app 进程的 16:30 日更不会被顺延——批次（37~44 分钟、逐格惰性读行情）
# 跨 16:30 时仍会读到两版 qfq。这里用一个文件哨兵补上进程间可见性：
# `is_frozen()` 同时看计数器与未见期的哨兵文件；超过 _STALE_SECONDS 的文件
# 视为崩溃残留（批次最长 44 分钟，6 小时足够宽松），不会造成永久冻结。
# ----------------------------------------------------------------------
_STALE_SECONDS = 6 * 3600
_FREEZE_FILE_ENV = "TREND_QUANT_FREEZE_FILE"


def freeze_file_path():
    """哨兵文件路径（测试可用 TREND_QUANT_FREEZE_FILE 注入）。"""
    import os
    from pathlib import Path

    override = os.environ.get(_FREEZE_FILE_ENV)
    if override:
        return Path(override)
    from core.paths import data_dir

    return data_dir() / "run_freeze.lock"


def cross_process_frozen(path=None):
    """跨进程冻结上下文：写哨兵文件，退出时删除（异常也删）。"""
    from contextlib import contextmanager as _cm

    @_cm
    def _ctx():
        target = path or freeze_file_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.write_text(
                f"{os.getpid()} {time.time():.0f}\n", encoding="utf-8"
            )
        except OSError:
            # 哨兵写不进去（磁盘/权限）不应让批次起不来：降级为进程内冻结
            yield
            return
        try:
            yield
        finally:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass

    return _ctx()


def file_frozen(path=None) -> bool:
    """哨兵文件是否存在且**未过期**（过期=崩溃残留，按未冻结处理）。"""
    target = path or freeze_file_path()
    try:
        stat = target.stat()
    except OSError:
        return False
    return (time.time() - stat.st_mtime) < _STALE_SECONDS


def is_frozen_anywhere(path=None) -> bool:
    """进程内计数或跨进程哨兵任一为真 → 写任务应顺延。"""
    return is_frozen() or file_frozen(path)
