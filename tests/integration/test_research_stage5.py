"""阶段 5：L4 正式化测试——worker 并发池 / 重复检测 / 模块治理门 /
recompute 复核 / 课题量化结论 / 课题文件夹 / head_to_head / API 服务面。

判定器 golden 对拍在 tests/unit/test_research_stats.py。
"""

from __future__ import annotations

import json
import time

import numpy as np
import pandas as pd
import pytest

from portfolio.registry import ModuleSpec, fresh_registry
from portfolio.seed import seed_default_library
from portfolio.slots import register_builtin_modules
from research import (
    experiments,
    lifecycle,
    modules,
    sessions,
    topics,
    verdict,
)
from research.api import ResearchService
from research.pipeline import run_experiment
from research.recompute import recompute_campaign
from research.worker import ResearchWorker

pytestmark = pytest.mark.integration


@pytest.fixture
def registry():
    reg = fresh_registry()
    register_builtin_modules(reg)
    return reg


def _write_symbol(db, symbol, closes, start="2022-01-03", asset_type="etf"):
    n = len(closes)
    dates = pd.bdate_range(start, periods=n)
    closes = np.asarray(closes, dtype=float)
    df = pd.DataFrame({
        "time": dates, "open": closes, "high": closes * 1.01, "low": closes * 0.99,
        "close": closes, "volume": np.full(n, 2e6), "amount": closes * 2e6 * 5,
    })
    db.save_market_data(symbol, df, price_mode="qfq")
    db.save_market_data(symbol, df, price_mode="raw")
    db.save_instrument_metadata([{
        "symbol": symbol, "name": symbol, "category_l1": "T", "category_l2": "T2",
        "category_l3": "", "enabled": 1, "asset_type": asset_type,
    }])


@pytest.fixture
def market(test_db):
    rng = np.random.default_rng(17)
    for i in range(6):
        closes = 12 * np.exp(np.cumsum(rng.normal(0.002 + i * 0.0004, 0.018, 360)))
        _write_symbol(test_db, f"W{i:03d}.SS", closes)
    return test_db


@pytest.fixture
def env(market, registry):
    versions = seed_default_library(market, registry)
    session = sessions.get_or_create_default_human_session(market)
    topic = topics.create_topic(
        market, session_id=session["session_id"], title="止损研究", question="硬止损倍数选型"
    )
    return {"db": market, "registry": registry, "versions": versions,
            "session": session, "topic": topic}


def _bt_spec(base_ref, window=("2022-06-01", "2023-12-29"), atr_mul=2.0):
    return {
        "base": base_ref,
        "diff": [{"slot": "position_risk", "from": "hard_stop@1", "to": "hard_stop@1",
                  "params": {"atr_mul": atr_mul}}],
        "window": list(window),
    }


def _run_and_confirm(db, registry, exp, session_id, final="confirmed", reasoning="验证通过"):
    run_experiment(db, exp["id"], registry=registry)
    latest = verdict.latest_verdict(db, exp["id"])
    return verdict.confirm_verdict(
        db, experiment_id=exp["id"],
        final_verdict=final if latest["suggested_verdict"] == "confirmed" else latest["suggested_verdict"],
        reasoning=reasoning, session_id=session_id,
    )


# ----------------------------------------------------------------------
# worker 并发池
# ----------------------------------------------------------------------


def test_worker_executes_queued_experiments(env):
    db, reg = env["db"], env["registry"]
    service = ResearchService(db, registry=reg, topics_dir=env["db"].db_path.parent / "topics")
    worker = ResearchWorker(db, registry=reg, max_workers=2)
    service.worker = worker
    worker.start()
    try:
        exps = [
            service.propose_experiment(
                session_id=env["session"]["session_id"], title=f"倍数 {m}",
                topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
                spec=_bt_spec(env["versions"]["base-v1"], atr_mul=m),
                hypothesis=f"硬止损放宽到 {m}×ATR 后 ΔSharpe 改善",
                allow_duplicate=True,
            )
            for m in (1.2, 1.8)
        ]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            statuses = [lifecycle.get_experiment(db, e["id"])["status"] for e in exps]
            if all(s in ("evaluating", "verdicted", "failed") for s in statuses):
                break
            time.sleep(0.5)
        statuses = [lifecycle.get_experiment(db, e["id"])["status"] for e in exps]
        assert all(s in ("evaluating", "verdicted") for s in statuses), statuses
        # 血缘：每次运行都落了 research_runs + engine_runs
        from research import runs as runs_mod

        assert runs_mod.list_runs(db, exps[0]["id"])
    finally:
        worker.stop()


