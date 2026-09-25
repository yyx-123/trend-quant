import threading
import time
from core import run_freeze
from core import jobs

def test_freeze_state_after_api_files():
    print("\n[diag] run_freeze.active_count =", run_freeze.active_count())
    print("[diag] is_frozen =", run_freeze.is_frozen())
    print("[diag] sentinel =", jobs._catchup_sentinel, "alive =",
          jobs._catchup_sentinel.is_alive() if jobs._catchup_sentinel else None)
    print("[diag] threads =", sorted(t.name for t in threading.enumerate()))
    import core.env as env
    print("[diag] scheduler_disabled =", env.scheduler_disabled())
    assert True
