"""策略库（详设 §5.5）：入库版本化、不可变、完整血缘。

- 入库规则：config_hash 唯一、版本不可变（db 触发器强制）、可带产出实验 id
  或 benchmark 标记；删除只做软标记（retired_at），不影响已被引用的运行；
- "实验 = 基准 + diff" 在配置层成立：parent_version_id 指回基准版本，
  diff 可机器计算（strategy.apply_diff）。
"""

from __future__ import annotations

from portfolio.registry import ModuleRegistry


def row_to_dict(row) -> dict | None:
    """sqlite3.Row -> dict（本层自用；L3 不反向依赖 L4，评审 A-P1-2）。"""
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}  # noqa: SIM118 —— Row.keys() 是列名来源


def rows_to_dicts(rows) -> list[dict]:
    return [row_to_dict(r) for r in rows]

from portfolio.strategy import StrategyConfig, parse_strategy_yaml


class LibraryError(ValueError):
    pass


def ensure_strategy(
    db,
    strategy_id: str,
    *,
    name: str = "",
    description: str = "",
    is_benchmark: bool = False,
    is_blank_base: bool = False,
    created_by: str = "human",
) -> dict:
    """登记策略线（幂等）。strategy_id 形如 base-v1 / blank-base / bench-*。"""
    strategy_id = str(strategy_id or "").strip()
    if not strategy_id:
        raise LibraryError("strategy_id must be non-empty")
    with db.connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO portfolio_strategies
               (id, name, description, is_benchmark, is_blank_base, created_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                strategy_id,
                name or strategy_id,
                description,
                1 if is_benchmark else 0,
                1 if is_blank_base else 0,
                created_by,
            ),
        )
    return get_strategy(db, strategy_id)


def get_strategy(db, strategy_id: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM portfolio_strategies WHERE id = ?", (strategy_id,)
        ).fetchone()
    return row_to_dict(row)


def list_strategies(db, *, include_retired: bool = False) -> list[dict]:
    sql = "SELECT * FROM portfolio_strategies"
    if not include_retired:
        sql += " WHERE retired_at IS NULL"
    sql += " ORDER BY id"
    with db.connect() as conn:
        rows = conn.execute(sql).fetchall()
    return rows_to_dicts(rows)


def add_version(
    db,
    strategy_id: str,
    config: StrategyConfig,
    *,
    created_by: str = "human",
    experiment_id: str | None = None,
    parent_version_id: str | None = None,
) -> dict:
    """入库一个不可变版本（幂等：同 config_hash 复用已有版本行）。

    配置必须已通过 parse 校验（模块已注册、参数合法）。返回版本行
    （id = "<strategy_id>@<version>"）。
    """
    strategy = get_strategy(db, strategy_id)
    if strategy is None:
        raise LibraryError(f"strategy not registered: {strategy_id}")
    if parent_version_id is not None and get_version(db, parent_version_id) is None:
        raise LibraryError(f"parent version not found: {parent_version_id}")

    config_yaml = config.canonical_yaml()
    config_hash = config.config_hash()

    existing = _version_by_hash(db, config_hash)
    if existing is not None:
        if existing["strategy_id"] != strategy_id:
            raise LibraryError(
                f"config hash collides with another strategy line: "
                f"{existing['id']} vs new {strategy_id}"
            )
        return existing

    with db.connect() as conn:
        row = conn.execute(
            "SELECT MAX(version) AS v FROM portfolio_strategy_versions WHERE strategy_id = ?",
            (strategy_id,),
        ).fetchone()
        version = int(row["v"] or 0) + 1
        version_id = f"{strategy_id}@{version}"
        conn.execute(
            """INSERT INTO portfolio_strategy_versions
               (id, strategy_id, version, config_yaml, config_hash,
                parent_version_id, experiment_id, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                version_id,
                strategy_id,
                version,
                config_yaml,
                config_hash,
                parent_version_id,
                experiment_id,
                created_by,
            ),
        )
    return get_version(db, version_id)


def add_version_yaml(
    db,
    strategy_id: str,
    config_yaml: str,
    registry: ModuleRegistry,
    **kwargs,
) -> dict:
    """YAML 文本入库的便捷入口（解析 + 校验后走 add_version）。"""
    config = parse_strategy_yaml(config_yaml, registry)
    return add_version(db, strategy_id, config, **kwargs)


def _version_by_hash(db, config_hash: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM portfolio_strategy_versions WHERE config_hash = ?",
            (config_hash,),
        ).fetchone()
    return row_to_dict(row)


def get_version(db, version_id: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM portfolio_strategy_versions WHERE id = ?", (version_id,)
        ).fetchone()
    return row_to_dict(row)


def require_version(db, version_id: str) -> dict:
    row = get_version(db, version_id)
    if row is None:
        raise LibraryError(f"strategy version not found: {version_id}")
    return row


def list_versions(db, strategy_id: str) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT * FROM portfolio_strategy_versions
               WHERE strategy_id = ? ORDER BY version""",
            (strategy_id,),
        ).fetchall()
    return rows_to_dicts(rows)


def latest_version(db, strategy_id: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(
            """SELECT * FROM portfolio_strategy_versions
               WHERE strategy_id = ? ORDER BY version DESC LIMIT 1""",
            (strategy_id,),
        ).fetchone()
    return row_to_dict(row)


def retire_strategy(db, strategy_id: str) -> dict:
    """软删除（软标记）：只禁止新引用处的展示，不影响已被引用的运行。"""
    strategy = get_strategy(db, strategy_id)
    if strategy is None:
        raise LibraryError(f"strategy not registered: {strategy_id}")
    with db.connect() as conn:
        conn.execute(
            """UPDATE portfolio_strategies
               SET retired_at = datetime('now','localtime') WHERE id = ?""",
            (strategy_id,),
        )
    return get_strategy(db, strategy_id)
