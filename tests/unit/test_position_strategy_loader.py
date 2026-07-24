"""Tests for PositionStrategyLoader + position_strategies DB table."""

from __future__ import annotations

import pytest

from data.storage.db import Database
from rule_backtest.sizing import PositionStrategyLoader


@pytest.fixture()
def loader(tmp_path):
    db = Database(tmp_path / "test.db")
    return PositionStrategyLoader(db=db)


class TestPositionStrategyLoader:
    def test_save_load_roundtrip(self, loader):
        saved = loader.save({"id": "kelly_10", "name": "凯利10", "sizer_type": "kelly",
                             "params": {"lookback": 5}})
        assert saved["storage"] == "db"
        loaded = loader.load("kelly_10")
        assert loaded["sizer_type"] == "kelly"
        assert loaded["params"]["lookback"] == 5
        assert loaded["params"]["fallback_pct"] == 0.10  # default filled

    def test_list_strategies(self, loader):
        loader.save({"id": "a", "sizer_type": "fixed_pct", "params": {"pct": 0.7}})
        loader.save({"id": "b", "sizer_type": "risk_budget", "params": {"value": 5000}})
        items = loader.list_strategies()
        ids = {item["id"] for item in items}
        assert ids == {"a", "b"}
        assert all(item["valid"] for item in items)
        by_id = {item["id"]: item for item in items}
        assert by_id["b"]["sizer_type"] == "risk_budget"

    def test_save_conflict_then_overwrite(self, loader):
        loader.save({"id": "a", "sizer_type": "fixed_pct"})
        with pytest.raises(FileExistsError):
            loader.save({"id": "a", "sizer_type": "fixed_pct"})
        loader.save({"id": "a", "sizer_type": "fixed_pct", "params": {"pct": 0.5}}, overwrite=True)
        assert loader.load("a")["params"]["pct"] == 0.5

    def test_save_invalid_rejected(self, loader):
        with pytest.raises(ValueError):
            loader.save({"id": "bad", "sizer_type": "martingale"})
        with pytest.raises(ValueError):
            loader.save({"id": "bad id with spaces", "sizer_type": "kelly"})

    def test_soft_deleted_strategy_fails_to_load(self, loader):
        loader.save({"id": "a", "sizer_type": "kelly"})
        loader.delete("a")
        with pytest.raises(FileNotFoundError):
            loader.load("a")
        assert loader.list_strategies() == []
        with pytest.raises(FileNotFoundError):
            loader.delete("a")

    def test_load_unknown_id(self, loader):
        with pytest.raises(FileNotFoundError):
            loader.load("nope")
