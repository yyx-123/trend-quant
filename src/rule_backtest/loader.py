from __future__ import annotations

import re
from pathlib import Path

import yaml

from audit.app_logger import get_logger
from rule_backtest.validators import StrategyConfigValidator, ValidationResult

logger = get_logger(__name__)

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class StrategyLoader:
    def __init__(
        self,
        base_dir: str | Path = "config/rule_strategies",
        db: object | None = None,
        use_db: bool = True,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.db = db
        self.use_db = use_db
        self.validator = StrategyConfigValidator()

    def list_strategies(self) -> list[dict]:
        db = self._get_db()
        if db is not None:
            rows = db.list_rule_strategies()
            if rows:
                return [self._db_row_to_list_item(row) for row in rows]
            if self._db_has_any_rows(db):
                # Every DB strategy is soft-deleted: report empty instead of
                # falling back to (and resurrecting) the YAML files.
                return []
        return self._list_yaml_strategies()

    def _list_yaml_strategies(self) -> list[dict]:
        items: list[dict] = []
        if not self.base_dir.exists():
            return items
        for path in sorted(self.base_dir.glob("*.yaml")):
            data = self._load_yaml(path)
            validation = self.validator.validate_and_normalize(data)
            payload = validation.normalized or data
            items.append(
                {
                    "id": str(payload.get("id", path.stem)),
                    "name": str(payload.get("name", payload.get("id", path.stem))),
                    "description": str(payload.get("description", "")),
                    "strategy": payload,
                    "path": str(path),
                    "storage": "yaml",
                    "valid": bool(validation.ok),
                    "errors": validation.errors,
                    "warnings": validation.warnings,
                }
            )
        return items

    def load(self, strategy_id: str) -> dict:
        strategy_id = str(strategy_id).strip()
        db = self._get_db()
        if db is not None:
            row = db.get_rule_strategy(strategy_id)
            if row:
                data = row.get("strategy", {})
                validation = self.validator.validate_and_normalize(data)
                if not validation.ok:
                    raise ValueError("; ".join(validation.errors))
                return validation.normalized or data
            if self._db_has_any_rows(db):
                # DB holds (soft-deleted) strategies: a missing active row
                # means deleted, so do not fall back to the YAML files.
                raise FileNotFoundError(f"rule strategy not found: {strategy_id}")

        for path in sorted(self.base_dir.glob("*.yaml")):
            data = self._load_yaml(path)
            if str(data.get("id", path.stem)).strip() == strategy_id:
                validation = self.validator.validate_and_normalize(data)
                if not validation.ok:
                    raise ValueError("; ".join(validation.errors))
                return validation.normalized or data
        raise FileNotFoundError(f"rule strategy not found: {strategy_id}")

    def save(self, strategy: dict, overwrite: bool = False) -> dict:
        validation = self.validator.validate_and_normalize(strategy)
        if not validation.ok:
            raise ValueError("; ".join(validation.errors))
        normalized = validation.normalized or strategy
        strategy_id = str(normalized.get("id", "")).strip()
        if not _SAFE_ID_RE.match(strategy_id):
            raise ValueError("strategy id can only contain letters, numbers, underscore, and hyphen")

        db = self._get_db()
        if db is not None:
            saved = db.save_rule_strategy(normalized, overwrite=overwrite)
            return {
                "id": strategy_id,
                "storage": "db",
                "warnings": validation.warnings,
                "strategy": saved.get("strategy", normalized),
                "updated_at": saved.get("updated_at"),
            }

        self.base_dir.mkdir(parents=True, exist_ok=True)
        path = self.base_dir / f"{strategy_id}.yaml"
        if path.exists() and not overwrite:
            raise FileExistsError(f"rule strategy already exists: {strategy_id}")
        with path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(normalized, f, allow_unicode=True, sort_keys=False)
        return {
            "id": strategy_id,
            "path": str(path),
            "storage": "yaml",
            "warnings": validation.warnings,
            "strategy": normalized,
        }

    def delete(self, strategy_id: str) -> dict:
        strategy_id = str(strategy_id).strip()
        if not strategy_id:
            raise ValueError("strategy id is required")
        if not _SAFE_ID_RE.match(strategy_id):
            raise ValueError("strategy id can only contain letters, numbers, underscore, and hyphen")

        db = self._get_db()
        if db is not None:
            if not db.delete_rule_strategy(strategy_id):
                raise FileNotFoundError(f"rule strategy not found: {strategy_id}")
            return {"id": strategy_id, "storage": "db", "deleted": True}

        for path in sorted(self.base_dir.glob("*.yaml")):
            data = self._load_yaml(path)
            if str(data.get("id", path.stem)).strip() == strategy_id:
                path.unlink()
                return {"id": strategy_id, "path": str(path), "storage": "yaml", "deleted": True}
        raise FileNotFoundError(f"rule strategy not found: {strategy_id}")

    def validate_file(self, path: str | Path) -> ValidationResult:
        return self.validator.validate_and_normalize(self._load_yaml(Path(path)))

    def _get_db(self) -> object | None:
        if not self.use_db:
            return None
        if self.db is not None:
            return self.db if self._db_path_available(self.db) else None
        try:
            from data.storage.db import get_db

            db = get_db()
            return db if self._db_path_available(db) else None
        except RuntimeError as exc:
            logger.warning("Database unavailable; falling back to YAML strategies: %s", exc)
            return None

    @staticmethod
    def _db_path_available(db: object) -> bool:
        db_path = getattr(db, "db_path", None)
        if db_path is None:
            return True
        return Path(db_path).parent.exists()

    @staticmethod
    def _db_has_any_rows(db: object) -> bool:
        """True if the DB has any rule_strategies rows, incl. soft-deleted.

        Duck-typed DBs without ``has_any_rule_strategy`` keep the legacy
        YAML-fallback behavior.
        """
        has_any = getattr(db, "has_any_rule_strategy", None)
        if not callable(has_any):
            return False
        try:
            return bool(has_any())
        except Exception as exc:
            logger.warning("has_any_rule_strategy check failed; assuming empty: %s", exc)
            return False

    def _db_row_to_list_item(self, row: dict) -> dict:
        data = row.get("strategy", {})
        validation = self.validator.validate_and_normalize(data)
        payload = validation.normalized or data
        return {
            "id": str(payload.get("id", row.get("id", ""))),
            "name": str(payload.get("name", payload.get("id", row.get("id", "")))),
            "description": str(payload.get("description", "")),
            "strategy": payload,
            "storage": "db",
            "valid": bool(validation.ok),
            "errors": validation.errors,
            "warnings": validation.warnings,
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }

    @staticmethod
    def _load_yaml(path: Path) -> dict:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise TypeError(f"strategy yaml must be an object: {path}")
        return data
