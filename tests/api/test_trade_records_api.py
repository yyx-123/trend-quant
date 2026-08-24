"""API tests for the manual-trade record endpoints（交易记录：录入/清仓/列表）。

鉴权为全站登录墙 session cookie（2026-08 起）：测试先调 /api/auth/login
换取 cookie（TestClient 自动携带），业务请求体不再携带用户名密码。
登录墙本身的行为（未登录拦截/跳转）见 test_auth_wall.py；
试算接口 POST /manual-trade/api/evaluate 见 test_manual_trade_api.py。
"""

from __future__ import annotations

import pytest

import services.stop_loss as sl


@pytest.fixture(autouse=True)
def _no_intraday_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认走纯 EOD 路径，避免测试在交易时段访问实时行情。"""
    monkeypatch.setattr(sl, "_fetch_intraday_bar", lambda symbol, df: None)
    monkeypatch.setattr(sl, "fetch_intraday_bars", lambda dfs: dict.fromkeys(dfs, None))


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


def _login(client, username: str, password: str) -> dict:
    """切换当前会话用户（cookie 覆盖 conftest 自动登录的 tester）。"""
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200
    return resp.json()


class TestTradeCreateListApi:
    def test_create_then_list(self, client, populated_db) -> None:
        _, bars, *_ = populated_db
        user = _login(client, "alice", "pw1")
        assert user["username"] == "alice"
        assert user["is_admin"] is False

        form = _buy_point(bars)
        resp = client.post("/manual-trade/api/trades/create", json={**form, "shares": 1000})
        assert resp.status_code == 200
        trade = resp.json()
        assert trade["symbol"] == "510300.SS"
        assert trade["status"] == "open"

        resp = client.post("/manual-trade/api/trades/list", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["user"]["username"] == "alice"
        assert len(data["trades"]) == 1
        item = data["trades"][0]
        assert item["id"] == trade["id"]
        assert item["name"] == "沪深300"
        assert item["position_value"] > 0
        assert item["stops"]["chandelier_stop_price"] > 0
        assert item["holding"]["hold_days"] >= 1

    def test_list_stop_mode_tight(self, client, populated_db) -> None:
        """列表接口支持 stop_mode：紧止损下持仓的止损倍数与价格同步切换。"""
        _, bars, *_ = populated_db
        _login(client, "alice", "pw1")
        client.post(
            "/manual-trade/api/trades/create",
            json={**_buy_point(bars), "shares": 100},
        )

        loose = client.post("/manual-trade/api/trades/list", json={}).json()["trades"][0]
        tight = client.post(
            "/manual-trade/api/trades/list", json={"stop_mode": "tight"}
        ).json()["trades"][0]

        assert loose["stops"]["stop_mode"] == "loose"
        assert tight["stops"]["stop_mode"] == "tight"
        assert tight["stops"]["hard_stop_atr_mul"] == 1.0
        assert tight["stops"]["chandelier_stop_atr_mul"] == 2.0
        assert tight["stops"]["hard_stop_price"] > loose["stops"]["hard_stop_price"]

    def test_create_price_out_of_range_400(self, client, populated_db) -> None:
        _, bars, *_ = populated_db
        _login(client, "alice", "pw1")
        row = bars.iloc[-3]
        resp = client.post(
            "/manual-trade/api/trades/create",
            json={
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
        _login(client, "alice", "pw1")
        client.post(
            "/manual-trade/api/trades/create",
            json={**_buy_point(bars), "shares": 100},
        )
        _login(client, "bob", "pw2")
        resp = client.post("/manual-trade/api/trades/list", json={})
        assert resp.status_code == 200
        assert resp.json()["trades"] == []


class TestMyTradeAnnotationsApi:
    """GET /market-view/api/my-trades：标的查看页买卖点 + 止损标注数据。"""

    def test_annotations_for_open_trade(self, client, populated_db) -> None:
        _, bars, *_ = populated_db
        _login(client, "alice", "pw1")
        client.post(
            "/manual-trade/api/trades/create",
            json={**_buy_point(bars, -5), "shares": 1000},
        )
        resp = client.get("/market-view/api/my-trades", params={"symbol": "510300"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == "510300.SS"
        assert len(data["trades"]) == 1
        item = data["trades"][0]
        assert item["buy_price"] > 0
        assert item["stops"]["tight"]["hard_stop_price"] > 0
        assert item["stops"]["loose"]["hard_stop_price"] > 0

    def test_no_position_returns_empty(self, client, populated_db) -> None:
        _ = populated_db
        _login(client, "bob", "pw2")
        resp = client.get("/market-view/api/my-trades", params={"symbol": "510300"})
        assert resp.status_code == 200
        assert resp.json()["trades"] == []

    def test_invalid_symbol_400(self, client, populated_db) -> None:
        _ = populated_db
        # 纯空白归一化为空串 → 400（与 /api/daily 同口径）
        resp = client.get("/market-view/api/my-trades", params={"symbol": " "})
        assert resp.status_code == 400


class TestTradeCloseApi:
    def _create(self, client, bars) -> int:
        resp = client.post(
            "/manual-trade/api/trades/create",
            json={**_buy_point(bars, -5), "shares": 1000},
        )
        assert resp.status_code == 200
        return resp.json()["id"]

    def test_close_then_list_shows_closed_last(self, client, populated_db) -> None:
        _, bars, *_ = populated_db
        _login(client, "alice", "pw1")
        trade_id = self._create(client, bars)
        sell = _buy_point(bars, -2)
        resp = client.post(
            "/manual-trade/api/trades/close",
            json={
                "trade_id": trade_id,
                "sell_date": sell["buy_date"],
                "sell_price": sell["buy_price"],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "closed"

        resp = client.post("/manual-trade/api/trades/list", json={})
        item = resp.json()["trades"][0]
        assert item["status"] == "closed"
        assert item["sell_date"] == sell["buy_date"]
        assert item["realized_pnl"] != 0
        assert item["holding"]["hold_days"] == 4  # idx -5 ~ -2 含两端

    def test_close_others_trade_403(self, client, populated_db) -> None:
        _, bars, *_ = populated_db
        _login(client, "alice", "pw1")
        trade_id = self._create(client, bars)
        _login(client, "bob", "pw2")
        sell = _buy_point(bars, -2)
        resp = client.post(
            "/manual-trade/api/trades/close",
            json={
                "trade_id": trade_id,
                "sell_date": sell["buy_date"],
                "sell_price": sell["buy_price"],
            },
        )
        assert resp.status_code == 403

    def test_admin_can_close_others_trade(self, client, populated_db) -> None:
        _, bars, *_ = populated_db
        _login(client, "alice", "pw1")
        trade_id = self._create(client, bars)
        _login(client, "admin", "root")
        sell = _buy_point(bars, -2)
        resp = client.post(
            "/manual-trade/api/trades/close",
            json={
                "trade_id": trade_id,
                "sell_date": sell["buy_date"],
                "sell_price": sell["buy_price"],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "closed"

    def test_double_close_400(self, client, populated_db) -> None:
        _, bars, *_ = populated_db
        _login(client, "alice", "pw1")
        trade_id = self._create(client, bars)
        sell = _buy_point(bars, -2)
        payload = {
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
        _login(client, "alice", "pw1")
        trade_id = self._create(client, bars)
        row = bars.iloc[-2]
        resp = client.post(
            "/manual-trade/api/trades/close",
            json={
                "trade_id": trade_id,
                "sell_date": str(row["time"])[:10],
                "sell_price": round(float(row["high"]) + 0.5, 4),
            },
        )
        assert resp.status_code == 400
        assert "当日价格区间" in resp.json()["detail"]
