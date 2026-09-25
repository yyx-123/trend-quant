"""Round 22 修复钉子。

覆盖第 22 轮两个独立代理（R22A 前端数字面 / R22B 通道与台账面）抓到的材质性问题：

- R22B-F1（P1）：并发下发时 `attempt_index` 撞号 → DSR/配对检验的试验次数退化
  （`n_trials<=1` 直接给 PSR(0)，多重检验校正静默失效），且该列 append-only 事后不可修。
- R22A-F1（P2）：同一个"交易数"标签在成交笔数/平仓回合两种口径间混用。
- R22A-F2（P2）：L4 组合报告的 summary 交易类字段全 0，而同载荷内有真值。
- R22B-F3（P2）：sqlite 约束冲突（业务结果）被 MCP 报成 internal error。
- R22B-F4（P2）：MCP 通道没有 holdout 通路（fail-closed 但空烧一次试次）。
"""

from __future__ import annotations

import threading

import numpy as np
import pandas as pd
import pytest

from portfolio.registry import fresh_registry
from portfolio.seed import seed_default_library
from portfolio.slots import register_builtin_modules
from research import (
    experiments,
    sessions,
    topics,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def registry():
    reg = fresh_registry()
    register_builtin_modules(reg)
    return reg


def _write_symbol(db, symbol, closes, start="2022-01-03", asset_type="etf"):
    n = len(closes)
    dates = pd.bdate_range(start, periods=n)
    arr = np.asarray(closes, dtype=float)
    df = pd.DataFrame({
        "time": dates, "open": arr, "high": arr * 1.01, "low": arr * 0.99,
        "close": arr, "volume": np.full(n, 2e6), "amount": arr * 2e6 * 5,
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
    for i in range(3):
        closes = 12 * np.exp(np.cumsum(rng.normal(0.002, 0.018, 360)))
        _write_symbol(test_db, f"W{i:03d}.SS", closes)
    return test_db


def _env(db, registry):
    versions = seed_default_library(db, registry)
    session = sessions.get_or_create_default_human_session(db)
    topic = topics.create_topic(
        db, session_id=session["session_id"], title="并发试次号", question="试次号唯一性"
    )
    return {"versions": versions, "session": session, "topic": topic}


def _spec(base_ref, atr_mul: float):
    return {
        "base": base_ref,
        "diff": [{"slot": "position_risk", "from": "hard_stop@1", "to": "hard_stop@1",
                  "params": {"atr_mul": atr_mul}}],
        "window": ["2022-06-01", "2023-06-01"],
    }


def test_concurrent_proposals_get_distinct_attempt_index(market, registry):
    """R22B-F1（P1）：并发下发（MCP 并行 tool call / 双击 / shell 并行）必须拿到互不相同的试次号。

    修复前读 COUNT 与 INSERT 分属两个连接：6 线程实测**全部**拿到 `attempt_index=1`
    → 该研究线的 DSR 试验次数退化为 1（`stats/psr.py` 里 `n_trials<=1` → PSR(0)，
    多重检验折扣完全消失），而 `attempt_index` 在 append-only 保护列内事后改不动。
    """
    env = _env(market, registry)
    base_ref = env["versions"]["base-v1"]
    topic_id = env["topic"]["id"]
    session_id = env["session"]["session_id"]

    out: list[int] = []
    errors: list[str] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            exp = experiments.propose_experiment(
                market, session_id=session_id, title=f"并发探针{i}", topic_id=topic_id,
                evaluation_module="portfolio_backtest@1", spec=_spec(base_ref, 1.5 + 0.5 * i),
                hypothesis="并发下发的试次号必须唯一（R22B-F1 钉子）",
                registry=registry, allow_duplicate=True,
            )
            with lock:
                out.append(exp["attempt_index"])
        except Exception as exc:  # 钉子要把失败原样带出来
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"并发下发不应失败：{errors}"
    assert sorted(out) == list(range(1, len(out) + 1)), (
        f"试次号撞号（DSR 试验次数失真）：{sorted(out)}"
    )

    # 库里也确实唯一（唯一索引兜底 + 分配正确性的双向确认）
    with market.connect() as conn:
        rows = conn.execute(
            """SELECT attempt_index, COUNT(*) c FROM research_experiments
               WHERE is_reproduction = 0 AND status <> 'rejected_intake'
               GROUP BY attempt_index HAVING c > 1"""
        ).fetchall()
    assert not rows, f"库内出现重复试次号：{[tuple(r) for r in rows]}"


def test_unique_index_blocks_duplicate_trial_number(market, registry):
    """R22B-F1（兜底面）：唯一索引必须把"同研究线同试次号"挡在库外。"""
    env = _env(market, registry)
    base_ref = env["versions"]["base-v1"]
    exp = experiments.propose_experiment(
        market, session_id=env["session"]["session_id"], title="先占号", topic_id=env["topic"]["id"],
        evaluation_module="portfolio_backtest@1", spec=_spec(base_ref, 2.0),
        hypothesis="占一个试次号以便测试唯一索引", registry=registry,
    )
    with market.connect() as conn:
        row = conn.execute(
            "SELECT subject_key FROM research_experiments WHERE id = ?", (exp["id"],)
        ).fetchone()
        import sqlite3

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO research_experiments
                   (id, title, owner_session, created_by, topic_id, evaluation_module,
                    subject_key, spec_json, hypothesis, status, attempt_index)
                   VALUES ('E9999', '撞号', ?, 'ai', ?, 'portfolio_backtest@1', ?, '{}', '撞号测试',
                           'queued', ?)""",
                (env["session"]["session_id"], env["topic"]["id"], row["subject_key"],
                 exp["attempt_index"]),
            )


# --------------------------------------------------------------------------
# R22A-F2（P2）：L4 组合报告的 summary 交易类字段曾全 0（同载荷内有真值）
# --------------------------------------------------------------------------


def test_report_summary_trade_fields_are_not_zero(market, registry):
    """R22A-F2：落库报告的 `summary` 交易类字段必须来自真实成交/回合。

    修复前 `build_report` 固定 `trades=[]` 喂 `compute_summary` → 报告印着
    "0 笔交易 / 0 费用"，而同载荷 `cost.total_fees` 是真值（实测 309.06 元），
    读者会据此推翻成本/换手结论；库内已有 6 份这种产物。
    """
    from research.pipeline import run_experiment

    env = _env(market, registry)
    exp = experiments.propose_experiment(
        market, session_id=env["session"]["session_id"], title="报告交易字段",
        topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
        spec=_spec(env["versions"]["base-v1"], 2.0),
        hypothesis="报告 summary 的交易类字段必须来自真实成交", registry=registry,
    )
    run_experiment(market, exp["id"], registry=registry)
    latest = None
    from research import verdict as verdict_mod
    latest = verdict_mod.latest_verdict(market, exp["id"])
    assert latest is not None, "实验必须产出 verdict 行"
    report = latest.get("report") or {}
    full = report.get("full_run_report")
    assert full is not None, "单 run 路径必须落 full_run_report"
    summary = full["summary"]
    cost_fees = float((full.get("cost") or {}).get("total_fees") or 0.0)
    assert cost_fees > 0, f"夹具必须产生成交（否则本钉子无对象）：{full.get('cost')}"
    assert summary["trade_count"] > 0, "summary.trade_count 不得为 0（同载荷里有真实成交）"
    assert summary["closed_trade_count"] > 0, "closed_trade_count 不得为 0"
    assert summary["total_commission"] > 0, "summary 费用合计不得为 0"
    assert abs(summary["total_commission"] - cost_fees) < 1e-6, (
        f"summary 佣金合计（{summary['total_commission']}）必须与 cost.total_fees"
        f"（{cost_fees}）同源"
    )
    # 与同载荷的 round_trips 交叉核对（两条独立来源：summary 由成交适配而来，
    # 回合表由 pair_round_trips 配对而来）
    trips = full.get("round_trips") or []
    assert trips, "报告必须带回合明细"
    closed = len(trips)
    wins = sum(
        1 for rt in trips if float(rt.get("pnl_net", rt.get("pnl", 0.0)) or 0.0) > 0
    )
    assert summary["closed_trade_count"] == closed, "平仓笔数必须等于回合数"
    assert abs(summary["win_rate"] - wins / closed) < 1e-12, (
        f"胜率（{summary['win_rate']}）必须等于盈利回合占比（{wins}/{closed}）"
    )
    gross_profit = sum(
        float(rt.get("pnl_net", rt.get("pnl", 0.0)) or 0.0)
        for rt in trips if float(rt.get("pnl_net", rt.get("pnl", 0.0)) or 0.0) > 0
    )
    gross_loss = abs(sum(
        float(rt.get("pnl_net", rt.get("pnl", 0.0)) or 0.0)
        for rt in trips if float(rt.get("pnl_net", rt.get("pnl", 0.0)) or 0.0) < 0
    ))
    expect_pf = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    assert abs(summary["profit_factor"] - expect_pf) < 1e-12, (
        f"盈亏比（{summary['profit_factor']}）必须由回合盈亏现算（{expect_pf}）"
    )
    assert summary["avg_holding_days"] > 0, "平均持仓天数不得为 0"


# --------------------------------------------------------------------------
# R22B-F2/F4/F5（P2）：通道信封 / holdout 通路 / 派发入口
# --------------------------------------------------------------------------

def _service_on(db, registry):
    from research.api import ResearchService

    return ResearchService(db, registry=registry, topics_dir=db.db_path.parent / "topics")


def _holdout_spec(base_ref, atr_mul: float = 2.0):
    """窗口落到 holdout 段（默认 2025-01-01 起）→ 无 token 必失败。"""
    return {
        "base": base_ref,
        "diff": [{"slot": "position_risk", "from": "hard_stop@1", "to": "hard_stop@1",
                  "params": {"atr_mul": atr_mul}}],
        "window": ["2022-06-01", "2025-06-01"],
    }


def _fake_mcp_tools():
    tools: dict = {}

    class FakeMCP:
        def tool(self):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn
            return deco

    from trend_mcp.research_tools import register_research_tools

    register_research_tools(FakeMCP())
    return tools


def test_mcp_dispatch_envelope_reports_run_failure(market, registry, monkeypatch):
    """R22B-F2（P2）：MCP 的 dispatch 必须表达"跑了但失败"及原因。

    修复前成功时 `dispatch.status` 恒 null（verdict 行没有 status 键）、失败时
    只有 `{"mode":"sync","status":"failed"}` 而**没有原因**（如"缺 holdout token"），
    顶层 `status` 还是运行前的 `queued` 快照——模型据此把失败读成"已跑完"。
    """
    from trend_mcp import research_tools as rt

    env = _env(market, registry)
    service = _service_on(market, registry)
    tools = _fake_mcp_tools()
    monkeypatch.setattr(rt, "_service", lambda: service)
    monkeypatch.setattr(
        rt, "_ai_session",
        lambda db, ctx=None: sessions.ensure_channel_session(
            db, session_id="ai-mcp-probe", label="AI（MCP·probe）", channel="mcp"
        ),
    )

    res = tools["research_propose_experiment"](
        topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
        spec=_holdout_spec(env["versions"]["base-v1"]),
        hypothesis="MCP 通道的运行信封必须说明失败与原因", run=True,
    )
    assert res["ok"] is True, res
    dispatch = res["dispatch"]
    assert dispatch["mode"] == "sync"
    assert dispatch["run_status"] == "failed", f"run 失败必须如实标记：{dispatch}"
    assert "holdout" in (dispatch["error"] or ""), f"失败原因必须透出：{dispatch}"
    assert res["status"] == "failed", f"顶层 status 必须是库内真实状态，得到 {res['status']}"

    # 补上 token 后同一步必须成功（fail-closed 不是"永远失败"）
    from research.holdout import grant_token

    token = grant_token(
        market, session_id=env["session"]["session_id"],
        purpose="钉子：验证 MCP holdout 通路", experiment_id=res["experiment_id"],
    )
    token_id = token["id"]
    res2 = tools["research_rerun_experiment"](
        experiment_id=res["experiment_id"], run=True, holdout_token=token_id,
    )
    assert res2["dispatch"]["run_status"] == "ran", res2


def test_holdout_token_follows_parent_for_reproduction(market, registry):
    """R22B-F4（P2）：复现链必须能拿到并使用**父实验**绑定的放行 token。

    两个半面（都是实测踩到的）：
    ① 自动带出：复现是新 id，只按新 id 查永远查不到 → 必须沿 parent 回溯；
    ② 绑定校验：即便显式传入，按 id 严格绑定也会拒（"is bound to experiment E0008"）
       → 只放行复现链，无关实验照旧拦住。
    现象：E0008 拿过 token，复现 E0009 仍失败且 token 未被消费（白烧一次试次）。
    """
    from research.holdout import grant_token
    from research.pipeline import _pick_unconsumed_token, run_experiment

    env = _env(market, registry)
    original = experiments.propose_experiment(
        market, session_id=env["session"]["session_id"], title="原实验",
        topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
        spec=_holdout_spec(env["versions"]["base-v1"]),
        hypothesis="父实验持有的放行 token 必须能被复现继承", registry=registry,
    )
    # 无 token 先跑一次 → failed（rerun 只接受终态）
    run_experiment(market, original["id"], registry=registry)
    assert experiments.get_experiment(market, original["id"])["status"] == "failed"

    token_id = grant_token(
        market, session_id=env["session"]["session_id"],
        purpose="钉子：复现继承父实验的 token", experiment_id=original["id"],
    )["id"]

    repro = experiments.rerun_experiment(
        market, experiment_id=original["id"], session_id=env["session"]["session_id"]
    )
    assert repro["id"] != original["id"] and repro["parent_experiment_id"] == original["id"]
    assert _pick_unconsumed_token(market, repro["id"]) == token_id, (
        "① 复现实验必须回溯父实验的未消费 token（否则空烧一次试次）"
    )

    # ② 显式传入父实验的 token：必须被接受并消费
    verdict_row = run_experiment(
        market, repro["id"], registry=registry, holdout_token=token_id
    )
    assert "suggested_verdict" in verdict_row, f"复现必须跑到 evaluating：{verdict_row}"
    with market.connect() as conn:
        consumed = conn.execute(
            "SELECT consumed_at FROM holdout_tokens WHERE id = ?", (token_id,)
        ).fetchone()["consumed_at"]
    assert consumed, "token 必须被消费（否则样本外门等于没开）"

    # 反向：无关实验拿同一个 token 仍必须被拦（父实验 token 不泄漏给别的研究线）
    stranger = experiments.propose_experiment(
        market, session_id=env["session"]["session_id"], title="无关实验",
        topic_id=env["topic"]["id"], evaluation_module="portfolio_backtest@1",
        spec=_holdout_spec(env["versions"]["base-v1"], atr_mul=2.5),
        hypothesis="无关实验不得借用别条复现链的 token", registry=registry,
        allow_duplicate=True,
    )
    stranger_verdict = run_experiment(
        market, stranger["id"], registry=registry, holdout_token=token_id
    )
    assert "suggested_verdict" not in stranger_verdict, "已消费/非本链的 token 不得放行"


def test_constraint_conflict_is_business_error_not_internal(market, registry):
    """R22B-F3（P2）：UNIQUE 约束冲突是业务结果，必须给可读原因。

    查重分支与 INSERT 之间有竞态：输家撞 `UNIQUE(name,version)` 时此前把
    sqlite 异常冒到通道层 → MCP 回 "internal error (see server logs)"。
    这里把查重分支打桩成"查不到"来**确定性地**扮演竞态输家。
    """
    import sqlite3

    from research import modules
    from research.errors import ResearchError

    env = _env(market, registry)
    session_id = env["session"]["session_id"]
    kwargs = {
        "slot": "universe", "name": "race_mod", "version": 1, "kind": "dsl",
        "source": '{"type": "literal", "value": 1}', "session_id": session_id,
    }
    modules.propose_module(market, **kwargs)          # 第一次成功
    with pytest.raises(ResearchError) as exc_info:    # 第二次：查重分支正常给出文案
        modules.propose_module(market, **kwargs)
    assert "already exists" in str(exc_info.value)

    # 竞态输家的等价形态：查重那一刻看不到已有行（并发下另一进程刚提交），
    # 于是走到 INSERT 撞 UNIQUE(name,version) —— 必须翻译成业务错误而不是
    # 让 sqlite 异常冒到通道层（修复前 MCP 会回 "internal error"）。
    original_connect = market.connect

    class _BlindConn:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            if "FROM module_drafts WHERE name = ?" in sql:
                return _EmptyCursor()
            return self._conn.execute(sql, params)

        def __getattr__(self, item):
            return getattr(self._conn, item)

    class _EmptyCursor:
        def fetchone(self):
            return None

    class _BlindCtx:
        def __enter__(self):
            self._ctx = original_connect()
            return _BlindConn(self._ctx.__enter__())

        def __exit__(self, *exc):
            return self._ctx.__exit__(*exc)

    market.connect = lambda: _BlindCtx()
    try:
        with pytest.raises(ResearchError) as exc_info2:
            modules.propose_module(market, **kwargs)
        assert "already exists" in str(exc_info2.value), (
            f"约束冲突必须翻译成业务错误，得到：{type(exc_info2.value).__name__}"
        )
    except sqlite3.IntegrityError as exc:  # pragma: no cover - 修复前会出现
        raise AssertionError(f"约束冲突冒到了通道层：{type(exc).__name__}: {exc}") from exc
    finally:
        market.connect = original_connect


def test_cli_run_command_dispatches_parked_experiment(market, registry):
    """R22B-F5（P2）：`run=False`（攒批）之后必须有派发入口，CLI 与 MCP 对称。

    此前除了"同调用内 run=True"与"进程启动补投"，没有任何入口 → 实验停在
    queued 直到重启，而关题要求课题内无在途实验（parked 实验把关题卡死）。
    """
    import json
    import subprocess
    import sys
    from pathlib import Path

    env = _env(market, registry)
    root = Path(__file__).resolve().parents[2]
    spec = _spec(env["versions"]["base-v1"], 2.0)

    # 1) 登记但不跑（CLI 侧"攒一批"）
    parked = subprocess.run(
        [sys.executable, str(root / "scripts" / "research_cli.py"), "--db", str(market.db_path),
         "propose-experiment", "--topic", env["topic"]["id"],
         "--eval", "portfolio_backtest@1", "--spec", json.dumps(spec),
         "--hypothesis", "攒批实验必须有显式派发入口"],
        capture_output=True, text=True, cwd=str(root), timeout=180, check=False,
    )
    assert parked.returncode == 0, parked.stdout + parked.stderr
    first = json.loads(parked.stdout.strip().splitlines()[0])
    assert first["status"] == "queued"
    exp_id = first["experiment_id"]

    # 2) 再跑（新入口）
    ran = subprocess.run(
        [sys.executable, str(root / "scripts" / "research_cli.py"), "--db", str(market.db_path),
         "run", exp_id],
        capture_output=True, text=True, cwd=str(root), timeout=600, check=False,
    )
    out = json.loads(ran.stdout.strip().splitlines()[-1])
    assert out["run_status"] == "ran", ran.stdout + ran.stderr
    assert ran.returncode == 0, ran.stdout + ran.stderr

    # 3) 非 queued 状态再派发 → 明确拒绝（幂等保护，不重复烧试次）
    again = subprocess.run(
        [sys.executable, str(root / "scripts" / "research_cli.py"), "--db", str(market.db_path),
         "run", exp_id],
        capture_output=True, text=True, cwd=str(root), timeout=180, check=False,
    )
    assert again.returncode == 1
    assert "not queued" in again.stdout
