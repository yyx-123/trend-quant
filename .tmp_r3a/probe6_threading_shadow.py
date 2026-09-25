import sys, threading
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from core import jobs
from core import settings as settings_mod
from datetime import date

created = []

class _FakeThread:
    def __init__(self, *a, **k):
        created.append(("fake", k.get("name")))
        self._target = k.get("target")
        self._alive = False
    def start(self):
        self._alive = True
        try:
            self._target()
        finally:
            self._alive = False
    def is_alive(self):
        return self._alive

real_before = {t.name for t in threading.enumerate()}
jobs.threading = type("T", (), {"Thread": _FakeThread})   # 钉子用的打桩方式
jobs._catchup_sentinel = None
jobs.run_freeze.is_frozen = lambda: True                  # 立刻进入"仍冻结"分支
try:
    jobs._spawn_same_day_catchup(settings_mod.load_settings(), None, force=True,
                                 today=date(2026, 9, 25))
finally:
    pass
real_after = {t.name for t in threading.enumerate()}
print("fake Thread 被调用次数 =", len(created), created)
print("新增真实线程 =", sorted(real_after - real_before))
print("_catchup_sentinel 类型 =", type(jobs._catchup_sentinel).__name__,
      "name =", getattr(jobs._catchup_sentinel, "name", None))
