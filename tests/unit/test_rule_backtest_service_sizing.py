"""Service-level tests: trade strategies x position sizers cartesian product."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest
import yaml

from data.storage.db import Database
from rule_backtest.loader import StrategyLoader
from rule_backtest.service import RuleBacktestService
from rule_backtest.sizing import PositionStrategyLoader

LITERAL_ENTRY_STRATEGY = {
    "schema_version": 1,
    "id": "literal_entry",
    "name": "恒真买入",
    "trade_mode": "single_symbol_all_in",
    "entry": {
        "type": "group",
        "combinator": "all",
        "children": [{"left": {"type": "literal", "value": 1}, "operator": ">=", "right": {"type": "literal", "value": 0}}],
    },
    "exit": {
        "type": "group",
        "combinator": "any",
        "children": [{"left": {"type": "literal", "value": 0}, "operator": ">=", "right": {"type": "literal", "value": 1}}],
    },
}


def make_bars(closes: list[float]) -> pd.DataFrame:
    start = date(2026, 1, 1)
    return pd.DataFrame(
        [
            {
                "date": start + timedelta(days=i),
                "open": c,
                "high": c + 1,
                "low": c - 1,
                "close": c,
                "volume": 1000,
                "amount": c * 1000,
            }
            for i, c in enumerate(closes)
        ]
    )


class FakeMarketStore:
    def load_history(self, symbol: str) -> pd.DataFrame:
        return make_bars([10.0] * 5)

    def list_stored_symbols(self) -> list[str]:
        return ["TEST"]


@pytest.fixture()
def service(tmp_path):
    for sid in ("s1", "s2"):
        strategy = dict(LITERAL_ENTRY_STRATEGY, id=sid, name=f"策略{sid}")
        (tmp_path / f"{sid}.yaml").write_text(yaml.safe_dump(strategy, allow_unicode=True), encoding="utf-8")
    db = Database(tmp_path / "test.db")
    position_loader = PositionStrategyLoader(db=db)
    position_loader.save({"id": "z1", "name": "半仓", "sizer_type": "fixed_pct", "params": {"pct": 0.5}})
    position_loader.save({"id": "z2", "name": "凯利", "sizer_type": "kelly"})
    return RuleBacktestService(
        strategy_loader=StrategyLoader(base_dir=tmp_path, use_db=False),
        market_store=FakeMarketStore(),
        position_loader=position_loader,
    )


class TestCartesianProduct:
    def test_two_by_two_produces_four_results(self, service):
        result = service.run(
            {
                "strategy_ids": ["s1", "s2"],
                "position_strategy_ids": ["z1", "z2"],
                "symbol": "TEST",
                "initial_capital": 100000.0,
                "slippage": 0.0,
                "fee_rate": 0.0,
                "fee_min": 0.0,
            }
        )
        results = result["results"]
        assert len(results) == 4
        combos = [(r["strategy_id"], r["sizer_id"]) for r in results]
        assert combos == [("s1", "z1"), ("s1", "z2"), ("s2", "z1"), ("s2", "z2")]
        # labels
        assert results[0]["strategy_name"] == "策略s1"
        assert results[0]["sizer_name"] == "半仓"
        # sizing actually applied (service treats 0.0 fees as falsy and falls
        # back to DEFAULT_FEE_RATE — pre-existing behavior, hence 9900/4900):
        # fixed 50% buys 4900, kelly first trade is all-in 9900
        z1_buys = [t for r in results if r["sizer_id"] == "z1" for t in r["trades"] if t["side"] == "BUY"]
        z2_buys = [t for r in results if r["sizer_id"] == "z2" for t in r["trades"] if t["side"] == "BUY"]
        assert {t["qty"] for t in z1_buys} == {4900}
        assert {t["qty"] for t in z2_buys} == {9900}
        # multi_kline carries sizer identity
        assert {(m["strategy_id"], m["sizer_id"]) for m in result["multi_kline"]} == set(combos)

    def test_no_position_strategies_means_builtin_all_in(self, service):
        result = service.run(
            {
                "strategy_ids": ["s1"],
                "symbol": "TEST",
                "initial_capital": 100000.0,
                "slippage": 0.0,
                "fee_rate": 0.0,
                "fee_min": 0.0,
            }
        )
        assert len(result["results"]) == 1
        assert result["results"][0]["sizer_id"] == ""
        buys = [t for t in result["results"][0]["trades"] if t["side"] == "BUY"]
        assert buys[0]["qty"] == 9900  # all-in (0.0 fees fall back to defaults — see above)
        assert "sizing" not in buys[0]

    def test_deleted_position_strategy_fails_loudly(self, service):
        service.position_loader.delete("z1")
        with pytest.raises(FileNotFoundError):
            service.run(
                {
                    "strategy_ids": ["s1"],
                    "position_strategy_ids": ["z1"],
                    "symbol": "TEST",
                }
            )
