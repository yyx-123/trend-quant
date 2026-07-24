"""Registry of position sizer types.

A stored position strategy is a plain dict::

    {"id": "...", "name": "...", "description": "",
     "sizer_type": "kelly", "params": {"lookback": 10, ...}}

Adding a new sizer = implement PositionSizer subclass + one registry entry.
"""

from __future__ import annotations

from rule_backtest.registry import ParamSpec
from rule_backtest.sizing.base import (
    DEGRADED_FLAGS,
    INFO_FLAGS,
    SKIP_INSUFFICIENT_CASH,
    SKIP_SIZER,
    SKIP_TARGET_BELOW_LOT,
    PositionSizer,
)
from rule_backtest.sizing.fixed_pct import FixedPctSizer
from rule_backtest.sizing.kelly import KellySizer
from rule_backtest.sizing.risk_budget import RiskBudgetSizer
from rule_backtest.validators import ValidationResult

SIZER_REGISTRY: dict[str, type[PositionSizer]] = {
    FixedPctSizer.sizer_type: FixedPctSizer,
    RiskBudgetSizer.sizer_type: RiskBudgetSizer,
    KellySizer.sizer_type: KellySizer,
}


def sizer_types_payload() -> list[dict]:
    """Sizer type schemas for /api/meta — the single source of truth that
    drives the frontend parameter forms."""
    out: list[dict] = []
    for sizer_type, cls in SIZER_REGISTRY.items():
        params = {
            name: {
                "type": spec.type,
                "required": spec.required,
                "default": spec.default,
                "min_value": spec.min_value,
            }
            for name, spec in cls.param_specs().items()
        }
        out.append({"type": sizer_type, "label": cls.label, "params": params})
    return out


def sizing_flags_payload() -> dict:
    """Degradation/skip enums for /api/meta — keeps the frontend's "degraded
    must be visible" highlighting in sync with the backend instead of
    relying on a hardcoded mirror."""
    return {
        "degraded_flags": sorted(DEGRADED_FLAGS),
        "info_flags": sorted(INFO_FLAGS),
        "skip_reasons": [SKIP_INSUFFICIENT_CASH, SKIP_TARGET_BELOW_LOT, SKIP_SIZER],
    }


def build_sizer(payload: dict) -> PositionSizer:
    """Instantiate a sizer from a (validated) stored position strategy."""
    sizer_type = str(payload.get("sizer_type", "")).strip()
    cls = SIZER_REGISTRY.get(sizer_type)
    if cls is None:
        raise ValueError(f"unknown sizer_type: {sizer_type!r}")
    params = payload.get("params", {}) or {}
    sizer = cls(**params)
    sizer.strategy_id = str(payload.get("id", "") or "")
    sizer.strategy_name = str(payload.get("name", "") or sizer.strategy_id)
    return sizer


def validate_position_strategy(payload: object) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(payload, dict):
        return ValidationResult(ok=False, errors=["position strategy must be an object"])

    normalized = dict(payload)
    strategy_id = str(normalized.get("id", "")).strip()
    if not strategy_id:
        errors.append("id is required")
    normalized["id"] = strategy_id
    normalized["name"] = str(normalized.get("name", "") or strategy_id)
    normalized["description"] = str(normalized.get("description", "") or "")

    sizer_type = str(normalized.get("sizer_type", "")).strip()
    cls = SIZER_REGISTRY.get(sizer_type)
    if cls is None:
        errors.append(f"unknown sizer_type: {sizer_type!r} (supported: {sorted(SIZER_REGISTRY)})")
        return ValidationResult(ok=False, errors=errors, warnings=warnings)
    normalized["sizer_type"] = sizer_type

    specs = cls.param_specs()
    raw_params = normalized.get("params", {}) or {}
    if not isinstance(raw_params, dict):
        return ValidationResult(ok=False, errors=["params must be an object"], warnings=warnings)

    params: dict = {}
    for key in raw_params:
        if key not in specs:
            errors.append(f"unknown param for {sizer_type}: {key}")
    for name, spec in specs.items():
        value = raw_params.get(name, spec.default)
        coerced, err = _coerce_param(name, value, spec)
        if err:
            errors.append(err)
        else:
            params[name] = coerced
    for name, spec in specs.items():
        if spec.required and name not in raw_params:
            errors.append(f"missing required param for {sizer_type}: {name}")
    if errors:
        return ValidationResult(ok=False, errors=errors, warnings=warnings)

    # Constructor-level semantic validation (ranges, enums).
    try:
        cls(**params)
    except (TypeError, ValueError) as exc:
        errors.append(str(exc))
    normalized["params"] = params
    return ValidationResult(ok=not errors, errors=errors, warnings=warnings, normalized=normalized)


def _coerce_param(name: str, value: object, spec: ParamSpec) -> tuple[object, str | None]:
    if value is None:
        return None, f"param {name} cannot be null"
    try:
        if spec.type == "int":
            if isinstance(value, bool):
                raise ValueError
            coerced: object = int(float(value))
        elif spec.type == "float":
            if isinstance(value, bool):
                raise ValueError
            coerced = float(value)
        elif spec.type == "str":
            coerced = str(value).strip()
        else:
            coerced = value
    except (TypeError, ValueError):
        return None, f"param {name} must be {spec.type}, got {value!r}"
    if spec.min_value is not None and isinstance(coerced, (int, float)) and coerced < spec.min_value:
        return None, f"param {name} must be >= {spec.min_value}, got {coerced}"
    return coerced, None
