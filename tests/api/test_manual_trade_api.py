"""API tests for the manual-trade endpoints.

POST /manual-trade/api/evaluate 同步计算止损价与持仓指标；
GET /manual-trade 渲染页面。使用 conftest 的隔离 test_db。
"""

from __future__ import annotations

import pandas as pd
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
    return test_db, bars


class TestManualTradePage:
    def test_page_renders(self, client) -> None:
        resp = client.get("/manual-trade")
        assert resp.status_code == 200
        assert "手工交易" in resp.text


class TestManualTradeEvaluateApi:
    def test_evaluate_ok(self, client, populated_db) -> None:
        _, bars = populated_db
        row = bars.iloc[-3]
        buy_date = str(row["time"])[:10]
        buy_price = round(float(row["close"]), 4)

        resp = client.post(
            "/manual-trade/api/evaluate",
            json={"symbol": "510300", "buy_date": buy_date, "buy_price": buy_price},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == "510300.SS"
        assert data["name"] == "沪深300"
        assert data["stops"]["hard_stop_price"] < buy_price
        assert data["stops"]["chandelier_stop_price"] > 0
        assert data["holding"]["hold_days"] >= 1
        assert "max_drawdown" in data["holding"]
        assert "sharpe" in data["holding"]

    def test_evaluate_unknown_symbol_400(self, client, populated_db) -> None:
        resp = client.post(
            "/manual-trade/api/evaluate",
            json={"symbol": "999999", "buy_date": "2025-01-10", "buy_price": 1.0},
        )
        assert resp.status_code == 400
        assert "未找到" in resp.json()["detail"]

    def test_evaluate_future_buy_date_400(self, client, populated_db) -> None:
        resp = client.post(
            "/manual-trade/api/evaluate",
            json={"symbol": "510300", "buy_date": "2099-01-01", "buy_price": 1.0},
        )
        assert resp.status_code == 400
        assert "晚于最新数据" in resp.json()["detail"]

    def test_evaluate_invalid_price_422(self, client, populated_db) -> None:
        resp = client.post(
            "/manual-trade/api/evaluate",
            json={"symbol": "510300", "buy_date": "2025-01-10", "buy_price": -1},
        )
        assert resp.status_code == 422

    def test_evaluate_missing_fields_422(self, client, populated_db) -> None:
        resp = client.post("/manual-trade/api/evaluate", json={"symbol": "510300"})
        assert resp.status_code == 422

    def test_evaluate_price_out_of_day_range_400(self, client, populated_db) -> None:
        """买入价超出买入日 [low, high] 区间 → 400，报错含区间。"""
        _, bars = populated_db
        row = bars.iloc[-3]
        buy_date = str(row["time"])[:10]
        too_high = round(float(row["high"]) + 0.5, 4)

        resp = client.post(
            "/manual-trade/api/evaluate",
            json={"symbol": "510300", "buy_date": buy_date, "buy_price": too_high},
        )
        assert resp.status_code == 400
        assert "当日价格区间" in resp.json()["detail"]

    def test_evaluate_intraday_overlay(self, client, populated_db, monkeypatch) -> None:
        _, bars = populated_db
        row = bars.iloc[-3]
        buy_date = str(row["time"])[:10]
        buy_price = round(float(row["close"]), 4)
        last_close = float(bars.iloc[-1]["close"])
        monkeypatch.setattr(
            sl,
            "_fetch_intraday_bar",
            lambda symbol, df: {
                "time": pd.Timestamp("2025-03-03 10:30:00"),
                "open": last_close,
                "high": last_close * 1.06,
                "low": last_close * 0.99,
                "close": last_close * 1.05,
                "volume": 0.0,
                "amount": 0.0,
            },
        )

        resp = client.post(
            "/manual-trade/api/evaluate",
            json={"symbol": "510300", "buy_date": buy_date, "buy_price": buy_price},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["is_intraday"] is True
        assert data["intraday_ts"] is not None
        assert data["stops"]["is_intraday"] is True
        assert data["stops"]["highest_since_buy"] == pytest.approx(round(last_close * 1.06, 4))