def test_worker_crash_isolation(env):
    """单 run 崩溃只影响自己（failed 入表），池不死。"""
    db, reg = env["db"], env["registry"]
    worker = ResearchWorker(db, registry=reg, max_workers=2)
    worker.start()
    try:
        good = experiments.propose_experiment(
            db, session_id=env["session"]["session_id"], title="好实验",
            topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
            spec=_bt_spec(env["versions"]["base-v1"], window=("2022-06-01", "2023-06-30")),
            hypothesis="正常实验应走到 verdicted 终态",
        )
        # 窗口无数据 → runner 抛 BacktestError → 工程失败（不污染台账内容——
        # 注意：上个版本试图 SQL 直改 spec_json 制造失败，被 append-only 触发器
        # 正确拦截；工程失败必须用合法路径制造）
        bad = experiments.propose_experiment(
            db, session_id=env["session"]["session_id"], title="会炸的实验",
            topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
            spec={"base": env["versions"]["base-v1"],
                  "diff": [{"slot": "position_risk", "to": "hard_stop@1", "params": {"atr_mul": 1.0}}],
                  "window": ["1990-01-01", "1990-12-31"]},
            hypothesis="窗口无数据会工程失败", allow_duplicate=True,
        )
        worker.submit(good["id"])
        worker.submit(bad["id"])

        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            gs = lifecycle.get_experiment(db, good["id"])["status"]
            bs = lifecycle.get_experiment(db, bad["id"])["status"]
            if gs in ("evaluating", "verdicted") and bs == "failed":
                break
            time.sleep(0.5)
        assert lifecycle.get_experiment(db, good["id"])["status"] in ("evaluating", "verdicted")
        assert lifecycle.get_experiment(db, bad["id"])["status"] == "failed"
    finally:
        worker.stop()


# ----------------------------------------------------------------------
# 重复检测（§6.6.6）
# ----------------------------------------------------------------------


def test_duplicate_detection(env):
    db, reg = env["db"], env["registry"]
    spec = _bt_spec(env["versions"]["base-v1"], atr_mul=2.0)
    first = experiments.propose_experiment(
        db, session_id=env["session"]["session_id"], title="首次",
        topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
        spec=spec, hypothesis="第一次提出这个止损宽度假设",
    )
    assert first["status"] == "queued"
    with pytest.raises(Exception) as err:
        experiments.propose_experiment(
            db, session_id=env["session"]["session_id"], title="重复",
            topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
            spec=spec, hypothesis="重复发现应被拦截并列出历史实验",
        )
    assert "duplicate_of" in str(err.value)
    assert first["id"] in str(err.value)
    # 两档语义（DS-R2 P2）：完全一致 → 硬拒（allow_duplicate 也不救）
    from research.errors import IntakeRejected

    with pytest.raises(IntakeRejected, match="完全重复，硬拒"):
        experiments.propose_experiment(
            db, session_id=env["session"]["session_id"], title="完全一致还硬提",
            topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
            spec=spec, hypothesis="完全一致 allow_duplicate 也不救", allow_duplicate=True,
        )
    # 相似（数值在容差内）→ 须显式确认后放行
    similar_spec = _bt_spec(env["versions"]["base-v1"], atr_mul=1.9)
    with pytest.raises(IntakeRejected, match="similar_to"):
        experiments.propose_experiment(
            db, session_id=env["session"]["session_id"], title="相似未确认",
            topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
            spec=similar_spec, hypothesis="相似实验未确认应被拦下",
        )
    second = experiments.propose_experiment(
        db, session_id=env["session"]["session_id"], title="相似确认后重提",
        topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
        spec=similar_spec, hypothesis="相似实验显式确认后放行的陈述", allow_duplicate=True,
    )
    assert second["status"] == "queued"
    assert second["attempt_index"] == 2  # 尝试计数沿研究线递增


