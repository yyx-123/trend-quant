"""services.trade_records.open_positions_overview：持仓概览的聚合与汇总口径。"""

from __future__ import annotations

import pytest

from services import trade_records as tr


@pytest.fixture
def canned_list_trades(monkeypatch: pytest.MonkeyPatch):
    def _set(trades: list[dict]) -> None:
        monkeypatch.setattr(
            tr,
            "list_trades",
            lambda user, db=None, intraday=True, stop_mode=None: {
                "user": user,
                "trades": trades,
            },
        )

    return _set


_USER = {"id": 1, "username": "yyx", "is_admin": True}


def _open_trade(**overrides) -> dict:
    base = {
        "id": 1,
        "status": "open",
        "symbol": "510300.SS",
        "name": "沪深300ETF",
        "buy_date": "2026-08-10",
        "buy_price": 4.0,
        "shares": 100,
        "latest_price": 4.2,
        "position_value": 420.0,
        "pnl_amount": 20.0,
        "stops": {"hard_stop_price": 3.9, "stop_mode": "loose"},
        "holding": {"pnl_pct": 5.0, "hold_days": 10},
    }
    base.update(overrides)
    return base


def test_summary_aggregates_open_positions(canned_list_trades) -> None:
    canned_list_trades([_open_trade()])
    result = tr.open_positions_overview(_USER)
    assert result["ok"] is True
    assert result["user"] == "yyx"
    assert result["summary"]["count"] == 1
    assert result["summary"]["total_position_value"] == 420.0
    assert result["summary"]["total_pnl_amount"] == 20.0
    assert result["summary"]["total_pnl_pct"] == pytest.approx(5.0)
    pos = result["positions"][0]
    assert pos["hard_stop_price"] == 3.9
    assert pos["pnl_pct"] == 5.0


def test_error_positions_excluded_from_summary(canned_list_trades) -> None:
    canned_list_trades(
        [
            _open_trade(),
            _open_trade(
                id=2,
                symbol="999999.SS",
                name="坏",
                error="未找到数据",
                latest_price=None,
                position_value=None,
                pnl_amount=None,
            ),
        ]
    )
    result = tr.open_positions_overview(_USER)
    assert len(result["positions"]) == 2
    # error 持仓不计入汇总金额/浮盈
    assert result["summary"]["total_position_value"] == 420.0
    assert result["summary"]["total_pnl_amount"] == 20.0
    error_row = [p for p in result["positions"] if p.get("error")]
    assert error_row and error_row[0]["symbol"] == "999999.SS"


def test_closed_trades_excluded(canned_list_trades) -> None:
    canned_list_trades([_open_trade(status="closed")])
    result = tr.open_positions_overview(_USER)
    assert result["positions"] == []
    assert result["summary"]["count"] == 0
    assert result["summary"]["total_pnl_pct"] == 0.0


def test_intraday_flags_propagate(canned_list_trades) -> None:
    canned_list_trades([_open_trade(is_intraday=True, intraday_ts="2026-08-27T10:00:00")])
    result = tr.open_positions_overview(_USER)
    assert result["is_intraday"] is True
    assert result["intraday_ts"] == "2026-08-27T10:00:00"
