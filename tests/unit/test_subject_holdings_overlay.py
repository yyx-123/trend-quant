"""Unit tests for 标的看板持仓叠加（app.routers.subject_market._overlay_holdings）。

口径：持仓金额 = 该行 mini K线末根收盘（快照模式下为实时报价合成K线）× 份数；
占比 = 占看板内全部持仓金额。共享 payload 绝不被原地改写。
"""

from __future__ import annotations

from app.routers.subject_market import _overlay_holdings


def _payload() -> dict:
    return {
        "as_of": "2026-08-24",
        "groups": [
            {
                "category_l1": "ETF",
                "items": [
                    {
                        "category_l2": "宽基",
                        "children": [
                            {
                                "category_l3": "大盘",
                                "children": [
                                    {"symbol": "510300.SS", "name": "沪深300", "kline": [{"c": 5.0}]},
                                    {"symbol": "510050.SS", "name": "上证50", "kline": [{"c": 2.5}]},
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }


def _inst(out: dict, symbol: str) -> dict:
    children = out["groups"][0]["items"][0]["children"][0]["children"]
    return next(c for c in children if c["symbol"] == symbol)


class TestOverlayHoldings:
    def test_value_and_weight(self, test_db) -> None:
        alice = test_db.create_user("alice", "pw1")
        test_db.create_manual_trade(alice["id"], "510300.SS", "2026-08-01", 4.0, 1000)
        out = _overlay_holdings(_payload(), alice["id"], test_db)
        inst = _inst(out, "510300.SS")
        assert inst["holding_value"] == 5000.0
        assert inst["holding_weight"] == 100.0
        # 悬停文案的计算组件：最新价 / 份数 / 看板内合计
        assert inst["holding_price"] == 5.0
        assert inst["holding_shares"] == 1000
        assert out["holdings_total"] == 5000.0
        # 未持仓标的行不带持仓字段
        assert "holding_value" not in _inst(out, "510050.SS")

    def test_multiple_trades_same_symbol_sum_shares(self, test_db) -> None:
        alice = test_db.create_user("alice", "pw1")
        test_db.create_manual_trade(alice["id"], "510300.SS", "2026-08-01", 4.0, 1000)
        test_db.create_manual_trade(alice["id"], "510300.SS", "2026-08-02", 4.2, 500)
        out = _overlay_holdings(_payload(), alice["id"], test_db)
        assert _inst(out, "510300.SS")["holding_value"] == 1500 * 5.0

    def test_weight_split_across_positions(self, test_db) -> None:
        alice = test_db.create_user("alice", "pw1")
        test_db.create_manual_trade(alice["id"], "510300.SS", "2026-08-01", 4.0, 1000)
        test_db.create_manual_trade(alice["id"], "510050.SS", "2026-08-01", 2.0, 1000)
        out = _overlay_holdings(_payload(), alice["id"], test_db)
        # 510300: 5000；510050: 2500 → 占比 66.67 / 33.33
        assert _inst(out, "510300.SS")["holding_weight"] == 66.67
        assert _inst(out, "510050.SS")["holding_weight"] == 33.33

    def test_closed_trades_excluded(self, test_db) -> None:
        alice = test_db.create_user("alice", "pw1")
        trade = test_db.create_manual_trade(alice["id"], "510300.SS", "2026-08-01", 4.0, 1000)
        test_db.close_manual_trade(trade["id"], "2026-08-10", 4.5)
        out = _overlay_holdings(_payload(), alice["id"], test_db)
        assert "holding_value" not in _inst(out, "510300.SS")

    def test_shared_payload_not_mutated(self, test_db) -> None:
        """RevisionCache / 快照 payload 是全用户共享对象：叠加必须走拷贝。"""
        alice = test_db.create_user("alice", "pw1")
        test_db.create_manual_trade(alice["id"], "510300.SS", "2026-08-01", 4.0, 1000)
        payload = _payload()
        _overlay_holdings(payload, alice["id"], test_db)
        assert "holding_value" not in _inst(payload, "510300.SS")

    def test_no_positions_returns_payload_unchanged(self, test_db) -> None:
        alice = test_db.create_user("alice", "pw1")
        payload = _payload()
        assert _overlay_holdings(payload, alice["id"], test_db) is payload

    def test_empty_kline_skipped(self, test_db) -> None:
        alice = test_db.create_user("alice", "pw1")
        test_db.create_manual_trade(alice["id"], "510300.SS", "2026-08-01", 4.0, 1000)
        payload = _payload()
        _inst(payload, "510300.SS")["kline"] = []
        out = _overlay_holdings(payload, alice["id"], test_db)
        assert "holding_value" not in _inst(out, "510300.SS")