# ----------------------------------------------------------------------
# 模块治理（决策 16）：DSL 免测 + python 门 + 前缀稳定性抓前视
# ----------------------------------------------------------------------


def test_dsl_module_fast_path(env):
    db, reg = env["db"], env["registry"]
    draft = modules.propose_module(
        db, session_id=env["session"]["session_id"], slot="signal",
        name="dsl_ma20_cross", version=1, kind="dsl",
        source="cross_above(close, sma(close, 20))",
        params_schema={},
    )
    assert draft["status"] == "reviewed"  # DSL 免测
    n = modules.load_reviewed_modules(db, reg)
    assert n >= 1
    spec = reg.get("dsl_ma20_cross@1", slot="signal")
    assert spec is not None and spec.kind == "dsl"


def test_dsl_rejects_lookahead_ref(env):
    draft = modules.propose_module(
        env["db"], session_id=env["session"]["session_id"], slot="signal",
        name="peek_tomorrow", version=1, kind="dsl",
        source="cross_above(close, ref(close, -1))",  # 偷看明天
    )
    assert draft["status"] == "rejected"
    assert "lookahead" in (draft["reject_reason"] or "")


def test_python_gate_catches_lookahead(env):
    """前缀稳定性探针：用未来数据的模块（全史最大价判定）必须被拒。"""
    lookahead_src = '''
class Module:
    def __init__(self, params):
        self._max = None
    def prepare(self, panel):
        import numpy as np
        self._max = np.nanmax(panel.data["close"], axis=0)  # 未来函数：全史最高
    def scan(self, ctx, members):
        from portfolio.slots.signal import SignalEvent
        out = []
        for m in members:
            col = ctx.panel.symbol_col(m.symbol)
            if col is None or self._max is None:
                continue
            cur = ctx.panel.value(m.symbol, "close")
            if cur is not None and cur >= self._max[col] - 1e-9:
                out.append(SignalEvent(symbol=m.symbol, kind="entry", date=ctx.date, meta={}))
        return out
'''
    draft = modules.propose_module(
        env["db"], session_id=env["session"]["session_id"], slot="signal",
        name="lookahead_max", version=1, kind="python", source=lookahead_src,
    )
    assert draft["status"] == "rejected"
    assert "prefix_stability" in json.dumps(draft["test_report"])


def test_python_gate_accepts_clean_module(env):
    clean_src = """import numpy as np


class Module:
    def __init__(self, params):
        self.n = int(params.get("n", 20))

    def prepare(self, panel):
        close = panel.data["close"]
        prev = np.vstack([np.full((20, close.shape[1]), np.nan), close[:-20]])
        self._mom = close / prev - 1.0  # 因果：20 日动量

    def scan(self, ctx, members):
        from portfolio.slots.signal import SignalEvent

        out = []
        t = ctx.panel.upto
        for m in members:
            col = ctx.panel.symbol_col(m.symbol)
            if col is None:
                continue
            v = self._mom[t, col]
            if np.isfinite(v) and v > 0.05:
                out.append(SignalEvent(symbol=m.symbol, kind="entry", date=ctx.date, meta={}))
        return out
"""
    draft = modules.propose_module(
        env["db"], session_id=env["session"]["session_id"], slot="signal",
        name="mom20_clean", version=1, kind="python", source=clean_src,
    )
    assert draft["status"] == "reviewed", draft["test_report"]


def test_retire_requires_human(env):
    db = env["db"]
    ai = sessions.register_session(db, "ai", label="bot", channel="cli")
    draft = modules.propose_module(
        db, session_id=env["session"]["session_id"], slot="signal",
        name="dsl_x", version=1, kind="dsl", source="cross_above(close, sma(close, 10))",
    )
    from research.errors import PermissionDenied

    with pytest.raises(PermissionDenied, match="human session"):
        modules.retire_module(db, draft_id=draft["id"], session_id=ai["session_id"])
    retired = modules.retire_module(db, draft_id=draft["id"],
                                    session_id=env["session"]["session_id"])
    assert retired["status"] == "retired"


# ----------------------------------------------------------------------
# recompute campaign（§6.6.7）
# ----------------------------------------------------------------------


