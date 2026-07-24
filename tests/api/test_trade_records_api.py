"""API tests for the manual-trade record endpoints（交易记录：登录/录入/清仓/列表）。

极简无状态鉴权：每个请求体携带 username + password。
试算接口 POST /manual-trade/api/evaluate 保持公开，见 test_manual_trade_api.py。
"""

from __future__ import annotations

import pytest

import services.stop_loss as sl


@pytest.fixture(autouse=True)
def _no_intraday_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认走纯 EOD 路径，避免测试在交易时段访问实时行情。"""
    monkeypatch.setattr(sl, "_fetch_intraday_bar", lambda symbol, df: None)


@pytest.fixture
def populated_db(test_db):
    from conftest import make_bull_bars

    bars = make_bull_bars(40)
    test_db.save_market_data("510300.SS", bars, price_mode="qfq")
    test_db.save_instrument_metadata([{"symbol": "510300.SS", "name": "沪深300ETF"}])
    alice = test_db.create_user("alice", "pw1")
    bob = test_db.create_user("bob", "pw2")
    admin = test_db.create_user("admin", "root", is_admin=True)
    return test_db, bars, alice, bob, admin


def _buy_point(bars, idx: int = -3) -> dict:
    row = bars.iloc[idx]
    return {
        "symbol": "510300",
        "buy_date": str(row["time"])[:10],
        "buy_price": round(float(row["close"]), 4),
    }


ALICE = {"username": "alice", "password": "pw1"}
BOB = {"username": "bob", "password": "pw2"}
ADMIN = {"username": "admin", "password": "root"}


class TestLoginApi:
    def test_login_ok(self, client, populated_db) -> None:
        resp = client.post("/manual-trade/api/login", json=ALICE)
        assert resp.status_code == 200
        assert resp.json() == {"id": 1, "username": "alice", "is_admin": False}

    def test_login_admin_flag(self, client, populated_db) -> None:
        resp = client.post("/manual-trade/api/login", json=ADMIN)
        assert resp.status_code == 200
        assert resp.json()["is_admin"] is True

    def test_login_wrong_password_401(self, client, populated_db) -> None:
        resp = client.post(
            "/manual-trade/api/login", json={"username": "alice", "password": "bad"}
        )
        assert resp.status_code == 401

    def test_login_missing_fields_422(self, client, populated_db) -> None:
        resp = client.post("/manual-trade/api/login", json={"username": "alice"})
        assert resp.status_code == 422


class TestTradeCreateListApi:
    def test_create_then_list(self, client, populated_db) -> None:
        _, bars, *_ = populated_db
        form = _buy_point(bars)
        resp = client.post(
            "/manual-trade/api/trades/create", json={**ALICE, **form, "shares": 1000}
        )
        assert resp.status_code == 200
        trade = resp.json()
        assert trade["symbol"] == "510300.SS"
        assert trade["status"] == "open"

        resp = client.post("/manual-trade/api/trades/list", json=ALICE)
        assert resp.status_code == 200
        data = resp.json()
        assert data["viewing"]["username"] == "alice"
        assert len(data["trades"]) == 1
        item = data["trades"][0]
        assert item["id"] == trade["id"]
        assert item["name"] == "沪深300"
        assert item["position_value"] > 0
        assert item["stops"]["chandelier_stop_price"] > 0
        assert item["holding"]["hold_days"] >= 1

    def test_create_requires_auth_401(self, client, populated_db) -> None:
        _, bars, *_ = populated_db
        resp = client.post(
            "/manual-trade/api/trades/create",
            json={"username": "alice", "password": "bad", **_buy_point(bars), "shares": 100},
        )
        assert resp.status_code == 401

    def test_create_price_out_of_range_400(self, client, populated_db) -> None:
        _, bars, *_ = populated_db
        row = bars.iloc[-3]
        resp = client.post(
            "/manual-trade/api/trades/create",
            json={
                **ALICE,
                "symbol": "510300",
                "buy_date": str(row["time"])[:10],
                "buy_price": round(float(row["high"]) + 0.5, 4),
                "shares": 100,
            },
        )
        assert resp.status_code == 400
        assert "当日价格区间" in resp.json()["detail"]

    def test_records_isolated_between_users(self, client, populated_db) -> None:
        _, bars, *_ = populated_db
        client.post(
            "/manual-trade/api/trades/create",
            json={**ALICE, **_buy_point(bars), "shares": 100},
        )
        resp = client.post("/manual-trade/api/trades/list", json=BOB)
        assert resp.status_code == 200
        assert resp.json()["trades"] == []

    def test_non_admin_cannot_view_others_403(self, client, populated_db) -> None:
        _, _, alice, *_ = populated_db
        resp = client.post(
            "/manual-trade/api/trades/list", json={**BOB, "user_id": alice["id"]}
        )
        assert resp.status_code == 403

    def test_admin_can_view_others(self, client, populated_db) -> None:
        _, bars, alice, *_ = populated_db
        client.post(
            "/manual-trade/api/trades/create",
            json={**ALICE, **_buy_point(bars), "shares": 100},
        )
        resp = client.post(
            "/manual-trade/api/trades/list", json={**ADMIN, "user_id": alice["id"]}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["viewing"]["username"] == "alice"
        assert len(data["trades"]) == 1


class TestTradeCloseApi:
    def _create(self, client, bars) -> int:
        resp = client.post(
            "/manual-trade/api/trades/create",
            json={**ALICE, **_buy_point(bars, -5), "shares": 1000},
        )
        assert resp.status_code == 200
        return resp.json()["id"]

    def test_close_then_list_shows_closed_last(self, client, populated_db) -> None:
        _, bars, *_ = populated_db
        trade_id = self._create(client, bars)
        sell = _buy_point(bars, -2)
        resp = client.post(
            "/manual-trade/api/trades/close",
            json={
                **ALICE,
                "trade_id": trade_id,
                "sell_date": sell["buy_date"],
                "sell_price": sell["buy_price"],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "closed"

        resp = client.post("/manual-trade/api/trades/list", json=ALICE)
        item = resp.json()["trades"][0]
        assert item["status"] == "closed"
        assert item["sell_date"] == sell["buy_date"]
        assert item["realized_pnl"] != 0
        assert item["holding"]["hold_days"] == 4  # idx -5 ~ -2 含两端

    def test_close_others_trade_403(self, client, populated_db) -> None:
        _, bars, *_ = populated_db
        trade_id = self._create(client, bars)
        sell = _buy_point(bars, -2)
        resp = client.post(
            "/manual-trade/api/trades/close",
            json={
                **BOB,
                "trade_id": trade_id,
                "sell_date": sell["buy_date"],
                "sell_price": sell["buy_price"],
            },
        )
        assert resp.status_code == 403

    def test_double_close_400(self, client, populated_db) -> None:
        _, bars, *_ = populated_db
        trade_id = self._create(client, bars)
        sell = _buy_point(bars, -2)
        payload = {
            **ALICE,
            "trade_id": trade_id,
            "sell_date": sell["buy_date"],
            "sell_price": sell["buy_price"],
        }
        assert client.post("/manual-trade/api/trades/close", json=payload).status_code == 200
        resp = client.post("/manual-trade/api/trades/close", json=payload)
        assert resp.status_code == 400
        assert "已清仓" in resp.json()["detail"]

    def test_close_price_out_of_range_400(self, client, populated_db) -> None:
        _, bars, *_ = populated_db
        trade_id = self._create(client, bars)
        row = bars.iloc[-2]
        resp = client.post(
            "/manual-trade/api/trades/close",
            json={
                **ALICE,
                "trade_id": trade_id,
                "sell_date": str(row["time"])[:10],
                "sell_price": round(float(row["high"]) + 0.5, 4),
            },
        )
        assert resp.status_code == 400
        assert "当日价格区间" in resp.json()["detail"]


class TestUsersListApi:
    def test_admin_gets_user_list(self, client, populated_db) -> None:
        resp = client.post("/manual-trade/api/users/list", json=ADMIN)
        assert resp.status_code == 200
        names = {u["username"] for u in resp.json()}
        assert names == {"alice", "bob", "admin"}
        assert all("password" not in u for u in resp.json())

    def test_non_admin_403(self, client, populated_db) -> None:
        resp = client.post("/manual-trade/api/users/list", json=ALICE)
        assert resp.status_code == 403
