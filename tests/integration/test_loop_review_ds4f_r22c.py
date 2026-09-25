"""Round 22 钉子（P3 批次）：前端展示口径 / 台账摘要 / 信号口径。

每条对应 round22-review.md 的一个发现，变异实证见 round22-fixes.md：
- R22A-F1：同屏两个"交易数"标签混用成交笔数与平仓回合数；
- R22A-F3：跨批次 Δ年化 是"两个中位数之差"，与后端逐格作差口径不一致；
- R22A-F4：看盘页"我的止损线"忽略棘轮档（引擎的独立出场条件）；
- R22A-F5：跳过原因映射缺 below_min_order（页面显示英文枚举）；
- R22A-F6：看盘页 MACD 用 warmup=False，与缓存/相位权威口径不一致；
- R22A-观察项：仓位% 列硬编码"全仓"。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def registry():
    from portfolio.registry import fresh_registry
    from portfolio.slots import register_builtin_modules

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


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# R22A-F1
# --------------------------------------------------------------------------


def test_trade_count_labels_disambiguate_fills_vs_round_trips():
    """R22A-F1：成交笔数（买卖各一笔）与平仓回合数不得共用"交易数"标签。

    实测同一格 `trade_count=290`（145 买 + 145 卖）而胜率分母是 145——同屏上下
    相邻的两个"交易数"差 2 倍，且与胜率/盈亏比不同源。
    """
    market = _read("web/static/js/market_view.js")
    assert "成交笔数" in market, "汇总表必须标明是成交笔数"
    assert "平仓笔数" in market, "年度表必须标明是平仓回合数"
    batch = _read("web/static/js/batch_backtest.js")
    tmpl = _read("web/templates/batch_backtest.html")
    assert "'成交笔数'" in batch or '"成交笔数"' in batch
    assert "平仓笔数" in tmpl, "格子弹窗年度表头必须标明平仓回合数"
    for src, name in ((market, "market_view.js"), (batch, "batch_backtest.js"), (tmpl, "batch_backtest.html")):
        assert "交易数" not in src, f"{name} 仍存在无口径的 交易数 标签"


# --------------------------------------------------------------------------
# R22A-F3
# --------------------------------------------------------------------------


def test_batch_compare_delta_is_paired_per_cell():
    """R22A-F3：跨批次 Δ年化 必须是**逐格作差后取中位数**（与后端同口径）。"""
    js = _read("web/static/js/batch_backtest.js")
    assert "medB.annual_return - medA.annual_return" not in js, (
        "不得再算 两个中位数之差（实测 −0.97pp vs 逐格 −1.74pp）"
    )
    block = js[js.index("function renderCompare"):js.index("function renderCompareSymbols")]
    assert "perPairDelta" in block, "必须逐格作差后再聚合"
    assert "p.b.annual_return - p.a.annual_return" in block


# --------------------------------------------------------------------------
# R22A-F4
# --------------------------------------------------------------------------


def test_annotation_stop_fields_include_ratchet():
    """R22A-F4：标注白名单必须含棘轮档（否则看盘页画不出系统的第三个出场条件）。"""
    src = _read("src/services/trade_records.py")
    block = src[src.index("_ANNOTATION_STOP_FIELDS"):src.index("def symbol_annotations")]
    assert "chandelier_stop_ratchet_price" in block
    assert "chandelier_stop_ratchet_triggered" in block
    js = _read("web/static/js/market_view.js")
    assert "chandelier_stop_ratchet_price" in js, "前端必须把棘轮计入有效止损线"
    assert "effectiveStopPrice" in js and "Math.max.apply(null, candidates)" in js
    assert "二者取价高者" in js and "三者取价高者" in js, "悬停文案需随档数变化"


# --------------------------------------------------------------------------
# R22A-F5
# --------------------------------------------------------------------------


def test_skip_reason_labels_cover_engine_codes():
    """R22A-F5：引擎的跳过原因码必须在页面有中文映射（否则露英文枚举）。"""
    js = _read("web/static/js/market_view.js")
    block = js[js.index("SKIP_REASON_TEXT"):js.index("let allSymbols")]
    engine = _read("src/rule_backtest/engine.py")
    for code in ("insufficient_cash", "below_min_order"):
        assert code in engine, f"引擎应仍有 {code} 原因码"
        assert code in block, f"前端缺 {code} 的中文映射"


# --------------------------------------------------------------------------
# R22A-F6 / R22A-观察项
# --------------------------------------------------------------------------


def test_market_view_macd_uses_authoritative_warmup():
    """R22A-F6：看盘页 MACD 必须与缓存/相位权威（warmup=True）同口径。

    短历史标的（551030.SS 只有 7 根）在 warmup=False 下 dif/dea 全 None →
    看盘页 MACD 副图空白、金叉标记消失，而看板与相位判定都有值。
    """
    from core import indicators as core_ind
    from services.market_indicators import compute_market_indicators

    n = 12
    close = pd.Series(np.linspace(10.0, 12.0, n))
    df = pd.DataFrame({
        "time": pd.bdate_range("2024-01-02", periods=n),
        "open": close, "high": close * 1.01, "low": close * 0.99,
        "close": close, "volume": np.full(n, 1e6), "amount": close * 1e6,
    })
    out = compute_market_indicators(df)
    expected = core_ind.macd(close, warmup=True)
    got_dif = out["macd"]["dif"]
    exp_dif = [None if pd.isna(v) else float(v) for v in expected["dif"]]
    assert len(got_dif) == len(exp_dif)
    for got, exp in zip(got_dif, exp_dif):
        assert (got is None) == (exp is None), "空值位置必须与权威口径一致"
        if exp is not None:
            assert abs(got - exp) < 1e-6, "看盘页 MACD 必须等于 warmup=True 的权威口径"
    assert any(v is not None for v in got_dif), "短历史也必须画出 MACD（此前整段为空）"


def test_trade_position_pct_is_computed_not_hardcoded():
    """R22A 观察项：仓位% 必须按成交自身算，而不是硬编码全仓。"""
    from rule_backtest.metrics import annotate_trade_display_metrics

    buy = {"date": "2024-01-02", "side": "BUY", "qty": 9900, "exec_price": 10.0,
           "commission": 8.4, "cash_after": 100.0}
    sell = {"date": "2024-01-10", "side": "SELL", "qty": 9900, "exec_price": 11.0,
            "commission": 9.3, "pnl": 9891.0}
    rows = annotate_trade_display_metrics(
        [buy, sell], nav_dates=["2024-01-02", "2024-01-10"],
        bar_dates=["2024-01-02", "2024-01-10"],
        candles=[[10.0, 10.0, 9.9, 11.2], [11.0, 11.0, 10.8, 11.3]],
    )
    pct = rows[0]["position_pct"]
    assert abs(pct - (99000.0 / 99100.0 * 100.0)) < 1e-9, pct
    assert pct < 100.0, "整手取整 + 费用使真实权重不是恰好 100%"
    js = _read("web/static/js/market_view.js")
    assert "trade?.position_pct" in js and "全仓</span>" not in js


def test_report_summary_trade_stats_derive_from_round_trips():
    """R22A-F2（算术面）：summary 的胜率/盈亏比/平仓笔数必须由回合盈亏现算。

    端到端钉子（pipeline 那条）覆盖"不再全 0 且与 cost 同源"；这条用手工夹具
    覆盖**算术本身**（含 2 胜 1 负，零化盈亏的实现会立刻红）。
    """
    from portfolio.reports import build_report

    nav = [{"date": f"2024-01-{d:02d}", "equity": 1_000_000.0} for d in range(1, 25)]
    fills = [
        {"symbol": "A.SS", "side": "buy", "fill_date": "2024-01-02", "fill_price": 10.0,
         "quantity": 1000, "commission": 5.0, "stamp_tax": 0.0, "fee_total": 5.0},
        {"symbol": "A.SS", "side": "sell", "fill_date": "2024-01-08", "fill_price": 11.0,
         "quantity": 1000, "commission": 5.0, "stamp_tax": 0.0, "fee_total": 5.0},
        {"symbol": "A.SS", "side": "buy", "fill_date": "2024-01-09", "fill_price": 11.0,
         "quantity": 900, "commission": 5.0, "stamp_tax": 0.0, "fee_total": 5.0},
        {"symbol": "A.SS", "side": "sell", "fill_date": "2024-01-12", "fill_price": 12.0,
         "quantity": 900, "commission": 5.0, "stamp_tax": 0.0, "fee_total": 5.0},
        {"symbol": "A.SS", "side": "buy", "fill_date": "2024-01-15", "fill_price": 12.0,
         "quantity": 900, "commission": 5.0, "stamp_tax": 0.0, "fee_total": 5.0},
        {"symbol": "A.SS", "side": "sell", "fill_date": "2024-01-22", "fill_price": 11.0,
         "quantity": 900, "commission": 5.0, "stamp_tax": 0.0, "fee_total": 5.0},
    ]
    # 传进来的回合只作展示（且刻意给"毛额"误导值）：报告的 summary 必须由
    # **成交自身**按净额配对算（R23B-F4），不受调用方口径影响
    trips = [
        {"symbol": "A.SS", "entry_date": "2024-01-02", "exit_date": "2024-01-08",
         "pnl": 1005.0},
        {"symbol": "A.SS", "entry_date": "2024-01-09", "exit_date": "2024-01-12",
         "pnl": 905.0},
        {"symbol": "A.SS", "entry_date": "2024-01-15", "exit_date": "2024-01-22",
         "pnl": -900.0},
    ]
    summary = build_report(
        None, run_id="R22C", nav_rows=nav, fills=fills, unfilled=[], gate_log=[],
        round_trips=trips,
    )["summary"]
    assert summary["trade_count"] == 6, "成交笔数（买卖各一笔）"
    assert summary["closed_trade_count"] == 3, "平仓回合数"
    assert abs(summary["win_rate"] - 2 / 3) < 1e-12, summary["win_rate"]
    # 净额由成交现算：每笔回合净 = 价差额 − 两笔佣金(5+5)
    win1, win2, loss = 1000 * 1.0 - 10.0, 900 * 1.0 - 10.0, 900 * -1.0 - 10.0
    # 盈亏比 = 盈利回合合计 / 亏损回合合计（与 compute_summary 同定义）
    assert abs(summary["profit_factor"] - (win1 + win2) / abs(loss)) < 1e-12, (
        f"盈亏比必须由成交净额现算（得到 {summary['profit_factor']}）；"
        "传入的毛额回合不得改变口径"
    )
    assert abs(summary["avg_win"] - (win1 + win2) / 2) < 1e-9
    assert abs(summary["avg_loss"] - abs(loss)) < 1e-9
    assert abs(summary["total_commission"] - 30.0) < 1e-9
    # 持有天数：用**净值序列下标差**（本夹具是连续自然日序列：01-02→01-08 = 6、
    # 01-09→01-12 = 3、01-15→01-22 = 7 → 均值 16/3）
    assert abs(summary["avg_holding_days"] - 16 / 3) < 1e-9, summary["avg_holding_days"]


# --------------------------------------------------------------------------
# R22B-F7 / R22B-F8 / R22B-F9
# --------------------------------------------------------------------------


def test_invalid_final_verdict_is_reported_as_invalid(market, registry):
    """R22B-F7：非法枚举必须报"取值非法"，而不是"可降不可升"。"""
    from research import experiments, sessions, topics, verdict
    from research.errors import LifecycleError
    from research.pipeline import run_experiment

    session = sessions.get_or_create_default_human_session(market)
    topic = topics.create_topic(market, session_id=session["session_id"],
                                title="枚举校验", question="非法取值文案")
    exp = experiments.propose_experiment(
        market, session_id=session["session_id"], title="枚举", topic_id=topic["id"],
        evaluation_module="portfolio_backtest@1",
        spec={"base": _seed_base(market, registry),
              "diff": [{"slot": "position_risk", "from": "hard_stop@1", "to": "hard_stop@1",
                        "params": {"atr_mul": 2.0}}],
              "window": ["2022-06-01", "2023-06-01"]},
        hypothesis="非法 verdict 枚举必须给取值非法文案", registry=registry,
    )
    run_experiment(market, exp["id"], registry=registry)
    with pytest.raises(LifecycleError) as exc:
        verdict.confirm_verdict(market, experiment_id=exp["id"], final_verdict="upgraded",
                                reasoning="钉子", session_id=session["session_id"])
    msg = str(exc.value)
    assert "invalid final_verdict" in msg, msg
    assert "downgrade only" not in msg, "非法枚举不得伪装成平台纪律"


def test_search_ledger_rows_carry_holdout_and_reproduction_flags(market, registry):
    """R22B-F9：台账检索行必须带 `holdout_touched` / `is_reproduction`。

    工具文档要求 AI"先读台账再提假设"；缺这两项时它读不出"哪条结论经过样本外
    验证"与"哪条是复现"，只能逐条 get_experiment 甚至把复现当成新尝试。
    """
    from research import experiments, sessions, topics
    from research.api import ResearchService

    session = sessions.get_or_create_default_human_session(market)
    topic = topics.create_topic(market, session_id=session["session_id"],
                                title="检索面", question="摘要字段")
    experiments.propose_experiment(
        market, session_id=session["session_id"], title="检索面实验", topic_id=topic["id"],
        evaluation_module="portfolio_backtest@1",
        spec={"base": _seed_base(market, registry),
              "diff": [{"slot": "position_risk", "from": "hard_stop@1", "to": "hard_stop@1",
                        "params": {"atr_mul": 2.0}}],
              "window": ["2022-06-01", "2023-06-01"]},
        hypothesis="检索行必须带样本外与复现标记", registry=registry,
    )
    rows = ResearchService(market, registry=registry,
                           topics_dir=market.db_path.parent / "t").search_ledger()
    assert rows, "台账应有行"
    row = rows[0]
    assert "holdout_touched" in row and "is_reproduction" in row, row.keys()
    assert row["is_reproduction"] is False


def test_cli_rejects_non_object_spec(market, registry):
    """R22B-F8：`--spec` 非 JSON 对象时给业务文案 + 退出码 1（此前抛裸异常文本）。"""
    import json
    import subprocess
    import sys

    from research import sessions, topics

    session = sessions.get_or_create_default_human_session(market)
    topic = topics.create_topic(market, session_id=session["session_id"],
                                title="spec 校验", question="非对象 spec")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "research_cli.py"), "--db", str(market.db_path),
         "propose-experiment", "--topic", topic["id"], "--eval", "portfolio_backtest@1",
         "--spec", "[1, 2]", "--hypothesis", "非对象 spec 必须给业务文案"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=180, check=False,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "必须是 JSON 对象" in payload["error"], payload
    assert "attribute" not in payload["error"], "不得把裸 Python 异常当业务原因"


def _seed_base(db, registry) -> str:
    from portfolio.seed import seed_default_library

    return seed_default_library(db, registry)["base-v1"]
