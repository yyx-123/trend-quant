"""有界并发池（详设 §2.6 决策 4/§6.7 并发隔离）。

- 并发位 2~4（按机器资源配置；防的是内存/CPU 挤爆，不是防冲突——
  并发安全靠只读锚定 + append-only 命名空间 + 无共享可变状态）；
- FIFO：按实验 id 顺序出队；每会话并发上限防霸占；
- 单 run 崩溃只影响自己（failed 入表），不拖垮池；
- 全部运行经 run_freeze 冻结写任务（决策 A3）。
- 池线程 daemon（GLM53F-P2-14）：进程退出不等在跑 run（与存量批量
  回测 worker 同口径）；调度循环异常守卫（GLM53F-P2-9）：瞬时 SQLite
  busy 不杀死调度线程。
"""

from __future__ import annotations

import concurrent.futures.thread as _cf_thread
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from queue import Empty, Queue

from audit.app_logger import get_logger
from core import run_freeze
from research import lifecycle
from research.pipeline import run_experiment

_logger = get_logger(__name__)


class _DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """daemon 工作线程的 ThreadPoolExecutor（GLM53F-P2-14）。

    标准库池线程非 daemon，解释器退出会 join 在跑 run（重启被长回测拖住）。
    仅覆写 _adjust_thread_count 加 daemon=True；stdlib 内部结构变动时
    由调用处回退到普通池（见 ResearchWorker.start）。
    """

    def _adjust_thread_count(self) -> None:
        if self._idle_semaphore.acquire(timeout=0):
            return

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            thread_name = f"{self._thread_name_prefix or self}_{num_threads}"
            t = threading.Thread(
                name=thread_name, target=_cf_thread._worker, daemon=True,
                args=(weakref.ref(self, weakref_cb),
                      self._work_queue, self._initializer, self._initargs),
            )
            t.start()
            self._threads.add(t)
            _cf_thread._threads_queues[t] = self._work_queue


