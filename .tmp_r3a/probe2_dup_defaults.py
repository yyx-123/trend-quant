"""R3 probe 2: 重复检测的两档判定对"显式写出平台缺省值"失效。

R1-P2-3 已把 window/universe 的缺省展开（"省略 = 平台缺省"不得成为逃逸通道），
但同一逻辑对其余声明字段（initial_capital / window_mode / n_folds / expect …）
未落实——runner 侧缺省与显式写缺省得到**完全相同**的实验，入口却判"另一个实验"。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.storage.db import Database  # noqa: E402
from portfolio.seed import seed_default_library  # noqa: E402
from portfolio.slots import REGISTRY, ensure_builtins  # noqa: E402
from research import experiments, sessions, topics  # noqa: E402
from research.errors import IntakeRejected  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="r3probe2-"))
    db = Database(tmp / "t.db")
    ensure_builtins()
    versions = seed_default_library(db, REGISTRY)
    base = versions["base-v1"]
    sess = sessions.get_or_create_default_human_session(db)
    topic = topics.create_topic(db, session_id=sess["session_id"], title="T", question="q")

    diff = [{"slot": "position_risk", "to": "hard_stop@1", "params": {"atr_mul": 1.5}}]
    common = {"base": base, "diff": diff, "window": ["2016-06-01", "2024-12-31"]}

    def propose(tag: str, extra: dict):
        spec = {**common, **extra}
        try:
            exp = experiments.propose_experiment(
                db, session_id=sess["session_id"], title=tag, topic_id=topic["id"],
                evaluation_module="portfolio_backtest@1", spec=spec,
                hypothesis="假设陈述足够长用于探针", registry=REGISTRY,
            )
            print(f"[{tag}] ACCEPTED exp={exp['id']} status={exp['status']} "
                  f"attempt={exp['attempt_index']}")
            return exp
        except IntakeRejected as exc:
            print(f"[{tag}] REJECTED reasons={exc.reasons}")
            return None

    print("--- E1: 基准（省略全部平台缺省字段） ---")
    propose("E1-omitted", {})
    print("--- E2: 显式写出 runner 的缺省值（同 base/同 diff/同 window） ---")
    propose("E2-explicit-defaults", {"initial_capital": 1000000,
                                     "window_mode": "static_holdout"})
    print("--- E3: expect 缺省 vs 显式 positive（event 侧同族字段，此处仅看判定） ---")
    propose("E3-explicit-expect", {"expect": "positive"})
    print("--- E4: 真差异（window_mode 改为 walk_forward，应放行） ---")
    propose("E4-real-diff", {"window_mode": "walk_forward"})
    print("--- E5: 仅 n_folds=4（缺省值）显式 ---")
    propose("E5-explicit-n_folds", {"n_folds": 4})

    # 交叉核对：入口放行的情况下，runner 侧解析出的运行参数是否逐值相同
    from research.evaluations import backtest as bt

    print("--- 运行参数解析对照（真源） ---")
    for tag, extra in (("omitted", {}), ("explicit", {"initial_capital": 1000000,
                                                      "window_mode": "static_holdout",
                                                      "n_folds": 4})):
        spec = {**common, **extra}
        print(f"  {tag:9s} window_mode={spec.get('window_mode') or 'static_holdout'!r} "
              f"initial_capital={float(spec.get('initial_capital', 1_000_000))} "
              f"n_folds={int(spec.get('n_folds', 4) or 4)} "
              f"canonical_sig_len={len(experiments._canonical_spec(spec))}")
    print("TMP", tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
