"""附录 A（H4）：trend_mcp/server.py 7 个工具的行为契约补测。

覆盖：trend_dashboard 缓存命中、intraday_dashboard 门控与 category 过滤、
symbol_detail 截尾/错误契约、calc_stop_loss 错误契约、add_trade/open_positions
错误路径与汇总口径。
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

pytest.importorskip("mcp")

from trend_mcp import server


def _bars(rows: int = 100) -> pd.DataFrame:
    start = date(2026, 1, 5)
    items = []
    price = 4.0
    for idx in range(rows):
        day = start + timedelta(days=idx)
        price += 0.01
        items.append(
            {
                "time": day.isoformat(),
                "open": price,
                "high": price + 0.05,
                "low": price - 0.05,
                "close": price + 0.02,
                "volume": 100000 + idx * 100,
                "amount": 400000 + idx * 400,
            }
        )
    return pd.DataFrame(items)


class _FakeDb:
    def __init__(self, df: pd.DataFrame) -> None:
        self.df = df

    def load_market_data(self, symbol: str, price_mode: str = "qfq") -> pd.DataFrame:
        df = self.df.copy()
        df["time"] = pd.to_datetime(df["time"])
        return df

    def get_market_data_summary(self, symbol: str, price_mode: str = "qfq") -> dict:
        return {
            "rows": len(self.df),
            "start": str(self.df["time"].iloc[0]),
            "end": str(self.df["time"].iloc[-1]),
        }


# ---------------------------------------------------------------------------
# trend_dashboard
# ---------------------------------------------------------------------------
class TestTrendDashboard:
    def test_second_call_hits_revision_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakeDb:
            def get_market_dashboard_revision(self):
                return ("2026-08-24 00:00:00", "", 1)

        monkeypatch.setattr(server, "get_db", lambda: FakeDb())
        calls: list[int] = []
        monkeypatch.setattr(
            server,
            "build_subject_dashboard_payload",
            lambda db: calls.append(1) or {"groups": []},
        )
        # 单例缓存可能已被其他测试填充——换一个独立缓存验证
        from services.dashboard import RevisionCache

        monkeypatch.setattr(server, "dashboard_revision_cache", RevisionCache())

        first = server.trend_dashboard()
        second = server.trend_dashboard()
        assert first is second
        assert calls == [1]


# ---------------------------------------------------------------------------
# intraday_dashboard
# ---------------------------------------------------------------------------
class TestIntradayDashboard:
    def test_non_trading_day_returns_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(server, "is_trading_day", lambda d: False)
        result = server.intraday_dashboard()
        assert result["ok"] is False
        assert "非交易日" in result["error"]

    def test_before_open_returns_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(server, "is_trading_day", lambda d: True)
        monkeypatch.setattr(server, "is_past_market_open", lambda now: False)
        result = server.intraday_dashboard()
        assert result["ok"] is False
        assert "尚未开盘" in result["error"]

    def _stub_market(self, monkeypatch: pytest.MonkeyPatch, symbols: list[str]) -> dict:
        class FakeDb:
            def list_market_symbols(self, price_mode="qfq"):
                return symbols

            def get_instrument_metadata_map(self):
                return {s: {"category_l1": "ETF", "category_l2": "宽基", "category_l3": "沪深300"} for s in symbols}

        monkeypatch.setattr(server, "get_db", lambda: FakeDb())
        monkeypatch.setattr(server, "is_trading_day", lambda d: True)
        monkeypatch.setattr(server, "is_past_market_open", lambda now: True)
        monkeypatch.setattr(server, "is_realtime_available", lambda now: False)  # post_close
        captured: dict = {}

        def fake_build(classified, db, ds, cfg):
            captured["classified"] = list(classified)
            return {"groups": []}

        monkeypatch.setattr(server, "build_intraday_dashboard", fake_build)
        monkeypatch.setattr(server, "get_data_service", lambda: object())
        return captured

    def test_category_filter_applies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = self._stub_market(monkeypatch, ["510300.SS", "510500.SS"])

        # category 不匹配任何标的 → ok=False
        result = server.intraday_dashboard(category="跨境")
        assert result["ok"] is False

        result = server.intraday_dashboard(category="宽基")
        assert result["ok"] is True
        assert captured["classified"] == ["510300.SS", "510500.SS"]
        assert result["post_close"] is True
        assert result["requested_category"] == "宽基"


# ---------------------------------------------------------------------------
# symbol_detail
# ---------------------------------------------------------------------------
class TestSymbolDetailContract:
    def test_days_tail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        df = _bars(100)
        monkeypatch.setattr(server, "get_db", lambda: _FakeDb(df))
        monkeypatch.setattr(server, "_config_name_map", dict)
        monkeypatch.setattr(server, "_load_instruments_raw", list)

        payload = server.symbol_detail("510300.SS", days=5)
        assert payload["ok"] is True
        assert len(payload["dates"]) == 5
        expected = [str(d.date()) for d in pd.to_datetime(df["time"])][-5:]
        assert payload["dates"] == expected

    def test_empty_symbol_error(self) -> None:
        assert server.symbol_detail("")["ok"] is False

    def test_no_data_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(server, "get_db", lambda: _FakeDb(pd.DataFrame(columns=["time", "close"])))
        payload = server.symbol_detail("999999.SS")
        assert payload["ok"] is False
        assert "未找到" in payload["error"]


# ---------------------------------------------------------------------------
# calc_stop_loss
# ---------------------------------------------------------------------------
class TestCalcStopLossContract:
    def test_stop_loss_error_contract(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*args, **kwargs):
            raise server.StopLossError("数据不足，无法计算 ATR")

        monkeypatch.setattr(server, "compute_stop_loss", _boom)
        result = server.calc_stop_loss("510300.SS", "2026-08-10", 4.0)
        assert result["ok"] is False
        assert "ATR" in result["error"]


# ---------------------------------------------------------------------------
# add_trade / open_positions
# ---------------------------------------------------------------------------
class _FakeCtx:
    def __init__(self, scope):
        class RC:
            request = type("R", (), {"scope": scope})()

        self.request_context = RC()


def _ctx_user_scope(username: str | None) -> dict:
    return {"state": {"mcp_user": username} if username else {}}


class TestAddTradeContract:
    def test_token_user_missing_in_db(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakeDb:
            def get_user_by_username(self, username):
                return None

        monkeypatch.setattr(server, "get_db", lambda: FakeDb())
        result = server.add_trade("510300.SS", "2026-08-10", 4.0, 100, ctx=_FakeCtx(_ctx_user_scope("ghost")))
        assert result["ok"] is False
        assert "users 表中不存在" in result["error"]

    def test_price_out_of_range(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakeDb:
            def get_user_by_username(self, username):
                return {"id": 1, "username": "yyx", "is_admin": True}

        monkeypatch.setattr(server, "get_db", lambda: FakeDb())
        monkeypatch.setattr(
            server.tr,
            "create_trade",
            lambda user, **kw: (_ for _ in ()).throw(server.tr.TradeRecordError("价格超出当日区间")),
        )
        result = server.add_trade("510300.SS", "2026-08-10", 99.0, 100, ctx=_FakeCtx(_ctx_user_scope("yyx")))
        assert result["ok"] is False
        assert "区间" in result["error"]


class TestOpenPositionsSummary:
    def test_error_positions_excluded_from_summary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_token_user(ctx):
            return {"id": 1, "username": "yyx", "is_admin": True}

        def fake_list_trades(user, **kwargs):
            return {
                "user": {"username": "yyx"},
                "trades": [
                    {
                        "id": 1, "status": "open", "symbol": "510300.SS", "name": "A",
                        "buy_date": "2026-08-10", "buy_price": 4.0, "shares": 100,
                        "latest_price": 4.2, "position_value": 420.0, "pnl_amount": 20.0,
                        "stops": {}, "holding": {},
                    },
                    {
                        "id": 2, "status": "open", "symbol": "999999.SS", "name": "坏",
                        "buy_date": "2026-08-10", "buy_price": 1.0, "shares": 100,
                        "error": "未找到数据",
                    },
                ],
            }

        monkeypatch.setattr(server, "_token_user", fake_token_user)
        monkeypatch.setattr(server.tr, "list_trades", fake_list_trades)
        result = server.open_positions(ctx=_FakeCtx(_ctx_user_scope("yyx")))
        assert result["ok"] is True
        assert len(result["positions"]) == 2
        # error 持仓不计入汇总金额/浮盈
        assert result["summary"]["total_position_value"] == 420.0
        assert result["summary"]["total_pnl_amount"] == 20.0
        error_row = [p for p in result["positions"] if p.get("error")]
        assert error_row and error_row[0]["symbol"] == "999999.SS"
