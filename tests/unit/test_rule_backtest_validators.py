"""Unit tests for rule_backtest.validators — StrategyConfigValidator."""

from __future__ import annotations

import pytest

from rule_backtest.validators import StrategyConfigValidator, ValidationResult


class TestStrategyConfigValidator:
    def test_valid_minimal_strategy(self) -> None:
        strategy = {
            "schema_version": 1,
            "id": "test_strategy",
            "name": "Test Strategy",
            "trade_mode": "single_symbol_all_in",
            "entry": {
                "type": "group",
                "combinator": "all",
                "children": [
                    {
                        "type": "condition",
                        "left": {"type": "price", "field": "close"},
                        "operator": ">=",
                        "right": {
                            "type": "indicator",
                            "name": "sma",
                            "params": {"field": "close", "period": 20},
                        },
                    }
                ],
            },
            "exit": {
                "type": "group",
                "combinator": "any",
                "children": [
                    {
                        "type": "condition",
                        "left": {"type": "price", "field": "close"},
                        "operator": "<=",
                        "right": {
                            "type": "indicator",
                            "name": "sma",
                            "params": {"field": "close", "period": 20},
                        },
                    }
                ],
            },
        }
        validator = StrategyConfigValidator()
        result = validator.validate_and_normalize(strategy)
        assert result.ok is True
        assert result.errors == []

    def test_validator_requires_id(self) -> None:
        """Validator requires 'id' to be present — not auto‑generated."""
        strategy = {
            "schema_version": 1,
            "trade_mode": "single_symbol_all_in",
            "entry": {
                "type": "group",
                "combinator": "all",
                "children": [
                    {
                        "type": "condition",
                        "left": {"type": "price", "field": "close"},
                        "operator": ">=",
                        "right": {"type": "literal", "value": 10},
                    }
                ],
            },
            "exit": {
                "type": "group",
                "combinator": "all",
                "children": [
                    {
                        "type": "condition",
                        "left": {"type": "price", "field": "close"},
                        "operator": "<=",
                        "right": {"type": "literal", "value": 5},
                    }
                ],
            },
        }
        validator = StrategyConfigValidator()
        result = validator.validate_and_normalize(strategy)
        assert result.ok is False
        assert "id" in " ".join(result.errors).lower()

    def test_missing_schema_version(self) -> None:
        strategy = {
            "id": "invalid",
            "trade_mode": "single_symbol_all_in",
            "entry": {"type": "group", "combinator": "all", "children": []},
            "exit": {"type": "group", "combinator": "all", "children": []},
        }
        validator = StrategyConfigValidator()
        result = validator.validate_and_normalize(strategy)
        assert result.ok is False
        assert len(result.errors) > 0

    def test_invalid_combinator(self) -> None:
        strategy = {
            "schema_version": 1,
            "id": "invalid_combo",
            "trade_mode": "single_symbol_all_in",
            "entry": {
                "type": "group",
                "combinator": "invalid",
                "children": [],
            },
            "exit": {
                "type": "group",
                "combinator": "any",
                "children": [],
            },
        }
        validator = StrategyConfigValidator()
        result = validator.validate_and_normalize(strategy)
        assert result.ok is False

    def test_invalid_operator(self) -> None:
        strategy = {
            "schema_version": 1,
            "id": "invalid_op",
            "trade_mode": "single_symbol_all_in",
            "entry": {
                "type": "group",
                "combinator": "all",
                "children": [
                    {
                        "type": "condition",
                        "left": {"type": "price", "field": "close"},
                        "operator": "==",  # unsupported
                        "right": {"type": "literal", "value": 10},
                    }
                ],
            },
            "exit": {"type": "group", "combinator": "any", "children": []},
        }
        validator = StrategyConfigValidator()
        result = validator.validate_and_normalize(strategy)
        assert result.ok is False

    @pytest.mark.parametrize("operator", ["cross_above", "cross_below"])
    def test_cross_operators_are_supported(self, operator: str) -> None:
        strategy = {
            "schema_version": 1,
            "id": "cross_op",
            "trade_mode": "single_symbol_all_in",
            "entry": {
                "type": "group",
                "combinator": "all",
                "children": [
                    {
                        "type": "condition",
                        "left": {"type": "indicator", "name": "macd_line", "params": {"field": "close"}},
                        "operator": operator,
                        "right": {"type": "indicator", "name": "macd_signal", "params": {"field": "close"}},
                    }
                ],
            },
            "exit": {
                "type": "group",
                "combinator": "any",
                "children": [
                    {
                        "type": "condition",
                        "left": {"type": "price", "field": "close"},
                        "operator": "<=",
                        "right": {"type": "literal", "value": 5},
                    }
                ],
            },
        }
        validator = StrategyConfigValidator()
        result = validator.validate_and_normalize(strategy)
        assert result.ok is True
        assert result.errors == []

    def test_days_since_last_exit_state_value_is_supported(self) -> None:
        """离场冷却期状态值在白名单内（入场条件：距上次离场 >= X 个交易日）。"""
        strategy = {
            "schema_version": 1,
            "id": "cooldown",
            "trade_mode": "single_symbol_all_in",
            "entry": {
                "type": "group",
                "combinator": "all",
                "children": [
                    {
                        "type": "condition",
                        "left": {"type": "state_value", "name": "days_since_last_exit"},
                        "operator": ">=",
                        "right": {"type": "literal", "value": 5},
                    }
                ],
            },
            "exit": {
                "type": "group",
                "combinator": "any",
                "children": [
                    {
                        "type": "condition",
                        "left": {"type": "price", "field": "close"},
                        "operator": "<=",
                        "right": {"type": "literal", "value": 5},
                    }
                ],
            },
        }
        validator = StrategyConfigValidator()
        result = validator.validate_and_normalize(strategy)
        assert result.ok is True
        assert result.errors == []
