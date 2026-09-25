"""Round 11 修复钉子（round11-review.md 各项的回归锚）。

覆盖本轮 R11B 独立审计的三个材质性发现：
- ETF 最小变动单位是 0.001（不是 0.01）→ 按 0.01 舍入会产出假涨停/假跌停；
- 创业板系 ETF 的 ±20% 幅度判据漏掉"创业大盘"这类不含"创业板"的名称；
- 脏数据守卫：close<=0 不得产出 is_limit_down=True；极端因子（1e12）不得
  产出 limit_up=0.0 这类"像真值的坏数字"。
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pandas as pd
import pytest

from gateway.tradability import board_limit_pct, compute_tradability

ETF = "159865.SZ"          # 养殖ETF国泰（±10% 主板系 ETF）
ETF_NAME = "养殖ETF国泰"
CYB = "159814.SZ"          # 创业大盘ETF西部利得（±20% 创业板系）
CYB_NAME = "创业大盘ETF西部利得"
D1, D2 = date(2024, 9, 27), date(2024, 9, 30)


def _frame(symbol: str, name: str, close1: float, close2: float, **kw):
    return compute_tradability(
        None, symbols=[symbol], dates=[D1, D2],
        raw_closes={symbol: pd.Series({D1: close1, D2: close2})},
        ex_factors={symbol: kw.pop("ex_factors", [])},
        listing_dates={symbol: None},
        asset_info={symbol: {"asset_type": "etf", "name": name}},
        **kw,
    )


def _row(frame, day):
    return frame[frame["date"] == day].iloc[0]


def test_etf_tick_is_three_decimals_not_two():
    """ETF 限价按 0.001 舍入（实测数据：ETF 收盘第 3 位小数 0~9 均匀分布）。

    0.616 × 1.1 = 0.6776 → 0.001 口径 0.678；0.01 口径 0.68（高一档）。
    收盘 0.678 是**真涨停**：旧口径把它判成"没到涨停"（漏报），
    而 0.616 × 0.9 = 0.5544 → 0.001 口径 0.554（0.01 口径 0.55 更低）——
    收盘 0.554 是真跌停，旧口径同样漏报（卖不出/止损不阻塞的镜像错误）。
    """
    up = _row(_frame(ETF, ETF_NAME, 0.616, 0.678), D2)
    assert up["limit_up_price"] == 0.678, "ETF 涨停价必须按 0.001 舍入"
    assert bool(up["is_limit_up"]) is True, "收盘等于真涨停价 → 必须判涨停（旧口径漏报）"
    down = _row(_frame(ETF, ETF_NAME, 0.616, 0.554), D2)
    assert down["limit_down_price"] == 0.554, "ETF 跌停价必须按 0.001 舍入"
    assert bool(down["is_limit_down"]) is True, "收盘等于真跌停价 → 必须判跌停（旧口径漏报）"
    # 反向：低于真涨停价的价格不得判涨停（0.01 口径会把 0.677 误判为涨停）
    below = _row(_frame(ETF, ETF_NAME, 0.616, 0.677), D2)
    assert bool(below["is_limit_up"]) is False


def test_stock_tick_stays_two_decimals():
    """股票仍是 0.01 口径（主板规则不能被 ETF 修复带偏）。"""
    frame = compute_tradability(
        None, symbols=["600519.SS"], dates=[D1, D2],
        raw_closes={"600519.SS": pd.Series({D1: 10.005, D2: 11.01})},
        ex_factors={"600519.SS": []},
        listing_dates={"600519.SS": None},
        asset_info={"600519.SS": {"asset_type": "stock", "name": "贵州茅台"}},
    )
    row = _row(frame, D2)
    assert row["limit_up_price"] == 11.01  # 10.005×1.1=11.0055 → 分位 11.01
    assert row["limit_down_price"] == 9.0
    assert bool(row["is_limit_up"]) is True


def test_chinext_etf_by_name_keyword_chuangye():
    """名称含"创业"（不含"创业板"）的 ETF 也按 ±20%——159814 实测 0.2005。

    创业板 ETF 的幅度跟随标的指数板块：2024-09-30 前收 0.364 → 涨停 0.437
    （+20.05%）。按 ±10% 会产出假涨停（0.364×1.1=0.4004 → 0.400）与
    次日的假跌停（0.449 vs 0.419，实测 2024-10-09）。
    """
    assert board_limit_pct(CYB, asset_type="etf", name=CYB_NAME) == pytest.approx(0.20)
    row = _row(_frame(CYB, CYB_NAME, 0.364, 0.437), D2)
    assert row["limit_up_price"] == 0.437, "创业板系 ETF 涨停价按 +20% 计"
    assert bool(row["is_limit_up"]) is True
    # 10% 口径下 0.4 就会被判涨停——这个价格其实在带内
    inside = _row(_frame(CYB, CYB_NAME, 0.364, 0.400), D2)
    assert bool(inside["is_limit_up"]) is False


def test_zero_or_negative_close_never_claims_limit_down():
    """close<=0 是坏数据：不得据"0 <= 跌停价恒真"产出 is_limit_down=True。"""
    for bad in (0.0, -1.0):
        row = _row(_frame(ETF, ETF_NAME, 0.616, bad), D2)
        assert bool(row["is_limit_down"]) is False, f"close={bad} 不得判跌停"
        assert bool(row["is_limit_up"]) is False
        assert row["limit_down_price"] is not None, "限价本身仍应照常给出（仅不用于比较）"


def test_ex_factor_must_be_corroborated_by_observed_jump():
    """因子必须与观测跳变互证：极端/带内脏因子一律跳过，合法因子照常生效。

    曾经的纯幅值带（[1e-3, 1e3]）挡不住带内的脏因子——R12A-F2 实证 f=1e3 会产出
    `limit_up=0.001` 且 `is_limit_up=True`（与 1e12 同形）；f∈[0.78,1.28] 也会因
    基准价被压低而伪造涨停。现在改为：implied = raw(上一根) / raw(除权那根)，
    |log f − log implied| > max(25%, 3×tick/前收) 即跳过。
    """
    # ① 带外脏因子（1e12 / 1e-12 / 1e3 / 1e-3）与"带内但跳变严重不一致"的 f=2.0
    #    （夹具 implied=0.9085 → |log2 − log0.9085| = 0.789 > 0.6）都必须跳过
    for bad in (1e12, 1e-12, 1e3, 1e-3, 2.0):
        row = _row(_frame(ETF, ETF_NAME, 0.616, 0.678, ex_factors=[("2024-09-27", bad)]), D2)
        assert row["limit_up_price"] == 0.678, f"f={bad} 未被互证判据拦下"
    # ② 合法因子 + 匹配的 raw 跳变（0.616 → 0.410667 = /1.5）：照常生效
    #    0.616/1.5 = 0.41067 ×1.1 = 0.4517 → 0.452
    legit = compute_tradability(
        None, symbols=[ETF], dates=[D1, D2],
        raw_closes={ETF: pd.Series({D1: 0.616, D2: 0.410667})},
        ex_factors={ETF: [("2024-09-27", 1.5)]},
        listing_dates={ETF: None},
        asset_info={ETF: {"asset_type": "etf", "name": ETF_NAME}},
    )
    row = _row(legit, D2)
    assert abs(row["limit_up_price"] - 0.452) < 1e-9, "跳变互证的合法因子必须生效"
    # ③ 大额份额折算（f=0.2，跳变互证）：生效而非被幅值带误杀
    consol = compute_tradability(
        None, symbols=[ETF], dates=[D1, D2],
        raw_closes={ETF: pd.Series({D1: 0.616, D2: 3.08})},   # 0.616/0.2 = 3.08
        ex_factors={ETF: [("2024-09-27", 0.2)]},
        listing_dates={ETF: None},
        asset_info={ETF: {"asset_type": "etf", "name": ETF_NAME}},
    )
    assert abs(_row(consol, D2)["limit_up_price"] - 3.388) < 1e-9


def test_tail_slippage_whitelist_is_direction_sensitive():
    """尾滑点白名单必须对方向敏感（只有"更差"才可归因）。

    合法尾滑点只能让成交更差（买价抬升、卖价压低）——"更有利"的系统性价差
    不可能来自任何合法参数（滑点符号写反/取错参考价的典型形态），此前用
    `abs(价差)` 判带对方向盲，这类真实错误会在 ≤1.1% 带内被静默归入白名单。
    真实 run（510300.SS 2015-2024、191 笔）实测：不利 191 / 有利 0。
    """
    from engine.parity import attribute_diffs

    nav = [{"date": "2024-01-02", "equity": 100_000.0, "cash": 100_000.0,
            "positions_value": 0.0},
           {"date": "2024-01-03", "equity": 100_000.0, "cash": 100_000.0,
            "positions_value": 0.0}]

    def res_old(price, side):
        # 旧引擎成交行的键是 exec_price（新引擎是 price）——parity 归一化层
        # 按 exec_price 取值，用错键会被判 trade_shape_invalid
        return {"daily_nav": nav, "trades": [
            {"date": D1, "side": side, "qty": 100, "exec_price": price}]}

    def res_new(price, side):
        return {"daily_nav": nav, "trades": [
            {"date": D1, "side": side, "qty": 100, "price": price}]}

    for side, worse_price, better_price in (("BUY", 10.06, 9.94),
                                            ("SELL", 9.94, 10.06)):
        base = res_old(10.0, side)
        worse = attribute_diffs(res_new(worse_price, side), base)
        assert worse["classified"]["tail_slippage"] == 1, f"{side}: 更差的价差应可归因"
        assert worse["unexplained"] == [], f"{side}: 合法差异不得判超纲（防假阳性）"
        better = attribute_diffs(res_new(better_price, side), base)
        assert better["classified"]["tail_slippage"] == 0, \
            f"{side}: 系统性更有利的价差不得被白名单吸收"
        assert any(u.get("kind") == "trade_mismatch" for u in better["unexplained"]), \
            f"{side}: 更有利的价差必须判超纲"


def test_extract_cell_gates_noise_ratios():
    """批量回测的落库路径必须过闸：噪声腿的比值记 None，超额不得用噪声作差。

    R11A 复现：IPO 一字板窗口的买持腿 |年化 sharpe| = 17022，旧实现把
    `benchmark_sharpe=17022` 与 `excess_sharpe=-17022` 平铺进
    `batch_backtest_cells`（经 HTTP/前端/CSV 外显）。`calmar` 不入闸。
    """
    from rule_backtest.batch_service import extract_cell

    nav = [{"date": "2024-01-02", "equity": 1_000_000.0},
           {"date": "2024-01-03", "equity": 1_010_000.0}]
    result = {
        "summary": {"sharpe": 0.0, "sortino": 0.0, "calmar": 0.0, "annual_return": 0.0},
        "benchmark_summary": {"sharpe": 17022.0, "sortino": None, "calmar": 78.5,
                              "annual_return": 3.1637},
        "daily_nav": nav, "round_trips": [], "trades": [],
    }
    cell = extract_cell(result, monthly_nav=[])
    assert cell["benchmark_sharpe"] is None, "噪声基准 Sharpe 不得落库"
    assert cell["excess_sharpe"] is None, "任一腿噪声时不得用噪声作差"
    # 策略腿干净 → 照常保留；calmar 两侧都不入闸
    assert cell["sharpe"] == 0.0
    assert cell["calmar"] == 0.0 and cell["benchmark_calmar"] == 78.5
    assert cell["excess_calmar"] == pytest.approx(0.0 - 78.5)
    # 策略腿自己超闸时同样记 None
    noisy = {**result, "summary": {**result["summary"], "sharpe": 1e5}}
    cell2 = extract_cell(noisy, monthly_nav=[])
    assert cell2["sharpe"] is None and cell2["excess_sharpe"] is None
    assert cell2["sortino"] is None


def test_sanitize_annual_blocks_clears_only_noise():
    """读取面清噪：修复前落库的年度噪声块（生产库 147 条，最大 16332.48）
    必须在出口处被清掉，且只清噪声。

    `calmar` 不入闸（低回撤/短窗口可合法 > 50，R10 结论）；合法值原样保留。
    """
    from rule_backtest.metrics import sanitize_annual_blocks

    blocks = [
        {"year": 2015, "return": 0.0, "sharpe": 0.0, "calmar": 0.0,
         "benchmark_return": 3.1637, "benchmark_sharpe": 16332.48, "benchmark_calmar": 78.5},
        {"year": 2016, "return": 0.12, "sharpe": 0.83, "calmar": 1.4,
         "benchmark_return": 0.02, "benchmark_sharpe": 0.62, "benchmark_calmar": 0.3},
    ]
    out = sanitize_annual_blocks(blocks)
    assert out[0]["benchmark_sharpe"] is None, "16332 级噪声必须在出口清掉"
    assert out[0]["benchmark_calmar"] == 78.5, "calmar 不在闸门内"
    assert out[1]["benchmark_sharpe"] == 0.62, "合法值不得被动"
    assert out[1]["sharpe"] == 0.83
    assert sanitize_annual_blocks(None) == []
    assert sanitize_annual_blocks([{"year": 1, "sharpe": "bad"}])[0]["sharpe"] == "bad"


def test_aggregate_annual_returns_skips_noise_rows():
    """策略×年份聚合（前端热力图 + annual_aggregates.csv 的输入）必须先清噪：

    否则中位数会被 16332 级噪声污染，且该噪声已落库（写入面闸门管不到历史行）。
    """
    import json as _json

    from rule_backtest.batch_service import aggregate_annual_returns

    rows = [
        {"strategy_id": "s1", "strategy_name": "s1", "annual_returns_json": _json.dumps([
            {"year": 2015, "return": 0.01, "benchmark_return": 0.0,
             "sharpe": 16332.48, "benchmark_sharpe": 0.5, "trade_count": 3},
        ])},
        {"strategy_id": "s1", "strategy_name": "s1", "annual_returns_json": _json.dumps([
            {"year": 2015, "return": 0.03, "benchmark_return": 0.0,
             "sharpe": 0.9, "benchmark_sharpe": 0.7, "trade_count": 4},
        ])},
    ]
    out = aggregate_annual_returns(rows)
    assert len(out) == 1 and out[0]["year"] == 2015
    # 未清噪时中位数 = (0.9 + 16332.48)/2 ≈ 8166 → 断言只剩合法值
    assert out[0]["median_sharpe"] == pytest.approx(0.9), "中位数只应由合法值参与"
    assert out[0]["trade_count"] == 7


def test_api_cell_blobs_are_sanitized():
    """HTTP 出口同样清噪（历史行经 API 仍会被前端显示）。"""
    import json as _json

    from app.routers.batch_backtest import _parse_cell_blobs

    row = {"symbol": "X.SS", "annual_returns_json": _json.dumps([
        {"year": 2015, "benchmark_sharpe": 16332.48, "sharpe": 0.4},
    ])}
    parsed = _parse_cell_blobs(row)
    assert parsed["annual_returns"][0]["benchmark_sharpe"] is None
    assert parsed["annual_returns"][0]["sharpe"] == 0.4


def test_offline_scripts_gate_noise_ratios():
    """两个离线写库/写文件脚本也必须过闸（R11A-F3）。

    `scripts/backfill_batch_excess_metrics.py` 把结果写回
    `batch_backtest_cells.benchmark_sharpe` 列；`scripts/run_base_v1_sample.py`
    把指标写进 `data/research/base_v1_sample/comparison.json|md`——都属闸门明文
    覆盖的持久化面。构造单调 NAV（日收益恒等 → std 是浮点残差）复现噪声形态。
    """
    import importlib.util
    import sys
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[2] / "scripts"
    sys.path.insert(0, str(scripts))

    def _load(name):
        spec = importlib.util.spec_from_file_location(name, scripts / name)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    monotone = [{"date": f"2024-01-{i + 1:02d}", "equity": 100_000.0 + i * 200.0}
                for i in range(28)]
    sample = _load("run_base_v1_sample.py")
    out = sample._summarize(monotone)
    # 单调序列：Sharpe 是 1e16 级浮点残差噪声 → 必须置 None；Sortino 在旧栈
    # 有 `std>0` 守卫、报 0.0（既有语义，非噪声）→ 照常保留（防过拦截的另一侧）
    assert out["sharpe"] is None, "落盘产物不得写噪声夏普"
    assert out["sortino"] == 0.0
    assert out["annual_return"] is not None and out["calmar"] is not None

    # backfill 脚本的闸门由 tests/integration/test_loop_review_ds4f_r13.py::
    # test_offline_backfill_helper_gates_noise 行为级钉住（此处原先是空钉：
    # 只断言 callable(助手) 与一个与脚本无关的常量，在完全无闸门的代码上也通过
    # ——R13B 实证）
    # 合法波动的序列不得被闸（对照）
    noisy = [{"date": f"2024-01-{i + 1:02d}",
              "equity": 100_000.0 + i * 150.0 + (2_500.0 if i % 2 else -2_000.0)}
             for i in range(28)]
    legit = sample._summarize(noisy)
    assert legit["sharpe"] is not None and abs(legit["sharpe"]) < 50


def test_engine_runs_header_cannot_be_deleted(test_db):
    """run 头的可复现性锚点不得被 DELETE 抹掉。

    R11B 独立审计实测：`DELETE FROM engine_runs` 此前**成功**，而 13 张兄弟表
    （engine 子表 / 研究台账 / 组合版本）全部 ABORT——run 头的 config_hash /
    data_version / git_hash / resolved_config_yaml 是 §5.6 锚点，一条 DELETE
    即可抹掉口径来源而子证据行仍在。允许的生命周期更新（status/finished_at）
    必须仍然可用。
    """
    with test_db.connect() as conn:
        conn.execute(
            "INSERT INTO engine_runs (run_id, kind, strategy_ref, config_hash, "
            "resolved_config_yaml, run_params_json, data_version, engine_version, git_hash) "
            "VALUES ('R-del','backtest','x','h','y','{}',1,'v','g')"
        )
        conn.execute(
            "UPDATE engine_runs SET status='failed', finished_at='2024-01-02 00:00:00' "
            "WHERE run_id='R-del'"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM engine_runs WHERE run_id='R-del'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE engine_runs SET git_hash='tampered' WHERE run_id='R-del'")
