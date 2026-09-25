"""Round 21 修复钉子。

- R21A-F1（P1）：R20 的清空守卫只拦住了"因子表被清空"，但调用方把**返回的空列表**
  继续传给 qfq 物化 → 真入口日更下仍复现 −66.94% 假断裂。本文件钉住两半：
  ① `sync_ex_factors` 被拒时**返回值同步回退**；② `rematerialize_qfq` 对空列表与 None
  同口径回读库内因子。
- R21A-F2（P1）：判据从"列表为空"改为"**本地已有因子日期不得缺失**"（清空/部分截断/整批异常同罪）。
- R21A-F3（P2）：多 heat_cap 门时取**约束最紧**（min），而非第一个。
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd


def _raw_bars(n: int = 40, factor_day: date = date(2024, 3, 4), factor: float = 2.0):
    """构造：某日价格真跳水（除权），因子记录在**跳水前**那根 bar。

    真实语义：`ex_factors.time` = 登记日（跳水前一根），跳水落在**下一根** bar——
    所以这里让跳水发生在 `factor_day` 的下一根。
    """
    days = [date(2024, 2, 26) + timedelta(days=i) for i in range(n)]
    closes = []
    for d in days:
        closes.append(100.0 if d <= factor_day else 100.0 / factor)
    arr = np.asarray(closes, dtype=float)
    return pd.DataFrame({
        "time": pd.to_datetime(days), "open": arr, "high": arr, "low": arr,
        "close": arr, "volume": np.full(n, 1e6), "amount": arr * 1e6,
    })


def _service(test_db):
    from data.service import DataService
    from data.storage.market_store import MarketStore

    svc = DataService.__new__(DataService)      # 跳过 provider 构造（无网络）
    svc.market_store = MarketStore(db=test_db)
    svc.raw_store = MarketStore(db=test_db, price_mode="raw")
    return svc


def test_sync_returns_stored_factors_when_upstream_shrinks(test_db, monkeypatch):
    """R21A-F1（返回值半）：被拒时**返回值**必须回退成本地存量。

    否则调用方（日更）拿空列表去物化 qfq → 与 R20B-F1 逐位相同的假断裂照旧。
    """
    test_db.save_market_data("X.SS", _raw_bars(), price_mode="raw")
    test_db.replace_ex_factors("X.SS", [(date(2024, 3, 4), 2.0)], provider="tickflow")
    svc = _service(test_db)
    monkeypatch.setattr(svc, "fetch_ex_factors", lambda symbols: ({"X.SS": []}, {}))
    fetched, changed = svc.sync_ex_factors(["X.SS"], db=test_db)
    assert changed == []
    assert fetched["X.SS"], "被拒标的的返回值必须回退成本地存量（否则下游会毁 qfq）"


def test_partial_upstream_does_not_shrink_factors(test_db, monkeypatch):
    """R21A-F2（P1）：上游**部分**截断（丢掉大因子）同样必须被拒。"""
    test_db.save_market_data("X.SS", _raw_bars(), price_mode="raw")
    test_db.replace_ex_factors(
        "X.SS", [(date(2024, 3, 4), 2.0), (date(2024, 6, 3), 1.1)], provider="tickflow"
    )
    svc = _service(test_db)
    # 上游只回了后一条（丢掉 2.0 那条）——真实断裂来源
    monkeypatch.setattr(
        svc, "fetch_ex_factors", lambda symbols: ({"X.SS": [(date(2024, 6, 3), 1.1)]}, {})
    )
    _fetched, changed = svc.sync_ex_factors(["X.SS"], db=test_db)
    assert changed == [], "因子日期集合缩短必须被拒（不是正常公司行为）"
    kept = test_db.load_all_ex_factors().get("X.SS") or []
    assert len(kept) == 2, "本地因子必须原样保留"


def test_rematerialize_with_empty_factors_reads_back_stored(test_db):
    """R21A-F1（物化半）：空列表 == None 口径（回读库内因子），不得整段写不复权。"""
    test_db.save_market_data("X.SS", _raw_bars(), price_mode="raw")
    test_db.replace_ex_factors("X.SS", [(date(2024, 3, 4), 2.0)], provider="tickflow")
    svc = _service(test_db)
    out = svc.rematerialize_qfq("X.SS", [], db=test_db)
    assert out["status"] == "ok", out
    qfq = test_db.load_market_data("X.SS")
    closes = qfq["close"].astype(float).to_numpy()
    # 正确口径：因子 2.0 折到**整段历史**（qfq(t) = raw(t) / Π_{ex≥t} f）→ 全序列 ≈50；
    # "当成无因子整段写不复权"会留下 100 → 50 的假断裂，首根就是 100
    assert abs(closes[0] - 50.0) < 1e-6, f"qfq 未按因子调整（写成了不复权）：首收盘 {closes[0]}"
    assert float(np.max(np.abs(np.diff(closes) / closes[:-1]))) < 0.05, "qfq 序列出现断裂"


def test_heat_cap_of_takes_binding_min_gate():
    """R21A-F3：多 heat_cap 门时取约束最紧的（min），否则告警会被整条抑制。"""
    from portfolio.backtester import heat_cap_of
    from portfolio.registry import REGISTRY
    from portfolio.slots import ensure_builtins
    from portfolio.strategy import parse_strategy_yaml

    ensure_builtins()
    yaml_text = """name: t
