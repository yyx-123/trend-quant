from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pandas as pd

from audit.app_logger import get_logger
from core.calendar import market_now
from core.display import category_path
from core.env import password_iterations
from core.paths import default_db_path

_logger = get_logger(__name__)

_db_instance: Database | None = None

# 密码哈希格式：pbkdf2_sha256$迭代次数$盐(hex)$摘要(hex)。
# 2026-08 之前库存的是明文，由 _migrate_schema 一次性改写为哈希。
_PASSWORD_ALGO = "pbkdf2_sha256"
_PASSWORD_ITERATIONS = 200_000


def _password_iterations() -> int:
    """新哈希的迭代数：生产固定 20 万；测试环境可经 env 调低提速
    （TREND_QUANT_PASSWORD_ITERATIONS，附录 B N4）。已存哈希的迭代数
    记录在哈希串内，校验不受影响。"""
    return password_iterations(_PASSWORD_ITERATIONS)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    iterations = _password_iterations()
    digest = hashlib.pbkdf2_hmac(
        "sha256", str(password).encode("utf-8"), bytes.fromhex(salt), iterations
    ).hex()
    return f"{_PASSWORD_ALGO}${iterations}${salt}${digest}"


def verify_password(stored: str, candidate: str) -> bool:
    """校验候选密码（仅接受 pbkdf2 哈希存储）。

    2026-08 迁移期的明文比对兜底已随生产库 100% 哈希化（2026-08-26 实测
    3/3 用户均为 pbkdf2 格式）清零——非哈希格式一律判失败，不再存在
    绕过 pbkdf2 的明文比对路径。
    """
    stored = str(stored)
    candidate = str(candidate)
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != _PASSWORD_ALGO:
        return False
    _, iterations, salt, digest = parts
    actual = hashlib.pbkdf2_hmac(
        "sha256", candidate.encode("utf-8"), bytes.fromhex(salt), int(iterations)
    ).hex()
    return hmac.compare_digest(actual, digest)


