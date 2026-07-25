"""Persistence for position strategies (仓位策略).

DB-only (no YAML fallback, unlike rule strategies). A stored strategy that
fails validation, or a referenced id that was soft-deleted, raises — no
silent degradation, consistent with rule strategy behavior.
"""

from __future__ import annotations

import logging
import re

from rule_backtest.sizing.registry import validate_position_strategy

logger = logging.getLogger(__name__)

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class PositionStrategyLoader:
    def __init__(self, db: object | None = None, use_db: bool = True) -> None:
        self.db = db
        self.use_db = use_db

    def list_strategies(self) -> list[dict]:
        db = self._get_db()
        if db is None:
            return []
        return [self._db_row_to_list_item(row) for row in db.list_position_strategies()]

    def load(self, strategy_id: str) -> dict:
        strategy_id = str(strategy_id).strip()
        db = self._get_db()
        row = db.get_position_strategy(strategy_id) if db is not None else None
        if not row:
            raise FileNotFoundError(f"position strategy not found: {strategy_id}")
        data = row.get("strategy", {})
        validation = validate_position_strategy(data)
        if not validation.ok:
            raise ValueError("; ".join(validation.errors))
        return validation.normalized or data

    def save(self, strategy: dict, overwrite: bool = False) -> dict:
        validation = validate_position_strategy(strategy)
        if not validation.ok:
            raise ValueError("; ".join(validation.errors))
        normalized = validation.normalized or strategy
        strategy_id = str(normalized.get("id", "")).strip()
        if not _SAFE_ID_RE.match(strategy_id):
            raise ValueError("strategy id can only contain letters, numbers, underscore, and hyphen")

        db = self._get_db()
        if db is None:
            raise RuntimeError("database unavailable; cannot save position strategy")
        saved = db.save_position_strategy(normalized, overwrite=overwrite)
        return {
            "id": strategy_id,
            "storage": "db",
            "warnings": validation.warnings,
            "strategy": saved.get("strategy", normalized),
            "updated_at": saved.get("updated_at"),
        }

    def delete(self, strategy_id: str) -> dict:
        strategy_id = str(strategy_id).strip()
        if not strategy_id:
            raise ValueError("strategy id is required")
        if not _SAFE_ID_RE.match(strategy_id):
            raise ValueError("strategy id can only contain letters, numbers, underscore, and hyphen")

        db = self._get_db()
        if db is None or not db.delete_position_strategy(strategy_id):
            raise FileNotFoundError(f"position strategy not found: {strategy_id}")
        return {"id": strategy_id, "storage": "db", "deleted": True}

    def _get_db(self) -> object | None:
        if not self.use_db:
            return None
        if self.db is not None:
            return self.db
        try:
            from data.storage.db import get_db

            return get_db()
        except RuntimeError as exc:
            logger.warning("Database unavailable; position strategies are empty: %s", exc)
            return None

    @staticmethod
    def _db_row_to_list_item(row: dict) -> dict:
        data = row.get("strategy", {})
        validation = validate_position_strategy(data)
        payload = validation.normalized or data
        return {
            "id": str(payload.get("id", row.get("id", ""))),
            "name": str(payload.get("name", payload.get("id", row.get("id", "")))),
            "description": str(payload.get("description", "")),
            "sizer_type": str(payload.get("sizer_type", row.get("sizer_type", ""))),
            "strategy": payload,
            "storage": "db",
            "valid": bool(validation.ok),
            "errors": validation.errors,
            "warnings": validation.warnings,
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }
