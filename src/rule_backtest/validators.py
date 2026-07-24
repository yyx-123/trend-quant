from __future__ import annotations

from dataclasses import dataclass, field

from rule_backtest.registry import IndicatorSpec, ParamSpec, default_indicator_registry


SUPPORTED_OPERATORS = {">=", "<=", "cross_above", "cross_below"}
SUPPORTED_PRICE_FIELDS = {"open", "high", "low", "close", "volume", "amount"}
SUPPORTED_VALUE_TYPES = {"price", "literal", "indicator", "state_value"}
SUPPORTED_STATE_VALUES = {
    "entry_price",
    "hard_stop",
    "highest_high_since_entry",
    "chandelier_stop",
    "days_since_last_exit",
}


@dataclass(slots=True)
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    normalized: dict | None = None


class StrategyConfigValidator:
    def __init__(self, registry: dict[str, IndicatorSpec] | None = None) -> None:
        self.registry = registry or default_indicator_registry()

    def validate_and_normalize(self, strategy: dict) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        if not isinstance(strategy, dict):
            return ValidationResult(ok=False, errors=["strategy must be an object"])

        normalized = dict(strategy)
        strategy_id = str(normalized.get("id", "")).strip()
        if not strategy_id:
            errors.append("id is required")
        normalized["id"] = strategy_id
        normalized["schema_version"] = int(normalized.get("schema_version", 1) or 1)
        normalized["trade_mode"] = str(normalized.get("trade_mode", "single_symbol_all_in")).strip()
        if normalized["trade_mode"] != "single_symbol_all_in":
            errors.append("trade_mode must be single_symbol_all_in")

        for side in ("entry", "exit"):
            group = normalized.get(side)
            if not isinstance(group, dict):
                errors.append(f"{side} must be a group")
                continue
            self._normalize_group(
                group,
                path=side,
                errors=errors,
                warnings=warnings,
                expected_combinator="all" if side == "entry" else "any",
            )

        return ValidationResult(ok=not errors, errors=errors, warnings=warnings, normalized=normalized)

    def _normalize_group(
        self,
        group: dict,
        path: str,
        errors: list[str],
        warnings: list[str],
        expected_combinator: str,
    ) -> None:
        group["type"] = str(group.get("type", "group")).strip() or "group"
        if group["type"] != "group":
            errors.append(f"{path}.type must be group")
        configured_combinator = str(group.get("combinator", expected_combinator)).strip().lower() or expected_combinator
        if configured_combinator not in {"all", "any"}:
            errors.append(f"{path}.combinator must be all or any")
        elif configured_combinator != expected_combinator:
            warnings.append(f"{path}.combinator is fixed to {expected_combinator}")
        group["combinator"] = expected_combinator

        children = group.get("children")
        if not isinstance(children, list) or not children:
            errors.append(f"{path}.children must contain at least one condition")
            group["children"] = []
            return

        for idx, condition in enumerate(children):
            if not isinstance(condition, dict):
                errors.append(f"{path}.children[{idx}] must be an object")
                continue
            self._normalize_condition(condition, path=f"{path}.children[{idx}]", errors=errors)

    def _normalize_condition(self, condition: dict, path: str, errors: list[str]) -> None:
        condition["type"] = str(condition.get("type", "condition")).strip() or "condition"
        if condition["type"] != "condition":
            errors.append(f"{path}.type must be condition")
        operator = str(condition.get("operator", "")).strip()
        if operator not in SUPPORTED_OPERATORS:
            errors.append(f"{path}.operator must be one of {sorted(SUPPORTED_OPERATORS)}")
        for side in ("left", "right"):
            value_spec = condition.get(side)
            if not isinstance(value_spec, dict):
                errors.append(f"{path}.{side} must be a value spec")
                continue
            self._normalize_value_spec(value_spec, path=f"{path}.{side}", errors=errors)

    def _normalize_value_spec(self, spec: dict, path: str, errors: list[str]) -> None:
        spec_type = str(spec.get("type", "")).strip()
        if spec_type not in SUPPORTED_VALUE_TYPES:
            errors.append(f"{path}.type must be one of {sorted(SUPPORTED_VALUE_TYPES)}")
            return

        if spec_type == "price":
            field = str(spec.get("field", "")).strip()
            if field not in SUPPORTED_PRICE_FIELDS:
                errors.append(f"{path}.field must be one of {sorted(SUPPORTED_PRICE_FIELDS)}")
            return

        if spec_type == "literal":
            try:
                spec["value"] = float(spec.get("value"))
            except Exception:
                errors.append(f"{path}.value must be a number")
            return

        if spec_type == "state_value":
            name = str(spec.get("name", "")).strip()
            if name not in SUPPORTED_STATE_VALUES:
                errors.append(f"{path}.name must be one of {sorted(SUPPORTED_STATE_VALUES)}")
            return

        name = str(spec.get("name", "")).strip()
        indicator = self.registry.get(name)
        if indicator is None:
            errors.append(f"{path}.name unsupported indicator: {name}")
            return
        spec["name"] = name
        params = spec.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            errors.append(f"{path}.params must be an object")
            params = {}
        spec["params"] = params
        self._normalize_params(params=params, indicator=indicator, path=f"{path}.params", errors=errors)

    @staticmethod
    def _normalize_params(params: dict, indicator: IndicatorSpec, path: str, errors: list[str]) -> None:
        for name, param_spec in indicator.params.items():
            if name not in params:
                if param_spec.required and param_spec.default is None:
                    errors.append(f"{path}.{name} is required")
                    continue
                params[name] = param_spec.default
            StrategyConfigValidator._normalize_param_value(
                params=params,
                name=name,
                param_spec=param_spec,
                path=path,
                errors=errors,
            )

    @staticmethod
    def _normalize_param_value(
        params: dict,
        name: str,
        param_spec: ParamSpec,
        path: str,
        errors: list[str],
    ) -> None:
        value = params.get(name)
        if value is None and not param_spec.required:
            params[name] = None
            return
        try:
            if param_spec.type == "int":
                value = int(value)
            elif param_spec.type == "float":
                value = float(value)
            elif param_spec.type == "price_field":
                value = str(value).strip()
                if value not in SUPPORTED_PRICE_FIELDS:
                    errors.append(f"{path}.{name} must be one of {sorted(SUPPORTED_PRICE_FIELDS)}")
            else:
                value = str(value)
        except Exception:
            errors.append(f"{path}.{name} must be {param_spec.type}")
            return
        if isinstance(value, (int, float)) and param_spec.min_value is not None and value < param_spec.min_value:
            errors.append(f"{path}.{name} must be >= {param_spec.min_value}")
        params[name] = value
