from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

_db_instance: Database | None = None


class Database:
    def __init__(self, db_path: str | Path = "data/trend_quant.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # In-process cache for list_market_symbols(): the DISTINCT query scans
        # a ~1M-row table and costs seconds on a cold page cache (notably on
        # WSL2, which reclaims cached pages aggressively). Invalidated by any
        # market-data write below. Note: writes from OTHER processes do not
        # invalidate this cache; all in-app writers go through these methods.
        self._market_symbols_cache: dict[str, list[str]] = {}
        self._init_tables()
        self._migrate_schema()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # WAL: readers are not blocked during indicator cache rebuilds.
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def backup_to(self, backup_dir: str | Path = "data/backups", keep: int = 3) -> Path:
        """Online backup via VACUUM INTO (WAL-safe), keeping the newest ``keep`` files."""
        target_dir = Path(backup_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        dest = target_dir / f"trend_quant-{stamp}.db"
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(f"VACUUM INTO '{dest}'")
        finally:
            conn.close()
        backups = sorted(target_dir.glob("trend_quant-*.db"))
        for old in backups[:-keep]:
            old.unlink(missing_ok=True)
        return dest

    def _init_tables(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS rule_strategies (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    trade_mode TEXT NOT NULL DEFAULT 'single_symbol_all_in',
                    payload_json TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_rule_strategies_active_updated
                    ON rule_strategies(is_active, updated_at);

                CREATE TABLE IF NOT EXISTS position_strategies (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    sizer_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_position_strategies_active_updated
                    ON position_strategies(is_active, updated_at);

                CREATE TABLE IF NOT EXISTS market_data_raw (
                    symbol TEXT NOT NULL,
                    time TEXT NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume REAL,
                    amount REAL,
                    provider TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (symbol, time)
                );
                CREATE INDEX IF NOT EXISTS idx_market_data_raw_symbol_time
                    ON market_data_raw(symbol, time);

                CREATE TABLE IF NOT EXISTS market_data_qfq (
                    symbol TEXT NOT NULL,
                    time TEXT NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume REAL,
                    amount REAL,
                    provider TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (symbol, time)
                );
                CREATE INDEX IF NOT EXISTS idx_market_data_qfq_symbol_time
                    ON market_data_qfq(symbol, time);

                CREATE TABLE IF NOT EXISTS instrument_metadata (
                    symbol TEXT PRIMARY KEY,
                    name TEXT,
                    category_l1 TEXT,
                    category_l2 TEXT,
                    category_l3 TEXT,
                    factor_tags TEXT,
                    region_tag TEXT,
                    priority_l1 INTEGER,
                    priority_l2 INTEGER,
                    priority_l3 INTEGER,
                    sort_order INTEGER,
                    source TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    stop_atr_mul REAL,
                    risk_budget_pct REAL,
                    asset_type TEXT,
                    start_date TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_instrument_metadata_category
                    ON instrument_metadata(category_l1, category_l2, category_l3);
                CREATE INDEX IF NOT EXISTS idx_instrument_metadata_sort
                    ON instrument_metadata(priority_l1, priority_l2, priority_l3, sort_order, symbol);

                CREATE TABLE IF NOT EXISTS instrument_categories (
                    path TEXT PRIMARY KEY,
                    level INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    parent_path TEXT,
                    priority INTEGER,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_instrument_categories_parent
                    ON instrument_categories(parent_path, priority, name);

                CREATE TABLE IF NOT EXISTS job_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_type TEXT NOT NULL,
                    run_date TEXT,
                    status TEXT,
                    payload TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_job_runs_type_id
                    ON job_runs(job_type, id);

                CREATE TABLE IF NOT EXISTS app_config (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS trend_param_sets (
                    param_set TEXT PRIMARY KEY,
                    params_json TEXT NOT NULL,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    formula_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS indicator_daily (
                    symbol TEXT NOT NULL,
                    time TEXT NOT NULL,
                    atr REAL,
                    vol_ma20 REAL,
                    er10 REAL,
                    sma5 REAL, sma10 REAL, sma20 REAL, sma60 REAL, sma120 REAL, sma200 REAL,
                    ema5 REAL, ema10 REAL, ema20 REAL,
                    rsi14 REAL,
                    macd_dif REAL, macd_dea REAL, macd_hist REAL,
                    boll_mid REAL, boll_up REAL, boll_dn REAL,
                    rsi_avg_gain REAL, rsi_avg_loss REAL,
                    macd_ema12 REAL, macd_ema26 REAL,
                    price_mode TEXT NOT NULL DEFAULT 'qfq',
                    formula_version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, time)
                );

                CREATE TABLE IF NOT EXISTS trend_daily (
                    symbol TEXT NOT NULL,
                    time TEXT NOT NULL,
                    param_set TEXT NOT NULL DEFAULT 'default',
                    trend_score REAL,
                    trend_ma5 REAL,
                    trend_ma10 REAL,
                    price_direction REAL,
                    confidence REAL,
                    price_mode TEXT NOT NULL DEFAULT 'qfq',
                    formula_version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, time, param_set)
                );

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS manual_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    symbol TEXT NOT NULL,
                    buy_date TEXT NOT NULL,
                    buy_price REAL NOT NULL,
                    shares REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    sell_date TEXT,
                    sell_price REAL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_manual_trades_user_status
                    ON manual_trades(user_id, status, id);

                """
            )

    # ------------------------------------------------------------------
    # schema migration
    # ------------------------------------------------------------------
    def _migrate_schema(self) -> None:
        """Idempotent column additions for existing databases."""
        new_columns = {
            "enabled": "INTEGER NOT NULL DEFAULT 1",
            "stop_atr_mul": "REAL",
            "risk_budget_pct": "REAL",
            "asset_type": "TEXT",
            "start_date": "TEXT",
        }
        with self._connect() as conn:
            existing = {
                row["name"] for row in conn.execute("PRAGMA table_info(instrument_metadata)")
            }
            for name, ddl in new_columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE instrument_metadata ADD COLUMN {name} {ddl}")

    # ------------------------------------------------------------------
    # rule_strategies
    # ------------------------------------------------------------------
    def save_rule_strategy(self, strategy: dict, overwrite: bool = False) -> dict:
        strategy_id = str(strategy.get("id", "")).strip()
        if not strategy_id:
            raise ValueError("rule strategy id is required")

        with self._connect() as conn:
            if not overwrite:
                row = conn.execute(
                    "SELECT id FROM rule_strategies WHERE id = ? AND is_active = 1",
                    (strategy_id,),
                ).fetchone()
                if row:
                    raise FileExistsError(f"rule strategy already exists: {strategy_id}")

            conn.execute(
                """INSERT INTO rule_strategies
                   (id, name, description, schema_version, trade_mode, payload_json, is_active)
                   VALUES (?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name,
                     description=excluded.description,
                     schema_version=excluded.schema_version,
                     trade_mode=excluded.trade_mode,
                     payload_json=excluded.payload_json,
                     is_active=1,
                     updated_at=CURRENT_TIMESTAMP""",
                (
                    strategy_id,
                    str(strategy.get("name", strategy_id) or strategy_id),
                    str(strategy.get("description", "") or ""),
                    int(strategy.get("schema_version", 1) or 1),
                    str(strategy.get("trade_mode", "single_symbol_all_in") or "single_symbol_all_in"),
                    json.dumps(strategy, ensure_ascii=False),
                ),
            )
        saved = self.get_rule_strategy(strategy_id)
        if saved is None:
            raise RuntimeError(f"failed to save rule strategy: {strategy_id}")
        return saved

    def get_rule_strategy(self, strategy_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM rule_strategies
                   WHERE id = ? AND is_active = 1""",
                (strategy_id,),
            ).fetchone()
        return self._rule_strategy_row(row) if row else None

    def list_rule_strategies(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM rule_strategies
                   WHERE is_active = 1
                   ORDER BY updated_at DESC, id ASC"""
            ).fetchall()
        return [self._rule_strategy_row(row) for row in rows]

    def has_any_rule_strategy(self) -> bool:
        """True if rule_strategies has any row, including soft-deleted ones.

        Used by the YAML seeding logic so that soft-deleting every strategy
        does not resurrect the YAML seed strategies on the next read.
        """
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM rule_strategies LIMIT 1").fetchone()
        return row is not None

    def delete_rule_strategy(self, strategy_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE rule_strategies
                   SET is_active = 0, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND is_active = 1""",
                (strategy_id,),
            )
            return cur.rowcount > 0

    @staticmethod
    def _rule_strategy_row(row: sqlite3.Row) -> dict:
        d = dict(row)
        payload = json.loads(d["payload_json"]) if d.get("payload_json") else {}
        d["strategy"] = payload
        return d

    # ------------------------------------------------------------------
    # position_strategies
    # ------------------------------------------------------------------
    def save_position_strategy(self, strategy: dict, overwrite: bool = False) -> dict:
        strategy_id = str(strategy.get("id", "")).strip()
        if not strategy_id:
            raise ValueError("position strategy id is required")

        with self._connect() as conn:
            if not overwrite:
                row = conn.execute(
                    "SELECT id FROM position_strategies WHERE id = ? AND is_active = 1",
                    (strategy_id,),
                ).fetchone()
                if row:
                    raise FileExistsError(f"position strategy already exists: {strategy_id}")

            conn.execute(
                """INSERT INTO position_strategies
                   (id, name, description, sizer_type, payload_json, is_active)
                   VALUES (?, ?, ?, ?, ?, 1)
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name,
                     description=excluded.description,
                     sizer_type=excluded.sizer_type,
                     payload_json=excluded.payload_json,
                     is_active=1,
                     updated_at=CURRENT_TIMESTAMP""",
                (
                    strategy_id,
                    str(strategy.get("name", strategy_id) or strategy_id),
                    str(strategy.get("description", "") or ""),
                    str(strategy.get("sizer_type", "") or ""),
                    json.dumps(strategy, ensure_ascii=False),
                ),
            )
        saved = self.get_position_strategy(strategy_id)
        if saved is None:
            raise RuntimeError(f"failed to save position strategy: {strategy_id}")
        return saved

    def get_position_strategy(self, strategy_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM position_strategies
                   WHERE id = ? AND is_active = 1""",
                (strategy_id,),
            ).fetchone()
        return self._position_strategy_row(row) if row else None

    def list_position_strategies(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM position_strategies
                   WHERE is_active = 1
                   ORDER BY updated_at DESC, id ASC"""
            ).fetchall()
        return [self._position_strategy_row(row) for row in rows]

    def delete_position_strategy(self, strategy_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE position_strategies
                   SET is_active = 0, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND is_active = 1""",
                (strategy_id,),
            )
            return cur.rowcount > 0

    @staticmethod
    def _position_strategy_row(row: sqlite3.Row) -> dict:
        d = dict(row)
        payload = json.loads(d["payload_json"]) if d.get("payload_json") else {}
        d["strategy"] = payload
        return d

    # ------------------------------------------------------------------
    # instrument_metadata
    # ------------------------------------------------------------------
    @staticmethod
    def _json_tags(value: Any) -> str:
        if isinstance(value, str):
            tags = [part.strip() for part in value.split("/") if part.strip()]
        elif isinstance(value, (list, tuple, set)):
            tags = [str(part).strip() for part in value if str(part).strip()]
        else:
            tags = []
        return json.dumps(tags, ensure_ascii=False)

    @staticmethod
    def _parse_tags(value: Any) -> list[str]:
        if value is None or value == "":
            return []
        try:
            parsed = json.loads(str(value))
        except json.JSONDecodeError:
            return [part.strip() for part in str(value).split("/") if part.strip()]
        if isinstance(parsed, list):
            return [str(part).strip() for part in parsed if str(part).strip()]
        return []

    @staticmethod
    def _category_path(row: dict[str, Any]) -> str:
        parts = [
            str(row.get("category_l1") or "").strip(),
            str(row.get("category_l2") or "").strip(),
            str(row.get("category_l3") or "").strip(),
        ]
        return "-".join(part for part in parts if part)

    @staticmethod
    def _metadata_row_to_dict(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["factor_tags"] = Database._parse_tags(item.get("factor_tags"))
        item["category_path"] = Database._category_path(item)
        return item

    def save_instrument_metadata(self, items: list[dict[str, Any]]) -> int:
        records: list[tuple] = []
        for item in items:
            symbol = str(item.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            enabled_raw = item.get("enabled", True)
            records.append(
                (
                    symbol,
                    str(item.get("name") or "").strip(),
                    str(item.get("category_l1") or "").strip(),
                    str(item.get("category_l2") or "").strip(),
                    str(item.get("category_l3") or "").strip(),
                    self._json_tags(item.get("factor_tags")),
                    str(item.get("region_tag") or "").strip(),
                    item.get("priority_l1"),
                    item.get("priority_l2"),
                    item.get("priority_l3"),
                    item.get("sort_order"),
                    str(item.get("source") or "").strip(),
                    1 if enabled_raw in (True, 1, "1", "true") else 0,
                    item.get("stop_atr_mul"),
                    item.get("risk_budget_pct"),
                    str(item.get("asset_type") or "").strip() or None,
                    str(item.get("start_date") or "").strip() or None,
                )
            )
        if not records:
            return 0

        with self._connect() as conn:
            conn.executemany(
                """INSERT INTO instrument_metadata
                   (symbol, name, category_l1, category_l2, category_l3, factor_tags,
                    region_tag, priority_l1, priority_l2, priority_l3, sort_order, source,
                    enabled, stop_atr_mul, risk_budget_pct, asset_type, start_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(symbol) DO UPDATE SET
                     name=excluded.name,
                     category_l1=excluded.category_l1,
                     category_l2=excluded.category_l2,
                     category_l3=excluded.category_l3,
                     factor_tags=excluded.factor_tags,
                     region_tag=excluded.region_tag,
                     priority_l1=excluded.priority_l1,
                     priority_l2=excluded.priority_l2,
                     priority_l3=excluded.priority_l3,
                     sort_order=excluded.sort_order,
                     source=excluded.source,
                     enabled=excluded.enabled,
                     stop_atr_mul=COALESCE(excluded.stop_atr_mul, instrument_metadata.stop_atr_mul),
                     risk_budget_pct=COALESCE(excluded.risk_budget_pct, instrument_metadata.risk_budget_pct),
                     asset_type=COALESCE(excluded.asset_type, instrument_metadata.asset_type),
                     start_date=COALESCE(excluded.start_date, instrument_metadata.start_date),
                     updated_at=CURRENT_TIMESTAMP""",
                records,
            )
        return len(records)

    def list_instrument_metadata(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM instrument_metadata
                   ORDER BY
                     priority_l1 IS NULL, priority_l1,
                     priority_l2 IS NULL, priority_l2,
                     priority_l3 IS NULL, priority_l3,
                     sort_order IS NULL, sort_order,
                     symbol"""
            ).fetchall()
        return [self._metadata_row_to_dict(row) for row in rows]

    def get_instrument_metadata(self, symbol: str) -> dict | None:
        normalized = str(symbol or "").strip().upper()
        if not normalized:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM instrument_metadata WHERE symbol = ?",
                (normalized,),
            ).fetchone()
        return self._metadata_row_to_dict(row) if row else None

    def get_instrument_metadata_map(self) -> dict[str, dict]:
        return {item["symbol"]: item for item in self.list_instrument_metadata()}

    def load_market_tail(self, days: int, price_mode: str = "qfq") -> list[dict]:
        """Lean K-line tail for all symbols (no metadata join) — bulk overlay reads."""
        table = self._market_table(price_mode)
        cutoff = (datetime.now().date() - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT symbol, time, open, high, low, close, volume
                    FROM {table} WHERE time >= ? ORDER BY symbol, time""",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def load_market_dashboard_history(self, days: int = 90) -> list[dict]:
        """Return recent adjusted daily bars for fully classified managed instruments."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT symbol, name, time, open, high, low, close, volume, amount,
                          category_l1, category_l2, category_l3,
                          priority_l1, priority_l2, priority_l3, sort_order
                   FROM (
                       SELECT d.symbol, m.name, d.time, d.open, d.high, d.low, d.close, d.volume, d.amount,
                              m.category_l1, m.category_l2, m.category_l3,
                              m.priority_l1, m.priority_l2, m.priority_l3, m.sort_order,
                              ROW_NUMBER() OVER (PARTITION BY d.symbol ORDER BY d.time DESC) AS rn
                       FROM market_data_qfq d
                       JOIN instrument_metadata m ON m.symbol = d.symbol
                       WHERE TRIM(COALESCE(m.category_l1, '')) <> ''
                         AND TRIM(COALESCE(m.category_l2, '')) <> ''
                         AND TRIM(COALESCE(m.category_l3, '')) <> ''
                   )
                   WHERE rn <= ?
                   ORDER BY category_l1, category_l2, category_l3, symbol, time""",
                (max(1, int(days)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_market_dashboard_revision(self) -> tuple[str, int, str]:
        """Small revision token used to invalidate the in-process subject-board cache."""
        with self._connect() as conn:
            market = conn.execute(
                "SELECT MAX(time) AS latest_time, COUNT(*) AS row_count FROM market_data_qfq"
            ).fetchone()
            metadata = conn.execute(
                "SELECT MAX(updated_at) AS latest_metadata FROM instrument_metadata"
            ).fetchone()
        return (
            str(market["latest_time"] or "") if market else "",
            int(market["row_count"] or 0) if market else 0,
            str(metadata["latest_metadata"] or "") if metadata else "",
        )

    def save_instrument_categories(self, categories: list[dict[str, Any]]) -> int:
        records: list[tuple] = []
        for item in categories:
            path = str(item.get("path") or "").strip()
            name = str(item.get("name") or "").strip()
            if not path or not name:
                continue
            records.append(
                (
                    path,
                    int(item.get("level") or 0),
                    name,
                    str(item.get("parent_path") or "").strip() or None,
                    item.get("priority"),
                )
            )
        if not records:
            return 0

        with self._connect() as conn:
            conn.executemany(
                """INSERT INTO instrument_categories
                   (path, level, name, parent_path, priority)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET
                     level=excluded.level,
                     name=excluded.name,
                     parent_path=excluded.parent_path,
                     priority=excluded.priority,
                     updated_at=CURRENT_TIMESTAMP""",
                records,
            )
        return len(records)

    def replace_instrument_categories(self, categories: list[dict[str, Any]]) -> int:
        with self._connect() as conn:
            conn.execute("DELETE FROM instrument_categories")
        return self.save_instrument_categories(categories)

    def list_instrument_categories(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM instrument_categories
                   ORDER BY level, parent_path IS NULL DESC, parent_path, priority IS NULL, priority, name"""
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # market_data
    # ------------------------------------------------------------------
    @staticmethod
    def _market_table(price_mode: str = "qfq") -> str:
        value = str(price_mode or "qfq").strip().lower()
        if value in {"qfq", "forward", "forward_additive"}:
            return "market_data_qfq"
        if value in {"raw", "none", "unadjusted"}:
            return "market_data_raw"
        raise ValueError(f"unsupported market data price_mode: {price_mode}")

    def save_market_data(self, symbol: str, df, price_mode: str = "qfq") -> None:
        if df.empty:
            return
        table = self._market_table(price_mode)
        records: list[tuple] = []
        for _, row in df.iterrows():
            records.append(
                (
                    symbol,
                    str(row.get("time", "")),
                    float(row["open"]) if hasattr(row, "__getitem__") and row.get("open") is not None and str(row.get("open")) != "nan" else None,
                    float(row["high"]) if hasattr(row, "__getitem__") and row.get("high") is not None and str(row.get("high")) != "nan" else None,
                    float(row["low"]) if hasattr(row, "__getitem__") and row.get("low") is not None and str(row.get("low")) != "nan" else None,
                    float(row["close"]) if hasattr(row, "__getitem__") and row.get("close") is not None and str(row.get("close")) != "nan" else None,
                    float(row["volume"]) if hasattr(row, "__getitem__") and row.get("volume") is not None and str(row.get("volume")) != "nan" else None,
                    float(row["amount"]) if hasattr(row, "__getitem__") and row.get("amount") is not None and str(row.get("amount")) != "nan" else None,
                    str(row.get("provider", "")) if hasattr(row, "__getitem__") and row.get("provider") is not None else None,
                )
            )
        with self._connect() as conn:
            conn.executemany(
                f"""INSERT OR REPLACE INTO {table}
                   (symbol, time, open, high, low, close, volume, amount, provider)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                records,
            )
        self._market_symbols_cache.pop(table, None)

    def load_market_data(self, symbol: str, price_mode: str = "qfq"):
        import pandas as pd

        table = self._market_table(price_mode)
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT time, open, high, low, close, volume, amount, symbol, provider
                   FROM {table} WHERE symbol = ? ORDER BY time""",
                (symbol,),
            ).fetchall()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame([dict(r) for r in rows])
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        for col in ("open", "high", "low", "close", "volume", "amount"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    def list_market_symbols(self, price_mode: str = "qfq") -> list[str]:
        table = self._market_table(price_mode)
        cached = self._market_symbols_cache.get(table)
        if cached is not None:
            return list(cached)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT symbol FROM {table} ORDER BY symbol"
            ).fetchall()
        symbols = [r["symbol"] for r in rows]
        self._market_symbols_cache[table] = symbols
        return list(symbols)

    def get_market_data_summary(self, symbol: str, price_mode: str = "qfq") -> dict:
        table = self._market_table(price_mode)
        with self._connect() as conn:
            row = conn.execute(
                f"""SELECT COUNT(*) AS rows, MIN(time) AS start, MAX(time) AS end
                   FROM {table} WHERE symbol = ?""",
                (symbol,),
            ).fetchone()
        if row is None or row["rows"] == 0:
            return {"rows": 0, "start": None, "end": None}
        return {"rows": row["rows"], "start": row["start"], "end": row["end"]}

    def clear_market_data(self, price_mode: str = "qfq") -> int:
        table = self._market_table(price_mode)
        with self._connect() as conn:
            cur = conn.execute(f"DELETE FROM {table}")
            self._market_symbols_cache.pop(table, None)
            return int(cur.rowcount or 0)

    # ------------------------------------------------------------------
    # users（手工交易记录的用户体系，密码明文存储 — 内部小工具口径）
    # ------------------------------------------------------------------
    def create_user(self, username: str, password: str, is_admin: bool = False) -> dict:
        username = str(username).strip()
        if not username:
            raise ValueError("username is required")
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, password, is_admin) VALUES (?, ?, ?)",
                (username, str(password), 1 if is_admin else 0),
            )
            user_id = int(cur.lastrowid or 0)
        user = self.get_user(user_id)
        if user is None:
            raise RuntimeError(f"failed to create user: {username}")
        return user

    def get_user(self, user_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (int(user_id),)
            ).fetchone()
        return self._user_row(row) if row else None

    def get_user_by_username(self, username: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ?", (str(username).strip(),)
            ).fetchone()
        return self._user_row(row) if row else None

    def list_users(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        return [self._user_row(row) for row in rows]

    @staticmethod
    def _user_row(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["is_admin"] = bool(d.get("is_admin"))
        return d

    # ------------------------------------------------------------------
    # manual_trades（手工交易记录：同一标的多次买入 = 多条独立记录）
    # ------------------------------------------------------------------
    def create_manual_trade(
        self,
        user_id: int,
        symbol: str,
        buy_date: str,
        buy_price: float,
        shares: float,
    ) -> dict:
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO manual_trades (user_id, symbol, buy_date, buy_price, shares)
                   VALUES (?, ?, ?, ?, ?)""",
                (int(user_id), str(symbol), str(buy_date), float(buy_price), float(shares)),
            )
            trade_id = int(cur.lastrowid or 0)
        trade = self.get_manual_trade(trade_id)
        if trade is None:
            raise RuntimeError("failed to create manual trade")
        return trade

    def get_manual_trade(self, trade_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM manual_trades WHERE id = ?", (int(trade_id),)
            ).fetchone()
        return dict(row) if row else None

    def list_manual_trades(self, user_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM manual_trades WHERE user_id = ? ORDER BY id",
                (int(user_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def close_manual_trade(
        self, trade_id: int, sell_date: str, sell_price: float
    ) -> dict | None:
        """open → closed；已清仓或不存在时返回 None（幂等防重）。"""
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE manual_trades
                   SET status = 'closed', sell_date = ?, sell_price = ?,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND status = 'open'""",
                (str(sell_date), float(sell_price), int(trade_id)),
            )
            if cur.rowcount == 0:
                return None
        return self.get_manual_trade(trade_id)

    # ------------------------------------------------------------------
    # job_runs
    # ------------------------------------------------------------------
    def record_job_run(
        self,
        job_type: str,
        payload: dict,
        run_date: str | None = None,
        status: str | None = None,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO job_runs (job_type, run_date, status, payload)
                   VALUES (?, ?, ?, ?)""",
                (
                    str(job_type),
                    run_date,
                    status or str(payload.get("status", "")),
                    json.dumps(payload, ensure_ascii=False, default=str),
                ),
            )
            return int(cursor.lastrowid or 0)

    def get_latest_job_run(self, job_type: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM job_runs WHERE job_type = ?
                   ORDER BY id DESC LIMIT 1""",
                (str(job_type),),
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["payload"] = json.loads(d["payload"]) if d.get("payload") else {}
        return d

    def list_job_runs(self, job_type: str, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM job_runs WHERE job_type = ?
                   ORDER BY id DESC LIMIT ?""",
                (str(job_type), int(limit)),
            ).fetchall()
        out: list[dict] = []
        for row in rows:
            d = dict(row)
            d["payload"] = json.loads(d["payload"]) if d.get("payload") else {}
            out.append(d)
        return out

    # ------------------------------------------------------------------
    # app_config
    # ------------------------------------------------------------------
    def get_config(self, key: str, default: Any = None) -> Any:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM app_config WHERE key = ?", (str(key),)
            ).fetchone()
        if row is None:
            return default
        text = row["value"]
        try:
            return json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return text

    def set_config(self, key: str, value: Any) -> None:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO app_config (key, value, updated_at)
                   VALUES (?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value,
                       updated_at = excluded.updated_at""",
                (str(key), text),
            )

    def get_all_config(self) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM app_config").fetchall()
        out: dict[str, Any] = {}
        for row in rows:
            try:
                out[row["key"]] = json.loads(row["value"])
            except (TypeError, json.JSONDecodeError):
                out[row["key"]] = row["value"]
        return out

    # ------------------------------------------------------------------
    # trend_param_sets / indicator_daily / trend_daily (precomputed caches)
    # ------------------------------------------------------------------
    def get_param_set(self, param_set: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM trend_param_sets WHERE param_set = ?", (param_set,)
            ).fetchone()
        return dict(row) if row else None

    def save_param_set(self, param_set: str, params_json: str, is_default: bool, formula_version: int) -> None:
        with self._connect() as conn:
            if is_default:
                conn.execute("UPDATE trend_param_sets SET is_default = 0")
            conn.execute(
                """INSERT INTO trend_param_sets (param_set, params_json, is_default, formula_version, created_at)
                   VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(param_set) DO UPDATE SET
                       params_json = excluded.params_json,
                       is_default = excluded.is_default,
                       formula_version = excluded.formula_version""",
                (param_set, params_json, 1 if is_default else 0, int(formula_version)),
            )

    def save_indicator_daily(self, symbol: str, df, formula_version: int, price_mode: str = "qfq") -> int:
        """Replace one symbol's cached indicator rows (full-symbol rebuild)."""
        if df.empty:
            return 0

        def col(name: str) -> list:
            return [None if pd.isna(v) else float(v) for v in df[name].tolist()] if name in df.columns else [None] * len(df)

        times = [str(t) for t in df["time"].tolist()]
        columns = (
            "atr", "vol_ma20", "er10",
            "sma5", "sma10", "sma20", "sma60", "sma120", "sma200",
            "ema5", "ema10", "ema20", "rsi14",
            "macd_dif", "macd_dea", "macd_hist",
            "boll_mid", "boll_up", "boll_dn",
            "rsi_avg_gain", "rsi_avg_loss", "macd_ema12", "macd_ema26",
        )
        values = [col(name) for name in columns]
        records = [
            (symbol, times[i], *row_vals, price_mode, int(formula_version))
            for i, row_vals in enumerate(zip(*values))
        ]
        with self._connect() as conn:
            conn.execute("DELETE FROM indicator_daily WHERE symbol = ?", (symbol,))
            conn.executemany(
                """INSERT INTO indicator_daily
                   (symbol, time, atr, vol_ma20, er10,
                    sma5, sma10, sma20, sma60, sma120, sma200,
                    ema5, ema10, ema20, rsi14,
                    macd_dif, macd_dea, macd_hist,
                    boll_mid, boll_up, boll_dn,
                    rsi_avg_gain, rsi_avg_loss, macd_ema12, macd_ema26,
                    price_mode, formula_version, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                records,
            )
        return len(records)

    def load_indicator_daily(self, symbol: str):
        import pandas as pd

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM indicator_daily WHERE symbol = ? ORDER BY time", (symbol,)
            ).fetchall()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame([dict(r) for r in rows])

    def save_trend_daily(self, symbol: str, df, formula_version: int, param_set: str = "default", price_mode: str = "qfq") -> int:
        if df.empty:
            return 0

        def col(name: str) -> list:
            return [None if pd.isna(v) else float(v) for v in df[name].tolist()] if name in df.columns else [None] * len(df)

        times = [str(t) for t in df["time"].tolist()]
        columns = ("trend_score", "trend_ma5", "trend_ma10", "price_direction", "confidence")
        values = [col(name) for name in columns]
        records = [
            (symbol, times[i], param_set, *row_vals, price_mode, int(formula_version))
            for i, row_vals in enumerate(zip(*values))
        ]
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM trend_daily WHERE symbol = ? AND param_set = ?", (symbol, param_set)
            )
            conn.executemany(
                """INSERT INTO trend_daily
                   (symbol, time, param_set, trend_score, trend_ma5, trend_ma10,
                    price_direction, confidence, price_mode, formula_version, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                records,
            )
        return len(records)

    def load_indicator_latest(self, formula_version: int | None = None) -> dict[str, dict]:
        """Latest indicator_daily row per symbol — one query for intraday overlays."""
        query = """
            SELECT t.* FROM indicator_daily t
            JOIN (SELECT symbol, MAX(time) AS mt FROM indicator_daily GROUP BY symbol) m
              ON t.symbol = m.symbol AND t.time = m.mt
        """
        params: list = []
        if formula_version is not None:
            query += " WHERE t.formula_version = ?"
            params.append(int(formula_version))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return {r["symbol"]: dict(r) for r in rows}

    def load_trend_daily_bulk(self, since: str, param_set: str = "default", formula_version: int | None = None) -> list[dict]:
        """All symbols' trend rows since a date — one bulk query for dashboards."""
        query = """SELECT symbol, time, trend_score, trend_ma5, trend_ma10,
                          price_direction, confidence
                   FROM trend_daily WHERE param_set = ? AND time >= ?"""
        params: list = [param_set, str(since)]
        if formula_version is not None:
            query += " AND formula_version = ?"
            params.append(int(formula_version))
        query += " ORDER BY symbol, time"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def load_trend_daily(self, symbol: str, param_set: str = "default", since: str | None = None):
        import pandas as pd

        query = "SELECT * FROM trend_daily WHERE symbol = ? AND param_set = ?"
        params: list = [symbol, param_set]
        if since is not None:
            query += " AND time >= ?"
            params.append(str(since))
        query += " ORDER BY time"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame([dict(r) for r in rows])

    def indicator_cache_info(self, symbol: str) -> dict:
        """Coverage/version info used for staleness checks."""
        with self._connect() as conn:
            ind = conn.execute(
                "SELECT COUNT(*) AS n, MAX(time) AS last, MAX(formula_version) AS ver FROM indicator_daily WHERE symbol = ?",
                (symbol,),
            ).fetchone()
            trend = conn.execute(
                "SELECT COUNT(*) AS n, MAX(time) AS last, MAX(formula_version) AS ver FROM trend_daily WHERE symbol = ? AND param_set = 'default'",
                (symbol,),
            ).fetchone()
        return {
            "indicator_rows": int(ind["n"] or 0),
            "indicator_last": ind["last"],
            "indicator_version": ind["ver"],
            "trend_rows": int(trend["n"] or 0),
            "trend_last": trend["last"],
            "trend_version": trend["ver"],
        }

    def indicator_cache_symbols(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT DISTINCT symbol FROM indicator_daily").fetchall()
        return {r["symbol"] for r in rows}

    def indicator_global_version(self) -> int | None:
        """MAX(formula_version) across indicator_daily; None when empty."""
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(formula_version) AS v FROM indicator_daily").fetchone()
        return int(row["v"]) if row and row["v"] is not None else None

    def clear_indicator_caches(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM indicator_daily")
            conn.execute("DELETE FROM trend_daily")


def init_db(db_path: str | Path = "data/trend_quant.db") -> Database:
    global _db_instance
    _db_instance = Database(db_path)
    return _db_instance


def get_db() -> Database:
    if _db_instance is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _db_instance


def record_job_run_safely(
    job_type: str,
    payload: dict,
    run_date: str | None = None,
    status: str | None = None,
) -> None:
    """Best-effort job_run recording — never breaks the caller's workflow."""
    try:
        get_db().record_job_run(job_type, payload, run_date=run_date, status=status)
    except Exception:
        import logging

        logging.getLogger(__name__).warning("Failed to record job run: %s", job_type, exc_info=True)
