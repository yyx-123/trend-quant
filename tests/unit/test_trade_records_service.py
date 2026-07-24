"""Unit tests for services.trade_records（手工交易记录：凭据/录入/清仓/列表聚合）。

止损与持仓指标本身的口径测试见 test_stop_loss.py / test_manual_trade_service.py；
这里只覆盖：极简鉴权、录入校验、清仓权限与校验、列表排序与错误隔离。
"""

from __future__ import annotations

import pandas as pd
import pytest

from core.strategy_config import DEFAULT_STRATEGY_CONFIG
from services import manual_trade as mt
from services import stop_loss as sl
from services import trade_records as tr


@pytest.fixture(autouse=True)
def _pins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin strategy config + 纯 EOD 路径（不访问实时行情）。"""
    monkeypatch.setattr(sl, "get_strategy_config", lambda: dict(DEFAULT_STRATEGY_CONFIG))
    monkeypatch.setattr(sl, "_fetch_intraday_bar", lambda symbol, df: None)


@pytest.fixture
def env(test_db):
    from conftest import make_bull_bars

    bars = make_bull_bars(40)
    test_db.save_market_data("510300.SS", bars, price_mode="qfq")
    alice = test_db.create_user("alice", "pw1")
    bob = test_db.create_user("bob", "pw2")
    admin = test_db.create_user("admin", "root", is_admin=True)
    return test_db, bars, alice, bob, admin


def _day(bars, idx: int) -> tuple[str, float]:
    row = bars.iloc[idx]
    return str(row["time"])[:10], round(float(row["close"]), 4)


class TestAuthenticate:
    def test_ok(self, env) -> None:
        db, _, alice, _, _ = env
        user = tr.authenticate("alice", "pw1", db=db)
        assert user == {"id": alice["id"], "username": "alice", "is_admin": False}

    def test_admin_flag(self, env) -> None:
        db, *_ = env
        assert tr.authenticate("admin", "root", db=db)["is_admin"] is True

    def test_wrong_password(self, env) -> None:
        db, *_ = env
        with pytest.raises(tr.TradeAuthError, match="用户名或密码错误"):
            tr.authenticate("alice", "nope", db=db)

    def test_unknown_user(self, env) -> None:
        db, *_ = env
        with pytest.raises(tr.TradeAuthError):
            tr.authenticate("nobody", "pw", db=db)


class TestCreateTrade:
    def test_create_ok(self, env) -> None:
        db, bars, alice, *_ = env
        buy_date, buy_price = _day(bars, -5)
        trade = tr.create_trade(
            "alice", "pw1", symbol="510300", buy_date=buy_date,
            buy_price=buy_price, shares=1000, db=db,
        )
        assert trade["symbol"] == "510300.SS"  # 归一化
        assert trade["status"] == "open"
        assert trade["shares"] == 1000
        assert trade["user_id"] == alice["id"]

    def test_same_symbol_multiple_buys_are_independent(self, env) -> None:
        db, bars, alice, *_ = env
        d1, p1 = _day(bars, -8)
        d2, p2 = _day(bars, -4)
        t1 = tr.create_trade("alice", "pw1", symbol="510300", buy_date=d1,
                             buy_price=p1, shares=100, db=db)
        t2 = tr.create_trade("alice", "pw1", symbol="510300", buy_date=d2,
                             buy_price=p2, shares=200, db=db)
        assert t1["id"] != t2["id"]
        assert len(db.list_manual_trades(alice["id"])) == 2

    def test_price_out_of_day_range_rejected(self, env) -> None:
        db, bars, *_ = env
        row = bars.iloc[-5]
        with pytest.raises(sl.StopLossError, match="当日价格区间"):
            tr.create_trade(
                "alice", "pw1", symbol="510300", buy_date=str(row["time"])[:10],
                buy_price=round(float(row["high"]) + 0.5, 4), shares=100, db=db,
            )

    def test_invalid_shares_rejected(self, env) -> None:
        db, bars, *_ = env
        buy_date, buy_price = _day(bars, -5)
        with pytest.raises(tr.TradeRecordError, match="份数"):
            tr.create_trade("alice", "pw1", symbol="510300", buy_date=buy_date,
                            buy_price=buy_price, shares=0, db=db)

    def test_bad_credentials_rejected(self, env) -> None:
        db, bars, *_ = env
        buy_date, buy_price = _day(bars, -5)
        with pytest.raises(tr.TradeAuthError):
            tr.create_trade("alice", "bad", symbol="510300", buy_date=buy_date,
                            buy_price=buy_price, shares=100, db=db)


class TestCloseTrade:
    def _open_trade(self, db, bars, shares: float = 1000) -> dict:
        buy_date, buy_price = _day(bars, -5)
        return tr.create_trade("alice", "pw1", symbol="510300", buy_date=buy_date,
                               buy_price=buy_price, shares=shares, db=db)

    def test_close_ok(self, env) -> None:
        db, bars, *_ = env
        trade = self._open_trade(db, bars)
        sell_date, sell_price = _day(bars, -2)
        closed = tr.close_trade("alice", "pw1", trade_id=trade["id"],
                                sell_date=sell_date, sell_price=sell_price, db=db)
        assert closed["status"] == "closed"
        assert closed["sell_date"] == sell_date
        assert closed["sell_price"] == sell_price

    def test_close_requires_owner(self, env) -> None:
        db, bars, *_ = env
        trade = self._open_trade(db, bars)
        sell_date, sell_price = _day(bars, -2)
        with pytest.raises(tr.TradePermissionError, match="自己的"):
            tr.close_trade("bob", "pw2", trade_id=trade["id"],
                           sell_date=sell_date, sell_price=sell_price, db=db)

    def test_admin_can_close_any_trade(self, env) -> None:
        db, bars, *_ = env
        trade = self._open_trade(db, bars)
        sell_date, sell_price = _day(bars, -2)
        closed = tr.close_trade("admin", "root", trade_id=trade["id"],
                                sell_date=sell_date, sell_price=sell_price, db=db)
        assert closed["status"] == "closed"

    def test_double_close_rejected(self, env) -> None:
        db, bars, *_ = env
        trade = self._open_trade(db, bars)
        sell_date, sell_price = _day(bars, -2)
        tr.close_trade("alice", "pw1", trade_id=trade["id"],
                       sell_date=sell_date, sell_price=sell_price, db=db)
        with pytest.raises(tr.TradeRecordError, match="已清仓"):
            tr.close_trade("alice", "pw1", trade_id=trade["id"],
                           sell_date=sell_date, sell_price=sell_price, db=db)

    def test_sell_before_buy_rejected(self, env) -> None:
        db, bars, *_ = env
        trade = self._open_trade(db, bars)
        early_date, price = _day(bars, -8)
        with pytest.raises(tr.TradeRecordError, match="早于买入日期"):
            tr.close_trade("alice", "pw1", trade_id=trade["id"],
                           sell_date=early_date, sell_price=price, db=db)

    def test_sell_price_out_of_day_range_rejected(self, env) -> None:
        db, bars, *_ = env
        trade = self._open_trade(db, bars)
        row = bars.iloc[-2]
        with pytest.raises(tr.TradeRecordError, match="当日价格区间"):
            tr.close_trade("alice", "pw1", trade_id=trade["id"],
                           sell_date=str(row["time"])[:10],
                           sell_price=round(float(row["high"]) + 0.5, 4), db=db)

    def test_close_missing_trade(self, env) -> None:
        db, bars, *_ = env
        sell_date, sell_price = _day(bars, -2)
        with pytest.raises(tr.TradeRecordError, match="不存在"):
            tr.close_trade("alice", "pw1", trade_id=999,
                           sell_date=sell_date, sell_price=sell_price, db=db)


class TestListTrades:
    def test_open_sorted_by_position_value_closed_last(self, env) -> None:
        db, bars, alice, *_ = env
        # 小持仓先录入，大持仓后录入 —— 验证不是按录入顺序而是按持仓金额
        d1, p1 = _day(bars, -6)
        d2, p2 = _day(bars, -4)
        small = tr.create_trade("alice", "pw1", symbol="510300", buy_date=d1,
                                buy_price=p1, shares=100, db=db)
        big = tr.create_trade("alice", "pw1", symbol="510300", buy_date=d2,
                              buy_price=p2, shares=10000, db=db)
        closed = tr.create_trade("alice", "pw1", symbol="510300", buy_date=d1,
                                 buy_price=p1, shares=50000, db=db)
        sd, sp = _day(bars, -3)
        tr.close_trade("alice", "pw1", trade_id=closed["id"],
                       sell_date=sd, sell_price=sp, db=db)

        out = tr.list_trades("alice", "pw1", db=db)
        trades = out["trades"]
        assert [t["id"] for t in trades] == [big["id"], small["id"], closed["id"]]
        assert trades[0]["position_value"] > trades[1]["position_value"]
        assert trades[-1]["status"] == "closed"

    def test_open_item_realtime_fields(self, env) -> None:
        db, bars, *_ = env
        d, p = _day(bars, -3)
        tr.create_trade("alice", "pw1", symbol="510300", buy_date=d,
                        buy_price=p, shares=1000, db=db)
        out = tr.list_trades("alice", "pw1", db=db)
        item = out["trades"][0]
        latest_close = round(float(bars.iloc[-1]["close"]), 4)
        assert item["latest_price"] == latest_close
        assert item["position_value"] == pytest.approx(round(latest_close * 1000, 2))
        assert item["pnl_amount"] == pytest.approx(round((latest_close - p) * 1000, 2))
        assert item["stops"]["chandelier_stop_price"] > 0
        assert item["holding"]["hold_days"] == 3

    def test_closed_item_uses_sell_date_cutoff(self, env) -> None:
        db, bars, *_ = env
        bd, bp = _day(bars, -6)
        sd, sp = _day(bars, -2)
        trade = tr.create_trade("alice", "pw1", symbol="510300", buy_date=bd,
                                buy_price=bp, shares=1000, db=db)
        tr.close_trade("alice", "pw1", trade_id=trade["id"],
                       sell_date=sd, sell_price=sp, db=db)

        out = tr.list_trades("alice", "pw1", db=db)
        item = out["trades"][0]
        assert item["realized_pnl"] == pytest.approx(round((sp - bp) * 1000, 2))
        assert item["realized_pnl_pct"] == pytest.approx(round((sp / bp - 1) * 100, 2))
        # 持有期指标按清仓日截断：buy idx -6 ~ sell idx -2，含两端共 5 根K线
        assert item["holding"]["hold_days"] == 5

    def test_non_admin_cannot_view_others(self, env) -> None:
        db, _, alice, bob, _ = env
        with pytest.raises(tr.TradePermissionError, match="自己的"):
            tr.list_trades("bob", "pw2", user_id=alice["id"], db=db)

    def test_admin_can_view_others(self, env) -> None:
        db, bars, alice, _, admin = env
        d, p = _day(bars, -3)
        tr.create_trade("alice", "pw1", symbol="510300", buy_date=d,
                        buy_price=p, shares=100, db=db)
        out = tr.list_trades("admin", "root", user_id=alice["id"], db=db)
        assert out["viewing"]["username"] == "alice"
        assert len(out["trades"]) == 1
        # admin 默认看自己
        own = tr.list_trades("admin", "root", db=db)
        assert own["viewing"]["id"] == admin["id"]
        assert own["trades"] == []

    def test_single_trade_error_does_not_break_list(self, env) -> None:
        db, bars, alice, *_ = env
        d, p = _day(bars, -3)
        ok = tr.create_trade("alice", "pw1", symbol="510300", buy_date=d,
                             buy_price=p, shares=100, db=db)
        bad = db.create_manual_trade(alice["id"], "999999.SS", d, 1.0, 100)  # 无数据标的
        out = tr.list_trades("alice", "pw1", db=db)
        by_id = {t["id"]: t for t in out["trades"]}
        assert "error" not in by_id[ok["id"]]
        assert "error" in by_id[bad["id"]]


class TestListUsers:
    def test_admin_only(self, env) -> None:
        db, *_ = env
        users = tr.list_users("admin", "root", db=db)
        assert {u["username"] for u in users} == {"alice", "bob", "admin"}
        with pytest.raises(tr.TradePermissionError, match="管理员"):
            tr.list_users("alice", "pw1", db=db)


class TestEndDateCutoff:
    """compute_manual_trade 的 end_date 截断口径（已清仓交易复用）。"""

    def test_end_date_truncates_nav_and_latest(self, env) -> None:
        db, bars, *_ = env
        bd, bp = _day(bars, -6)
        sd, _ = _day(bars, -2)
        out = mt.compute_manual_trade("510300", bd, bp, db=db,
                                      intraday=False, end_date=sd)
        assert out["latest_date"] == sd
        assert out["is_intraday"] is False
        assert out["holding"]["hold_days"] == 5
        sell_close = round(float(bars.iloc[-2]["close"]), 4)
        assert out["stops"]["latest_price"] == sell_close

    def test_end_date_before_buy_rejected(self, env) -> None:
        db, bars, *_ = env
        bd, bp = _day(bars, -3)
        early, _ = _day(bars, -6)
        with pytest.raises(sl.StopLossError, match="早于买入日期"):
            mt.compute_manual_trade("510300", bd, bp, db=db,
                                    intraday=False, end_date=early)