def _dt_str(dt: datetime) -> str:
    """与 SQLite datetime('now','localtime') 相同的字符串格式，保证可直接比较。"""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class Database:
    def __init__(self, db_path: str | Path | None = None) -> None:
        db_path = db_path if db_path is not None else default_db_path()
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
        # timeout=30 即 busy_timeout：批量回测长事务 + 调度器三类任务并发
        # 命中同一 WAL 库时，默认 5s 可能不够（P2-16/P2-23）。
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        # WAL: readers are not blocked during indicator cache rebuilds.
        conn.execute("PRAGMA journal_mode=WAL")
        # 外键实际生效（manual_trades.user_id / sessions.user_id），删用户
        # 不再留孤儿行；生产库已实测零孤儿（P2-23）。
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def backup_to(self, backup_dir: str | Path | None = None, keep: int = 3) -> Path:
        """Online backup via VACUUM INTO (WAL-safe), keeping the newest ``keep`` files.

        默认备份目录是 **DB 文件所在目录下的 backups/**（生产库 → data/backups，
        行为不变），而不是 CWD 相对的固定路径——否则从项目根目录跑的测试会把
        临时库的快照写进生产备份目录，并触发 keep 修剪挤掉真实备份。
        """
        target_dir = Path(backup_dir) if backup_dir is not None else self.db_path.parent / "backups"
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        dest = target_dir / f"trend_quant-{stamp}.db"
        # VACUUM INTO 目标路径以单引号拼接进 SQL，含单引号的路径必须显式拒绝。
        if "'" in str(dest):
            raise ValueError(f"backup destination path must not contain a single quote: {dest}")
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            # 显式 WAL checkpoint：确保最近写入都在主库文件内，备份不缺口。
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
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
                    created_at TEXT DEFAULT (datetime('now','localtime')),
                    updated_at TEXT DEFAULT (datetime('now','localtime'))
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
                    created_at TEXT DEFAULT (datetime('now','localtime')),
                    updated_at TEXT DEFAULT (datetime('now','localtime'))
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
                    updated_at TEXT DEFAULT (datetime('now','localtime')),
                    PRIMARY KEY (symbol, time)
                );
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
                    updated_at TEXT DEFAULT (datetime('now','localtime')),
                    PRIMARY KEY (symbol, time)
                );
                CREATE TABLE IF NOT EXISTS ex_factors (
                    symbol TEXT NOT NULL,
                    time TEXT NOT NULL,
                    factor REAL NOT NULL,
                    provider TEXT,
                    updated_at TEXT DEFAULT (datetime('now','localtime')),
                    PRIMARY KEY (symbol, time)
                );
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
                    updated_at TEXT DEFAULT (datetime('now','localtime'))
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
                    updated_at TEXT DEFAULT (datetime('now','localtime'))
                );
                CREATE INDEX IF NOT EXISTS idx_instrument_categories_parent
                    ON instrument_categories(parent_path, priority, name);

                -- 申万行业分类 fact 表（stock_industry_etf_holdings 方案 §4.1）。
                -- sw_l3_code 混存两套码（tushare 850xxx.SI / tickflow 6 位内部码），
                -- 消费方须按 source 解释；当前仅留档，无功能消费方。
                CREATE TABLE IF NOT EXISTS stock_industry (
                    symbol TEXT PRIMARY KEY,
                    sw_l1_name TEXT NOT NULL,
                    sw_l2_name TEXT NOT NULL,
                    sw_l3_name TEXT NOT NULL DEFAULT '',
                    sw_l3_code TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL,
                    updated_at TEXT DEFAULT (datetime('now','localtime'))
                );

                -- ETF 前十大重仓股季度快照（方案 §4.2）。软失效：整只 ETF 翻转
                -- is_current，从不删行；fetched_at 必须本地时间（全库约定）。
                CREATE TABLE IF NOT EXISTS etf_constituents (
                    etf_symbol TEXT NOT NULL,
                    stock_symbol TEXT NOT NULL,
                    stock_name TEXT NOT NULL DEFAULT '',
                    weight REAL,
                    rank INTEGER NOT NULL,
                    period TEXT NOT NULL,
                    ann_date TEXT,
                    is_current INTEGER NOT NULL DEFAULT 1,
                    source TEXT NOT NULL DEFAULT 'tushare_fund_portfolio',
                    fetched_at TEXT DEFAULT (datetime('now','localtime')),
                    PRIMARY KEY (etf_symbol, stock_symbol, period)
                );
                CREATE INDEX IF NOT EXISTS idx_etf_constituents_current
                    ON etf_constituents(etf_symbol, is_current, rank);
                CREATE INDEX IF NOT EXISTS idx_etf_constituents_stock
                    ON etf_constituents(stock_symbol, is_current);

                -- 一次性迁移的旧类目归档（方案 §4.3），可随时回溯。
                CREATE TABLE IF NOT EXISTS stock_category_archive (
                    symbol TEXT PRIMARY KEY,
                    category_l2 TEXT,
                    category_l3 TEXT,
                    migration TEXT NOT NULL,
                    archived_at TEXT DEFAULT (datetime('now','localtime'))
                );

                CREATE TABLE IF NOT EXISTS job_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_type TEXT NOT NULL,
                    run_date TEXT,
                    status TEXT,
                    payload TEXT,
                    created_at TEXT DEFAULT (datetime('now','localtime'))
                );
                CREATE INDEX IF NOT EXISTS idx_job_runs_type_id
                    ON job_runs(job_type, id);

                -- 内容版本计数器：qfq 原位重写（除权重物化）不改变行数/最大
                -- 日期，任何「按日期判断新鲜度」的机制都会失明；每次行情写
                -- 入都把对应 name 的计数器 +1，让缓存/看板能感知价格口径变化。
                -- 命名：<table>（表级）与 <table>:<symbol>（标的级）。
                CREATE TABLE IF NOT EXISTS data_versions (
                    name TEXT PRIMARY KEY,
                    version INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS app_config (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT DEFAULT (datetime('now','localtime'))
                );

                -- 标的大盘盘中实时看板的最新快照（单行，id 固定为 1）。
                -- 仅用于看板展示，与 market_data_* 日K库完全隔离：盘中合成
                -- K线/指标只存在于本表的 payload 里，日K库只由收盘后的
                -- 补库任务写入稳定数据。
                CREATE TABLE IF NOT EXISTS dashboard_snapshot (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    kind TEXT NOT NULL,
                    as_of TEXT,
                    computed_at TEXT NOT NULL,
                    payload TEXT NOT NULL
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
                    ema_s REAL, ema_m REAL, ema_l REAL,
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
                    created_at TEXT DEFAULT (datetime('now','localtime'))
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
                    created_at TEXT DEFAULT (datetime('now','localtime')),
                    updated_at TEXT DEFAULT (datetime('now','localtime'))
                );
                CREATE INDEX IF NOT EXISTS idx_manual_trades_user_status
                    ON manual_trades(user_id, status, id);

                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    created_at TEXT DEFAULT (datetime('now','localtime')),
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_expires
                    ON sessions(expires_at);

                CREATE TABLE IF NOT EXISTS batch_backtest_runs (
                    batch_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'running',
                    categories_json TEXT NOT NULL DEFAULT '[]',
                    strategy_snapshot_json TEXT NOT NULL DEFAULT '[]',
                    config_json TEXT NOT NULL DEFAULT '{}',
                    total_cells INTEGER NOT NULL DEFAULT 0,
                    done_cells INTEGER NOT NULL DEFAULT 0,
                    ok_cells INTEGER NOT NULL DEFAULT 0,
                    failed_cells INTEGER NOT NULL DEFAULT 0,
                    skipped_cells INTEGER NOT NULL DEFAULT 0,
                    current_symbol TEXT,
                    data_anchor_date TEXT,
                    data_version TEXT,
                    engine_version TEXT NOT NULL DEFAULT '1.0',
                    created_at TEXT DEFAULT (datetime('now','localtime')),
                    finished_at TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_batch_backtest_runs_status
                    ON batch_backtest_runs(status, created_at);

                CREATE TABLE IF NOT EXISTS batch_backtest_cells (
                    batch_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    symbol_name TEXT,
                    strategy_name TEXT,
                    category_l1 TEXT,
                    category_l2 TEXT,
                    category_l3 TEXT,
                    asset_type TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    start_date TEXT,
                    end_date TEXT,
                    bar_count INTEGER,
                    partial_window INTEGER NOT NULL DEFAULT 0,
                    total_return REAL,
                    annual_return REAL,
                    max_drawdown REAL,
                    sharpe REAL,
                    sortino REAL,
                    calmar REAL,
                    win_rate REAL,
                    profit_factor REAL,
                    trade_count INTEGER,
                    avg_holding_days REAL,
                    avg_flat_days REAL,
                    final_equity REAL,
                    benchmark_total_return REAL,
                    benchmark_annual_return REAL,
                    benchmark_sharpe REAL,
                    benchmark_calmar REAL,
                    excess_annual_return REAL,
                    excess_sharpe REAL,
                    excess_calmar REAL,
                    annual_returns_json TEXT,
                    monthly_heatmap_json TEXT,
                    trades_json TEXT,
                    skipped_buys_json TEXT,
                    monthly_nav_json TEXT,
                    created_at TEXT DEFAULT (datetime('now','localtime')),
                    PRIMARY KEY (batch_id, symbol, strategy_id)
                );
                CREATE INDEX IF NOT EXISTS idx_batch_cells_batch
                    ON batch_backtest_cells(batch_id);
                CREATE INDEX IF NOT EXISTS idx_batch_cells_annual_return
                    ON batch_backtest_cells(batch_id, annual_return);

                CREATE TABLE IF NOT EXISTS batch_backtest_symbol_features (
                    batch_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    ann_volatility REAL,
                    momentum_250 REAL,
                    bh_max_drawdown REAL,
                    trend_score_avg REAL,
                    amount_ma20 REAL,
                    bar_count INTEGER,
                    created_at TEXT DEFAULT (datetime('now','localtime')),
                    PRIMARY KEY (batch_id, symbol)
                );

                """
            )

    # ------------------------------------------------------------------
    # schema migration
    # ------------------------------------------------------------------
    def _migrate_schema(self) -> None:
        """Idempotent column additions for existing databases."""
        metadata_columns = {
            "enabled": "INTEGER NOT NULL DEFAULT 1",
            "stop_atr_mul": "REAL",
            "risk_budget_pct": "REAL",
            "asset_type": "TEXT",
            "start_date": "TEXT",
        }
        # 指标/趋势缓存记录构建时的行情内容版本（见 data_versions），
        # 用于识别「日期没变但价格口径变了」的陈旧缓存。
        cache_columns = {"data_version": "INTEGER NOT NULL DEFAULT 0"}
        # EMA 递推锚点列改为跟随 n_short/n_mid/n_long 的通用命名（原 ema5/10/20
        # 与新周期语义脱钩；旧列不再写入，存量库保留为历史遗留）。
        indicator_columns = {
            **cache_columns,
            "ema_s": "REAL",
            "ema_m": "REAL",
            "ema_l": "REAL",
        }
        batch_cell_columns = {
            "avg_holding_days": "REAL",
            "partial_window": "INTEGER NOT NULL DEFAULT 0",
            "avg_flat_days": "REAL",
            "benchmark_sharpe": "REAL",
            "benchmark_calmar": "REAL",
            "excess_sharpe": "REAL",
            "excess_calmar": "REAL",
        }
        targets = {
            "instrument_metadata": metadata_columns,
            "indicator_daily": indicator_columns,
            "trend_daily": cache_columns,
            "batch_backtest_cells": batch_cell_columns,
        }
        with self._connect() as conn:
            # N1（2026-08-25）：删除与 PRIMARY KEY (symbol,time) 完全同列的
            # 冗余索引——rowid 表上 PK 已自动建同列索引，这三个白白放大
            # 百万行表的每次写入。
            for redundant_index in (
                "idx_market_data_raw_symbol_time",
                "idx_market_data_qfq_symbol_time",
                "idx_ex_factors_symbol_time",
            ):
                conn.execute(f"DROP INDEX IF EXISTS {redundant_index}")
            for table, new_columns in targets.items():
                existing = {
                    row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
                }
                for name, ddl in new_columns.items():
                    if name not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

            # 2026-08 登录墙改造：users 表由明文密码迁移为 pbkdf2 哈希。
            # 旧库明文就在库里，直接读出重哈希即可，幂等（已哈希的行跳过）。
            rows = conn.execute("SELECT id, password FROM users").fetchall()
            for row in rows:
                stored = str(row["password"])
                if stored.startswith(f"{_PASSWORD_ALGO}$"):
                    continue
                conn.execute(
                    "UPDATE users SET password = ? WHERE id = ?",
                    (hash_password(stored), row["id"]),
                )
                _logger.info("Migrated plaintext password to hash for user id=%s", row["id"])

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
                   (id, name, description, schema_version, trade_mode, payload_json, is_active,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 1,
                    datetime('now','localtime'), datetime('now','localtime'))
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name,
                     description=excluded.description,
                     schema_version=excluded.schema_version,
                     trade_mode=excluded.trade_mode,
                     payload_json=excluded.payload_json,
                     is_active=1,
                     updated_at=datetime('now','localtime')""",
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
                   SET is_active = 0, updated_at = datetime('now','localtime')
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
                   (id, name, description, sizer_type, payload_json, is_active,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 1,
                    datetime('now','localtime'), datetime('now','localtime'))
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name,
                     description=excluded.description,
                     sizer_type=excluded.sizer_type,
                     payload_json=excluded.payload_json,
                     is_active=1,
                     updated_at=datetime('now','localtime')""",
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
                   SET is_active = 0, updated_at = datetime('now','localtime')
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
        return category_path(row)

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
                    enabled, stop_atr_mul, risk_budget_pct, asset_type, start_date, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    datetime('now','localtime'))
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
                     updated_at=datetime('now','localtime')""",
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
        """Lean K-line tail for all symbols (no metadata join) — bulk overlay reads.

        amount 一并取出：盘中看板的成交额加权聚合（_weighted_daily_trend_series）
        在缓存路径下只有 tail 可用，缺列会 KeyError。
        """
        table = self._market_table(price_mode)
        cutoff = (market_now().date() - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT symbol, time, open, high, low, close, volume, amount
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

    def get_market_dashboard_revision(self) -> tuple[str, str, int]:
        """Small revision token used to invalidate the in-process subject-board cache.

        三元素 token（P2-18：去掉对百万行表的 COUNT(*)）：
        - MAX(time)：走 (symbol,time) 主键索引近零成本，感知 append；
        - metadata 最大更新时间：感知标的池/分类变更；
        - qfq 表内容版本（data_versions）：除权重物化等原位改写不改行数/
          最大日期，由写入侧 bump 的版本感知（行数提供的信息已被三者覆盖）。
        """
        with self._connect() as conn:
            market = conn.execute(
                "SELECT MAX(time) AS latest_time FROM market_data_qfq"
            ).fetchone()
            metadata = conn.execute(
                "SELECT MAX(updated_at) AS latest_metadata FROM instrument_metadata"
            ).fetchone()
            version = self._bump_free_version(conn, "market_data_qfq")
        return (
            str(market["latest_time"] or "") if market else "",
            str(metadata["latest_metadata"] or "") if metadata else "",
            version,
        )

    @staticmethod
    def _bump_free_version(conn, name: str) -> int:
        row = conn.execute("SELECT version FROM data_versions WHERE name = ?", (name,)).fetchone()
        return int(row["version"] or 0) if row else 0

    def save_dashboard_snapshot(self, kind: str, as_of: str | None, payload: dict) -> str:
        """Persist the latest subject-dashboard snapshot (single-row replace).

        返回写入的 computed_at（本地时间 ISO 字符串）。payload  JSON 序列化
        存入独立快照表，与日K库无任何交集。
        """
        computed_at = market_now().replace(tzinfo=None).isoformat(timespec="seconds")
        blob = json.dumps(payload, ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO dashboard_snapshot (id, kind, as_of, computed_at, payload)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind = excluded.kind,
                    as_of = excluded.as_of,
                    computed_at = excluded.computed_at,
                    payload = excluded.payload
                """,
                (kind, as_of, computed_at, blob),
            )
        return computed_at

    def load_dashboard_snapshot(self) -> dict | None:
        """Load the persisted snapshot, or None if none has been saved yet."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT kind, as_of, computed_at, payload FROM dashboard_snapshot WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            _logger.warning("Corrupt dashboard_snapshot payload; ignoring")
            return None
        return {
            "kind": row["kind"],
            "as_of": row["as_of"],
            "computed_at": row["computed_at"],
            "payload": payload,
        }


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
                   (path, level, name, parent_path, priority, updated_at)
                   VALUES (?, ?, ?, ?, ?, datetime('now','localtime'))
                   ON CONFLICT(path) DO UPDATE SET
                     level=excluded.level,
                     name=excluded.name,
                     parent_path=excluded.parent_path,
                     priority=excluded.priority,
                     updated_at=datetime('now','localtime')""",
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
    # stock_industry（申万行业分类 fact 表）
    # ------------------------------------------------------------------
    # 来源优先级：数值大者可覆盖数值小者；manual 任何同步都不动。
    _INDUSTRY_SOURCE_RANK: ClassVar[dict[str, int]] = {"tickflow_universe": 1, "tushare_sw2021": 2, "manual": 3}

    def upsert_stock_industry(self, rows: list[dict[str, Any]], source: str) -> int:
        """按来源优先级合并写入，只增/改、从不删行。

        返回实际写入行数（被更高优先级来源挡下的行不计）。
        """
        new_rank = self._INDUSTRY_SOURCE_RANK.get(str(source or "").strip())
        if new_rank is None:
            raise ValueError(f"unknown stock_industry source: {source}")

        prepared: dict[str, tuple] = {}
        for item in rows:
            symbol = str(item.get("symbol") or "").strip().upper()
            l1 = str(item.get("sw_l1_name") or "").strip()
            l2 = str(item.get("sw_l2_name") or "").strip()
            if not symbol or not l1 or not l2:
                continue
            prepared[symbol] = (
                symbol,
                l1,
                l2,
                str(item.get("sw_l3_name") or "").strip(),
                str(item.get("sw_l3_code") or "").strip(),
                source,
            )
        if not prepared:
            return 0

        with self._connect() as conn:
            ph = ",".join("?" * len(prepared))
            existing = {
                row["symbol"]: row["source"]
                for row in conn.execute(
                    f"SELECT symbol, source FROM stock_industry WHERE symbol IN ({ph})",
                    list(prepared.keys()),
                )
            }
            writable = [
                rec
                for sym, rec in prepared.items()
                if new_rank
                >= self._INDUSTRY_SOURCE_RANK.get(str(existing.get(sym) or ""), 0)
            ]
            if writable:
                conn.executemany(
                    """INSERT INTO stock_industry
                       (symbol, sw_l1_name, sw_l2_name, sw_l3_name, sw_l3_code, source, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime'))
                       ON CONFLICT(symbol) DO UPDATE SET
                         sw_l1_name=excluded.sw_l1_name,
                         sw_l2_name=excluded.sw_l2_name,
                         sw_l3_name=excluded.sw_l3_name,
                         sw_l3_code=excluded.sw_l3_code,
                         source=excluded.source,
                         updated_at=datetime('now','localtime')""",
                    writable,
                )
        return len(writable)

    def get_stock_industry(self, symbol: str) -> dict | None:
        normalized = str(symbol or "").strip().upper()
        if not normalized:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM stock_industry WHERE symbol = ?", (normalized,)
            ).fetchone()
        return dict(row) if row else None

    def list_stock_industry(self, symbols: list[str] | None = None) -> list[dict]:
        with self._connect() as conn:
            if symbols is None:
                rows = conn.execute("SELECT * FROM stock_industry ORDER BY symbol").fetchall()
            else:
                normalized = [str(s or "").strip().upper() for s in symbols]
                normalized = [s for s in normalized if s]
                if not normalized:
                    return []
                ph = ",".join("?" * len(normalized))
                rows = conn.execute(
                    f"SELECT * FROM stock_industry WHERE symbol IN ({ph}) ORDER BY symbol",
                    normalized,
                ).fetchall()
        return [dict(row) for row in rows]

    def update_instrument_category(
        self,
        symbol: str,
        category_l1: str,
        category_l2: str,
        category_l3: str,
        priority_l1: int | None,
        priority_l2: int | None,
        priority_l3: int | None,
    ) -> bool:
        """只改类目与排序字段。显式刷新 updated_at —— 看板 revision 只看
        MAX(updated_at)，漏写会静默继续读旧分组缓存（方案评审 B1）。"""
        normalized = str(symbol or "").strip().upper()
        if not normalized:
            return False
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE instrument_metadata
                   SET category_l1 = ?, category_l2 = ?, category_l3 = ?,
                       priority_l1 = ?, priority_l2 = ?, priority_l3 = ?,
                       updated_at = datetime('now','localtime')
                   WHERE symbol = ?""",
                (
                    str(category_l1 or "").strip(),
                    str(category_l2 or "").strip(),
                    str(category_l3 or "").strip(),
                    priority_l1,
                    priority_l2,
                    priority_l3,
                    normalized,
                ),
            )
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # etf_constituents（ETF 前十大重仓股季度快照）
    # ------------------------------------------------------------------
    def save_etf_constituents(
        self,
        etf_symbol: str,
        rows: list[dict[str, Any]],
        period: str,
        source: str = "tushare_fund_portfolio",
    ) -> int:
        """单事务：先把该 ETF 全部行置 is_current=0，再 upsert 本期行（=1）。

        保证「查询当前前十」永远只命中一个期次；空 rows 也合法（整只翻转失效）。
        """
        etf = str(etf_symbol or "").strip().upper()
        period = str(period or "").strip()
        if not etf or not period:
            raise ValueError("etf_symbol 与 period 均不能为空")

        records: list[tuple] = []
        for item in rows:
            stock = str(item.get("stock_symbol") or "").strip().upper()
            if not stock:
                continue
            records.append(
                (
                    etf,
                    stock,
                    str(item.get("stock_name") or "").strip(),
                    item.get("weight"),
                    int(item.get("rank") or 0),
                    period,
                    str(item.get("ann_date") or "").strip() or None,
                    source,
                )
            )

        with self._connect() as conn:
            conn.execute(
                "UPDATE etf_constituents SET is_current = 0 WHERE etf_symbol = ?",
                (etf,),
            )
            if records:
                conn.executemany(
                    """INSERT INTO etf_constituents
                       (etf_symbol, stock_symbol, stock_name, weight, rank, period,
                        ann_date, is_current, source, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, datetime('now','localtime'))
                       ON CONFLICT(etf_symbol, stock_symbol, period) DO UPDATE SET
                         stock_name=excluded.stock_name,
                         weight=excluded.weight,
                         rank=excluded.rank,
                         ann_date=excluded.ann_date,
                         is_current=1,
                         source=excluded.source,
                         fetched_at=datetime('now','localtime')""",
                    records,
                )
        return len(records)

    def list_current_etf_constituents(self, etf_symbol: str) -> list[dict]:
        etf = str(etf_symbol or "").strip().upper()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM etf_constituents
                   WHERE etf_symbol = ? AND is_current = 1 ORDER BY rank""",
                (etf,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_all_current_etf_constituents(self) -> list[dict]:
        """全部 ETF 的当前重仓股（按 etf_symbol, rank），批量导入脚本用。"""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM etf_constituents
                   WHERE is_current = 1 ORDER BY etf_symbol, rank"""
            ).fetchall()
        return [dict(row) for row in rows]

    def list_etf_constituent_periods(self) -> list[dict]:
        """每只 ETF 当前期次与抓取时间（新鲜度展示用）。"""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT etf_symbol, period, MAX(fetched_at) AS fetched_at,
                          COUNT(*) AS constituent_count
                   FROM etf_constituents WHERE is_current = 1
                   GROUP BY etf_symbol, period ORDER BY etf_symbol"""
            ).fetchall()
        return [dict(row) for row in rows]

    def has_etf_constituents_for_period(self, etf_symbol: str, period: str) -> bool:
        etf = str(etf_symbol or "").strip().upper()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM etf_constituents WHERE etf_symbol = ? AND period = ? LIMIT 1",
                (etf, str(period or "").strip()),
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # stock_category_archive（迁移前旧类目归档）
    # ------------------------------------------------------------------
    def archive_stock_categories(self, rows: list[dict[str, Any]], migration: str) -> int:
        records = [
            (
                str(item.get("symbol") or "").strip().upper(),
                str(item.get("category_l2") or "").strip(),
                str(item.get("category_l3") or "").strip(),
                str(migration or "").strip(),
            )
            for item in rows
            if str(item.get("symbol") or "").strip()
        ]
        if not records:
            return 0
        with self._connect() as conn:
            conn.executemany(
                """INSERT INTO stock_category_archive
                   (symbol, category_l2, category_l3, migration, archived_at)
                   VALUES (?, ?, ?, ?, datetime('now','localtime'))
                   ON CONFLICT(symbol) DO NOTHING""",
                records,
            )
        return len(records)

    # ------------------------------------------------------------------
    # data_versions（行情内容版本）
    # ------------------------------------------------------------------
    @staticmethod
    def _bump_data_version_conn(conn, name: str) -> int:
        conn.execute(
            """INSERT INTO data_versions (name, version) VALUES (?, 1)
               ON CONFLICT(name) DO UPDATE SET version = version + 1""",
            (name,),
        )
        row = conn.execute("SELECT version FROM data_versions WHERE name = ?", (name,)).fetchone()
        return int(row["version"] or 0)

    def get_data_version(self, name: str) -> int:
        """Current content version for *name* (0 when never written)."""
        with self._connect() as conn:
            row = conn.execute("SELECT version FROM data_versions WHERE name = ?", (name,)).fetchone()
        return int(row["version"] or 0) if row else 0

    @staticmethod
    def market_data_version_name(symbol: str, price_mode: str = "qfq") -> str:
        return f"{Database._market_table(price_mode)}:{symbol}"

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

    def _market_records(self, symbol: str, df, table: str) -> tuple[list[tuple], int]:
        """构建 upsert 记录；非正价格行（复权事故/脏数据）拦截并计数。"""
        records: list[tuple] = []
        dropped_nonpositive = 0
        for _, row in df.iterrows():
            values = {}
            for col in ("open", "high", "low", "close", "volume", "amount"):
                raw_value = row.get(col) if hasattr(row, "__getitem__") else None
                if raw_value is None or str(raw_value) == "nan":
                    values[col] = None
                else:
                    values[col] = float(raw_value)
            # 防御：非正价格永不落库 —— 宁可缺行不出错数据
            price_vals = [values[c] for c in ("open", "high", "low", "close")]
            if any(v is not None and v <= 0 for v in price_vals):
                dropped_nonpositive += 1
                continue
            records.append(
                (
                    symbol,
                    str(row.get("time", "")),
                    values["open"],
                    values["high"],
                    values["low"],
                    values["close"],
                    values["volume"],
                    values["amount"],
                    str(row.get("provider", "")) if hasattr(row, "__getitem__") and row.get("provider") is not None else None,
                )
            )
        if dropped_nonpositive:
            _logger.warning(
                "Dropped %d rows with non-positive OHLC for %s (%s)",
                dropped_nonpositive, symbol, table,
            )
        return records, dropped_nonpositive

    def save_market_data(self, symbol: str, df, price_mode: str = "qfq") -> None:
        if df.empty:
            return
        table = self._market_table(price_mode)
        records, _ = self._market_records(symbol, df, table)
        if not records:
            return
        with self._connect() as conn:
            conn.executemany(
                f"""INSERT OR REPLACE INTO {table}
                   (symbol, time, open, high, low, close, volume, amount, provider, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))""",
                records,
            )
            self._bump_data_version_conn(conn, table)
            self._bump_data_version_conn(conn, f"{table}:{symbol}")
        self._market_symbols_cache.pop(table, None)

    def replace_market_data(self, symbol: str, df, price_mode: str = "qfq") -> int:
        """同事务全量重写一个标的的行情（本地物化 qfq 用）。返回写入行数。"""
        table = self._market_table(price_mode)
        records, _ = self._market_records(symbol, df, table)
        with self._connect() as conn:
            conn.execute(f"DELETE FROM {table} WHERE symbol = ?", (symbol,))
            if records:
                conn.executemany(
                    f"""INSERT OR REPLACE INTO {table}
                       (symbol, time, open, high, low, close, volume, amount, provider, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))""",
                    records,
                )
            # 原位重写也要推进版本：除权重物化后行数/日期不变，
            # 只有版本号能让看板与指标缓存感知价格口径已变。
            self._bump_data_version_conn(conn, table)
            self._bump_data_version_conn(conn, f"{table}:{symbol}")
        self._market_symbols_cache.pop(table, None)
        return len(records)

    def load_market_data(self, symbol: str, price_mode: str = "qfq"):

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

    def list_market_data_summaries(self, price_mode: str = "qfq") -> dict[str, dict]:
        """全标的 {symbol: {rows, start, end}}——单条 GROUP BY（P2-18：
        标的管理列表接口原对 600+ 标的逐只 get_market_data_summary，
        每次新建连接；改单条聚合查询一次出结果）。"""
        table = self._market_table(price_mode)
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT symbol, COUNT(*) AS rows, MIN(time) AS start, MAX(time) AS end
                   FROM {table} GROUP BY symbol"""
            ).fetchall()
        return {
            str(row["symbol"]): {"rows": row["rows"], "start": row["start"], "end": row["end"]}
            for row in rows
        }

    def clear_market_data(self, price_mode: str = "qfq") -> int:
        table = self._market_table(price_mode)
        with self._connect() as conn:
            cur = conn.execute(f"DELETE FROM {table}")
            self._bump_data_version_conn(conn, table)
            self._market_symbols_cache.pop(table, None)
            return int(cur.rowcount or 0)

    # ------------------------------------------------------------------
    # ex_factors（除权因子：raw 真源 + 本地物化 qfq 架构的因子存储）
    # ------------------------------------------------------------------
    def save_ex_factors(self, symbol: str, factors, provider: str = "") -> None:
        """Upsert 一个标的的除权因子。factors: 可迭代的 (date-like, factor)。"""
        records: list[tuple] = []
        for item in factors or []:
            try:
                day, value = str(item[0])[:10], float(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if value <= 0:
                continue
            records.append((symbol, day, value, provider or None))
        if not records:
            return
        with self._connect() as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO ex_factors (symbol, time, factor, provider, updated_at)
                   VALUES (?, ?, ?, ?, datetime('now','localtime'))""",
                records,
            )

    def replace_ex_factors(self, symbol: str, factors, provider: str = "") -> None:
        """全量重写一个标的的因子表（vendor 同步下来的权威快照）。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM ex_factors WHERE symbol = ?", (symbol,))
        self.save_ex_factors(symbol, factors, provider=provider)

    def load_ex_factors(self, symbol: str) -> list[tuple]:
        """返回 [(time_str, factor)] 升序；time_str 为 'YYYY-MM-DD'。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT time, factor FROM ex_factors WHERE symbol = ? ORDER BY time",
                (symbol,),
            ).fetchall()
        return [(row["time"], float(row["factor"])) for row in rows]

    def load_all_ex_factors(self) -> dict[str, list[tuple]]:
        """返回 {symbol: [(time_str, factor)]}，供批量 diff 用。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT symbol, time, factor FROM ex_factors ORDER BY symbol, time"
            ).fetchall()
        result: dict[str, list[tuple]] = {}
        for row in rows:
            result.setdefault(row["symbol"], []).append((row["time"], float(row["factor"])))
        return result

    # ------------------------------------------------------------------
    # users（密码 pbkdf2 哈希存储；2026-08 前的明文由 _migrate_schema 改写）
    # ------------------------------------------------------------------
    def create_user(self, username: str, password: str, is_admin: bool = False) -> dict:
        username = str(username).strip()
        if not username:
            raise ValueError("username is required")
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, password, is_admin, created_at)"
                " VALUES (?, ?, ?, datetime('now','localtime'))",
                (username, hash_password(str(password)), 1 if is_admin else 0),
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

    def set_user_admin(self, username: str, is_admin: bool) -> None:
        """按用户名设置 admin 标记（内置管理员 ensure 用）。"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET is_admin = ? WHERE username = ?",
                (1 if is_admin else 0, str(username).strip()),
            )

    @staticmethod
    def _user_row(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["is_admin"] = bool(d.get("is_admin"))
        return d

    # ------------------------------------------------------------------
    # sessions（登录墙会话：token → user_id，滑动过期由 services/auth 驱动）
    # ------------------------------------------------------------------
    def create_session(self, user_id: int, token: str, expires_at: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (token, user_id, created_at, expires_at)"
                " VALUES (?, ?, datetime('now','localtime'), ?)",
                (str(token), int(user_id), _dt_str(expires_at)),
            )

    def get_session_user(self, token: str) -> dict | None:
        """按 token 查 session 并联查用户。返回 {session_expires_at, user} 或 None；
        不过滤过期——是否过期/是否删除由 services/auth 决定。"""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT s.expires_at AS session_expires_at, u.*
                   FROM sessions s JOIN users u ON u.id = s.user_id
                   WHERE s.token = ?""",
                (str(token),),
            ).fetchone()
        if row is None:
            return None
        user = self._user_row(row)
        expires_at = user.pop("session_expires_at")
        return {"session_expires_at": expires_at, "user": user}

    def touch_session(self, token: str, expires_at: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET expires_at = ? WHERE token = ?",
                (_dt_str(expires_at), str(token)),
            )

    def delete_session(self, token: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (str(token),))

    def delete_expired_sessions(self, now: datetime) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM sessions WHERE expires_at < ?", (_dt_str(now),)
            )
            return int(cur.rowcount or 0)

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
                """INSERT INTO manual_trades
                   (user_id, symbol, buy_date, buy_price, shares, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?,
                    datetime('now','localtime'), datetime('now','localtime'))""",
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
                       updated_at = datetime('now','localtime')
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
                """INSERT INTO job_runs (job_type, run_date, status, payload, created_at)
                   VALUES (?, ?, ?, ?, datetime('now','localtime'))""",
                (
                    str(job_type),
                    run_date,
                    status or str(payload.get("status", "")),
                    json.dumps(payload, ensure_ascii=False, default=str),
                ),
            )
            return int(cursor.lastrowid or 0)

    def mark_interrupted_job_runs(self, job_types: list[str]) -> int:
        """启动清扫（P2-9）：把状态停在 running 且无配对终态行的 job_runs
        标记为 interrupted。

        配对规则：同 job_type 且 payload 含相同 job_id 的非 running 行存在
        即视为已善终（进程内完成了终态落库）；否则该行是进程重启的孤儿。
        """
        if not job_types:
            return 0
        placeholders = ",".join("?" for _ in job_types)
        interrupted = 0
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id, job_type, payload FROM job_runs WHERE status = 'running' AND job_type IN ({placeholders})",
                tuple(job_types),
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload"] or "{}")
                job_id = str(payload.get("job_id") or "")
                if job_id:
                    terminal = conn.execute(
                        f"""SELECT COUNT(*) AS c FROM job_runs
                           WHERE job_type IN ({placeholders}) AND status != 'running'
                           AND payload LIKE ?""",
                        (*job_types, f'%"{job_id}"%'),
                    ).fetchone()
                    if terminal["c"]:
                        continue
                conn.execute("UPDATE job_runs SET status = 'interrupted' WHERE id = ?", (row["id"],))
                interrupted += 1
        return interrupted

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
                   VALUES (?, ?, datetime('now','localtime'))
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
                   VALUES (?, ?, ?, ?, datetime('now','localtime'))
                   ON CONFLICT(param_set) DO UPDATE SET
                       params_json = excluded.params_json,
                       is_default = excluded.is_default,
                       formula_version = excluded.formula_version""",
                (param_set, params_json, 1 if is_default else 0, int(formula_version)),
            )

    def save_indicator_daily(
        self, symbol: str, df, formula_version: int, price_mode: str = "qfq", data_version: int = 0
    ) -> int:
        """Replace one symbol's cached indicator rows (full-symbol rebuild).

        ``data_version`` 记录构建时的行情内容版本（data_versions），
        供 ``indicator_cache_info`` / ``_cache_fresh`` 识别价格口径漂移。
        """
        if df.empty:
            return 0

        def col(name: str) -> list:
            return [None if pd.isna(v) else float(v) for v in df[name].tolist()] if name in df.columns else [None] * len(df)

        times = [str(t) for t in df["time"].tolist()]
        columns = (
            "atr", "vol_ma20", "er10",
            "sma5", "sma10", "sma20", "sma60", "sma120", "sma200",
            "ema_s", "ema_m", "ema_l", "rsi14",
            "macd_dif", "macd_dea", "macd_hist",
            "boll_mid", "boll_up", "boll_dn",
            "rsi_avg_gain", "rsi_avg_loss", "macd_ema12", "macd_ema26",
        )
        values = [col(name) for name in columns]
        records = [
            (symbol, times[i], *row_vals, price_mode, int(formula_version), int(data_version))
            for i, row_vals in enumerate(zip(*values))
        ]
        with self._connect() as conn:
            conn.execute("DELETE FROM indicator_daily WHERE symbol = ?", (symbol,))
            conn.executemany(
                """INSERT INTO indicator_daily
                   (symbol, time, atr, vol_ma20, er10,
                    sma5, sma10, sma20, sma60, sma120, sma200,
                    ema_s, ema_m, ema_l, rsi14,
                    macd_dif, macd_dea, macd_hist,
                    boll_mid, boll_up, boll_dn,
                    rsi_avg_gain, rsi_avg_loss, macd_ema12, macd_ema26,
                    price_mode, formula_version, data_version, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))""",
                records,
            )
        return len(records)

    def load_indicator_daily(self, symbol: str):

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM indicator_daily WHERE symbol = ? ORDER BY time", (symbol,)
            ).fetchall()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame([dict(r) for r in rows])

    def save_trend_daily(
        self, symbol: str, df, formula_version: int, param_set: str = "default",
        price_mode: str = "qfq", data_version: int = 0
    ) -> int:
        if df.empty:
            return 0

        def col(name: str) -> list:
            return [None if pd.isna(v) else float(v) for v in df[name].tolist()] if name in df.columns else [None] * len(df)

        times = [str(t) for t in df["time"].tolist()]
        columns = ("trend_score", "trend_ma5", "trend_ma10", "price_direction", "confidence")
        values = [col(name) for name in columns]
        records = [
            (symbol, times[i], param_set, *row_vals, price_mode, int(formula_version), int(data_version))
            for i, row_vals in enumerate(zip(*values))
        ]
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM trend_daily WHERE symbol = ? AND param_set = ?", (symbol, param_set)
            )
            conn.executemany(
                """INSERT INTO trend_daily
                   (symbol, time, param_set, trend_score, trend_ma5, trend_ma10,
                    price_direction, confidence, price_mode, formula_version, data_version, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))""",
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
                "SELECT COUNT(*) AS n, MAX(time) AS last, MAX(formula_version) AS ver, MAX(data_version) AS dv FROM indicator_daily WHERE symbol = ?",
                (symbol,),
            ).fetchone()
            trend = conn.execute(
                "SELECT COUNT(*) AS n, MAX(time) AS last, MAX(formula_version) AS ver, MAX(data_version) AS dv FROM trend_daily WHERE symbol = ? AND param_set = 'default'",
                (symbol,),
            ).fetchone()
        return {
            "indicator_rows": int(ind["n"] or 0),
            "indicator_last": ind["last"],
            "indicator_version": ind["ver"],
            "indicator_data_version": int(ind["dv"] or 0),
            "trend_rows": int(trend["n"] or 0),
            "trend_last": trend["last"],
            "trend_version": trend["ver"],
            "trend_data_version": int(trend["dv"] or 0),
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

    # ------------------------------------------------------------------
    # batch backtest (批量回测)
    # ------------------------------------------------------------------
    def create_batch_run_if_idle(self, batch: dict) -> bool:
        """Insert a new batch run only when no other run is ``running``.

        The check-and-insert is wrapped in BEGIN IMMEDIATE so two concurrent
        POST /run requests cannot both pass the idle check (409 race).
        Returns True when the batch was created.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT batch_id FROM batch_backtest_runs WHERE status = 'running' LIMIT 1"
            ).fetchone()
            if row:
                return False
            conn.execute(
                """INSERT INTO batch_backtest_runs
                   (batch_id, name, status, categories_json, strategy_snapshot_json,
                    config_json, total_cells, data_anchor_date, data_version, engine_version,
                    created_at)
                   VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))""",
                (
                    batch["batch_id"],
                    batch.get("name", ""),
                    batch.get("categories_json", "[]"),
                    batch.get("strategy_snapshot_json", "[]"),
                    batch.get("config_json", "{}"),
                    int(batch.get("total_cells", 0)),
                    batch.get("data_anchor_date"),
                    batch.get("data_version"),
                    batch.get("engine_version", "1.0"),
                ),
            )
            return True

    def update_batch_run(self, batch_id: str, **fields: Any) -> None:
        """Generic field update for batch progress / status transitions."""
        allowed = {
            "name", "status", "total_cells", "done_cells", "ok_cells",
            "failed_cells", "skipped_cells", "current_symbol", "finished_at", "error",
        }
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        clause = ", ".join(f"{k} = ?" for k in sets)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE batch_backtest_runs SET {clause} WHERE batch_id = ?",
                (*sets.values(), batch_id),
            )

    def get_batch_run(self, batch_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM batch_backtest_runs WHERE batch_id = ?", (batch_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_running_batch_run(self) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM batch_backtest_runs WHERE status = 'running' LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def list_batch_runs(self, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM batch_backtest_runs ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_interrupted_batch_runs(self) -> int:
        """Startup cleanup: daemon worker threads die with the process, so any
        batch still 'running' at boot is an orphan — mark it interrupted."""
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE batch_backtest_runs
                   SET status = 'interrupted', error = '服务重启导致批次中断',
                       finished_at = datetime('now','localtime')
                   WHERE status = 'running'"""
            )
            return cur.rowcount

    def insert_batch_cell(self, cell: dict) -> None:
        """Insert one result cell. Called per cell (per-cell commit) so a
        crash mid-batch never loses already-computed cells."""
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO batch_backtest_cells
                   (batch_id, symbol, strategy_id, symbol_name, strategy_name,
                    category_l1, category_l2, category_l3, asset_type,
                    status, error, start_date, end_date, bar_count, partial_window,
                    total_return, annual_return, max_drawdown, sharpe, sortino, calmar,
                    win_rate, profit_factor, trade_count, avg_holding_days, avg_flat_days,
                    final_equity,
                    benchmark_total_return, benchmark_annual_return,
                    benchmark_sharpe, benchmark_calmar,
                    excess_annual_return, excess_sharpe, excess_calmar,
                    annual_returns_json, monthly_heatmap_json, trades_json,
                    skipped_buys_json, monthly_nav_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))""",
                (
                    cell["batch_id"], cell["symbol"], cell["strategy_id"],
                    cell.get("symbol_name"), cell.get("strategy_name"),
                    cell.get("category_l1"), cell.get("category_l2"), cell.get("category_l3"),
                    cell.get("asset_type"), cell.get("status"), cell.get("error"),
                    cell.get("start_date"), cell.get("end_date"), cell.get("bar_count"),
                    int(cell.get("partial_window", 0) or 0),
                    cell.get("total_return"), cell.get("annual_return"), cell.get("max_drawdown"),
                    cell.get("sharpe"), cell.get("sortino"), cell.get("calmar"),
                    cell.get("win_rate"), cell.get("profit_factor"), cell.get("trade_count"),
                    cell.get("avg_holding_days"), cell.get("avg_flat_days"),
                    cell.get("final_equity"),
                    cell.get("benchmark_total_return"), cell.get("benchmark_annual_return"),
                    cell.get("benchmark_sharpe"), cell.get("benchmark_calmar"),
                    cell.get("excess_annual_return"), cell.get("excess_sharpe"),
                    cell.get("excess_calmar"),
                    cell.get("annual_returns_json"), cell.get("monthly_heatmap_json"),
                    cell.get("trades_json"), cell.get("skipped_buys_json"),
                    cell.get("monthly_nav_json"),
                ),
            )

    _CELL_METRIC_COLUMNS = (
        "c.batch_id, c.symbol, c.strategy_id, c.symbol_name, c.strategy_name,"
        " c.category_l1, c.category_l2, c.category_l3, c.asset_type,"
        " c.status, c.error, c.start_date, c.end_date, c.bar_count, c.partial_window,"
        " c.total_return, c.annual_return, c.max_drawdown, c.sharpe, c.sortino, c.calmar,"
        " c.win_rate, c.profit_factor, c.trade_count, c.avg_holding_days, c.avg_flat_days,"
        " c.final_equity,"
        " c.benchmark_total_return, c.benchmark_annual_return,"
        " c.benchmark_sharpe, c.benchmark_calmar,"
        " c.excess_annual_return, c.excess_sharpe, c.excess_calmar"
    )

    def get_batch_cells(self, batch_id: str) -> list[dict]:
        """All cells with metric columns (no blobs), LEFT JOIN symbol features
        (skipped symbols have no feature row — views must handle nulls)."""
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT {self._CELL_METRIC_COLUMNS},
                           f.ann_volatility, f.momentum_250, f.bh_max_drawdown,
                           f.trend_score_avg, f.amount_ma20
                    FROM batch_backtest_cells c
                    LEFT JOIN batch_backtest_symbol_features f
                      ON f.batch_id = c.batch_id AND f.symbol = c.symbol
                    WHERE c.batch_id = ?
                    ORDER BY c.symbol, c.strategy_id""",
                (batch_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_batch_cell_detail(self, batch_id: str, symbol: str, strategy_id: str) -> dict | None:
        """Single cell including JSON blobs (annual/monthly/trades/monthly_nav)."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM batch_backtest_cells
                   WHERE batch_id = ? AND symbol = ? AND strategy_id = ?""",
                (batch_id, symbol, strategy_id),
            ).fetchone()
        return dict(row) if row else None

    def get_batch_annual_blobs(self, batch_id: str) -> list[dict]:
        """ok 格子的年度收益 blob（策略×年份聚合用，列表端点不带 blob 故单列）。"""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT strategy_id, strategy_name, annual_returns_json
                   FROM batch_backtest_cells
                   WHERE batch_id = ? AND status = 'ok' AND annual_returns_json IS NOT NULL""",
                (batch_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def insert_batch_symbol_features(self, batch_id: str, symbol: str, features: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO batch_backtest_symbol_features
                   (batch_id, symbol, ann_volatility, momentum_250, bh_max_drawdown,
                    trend_score_avg, amount_ma20, bar_count, created_at)
                   VALUES (?,?,?,?,?,?,?,?,datetime('now','localtime'))""",
                (
                    batch_id, symbol,
                    features.get("ann_volatility"), features.get("momentum_250"),
                    features.get("bh_max_drawdown"), features.get("trend_score_avg"),
                    features.get("amount_ma20"), features.get("bar_count"),
                ),
            )

    def delete_batch_run(self, batch_id: str) -> bool:
        """Cascade-delete run + cells + features. Refuses to delete a running
        batch (cancel first). Returns True when deleted."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM batch_backtest_runs WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if row is None or row["status"] == "running":
                return False
            conn.execute("DELETE FROM batch_backtest_cells WHERE batch_id = ?", (batch_id,))
            conn.execute(
                "DELETE FROM batch_backtest_symbol_features WHERE batch_id = ?", (batch_id,)
            )
            conn.execute("DELETE FROM batch_backtest_runs WHERE batch_id = ?", (batch_id,))
            return True

    def get_market_data_anchor(self) -> dict:
        """Batch anchor: latest bar date + data version of the qfq table."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(time) AS anchor_date, MAX(updated_at) AS data_version"
                " FROM market_data_qfq"
            ).fetchone()
        return {
            "anchor_date": row["anchor_date"] if row else None,
            "data_version": row["data_version"] if row else None,
        }

    def count_bars_by_symbol(
        self,
        price_mode: str = "qfq",
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, int]:
        """Bar counts per symbol (single indexed GROUP BY) — batch ETA estimates.

        start/end 限定统计窗口（按交易日计数，供窗口批次 ETA 使用）。
        time 列为 'YYYY-MM-DD HH:MM:SS' 文本，end 补到当日末尾做闭区间比较。
        """
        table = self._market_table(price_mode)
        clauses: list[str] = []
        params: list[str] = []
        if start is not None:
            clauses.append("time >= ?")
            params.append(start.isoformat())
        if end is not None:
            clauses.append("time <= ?")
            params.append(end.isoformat() + " 23:59:59")
        sql = f"SELECT symbol, COUNT(*) AS n FROM {table}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " GROUP BY symbol"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return {r["symbol"]: int(r["n"]) for r in rows}


def init_db(db_path: str | Path = "data/trend_quant.db") -> Database:
    global _db_instance
    _db_instance = Database(db_path)
    return _db_instance


def reset_db_instance_for_tests() -> None:
    """还原进程级单例（P2-25 测试卫生）：直接 init_db() 的测试在 tearDown
    调用，避免临时库句柄泄漏到后续测试。"""
    global _db_instance
    _db_instance = None


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

        get_logger(__name__).warning("Failed to record job run: %s", job_type, exc_info=True)