def test_recompute_campaign(env):
    db, reg = env["db"], env["registry"]
    exp = experiments.propose_experiment(
        db, session_id=env["session"]["session_id"], title="旧模块实验",
        topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
        spec=_bt_spec(env["versions"]["base-v1"], atr_mul=1.5),
        hypothesis="硬止损 1.5 基准的复核实验", allow_duplicate=True,
    )
    _run_and_confirm(db, reg, exp, env["session"]["session_id"])
    v0 = verdict.latest_verdict(db, exp["id"])

    # 注册 hard_stop@2（同公式的"修正版"占位——换 atr_period 默认值的兄弟实现）
    from portfolio.slots.position_risk import HardStopModule

    reg.register(ModuleSpec(slot="position_risk", name="hard_stop", version=2,
                            factory=HardStopModule, kind="builtin",
                            params_schema={
                                "atr_mul": {"type": "number", "default": 1.5, "min": 0.1, "max": 10},
                                "atr_period": {"type": "integer", "default": 20, "min": 5, "max": 120},
                            },
                            description="v2 占位"), replace=False)

    result = recompute_campaign(db, old_module_ref="hard_stop@1",
                                new_module_ref="hard_stop@2", registry=reg)
    assert len(result["recomputed"]) >= 1
    # 定论不被复核稿劫持（K3-P2-6/DS-P2-3）：latest_verdict 优先
    # supersedes IS NULL 的原生确认链
    v1 = verdict.latest_verdict(db, exp["id"])
    assert v1["id"] == v0["id"]  # 定论仍是原 verdict
    # 复核稿存在且带 supersedes 指针（指针只写在新记录上）
    verdicts = verdict.list_verdicts(db, exp["id"])
    assert len(verdicts) == 2
    recomputed = [v for v in verdicts if v["supersedes"] == exp["id"]]
    assert len(recomputed) == 1
    assert recomputed[0]["id"] != v0["id"]
    # 旧 verdict 一行未改
    old = verdict.get_verdict(db, v0["id"])
    assert old["evidence"] == v0["evidence"]


# ----------------------------------------------------------------------
# 课题量化结论（决策 20）+ 课题文件夹（决策 17）
# ----------------------------------------------------------------------


