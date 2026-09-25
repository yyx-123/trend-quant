"""Round 15 修复钉子（round15-review.md 各项的回归锚）。

本轮抓到 **1 项 P1**（14 轮里第一次出现在 A 股交易规则自身）：
创业板涨跌停幅度没有日期维度——2020-08-24 之前真实是 ±10%，代码却一直按 ±20%，
导致真实涨停/跌停日被判可成交（生产库 13 笔不可达成交、1437 个 symbol-day 受影响）。
另两条 P2：`reconcile_daily_list` 的写回缺 user_id、写入侧守卫只覆盖一个入口。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest


def test_chinext_band_has_date_dimension():
    """R15B-P1-1：创业板 ±20% 是 2020-08-24 起的规则，此前是 ±10%。

    同业口径自洽检查：`_ipo_no_limit_days` 早已用同一分界（创业板注册制），
    只有幅度漏了日期维度。
    """
    from gateway.tradability import board_limit_pct

    cut = date(2020, 8, 24)
    # 创业板股票
    assert board_limit_pct("300059.SZ", asset_type="stock", as_of=date(2015, 1, 12)) == 0.10
    assert board_limit_pct("300059.SZ", asset_type="stock", as_of=date(2020, 8, 21)) == 0.10
    assert board_limit_pct("300059.SZ", asset_type="stock", as_of=cut) == 0.20
    # 创业板系 ETF（同样随改革日切换）
    assert board_limit_pct("159915.SZ", asset_type="etf", name="创业板ETF易方达",
                           as_of=date(2016, 5, 5)) == 0.10
    assert board_limit_pct("159915.SZ", asset_type="etf", name="创业板ETF易方达",
                           as_of=date(2024, 5, 5)) == 0.20
    # 科创板自开板即 20%（不随该日期变化）；科创板系 ETF 同理
    assert board_limit_pct("688001.SS", asset_type="stock", as_of=date(2019, 8, 1)) == 0.20
    assert board_limit_pct("588000.SS", asset_type="etf", name="科创50ETF华夏",
                           as_of=date(2019, 8, 1)) == 0.20
    # 主板不受影响
    assert board_limit_pct("600519.SS", asset_type="stock", as_of=date(2016, 1, 4)) == 0.10
    # 缺省 = 当前口径（=20%），保持既有调用点行为
    assert board_limit_pct("300750.SZ") == 0.20


def test_tradability_applies_chinext_band_by_day():
    """端到端：同一创业板标的在改革前后用不同幅度算限价。"""
    from gateway.tradability import compute_tradability

    days = [date(2015, 1, 9), date(2015, 1, 12), date(2021, 1, 11), date(2021, 1, 12)]
    closes = pd.Series({days[0]: 35.0, days[1]: 38.5, days[2]: 30.0, days[3]: 36.0})
    frame = compute_tradability(
        None, symbols=["300059.SZ"], dates=days,
        raw_closes={"300059.SZ": closes}, ex_factors={"300059.SZ": []},
        asset_info={"300059.SZ": {"asset_type": "stock", "name": "东方财富"}},
        listing_dates={"300059.SZ": None},
    )
    r = {row["date"]: row for row in frame.to_dict("records")}
    # 2015-01-12：前收 35.0 → ±10% → 涨停 38.5（正是当年那笔"买在涨停收盘"的成交价）
    assert r[days[1]]["limit_up_price"] == pytest.approx(38.5, abs=1e-9)
    assert bool(r[days[1]]["is_limit_up"]) is True, "改革前真涨停必须被判为不可买"
    # 2021-01-12：前收 30.0 → ±20% → 涨停 36.0
    assert r[days[3]]["limit_up_price"] == pytest.approx(36.0, abs=1e-9)
    assert bool(r[days[3]]["is_limit_up"]) is True


def test_reconcile_writeback_is_user_scoped(test_db):
    """R15A-F1：对账写回必须带 user_id（否则同键下跨用户串写）。"""
    import json

    from portfolio.live import reconcile_daily_list

    u1 = test_db.create_user("r15a_u1", "pass12345")["id"]
    u2 = test_db.create_user("r15a_u2", "pass12345")["id"]
    with test_db.connect() as conn:
        for uid in (u1, u2):
            conn.execute(
                "INSERT INTO portfolio_live_lists (list_date, strategy_version_id,"
                " user_id, as_of, target_json) VALUES (?,?,?,?,?)",
                ("2026-09-24", "s@1", uid, "2026-09-24 14:00:00",
                 json.dumps({"buys": [], "sells": [], "target_holdings": []})),
            )
    # 用户 2 有持仓（user 1 没有）→ 对账结果只应落到 user 2 的行
    test_db.create_manual_trade(u2, "X2.SS", "2026-09-24", 10.0, 100)
    reconcile_daily_list(test_db, list_date="2026-09-24",
                         strategy_version_id="s@1", user_id=u2)
    with test_db.connect() as conn:
        rows = {r["user_id"]: r["reconcile_json"] for r in conn.execute(
            "SELECT user_id, reconcile_json FROM portfolio_live_lists"
            " WHERE list_date='2026-09-24' AND strategy_version_id='s@1'"
        )}
    assert rows[u2], "user 2 的对账结果必须写回自己的行"
    assert rows[u1] is None, "user 1 的行不得被 user 2 的对账串写"


def test_rebuild_all_is_frozen_guarded(test_db):
    """R15A-F2：守卫下沉到 rebuild_all —— 启动补偿/日更尾该重构入口同样被拦。

    此前只有 `rebuild_after_backfill` 有闸：冻结态下 `rebuild_if_needed` 仍会
    整段重写 indicator_daily/trend_daily（实测各 120 行）。
    """
    from core import run_freeze
    from data.service import FrozenWritesError
    from services import indicator_builder as ib

    import os
    import tempfile

    fd, lock = tempfile.mkstemp(suffix=".lock")
    os.close(fd)
    old = os.environ.get(run_freeze._FREEZE_FILE_ENV)
    os.environ[run_freeze._FREEZE_FILE_ENV] = lock
    try:
        with run_freeze.cross_process_frozen(lock):
            with pytest.raises(FrozenWritesError):
                ib.rebuild_all(["X.SS"], db=test_db)
            # 启动补偿 best-effort：跳过而不是抛（否则一次启动撞上批次会中断启动）
            out = ib.rebuild_if_needed(db=test_db)
            assert out.get("status") == "skipped_frozen", out
    finally:
        if old is None:
            os.environ.pop(run_freeze._FREEZE_FILE_ENV, None)
        else:
            os.environ[run_freeze._FREEZE_FILE_ENV] = old
        if os.path.exists(lock):
            os.unlink(lock)
