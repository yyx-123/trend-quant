"""R3 probe 9: 策略线退役语义（零测试路径）+ 并发写版本号。

(a) retire_strategy 重复调用是否改写历史标记 retired_at（触发器未保护该列）；
(b) 退役线是否仍可被**新实验**引用（resolve_experiment_config 不看 retired_at）；
(c) 两个并发 add_version（不同 config）在同一策略线上的结果（是否落到裸 sqlite 异常）。
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.storage.db import Database  # noqa: E402
from portfolio import library  # noqa: E402
from portfolio.service import resolve_experiment_config  # noqa: E402
from portfolio.slots import REGISTRY, ensure_builtins  # noqa: E402
from portfolio.strategy import parse_strategy_yaml  # noqa: E402
from research import experiments, sessions, topics  # noqa: E402


def _cfg(slot_module: str, atr: float) -> str:
    return (
        "name: probe-line\n"
        "universe: {module: category_filter@1, params: {asset_type: all}}\n"
        "signal: {module: macd_cross@1, params: {}}\n"
        "rank: {module: by_freshness@1, params: {}}\n"
        "sizing: {module: equal_risk@1, params: {risk_budget_pct: 0.0075}}\n"
        "portfolio_risk: []\n"
        f"position_risk: {{module: hard_stop@1, params: {{atr_mul: {atr}}}}}\n"
        "execution: {module: tail_session@1, params: {}}\n"
    )


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="r3probe9-"))
    db = Database(tmp / "t.db")
    ensure_builtins()

    print("=== (a) retire 幂等性 ===")
    library.ensure_strategy(db, "probe-line", name="probe")
    v1 = library.add_version_yaml(db, "probe-line", _cfg("x", 1.5), REGISTRY)
    r1 = library.retire_strategy(db, "probe-line")
    time.sleep(1.1)
    r2 = library.retire_strategy(db, "probe-line")
    print(f"  第一次 retired_at = {r1['retired_at']}")
    print(f"  第二次 retired_at = {r2['retired_at']}")
    print(f"  历史标记被改写 = {r1['retired_at'] != r2['retired_at']}")
    try:
        library.add_version_yaml(db, "probe-line", _cfg("x", 2.0), REGISTRY)
        print("  退役后 add_version = ALLOWED（守卫失效）")
    except library.LibraryError as exc:
        print(f"  退役后 add_version = BLOCKED: {exc}")
    print("  list_strategies(默认) 含 retired 行 =",
          any(s["id"] == "probe-line" for s in library.list_strategies(db)),
          "| include_retired =",
          any(s["id"] == "probe-line" for s in library.list_strategies(db, include_retired=True)))

    print("=== (b) 退役线能否被新实验引用 ===")
    cfg = parse_strategy_yaml(v1["config_yaml"], REGISTRY)
    resolved, _y = resolve_experiment_config(
        db, base_version_id=v1["id"], diff=[], registry=REGISTRY,
    )
    print("  resolve_experiment_config(退役线版本) = ALLOWED（config_hash=%s）"
          % cfg.config_hash()[:8])
    s = sessions.get_or_create_default_human_session(db)
    t = topics.create_topic(db, session_id=s["session_id"], title="T", question="q")
    exp = experiments.propose_experiment(
        db, session_id=s["session_id"], title="退役线实验", topic_id=t["id"],
        evaluation_module="portfolio_backtest@1",
        spec={"base": v1["id"], "diff": [{"slot": "position_risk", "to": "hard_stop@1",
                                          "params": {"atr_mul": 2.0}}]},
        hypothesis="假设陈述足够长用于探针", registry=REGISTRY,
    )
    print(f"  在退役线上提实验 = {exp['status']}（{exp['id']}）")

    print("=== (c) 并发 add_version（不同 config，同一条线）===")
    library.ensure_strategy(db, "race-line", name="race")
    results: dict = {}

    def worker(tag: str, atr: float) -> None:
        try:
            row = library.add_version_yaml(db, "race-line", _cfg(tag, atr), REGISTRY)
            results[tag] = ("ok", row["id"], row["version"])
        except Exception as exc:  # noqa: BLE001
            results[tag] = (type(exc).__name__, str(exc)[:120])

    ts = [threading.Thread(target=worker, args=(t, a)) for t, a in (("A", 1.5), ("B", 2.0), ("C", 2.5))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    for k in ("A", "B", "C"):
        print(f"  {k}: {results.get(k)}")
    print("  最终版本行 =", [(v["id"], v["version"]) for v in library.list_versions(db, "race-line")])
    print("TMP", tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