def test_quantified_conclusion_and_grade_enforcement(env, tmp_path):
    db, reg = env["db"], env["registry"]
    # 两个实验：一个 confirmed（人工落定）、一个 rejected
    for title, final in (("成立实验", "confirmed"), ("被否实验", "rejected")):
        exp = experiments.propose_experiment(
            db, session_id=env["session"]["session_id"], title=title,
            topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
            spec=_bt_spec(env["versions"]["base-v1"], atr_mul=1.3 if "成立" in title else 3.0),
            hypothesis=f"{title}的假设陈述：止损宽度影响收益结构", allow_duplicate=True,
        )
        run_experiment(db, exp["id"], registry=reg)
        latest = verdict.latest_verdict(db, exp["id"])
        # 人工可降不可升——测试里选择与建议一致或降级
        chosen = final if final == "rejected" else latest["suggested_verdict"]
        if chosen not in ("confirmed", "rejected", "inconclusive"):
            chosen = "inconclusive"
        verdict.confirm_verdict(
            db, experiment_id=exp["id"], final_verdict=chosen,
            reasoning=f"{title}落定", session_id=env["session"]["session_id"],
        )

    service = ResearchService(db, registry=reg, topics_dir=tmp_path / "topics")
    topic = topics.get_topic(db, env["topic"]["id"])
    done = service.conclude_topic(
        topic_id=topic["id"], conclusion="两实验一成立一被否",
        session_id=env["session"]["session_id"],
    )
    assert done["status"] == "concluded"
    summary = json.loads(done["conclusion_summary_json"])
    assert summary["verdict_counts"]
    assert "suggested_grade" in summary
    assert summary["note"].find("不做跨实验 p 值合并") >= 0

    # 课题文件夹物化（决策 17）
    topic_dir = tmp_path / "topics"
    folders = list(topic_dir.glob(f"{topic['id']}_*"))
    assert folders, "课题文件夹应已物化"
    topic_md = folders[0] / "TOPIC.md"
    assert topic_md.exists()
    content = topic_md.read_text(encoding="utf-8")
    assert "平台量化摘要" in content
    exp_dirs = list((folders[0] / "experiments").glob("E*"))
    assert len(exp_dirs) == 2
    manifest = json.loads((exp_dirs[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["spec"] and manifest["engine_runs"]
    assert manifest["data_version"] is not None


def test_grade_upgrade_blocked(env):
    """平台建议可降不可升：空课题建议 insufficient-evidence，写 supported 被拒。"""
    db = env["db"]
    empty_topic = topics.create_topic(
        db, session_id=env["session"]["session_id"], title="空课题", question="无实验"
    )
    from research.errors import TopicError

    with pytest.raises(TopicError, match="exceeds platform suggestion"):
        topics.conclude_topic(
            db, topic_id=empty_topic["id"], conclusion="强行 supported",
            grade="supported",
        )


# ----------------------------------------------------------------------
# head_to_head（决策 E5）
# ----------------------------------------------------------------------


def test_head_to_head(env):
    db, reg = env["db"], env["registry"]
    from portfolio import library

    for line, symbols in (("h2h-a", ["W000.SS", "W002.SS", "W004.SS"]),
                          ("h2h-b", ["W001.SS", "W003.SS", "W005.SS"])):
        library.ensure_strategy(db, line, name=line)
        library.add_version_yaml(
            db, line,
            f"""
name: {line}
universe: {{module: static_list@1, params: {{symbols: {symbols}}}}}
signal: {{module: always_entry@1, params: {{}}}}
rank: {{module: by_freshness@1, params: {{}}}}
sizing: {{module: fixed_slots@1, params: {{slots: 3}}}}
portfolio_risk: []
position_risk: {{module: hard_stop@1, params: {{atr_mul: 2.0}}}}
execution: {{module: tail_session@1, params: {{slippage_base: 0.002, slippage_tail: 0.001}}}}
""",
            reg, created_by="human",
        )
    va = library.latest_version(db, "h2h-a")
    vb = library.latest_version(db, "h2h-b")
    exp = experiments.propose_experiment(
        db, session_id=env["session"]["session_id"], title="A vs B",
        topic_id=env["topic"]["id"], evaluation_module="head_to_head@1",
        spec={"base": va["id"], "ref": vb["id"], "window": ["2022-06-01", "2023-12-29"]},
        hypothesis="A 线相对 B 线有显著的 ΔSharpe 优势",
    )
    assert exp["status"] == "queued"
    v = run_experiment(db, exp["id"], registry=reg)
    # 钉死期望判定（合成固定种子数据上的回归锚，不是枚举重言式）：
    # 两条同族策略线的微小差异 → 配对带跨零 → inconclusive
    assert v["suggested_verdict"] == "inconclusive"
    assert v["baseline"]["kind"] == "strategy"
    assert v["evidence"]["paired"]["n_days"] > 100
    assert v["evidence"]["paired"]["delta_sharpe_band"] is not None


def test_append_experiment_to_topic(env):
    """服务面 §6.7：queued 实验可改挂课题；开跑后冻结。"""
    db, reg = env["db"], env["registry"]
    service = ResearchService(db, registry=reg, topics_dir=env["db"].db_path.parent / "t2")
    t2 = topics.create_topic(
        db, session_id=env["session"]["session_id"], title="新课题", question="归属修正目标"
    )
    exp = experiments.propose_experiment(
        db, session_id=env["session"]["session_id"], title="待移动实验",
        topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
        spec=_bt_spec(env["versions"]["base-v1"], atr_mul=1.4),
        hypothesis="移动归属的实验假设陈述", allow_duplicate=True,
    )
    moved = service.append_experiment_to_topic(
        experiment_id=exp["id"], topic_id=t2["id"],
        session_id=env["session"]["session_id"],
    )
    assert moved["topic_id"] == t2["id"]
    # 开跑后冻结（状态机外的归属冻结）
    run_experiment(db, exp["id"], registry=reg)
    from research.errors import ResearchError

    with pytest.raises(ResearchError, match="only queued"):
        service.append_experiment_to_topic(
            experiment_id=exp["id"], topic_id=env["topic"]["id"],
            session_id=env["session"]["session_id"],
        )