class ResearchWorker:
    """进程内研究 worker：从台账 queued 队列取实验执行（FIFO）。"""

    def __init__(self, db, *, registry, max_workers: int = 2, per_session_cap: int = 1) -> None:
        self.db = db
        self.registry = registry
        self.max_workers = max(1, min(int(max_workers), 4))
        self.per_session_cap = max(1, int(per_session_cap))
        self._queue: Queue[str] = Queue()
        self._queued_ids: set[str] = set()
        self._lock = threading.Lock()
        self._pool: ThreadPoolExecutor | None = None
        self._stop = threading.Event()
        self._dispatcher: threading.Thread | None = None
        self._active_by_session: dict[str, int] = {}
        # 已派发但未完成的 future → (experiment_id, owner)：stop() 据此回灌队列
        self._inflight: dict = {}
        self._dispatch_iterations = 0  # 调度循环计数（热自旋回归钉子的观测点）

    # ------------------------------------------------------------------
    def submit(self, experiment_id: str) -> bool:
        """入队（幂等：重复提交直接忽略）。"""
        with self._lock:
            if experiment_id in self._queued_ids:
                return False
            exp = lifecycle.get_experiment(self.db, experiment_id)
            if exp is None or exp["status"] != "queued":
                return False
            self._queued_ids.add(experiment_id)
            self._queue.put(experiment_id)
            return True

    def submit_all_queued(self) -> int:
        n = 0
        for exp in lifecycle.list_experiments(self.db, status="queued"):
            if self.submit(exp["id"]):
                n += 1
        return n

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._dispatcher is not None:
            # P3-4（R4A 复核）：join 超时后引用被保留——若旧线程仍活着，绝不能
            # 再起第二个调度线程（会共享 _queue/_queued_ids/_active_by_session）。
            # 旧线程**已经退出**时必须清引用并允许重启（否则本进程内
            # 再也起不来）。
            if getattr(self._dispatcher, "is_alive", lambda: False)():
                _logger.warning("research worker dispatcher still alive; start() ignored")
                return
            self._dispatcher = None
        swept = lifecycle.mark_interrupted_research_runs(self.db)
        if swept["experiments"] or swept["engine_runs"]:
            _logger.warning("startup sweep: %s experiments, %s engine runs marked failed",
                            swept["experiments"], swept["engine_runs"])
        try:
            self._pool = _DaemonThreadPoolExecutor(
                max_workers=self.max_workers, thread_name_prefix="research"
            )
        except Exception:  # stdlib 私有钩子变动的兜底：退回普通池（功能不破）
            _logger.warning("daemon pool unavailable, falling back to ThreadPoolExecutor")
            self._pool = ThreadPoolExecutor(
                max_workers=self.max_workers, thread_name_prefix="research"
            )
        self._stop.clear()
        self._dispatcher = threading.Thread(target=self._dispatch_loop, daemon=True, name="research-dispatch")
        self._dispatcher.start()

    def stop(self) -> None:
        """停止调度与线程池（修正）。

        - **P3-3**：`cancel_futures=True` 会取消"已派发未开跑"的 future，而这些
          id 在派发时已从 `_queued_ids` 摘除、减计数只在 `_run_one` 里——直接
          shutdown 会**丢派发**（实验永远停在 queued、worker 内无痕）并**泄漏
          会话计数**（泄漏到 per_session_cap 后该会话的实验永不执行）。现在先
          把被取消的 future 对应实验放回队列并回退计数，再关闭线程池。
        - **P3-4**：`join(timeout=5)` 超时后不得直接置空 `_dispatcher`——旧线程
          仍活着，再 `start()` 会起第二个调度线程共享同一状态。改为保留引用。
        """
        self._stop.set()
        if self._dispatcher:
            join = getattr(self._dispatcher, "join", None)
            if callable(join):
                join(timeout=5)
            if getattr(self._dispatcher, "is_alive", lambda: False)():
                _logger.warning(
                    "research worker dispatcher still alive after join timeout; "
                    "start() will be refused until it exits"
                )
            else:
                self._dispatcher = None
        # 把原始队列里尚未消费的 id 并回 `_queued_ids`（dispatcher 退出
        # 时可能有 id 只在 `_queue` 里而不在集合里 → 之后 `status()`/重派都会漏它）
        with self._lock:
            # 先把队列里剩余的 id 全部取出（**再**统一放回：边取边放会自旋），
            # 保证"集合里有它"与"队列里有它"始终一致
            pending_ids: list[str] = []
            while True:
                try:
                    pending_ids.append(self._queue.get_nowait())
                except Exception:
                    break
            for pending_id in pending_ids:
                self._queued_ids.add(pending_id)
                self._queue.put(pending_id)
        if self._pool:
            pool = self._pool
            self._pool = None
            with self._lock:
                for fut, (exp_id, owner) in list(self._inflight.items()):
                    if fut.done() or fut.cancelled():
                        self._inflight.pop(fut, None)
                        continue
                    if getattr(fut, "running", lambda: False)():
                        continue  # 在跑：由 _run_one 的 finally 收口（含减计数）
                    # 未开跑：取消并回灌队列 + 回退计数；`cancel()` 也可能因竞态
                    # 返回 False（判不出状态时保守同样回灌，绝不静默丢）
                    fut.cancel()
                    self._queued_ids.add(exp_id)
                    self._queue.put(exp_id)  # 回灌必须落回队列
                    self._active_by_session[owner] = max(
                        0, self._active_by_session.get(owner, 1) - 1
                    )
                    self._inflight.pop(fut, None)
            pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------
    def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                experiment_id = self._queue.get(timeout=0.5)
            except Empty:
                self._dispatch_iterations += 1
                continue
            try:
                exp = lifecycle.get_experiment(self.db, experiment_id)
            except Exception:
                # GLM53F-P2-9：循环体异常守卫——一次瞬时 SQLite busy 不得
                # 杀死调度线程（否则队列永久卡死且无告警）
                _logger.exception("dispatch probe failed for %s", experiment_id)
                with self._lock:
                    self._queued_ids.discard(experiment_id)
                self._stop.wait(0.5)
                continue
            if exp is None or exp["status"] != "queued":
                with self._lock:
                    self._queued_ids.discard(experiment_id)
                continue
            owner = exp["owner_session"]
            with self._lock:
                if self._active_by_session.get(owner, 0) >= self.per_session_cap:
                    # 会话霸占防护：放回队尾——但立刻 requeue 会让队列里只有
                    # 同会话实验时调度器紧循环热自旋（评审 DS-P1-4 实测 576/s），
                    # 故在锁外退避后再续循环。
                    self._queue.put(experiment_id)
                    capped = True
                else:
                    capped = False
                if not capped:
                    self._active_by_session[owner] = self._active_by_session.get(owner, 0) + 1
                    self._queued_ids.discard(experiment_id)
            if capped:
                self._dispatch_iterations += 1
                self._stop.wait(0.5)
                continue
            if self._pool is None:
                # stop() 已置空池（join 超时路径）——dispatcher 优雅退出，
                # 不再裸 assert 崩线程。退出前必须把已消费的 id 还回去
                # 并回退会话计数（否则该实验既不在队列也不在跑，计数永久泄漏）。
                with self._lock:
                    self._queued_ids.add(experiment_id)
                    self._queue.put(experiment_id)  # 回灌必须落回队列
                    self._active_by_session[owner] = max(
                        0, self._active_by_session.get(owner, 1) - 1
                    )
                break
            fut = self._pool.submit(self._run_one, experiment_id, owner)
            with self._lock:
                self._inflight[fut] = (experiment_id, owner)

    def _run_one(self, experiment_id: str, owner: str) -> None:
        try:
            with run_freeze.frozen_writes():
                run_experiment(self.db, experiment_id, registry=self.registry)
        except Exception:
            _logger.exception("worker crashed on %s", experiment_id)
            try:
                exp = lifecycle.get_experiment(self.db, experiment_id)
                # GLM53F-P2-10：只收"崩溃在转移前"（仍为 queued）的孤儿——
                # running 态可能是另一通道的在途 run，不得代为判死
                # （跨进程互杀的收敛以单实例运行约定兜底，见开发日志）
                if exp is not None and exp["status"] == "queued":
                    lifecycle.transition(self.db, experiment_id, "failed", error="worker crash")
            except Exception:
                _logger.exception("failed to mark %s failed", experiment_id)
        finally:
            with self._lock:
                self._active_by_session[owner] = max(0, self._active_by_session.get(owner, 1) - 1)
                # 完成即从在途表摘除（stop() 只处理"未开跑被取消"的那批）
                for _fut, _info in list(self._inflight.items()):
                    if _info == (experiment_id, owner):
                        del self._inflight[_fut]
                        break

    # ------------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            return {
                "max_workers": self.max_workers,
                "queued": self._queue.qsize(),
                "active_by_session": dict(self._active_by_session),
                "frozen_writes": run_freeze.is_frozen(),
            }