universe: {module: category_filter@1}
signal: {module: macd_cross@1}
rank: {module: by_freshness@1}
sizing: {module: all_in@1}
portfolio_risk: [{module: "heat_cap@1", params: {max_heat_pct: 0.25}}, {module: "heat_cap@1", params: {max_heat_pct: 0.06}}]
position_risk: {module: hard_stop@1}
execution: {module: tail_session@1}
"""
    assert heat_cap_of(parse_strategy_yaml(yaml_text, REGISTRY)) == 0.06


# --------------------------------------------------------------------------
# R21B-P2-1（P2）：成交明细的持有天数/本次收益/浮盈/回撤曾由**前端**按屏幕 K 线
# payload 现算 → 切周/月 K 后静默错数（实测持有天数 2/3/3 对引擎 44/60/60、最大
# 浮盈 0.5% 对 6.05%），算不出来时退化成自然日天数。修法：口径搬到数据源侧
# （slim 结果随 trades 带出），前端只显示。
# --------------------------------------------------------------------------

# 13 个交易日（跳周末）：买入在下标 3、卖出在下标 9 → 持有 6 个交易日、自然日 8 天；
# 下标 10~12 是**出场之后**的 bar（锁"区间不得越出出场日"）
_BAR_DATES = [
    "2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05",
    "2024-01-08", "2024-01-09", "2024-01-10", "2024-01-11", "2024-01-12",
    "2024-01-15", "2024-01-16", "2024-01-17",
]
_NAV_DATES = [f"{d}T00:00:00" for d in _BAR_DATES]     # 净值序列带时间戳（真实形态）
_BUY_DATE, _SELL_DATE = _BAR_DATES[3], _BAR_DATES[9]


def _candles(buy_price: float = 10.0):
    """[open, close, low, high]：持有期内先冲到 10.8，随后砸到 9.5。

    入场前（下标 1）与出场后（下标 15）各放一根尖刺/深坑——它们落在持有区间**之外**，
    钉子据此锁住"只扫持有区间"（扫全序列会把 20% / 33% 冒充成本次交易的浮盈回撤）。
    """
    out = []
    for i, d in enumerate(_BAR_DATES):
        if i == 1:
            out.append([10.0, 10.0, 9.0, 12.0])
        elif i == 2:
            out.append([10.0, 10.0, 9.0, 13.0])
        elif i == 11:
            out.append([10.0, 10.0, 8.0, 10.0])
        elif i < 3 or i > 9:
            out.append([10.0, 10.0, 10.0, 10.0])
        elif d == _SELL_DATE:
            out.append([10.0, 10.0, 9.5, 10.6])
        elif i == 6:
            out.append([10.0, 10.7, 10.0, 10.8])
        else:
            out.append([10.0, 10.0, 9.9, 10.2])
    return out


def _trades():
    return [
        {"date": _BUY_DATE, "side": "BUY", "qty": 1000, "exec_price": 10.0, "commission": 5.0},
        {"date": _SELL_DATE, "side": "SELL", "qty": 1000, "exec_price": 10.5,
         "commission": 5.0, "stamp_tax": 0.0, "pnl": 495.0},
    ]


def test_trade_display_metrics_use_backtest_own_series():
    """R21B-P2-1：持有天数 = 净值下标差（交易日），浮盈/回撤 = 回测自身 bar 的 high/low。"""
    from rule_backtest.metrics import annotate_trade_display_metrics

    rows = annotate_trade_display_metrics(
        _trades(), nav_dates=_NAV_DATES, bar_dates=_BAR_DATES, candles=_candles()
    )
    sell = rows[1]
    assert sell["holding_days"] == 6, f"必须按交易日下标差（6），得到 {sell['holding_days']}"
    assert abs(sell["return_pct"] - (495.0 / (10.0 * 1000 + 5.0)) * 100) < 1e-9
    assert abs(sell["max_profit_pct"] - 8.0) < 1e-9          # 10.8 / 10 - 1
    assert abs(sell["max_drawdown_pct"] + (10.8 - 9.5) / 10.8 * 100) < 1e-9
    assert rows[0].get("holding_days", None) is None          # 买入行不填


def test_trade_display_metrics_degrade_to_dash_not_calendar_days():
    """序列对不上时必须是 None（前端显示 "-"），不得回退自然日（8 天≠持有口径）。"""
    from rule_backtest.metrics import annotate_trade_display_metrics

    rows = annotate_trade_display_metrics(
        _trades(), nav_dates=[], bar_dates=["2025-06-02"], candles=[]
    )
    sell = rows[1]
    assert sell["holding_days"] is None, "口径缺失必须留空，不能拿自然日天数冒充持有天数"
    assert sell.get("max_profit_pct") is None and sell.get("max_drawdown_pct") is None


def test_slim_result_carries_annotated_trades():
    """R21B-P2-1（传输面）：剥掉 daily_nav/charts 的同时必须把算好的展示字段带出去。"""
    from rule_backtest.service import slim_backtest_result

    per_strategy = {
        "strategy_id": "s1", "trades": _trades(), "daily_nav": [{"date": d} for d in _NAV_DATES],
        "charts": {"kline": {"dates": _BAR_DATES, "candles": _candles()}},
    }
    full = {"status": "ok", "results": [per_strategy], "trades": per_strategy["trades"]}
    slim = slim_backtest_result(full)

    assert "daily_nav" not in slim["results"][0] and "charts" not in slim["results"][0]
    for key in ("holding_days", "return_pct", "max_profit_pct", "max_drawdown_pct"):
        assert slim["results"][0]["trades"][1][key] is not None, f"slim 交易明细缺 {key}"
    assert slim["results"][0]["trades"][1]["holding_days"] == 6
    assert slim["trades"][1]["holding_days"] == 6, "顶层 backward-compat trades 必须同一份标注"
    assert slim["trades"] is slim["results"][0]["trades"]


def test_trade_table_does_not_compute_from_chart_payload():
    """R21B-P2-1（前端面）：成交明细区不得再出现按屏幕 K 线现算的代码。"""
    from pathlib import Path

    js = (Path(__file__).resolve().parents[2] / "web" / "static" / "js" / "market_view.js").read_text(
        encoding="utf-8"
    )
    block = js[js.index("function tradeReturnPctCell"):js.index("function renderBacktestDebug")]
    assert "currentPayload" not in block, "成交明细不得依赖屏幕 K 线 payload（切周/月K必错）"
    assert "holding_days" in block and "max_drawdown_pct" in block, "应直接展示后端字段"


def test_mcp_promote_errors_are_not_reported_as_internal():
    """R21B-P2-2（P2）：MCP 通道的业务错误（策略配置/模块注册）必须透真实原因。

    CLI 同调用会给 "position_risk: param atr_mul: -1.0 < min 0.1" 这类可读解释，
    MCP 此前回 "internal error (see server logs)" → 模型误判平台故障并盲目重试。
    """
    from portfolio.strategy import StrategyConfigError
    from trend_mcp.research_tools import _error_payload

    exc = StrategyConfigError(["position_risk: param atr_mul: -1.0 < min 0.1"])
    payload = _error_payload(exc)
    assert payload["ok"] is False
    assert "atr_mul" in payload["error"], f"业务原因被吞：{payload}"

    internal = _error_payload(RuntimeError("sqlite3.OperationalError: no such table"))
    assert internal["error"] == "internal error (see server logs)", "内部错误不得透细节"


_MACD_PROBE = {
    "id": "r21_probe", "trade_mode": "single_symbol_all_in",
    "entry": {"type": "group", "combinator": "all", "children": [
        {"id": "e1", "type": "condition",
         "left": {"type": "indicator", "name": "macd_line", "params": {}},
         "operator": "cross_above",
         "right": {"type": "indicator", "name": "macd_signal", "params": {}}}]},
    "exit": {"type": "group", "combinator": "any", "children": [
        {"id": "x1", "type": "condition",
         "left": {"type": "indicator", "name": "macd_line", "params": {}},
         "operator": "cross_below",
         "right": {"type": "indicator", "name": "macd_signal", "params": {}}}]},
}


def test_slim_trade_metrics_match_engine_round_trips():
    """真引擎交叉核对：slim 成交明细的持有天数/最大浮盈必须与引擎自算的 round_trips 一致。

    两条独立代码路径（`roundtrip_replay` 用 all_bars 下标 + highs 切片；标注用
    daily_nav 下标 + kline candles）在真实引擎上给出同一组数字 → 口径确实落在
    回测自身的日线上，而**不是**屏幕 K 线（屏幕口径在月 K 下会给 2 而不是 31）。
    """
    import numpy as np
    import pandas as pd

    from rule_backtest import (
        BacktestExecutionConfig,
        RuleBacktestRequest,
        SingleSymbolAllInBacktestEngine,
    )
    from rule_backtest.service import slim_backtest_result

    n = 260
    t = np.arange(n)
    close = 100 + 18 * np.sin(t / 9.0) + 4 * np.sin(t / 3.7)
    close = close * (1 + 0.004 * np.sin(t * 1.7))
    dates = pd.bdate_range("2023-01-03", periods=n)
    bars = pd.DataFrame({
        "date": dates, "time": dates, "open": close, "close": close,
        "high": close * 1.012, "low": close * 0.986,
        "volume": np.full(n, 1e6), "amount": close * 1e6,
    })
    full = SingleSymbolAllInBacktestEngine().run(RuleBacktestRequest(
        strategy=_MACD_PROBE, symbol="PROBE.SS", bars=bars,
        execution=BacktestExecutionConfig(initial_capital=100_000.0, slippage=0.002),
    ))
    assert full["round_trips"], "夹具必须产生成交（否则核对无对象）"
    slim = slim_backtest_result({"status": "ok", "results": [full], "trades": full["trades"]})

    by_exit = {str(r["exit_date"])[:10]: r for r in full["round_trips"]}
    checked = 0
    for trade in slim["results"][0]["trades"]:
        if str(trade.get("side", "")).upper() != "SELL":
            continue
        rt = by_exit.get(str(trade["date"])[:10])
        assert rt is not None, f"引擎 round_trips 里没有出场日 {trade['date']}"
        assert trade["holding_days"] == rt["holding_days"], "持有天数口径必须与引擎一致"
        assert abs(trade["max_profit_pct"] - rt["mfe_pct"]) < 1e-9, "最大浮盈须同源"
        assert trade["max_drawdown_pct"] <= 0 and trade["return_pct"] is not None
        checked += 1
    assert checked >= 2, f"至少核对两笔，实际 {checked}"


def test_catchup_spawner_tests_do_not_leak_real_threads():
    """R21-巡-1（P3，测试卫生）：集成测试不得真起"当日补跑哨兵"线程。

    哨兵是进程级单例且比用例活得久：真线程一旦漏出，解除 monkeypatch 后它会
    看到"已解冻"→真跑一次日更，并长期占住 `jobs._catchup_sentinel`，让后续
    test_review_r3 的哨兵钉子 join 超时（整目录跑必红、单跑全绿）。源头用例
    必须打桩 `_spawn_same_day_catchup`，受害用例必须先清空单例。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    spawner = (root / "tests" / "integration" / "test_critical_paths.py").read_text(encoding="utf-8")
    block = spawner[spawner.index("def test_freeze_gate_applies_even_for_force"):
                    spawner.index("def test_live_t1_same_day_buy_not_sellable")]
    assert "_spawn_same_day_catchup" in block, "顺延用例必须打桩哨兵生成，不得真起线程"

    victim = (root / "tests" / "integration" / "test_review_r3.py").read_text(encoding="utf-8")
    vblock = victim[victim.index("def test_freeze_defer_spawns_same_day_catchup"):
                    victim.index("def test_live_reconcile_uses_ref_price_and_checks_qty")]
    assert '"_catchup_sentinel", None' in vblock or '"_catchup_sentinel", None)' in vblock, \
        "受害用例必须先清空哨兵单例（否则 join 到别人的线程）"
