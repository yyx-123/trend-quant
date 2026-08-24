"""trend_mcp.server 止损工具的 stop_mode 透传测试（与网页端手工交易同口径）。"""

from __future__ import annotations

from trend_mcp import server


def test_calc_stop_loss_passes_stop_mode(monkeypatch):
    captured = {}

    def fake_compute(symbol, buy_date, buy_price, **kwargs):
        captured.update(kwargs)
        return {"hard_stop_price": 1.0, "stop_mode": kwargs.get("stop_mode") or "loose"}

    monkeypatch.setattr(server, "compute_stop_loss", fake_compute)

    tight = server.calc_stop_loss("510300.SS", "2026-08-10", 4.0, stop_mode="tight")
    assert tight["ok"] is True
    assert captured["stop_mode"] == "tight"
    assert tight["stop_mode"] == "tight"

    default = server.calc_stop_loss("510300.SS", "2026-08-10", 4.0)
    assert default["ok"] is True
    assert captured["stop_mode"] is None  # 不传则底层走默认宽松口径


def test_open_positions_passes_stop_mode(monkeypatch):
    captured = {}

    def fake_authenticate(username, password):
        return {"id": 1, "username": username, "is_admin": False}

    def fake_list_trades(user, **kwargs):
        captured.update(kwargs)
        return {
            "user": {"username": user["username"]},
            "trades": [
                {
                    "id": 1,
                    "status": "open",
                    "symbol": "510300.SS",
                    "name": "沪深300ETF",
                    "buy_date": "2026-08-10",
                    "buy_price": 4.0,
                    "shares": 100,
                    "latest_price": 4.2,
                    "stops": {"hard_stop_price": 3.9, "stop_mode": kwargs.get("stop_mode") or "loose"},
                    "holding": {},
                }
            ],
        }

    monkeypatch.setattr(server.tr, "authenticate", fake_authenticate)
    monkeypatch.setattr(server.tr, "list_trades", fake_list_trades)

    result = server.open_positions("alice", "pw", stop_mode="tight")
    assert result["ok"] is True
    assert captured["stop_mode"] == "tight"
    assert captured["intraday"] is True
    assert result["positions"][0]["stop_mode"] == "tight"

    server.open_positions("alice", "pw")
    assert captured["stop_mode"] is None
