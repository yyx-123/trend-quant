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
from core.bars import normalize_period
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


# ---------------------------------------------------------------------------
# 投研基建新栈 DDL（2026-09-23 一期，docs/26-09-20-投研基建架构/）
#
# 全部为新增表/触发器，对存量表零改动。覆盖：
#   L1.5 gateway_audit（数据请求留痕）
#   L2   engine_*（运行/订单/成交/未成交/持仓快照/净值）
#   L3   portfolio_strategies + portfolio_strategy_versions（策略库，版本不可变）
#        portfolio_live_lists（实盘运行器每日清单）
#   L4   research_*（sessions/topics/experiments/runs/verdicts 台账五表 +
#        id_seq/holdout_tokens/module_drafts）
#
# append-only 的落实：内容字段 UPDATE/DELETE 被触发器拒绝；生命周期字段
# （status / final_verdict / reasoning 等）在白名单内允许更新，状态机合法性由
# research/lifecycle.py 代码层强制。设计出处：详设 §4.1/§5.5/§6.3/§6.6.7/§6.7。
# ---------------------------------------------------------------------------
_LIVE_LISTS_DDL = """CREATE TABLE IF NOT EXISTS portfolio_live_lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    list_date TEXT NOT NULL,              -- 决策日（交易日）
    strategy_version_id TEXT NOT NULL,
    user_id INTEGER NOT NULL DEFAULT 1,   -- 清单归属的操作者（逐用户隔离，见 R14B-F2）
    as_of TEXT NOT NULL,                  -- 取数时刻（如 2026-09-23 14:00:00）
    engine_run_id TEXT,
    target_json TEXT NOT NULL DEFAULT '{}',   -- 目标持仓 + 应买应卖 + 风控拦截说明
    reconcile_json TEXT,                      -- 次日对账结果
    reconciled_at TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(user_id, list_date, strategy_version_id)
);"""


_RESEARCH_STACK_DDL = """
-- ===== L1.5 数据门面层 =====
CREATE TABLE IF NOT EXISTS gateway_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_layer TEXT NOT NULL,          -- engine / portfolio / research / live
    run_id TEXT,
    method TEXT NOT NULL,                -- get_panel / get_tradability / metadata
    as_of TEXT NOT NULL,
    symbols_count INTEGER NOT NULL DEFAULT 0,
    date_start TEXT,
    date_end TEXT,
    fields TEXT,
    adjust TEXT,
    mode TEXT,
    data_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_gateway_audit_run ON gateway_audit(run_id, id);
CREATE INDEX IF NOT EXISTS idx_gateway_audit_asof ON gateway_audit(as_of, id);

-- ===== L2 交易引擎层（详设 §4.1） =====
CREATE TABLE IF NOT EXISTS engine_runs (
    run_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('backtest','live')),
    strategy_ref TEXT NOT NULL DEFAULT '',
    config_hash TEXT NOT NULL DEFAULT '',
    resolved_config_yaml TEXT NOT NULL DEFAULT '',
    run_params_json TEXT NOT NULL DEFAULT '{}',
    data_version INTEGER NOT NULL DEFAULT 0,
    engine_version TEXT NOT NULL DEFAULT '',
    git_hash TEXT NOT NULL DEFAULT '',
    started_at TEXT DEFAULT (datetime('now','localtime')),
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running' CHECK(status IN ('running','finished','failed')),
    error TEXT
);

-- engine_runs 白名单守卫：resolved_config_yaml 是
-- §5.6 可复现性的落库锚点，与 research 栈证据表同制——内容字段禁改，
-- 可改仅 status/finished_at/error（finish_run/lifecycle 收口所需）。
-- DROP+CREATE 使定义修订传播到存量库（同 is_reproduction 整改注记）。
DROP TRIGGER IF EXISTS trg_engine_runs_guard_update;
CREATE TRIGGER trg_engine_runs_guard_update
BEFORE UPDATE ON engine_runs
WHEN OLD.run_id <> NEW.run_id
  OR OLD.kind <> NEW.kind
  OR OLD.strategy_ref <> NEW.strategy_ref
  OR OLD.config_hash <> NEW.config_hash
  OR OLD.resolved_config_yaml <> NEW.resolved_config_yaml
  OR OLD.run_params_json <> NEW.run_params_json
  OR OLD.data_version <> NEW.data_version
  OR OLD.engine_version <> NEW.engine_version
  OR OLD.git_hash <> NEW.git_hash
  OR OLD.started_at <> NEW.started_at
BEGIN SELECT RAISE(ABORT, 'engine_runs content is append-only'); END;

-- engine_runs 禁删守卫：run 头里的 config_hash/data_version/git_hash/
-- resolved_config_yaml 是 §5.6 可复现性锚点，子证据表（orders/fills/nav/…）
-- 已各有 no_delete 守卫，唯独 run 头此前只挡 UPDATE——一条 DELETE 即可抹掉
-- 该 run 的口径来源而证据行仍在，事后无法复核（R11B 独立审计实测：
-- DELETE 成功，而其余 13 张兄弟表全部 ABORT）。与兄弟表同制补上。
DROP TRIGGER IF EXISTS trg_engine_runs_no_delete;
CREATE TRIGGER trg_engine_runs_no_delete
BEFORE DELETE ON engine_runs
BEGIN SELECT RAISE(ABORT, 'engine_runs is append-only'); END;

CREATE INDEX IF NOT EXISTS idx_engine_runs_status ON engine_runs(status, started_at);

CREATE TABLE IF NOT EXISTS engine_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES engine_runs(run_id),
    order_id TEXT NOT NULL,
    decision_date TEXT NOT NULL,
    target_fill_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('buy','sell')),
    order_type TEXT NOT NULL DEFAULT 'tail_market',
    intent_type TEXT NOT NULL,
    intent_value REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'signal',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','filled','rejected','unfilled')),
    created_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(run_id, order_id)
);
CREATE INDEX IF NOT EXISTS idx_engine_orders_run ON engine_orders(run_id, id);

CREATE TABLE IF NOT EXISTS engine_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES engine_runs(run_id),
    order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    fill_date TEXT NOT NULL,
    base_price REAL NOT NULL,
    slippage_base REAL NOT NULL DEFAULT 0,
    slippage_tail REAL NOT NULL DEFAULT 0,
    fill_price REAL NOT NULL,
    quantity INTEGER NOT NULL,
    commission REAL NOT NULL DEFAULT 0,
    stamp_tax REAL NOT NULL DEFAULT 0,
    fee_total REAL NOT NULL DEFAULT 0,
    cash_after REAL NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_engine_fills_run ON engine_fills(run_id, id);

CREATE TABLE IF NOT EXISTS engine_unfilled (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES engine_runs(run_id),
    order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    decision_date TEXT NOT NULL,
    reason TEXT NOT NULL CHECK(reason IN
        ('limit_up','limit_down','suspended','t1_block','insufficient_cash','lot_rounding')),
    intent_snapshot_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_engine_unfilled_run ON engine_unfilled(run_id, id);

CREATE TABLE IF NOT EXISTS engine_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES engine_runs(run_id),
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    sellable_quantity INTEGER NOT NULL,
    avg_cost REAL NOT NULL DEFAULT 0,
    entry_date TEXT,
    entry_price REAL NOT NULL DEFAULT 0,
    stop_price REAL,
    highest_since_buy REAL NOT NULL DEFAULT 0,
    atr_at_entry REAL NOT NULL DEFAULT 0,
    module_state_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_engine_positions_unique
    ON engine_positions(run_id, date, symbol);

CREATE TABLE IF NOT EXISTS engine_daily_nav (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES engine_runs(run_id),
    date TEXT NOT NULL,
    cash REAL NOT NULL DEFAULT 0,
    positions_value REAL NOT NULL DEFAULT 0,
    equity REAL NOT NULL DEFAULT 0,
    heat REAL,
    exposure REAL NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(run_id, date)
);


-- ===== L3 组合策略层（详设 §5.3/§5.5） =====
-- engine 子证据表守卫：判定所依赖的逐笔成交/净值/
-- 持仓此前**完全不受 append-only 保护**（`UPDATE engine_daily_nav SET equity=...`
-- 与 `DELETE FROM engine_fills` 都被允许），与 engine_runs 同制补上：
-- 内容字段禁改（UPDATE 一律拒），行一律禁删。启用需要重写子表列的场景由
-- 新的 run 承担（幂等重跑本就是 run 级隔离）。
-- 注：与 engine_runs 一致采用 DROP+CREATE，使定义修订传播到存量库。
DROP TRIGGER IF EXISTS trg_engine_orders_no_delete;
CREATE TRIGGER trg_engine_orders_no_delete
BEFORE DELETE ON engine_orders
BEGIN SELECT RAISE(ABORT, 'engine_orders is append-only'); END;
DROP TRIGGER IF EXISTS trg_engine_orders_no_update;
CREATE TRIGGER trg_engine_orders_no_update
BEFORE UPDATE ON engine_orders
BEGIN SELECT RAISE(ABORT, 'engine_orders is append-only'); END;
DROP TRIGGER IF EXISTS trg_engine_fills_no_delete;
CREATE TRIGGER trg_engine_fills_no_delete
BEFORE DELETE ON engine_fills
BEGIN SELECT RAISE(ABORT, 'engine_fills is append-only'); END;
DROP TRIGGER IF EXISTS trg_engine_fills_no_update;
CREATE TRIGGER trg_engine_fills_no_update
BEFORE UPDATE ON engine_fills
BEGIN SELECT RAISE(ABORT, 'engine_fills is append-only'); END;
DROP TRIGGER IF EXISTS trg_engine_unfilled_no_delete;
CREATE TRIGGER trg_engine_unfilled_no_delete
BEFORE DELETE ON engine_unfilled
BEGIN SELECT RAISE(ABORT, 'engine_unfilled is append-only'); END;
DROP TRIGGER IF EXISTS trg_engine_unfilled_no_update;
CREATE TRIGGER trg_engine_unfilled_no_update
BEFORE UPDATE ON engine_unfilled
BEGIN SELECT RAISE(ABORT, 'engine_unfilled is append-only'); END;
DROP TRIGGER IF EXISTS trg_engine_positions_no_delete;
CREATE TRIGGER trg_engine_positions_no_delete
BEFORE DELETE ON engine_positions
BEGIN SELECT RAISE(ABORT, 'engine_positions is append-only'); END;
DROP TRIGGER IF EXISTS trg_engine_positions_no_update;
CREATE TRIGGER trg_engine_positions_no_update
BEFORE UPDATE ON engine_positions
BEGIN SELECT RAISE(ABORT, 'engine_positions is append-only'); END;
DROP TRIGGER IF EXISTS trg_engine_daily_nav_no_delete;
CREATE TRIGGER trg_engine_daily_nav_no_delete
BEFORE DELETE ON engine_daily_nav
BEGIN SELECT RAISE(ABORT, 'engine_daily_nav is append-only'); END;
DROP TRIGGER IF EXISTS trg_engine_daily_nav_no_update;
CREATE TRIGGER trg_engine_daily_nav_no_update
BEFORE UPDATE ON engine_daily_nav
BEGIN SELECT RAISE(ABORT, 'engine_daily_nav is append-only'); END;

CREATE TABLE IF NOT EXISTS portfolio_strategies (
    id TEXT PRIMARY KEY,                  -- 策略线 id（如 base-v1）
    name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    is_benchmark INTEGER NOT NULL DEFAULT 0,
    is_blank_base INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL DEFAULT 'human',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    retired_at TEXT                       -- 软删除标记，不影响已被引用的运行
);

CREATE TABLE IF NOT EXISTS portfolio_strategy_versions (
    id TEXT PRIMARY KEY,                  -- <strategy_id>@<version>
    strategy_id TEXT NOT NULL REFERENCES portfolio_strategies(id),
    version INTEGER NOT NULL,
    config_yaml TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    parent_version_id TEXT,
    experiment_id TEXT,                   -- 产出它的实验（benchmark/空白基准为 NULL）
    created_by TEXT NOT NULL DEFAULT 'human',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(strategy_id, version),
    UNIQUE(config_hash)
);

-- 实盘运行器每日清单（详设 §5.7）：一次运行一行，清单/对账结果存 payload。
__LIVE_LISTS_DDL__

-- ===== L4 投研层（详设 §6.3/§6.6.7） =====
CREATE TABLE IF NOT EXISTS research_sessions (
    session_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('human','ai')),
    label TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT 'api',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS research_id_seq (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS research_topics (
    id TEXT PRIMARY KEY,                  -- T + 序号
    title TEXT NOT NULL,
    question TEXT NOT NULL,
    owner_session TEXT NOT NULL REFERENCES research_sessions(session_id),
    created_by TEXT NOT NULL CHECK(created_by IN ('human','ai')),
    created_at TEXT DEFAULT (datetime('now','localtime')),
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','concluded')),
    conclusion TEXT,
    conclusion_grade TEXT,                -- supported/refuted/mixed/insufficient-evidence（阶段 5）
    conclusion_summary_json TEXT,         -- 平台量化摘要（决策 20，阶段 5）
    concluded_at TEXT
);

CREATE TABLE IF NOT EXISTS research_experiments (
    id TEXT PRIMARY KEY,                  -- E + 序号
    title TEXT NOT NULL,
    owner_session TEXT NOT NULL REFERENCES research_sessions(session_id),
    created_by TEXT NOT NULL CHECK(created_by IN ('human','ai')),
    created_at TEXT DEFAULT (datetime('now','localtime')),
    topic_id TEXT NOT NULL REFERENCES research_topics(id),
    evaluation_module TEXT NOT NULL,      -- name@version
    subject_key TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    parent_experiment_id TEXT,
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK(status IN ('proposed','rejected_intake','queued','running','evaluating','verdicted','failed')),
    attempt_index INTEGER NOT NULL DEFAULT 1,
    reject_reason TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    holdout_touched INTEGER NOT NULL DEFAULT 0,
    is_reproduction INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_research_experiments_topic ON research_experiments(topic_id, id);
CREATE INDEX IF NOT EXISTS idx_research_experiments_subject ON research_experiments(subject_key, id);
CREATE INDEX IF NOT EXISTS idx_research_experiments_status ON research_experiments(status, created_at);

CREATE TABLE IF NOT EXISTS research_runs (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
    engine_run_id TEXT,
    window_start TEXT,
    window_end TEXT,
    window_kind TEXT NOT NULL DEFAULT 'sample'
        CHECK(window_kind IN ('sample','holdout','plateau_probe')),
    holdout_touched INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_research_runs_experiment ON research_runs(experiment_id, id);

CREATE TABLE IF NOT EXISTS research_verdicts (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
    generated_at TEXT DEFAULT (datetime('now','localtime')),
    baseline_json TEXT NOT NULL DEFAULT '{}',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    report_json TEXT NOT NULL DEFAULT '{}',
    suggested_verdict TEXT NOT NULL CHECK(suggested_verdict IN ('confirmed','rejected','inconclusive')),
    final_verdict TEXT CHECK(final_verdict IN ('confirmed','rejected','inconclusive')),
    reasoning TEXT,
    confirmed_by TEXT,
    confirmed_at TEXT,
    holdout_touched INTEGER NOT NULL DEFAULT 0,
    attempt_index_snapshot INTEGER NOT NULL DEFAULT 1,
    supersedes TEXT                       -- 复核产物指向被复核 verdict 的 experiment_id（详设 §6.6.7）
);
CREATE INDEX IF NOT EXISTS idx_research_verdicts_experiment ON research_verdicts(experiment_id, id);

CREATE TABLE IF NOT EXISTS holdout_tokens (
    id TEXT PRIMARY KEY,
    granted_by TEXT NOT NULL,             -- human session id
    purpose TEXT NOT NULL DEFAULT '',
    experiment_id TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    consumed_at TEXT
);

CREATE TABLE IF NOT EXISTS module_drafts (
    id TEXT PRIMARY KEY,                  -- M + 序号
    slot TEXT NOT NULL,
    name TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    kind TEXT NOT NULL CHECK(kind IN ('dsl','python')),
    source TEXT NOT NULL,
    params_schema_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK(status IN ('draft','reviewed','rejected','retired')),
    test_report_json TEXT NOT NULL DEFAULT '{}',
    reject_reason TEXT,
    created_by TEXT NOT NULL DEFAULT 'ai',
    owner_session TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    reviewed_at TEXT,
    UNIQUE(name, version)
);

-- ===== append-only 触发器（列白名单制；设计：详设 §6.7） =====
-- experiments：内容字段禁改；可改 = status/reject_reason/archived/holdout_touched/
--              started_at/finished_at/error/topic_id（append_experiment_to_topic）
-- 注意：守卫触发器定义若有修订，必须 DROP IF EXISTS + CREATE——
-- SQLite 的 CREATE TRIGGER IF NOT EXISTS 不会把新定义传播到已存在同名触发器
-- 的存量库。is_reproduction（仅 INSERT 写入的复现标记，直改它会增减研究线
-- 尝试计数 = 篡改 DSR 输入）由此并入白名单守卫。
DROP TRIGGER IF EXISTS trg_research_experiments_guard_update;
CREATE TRIGGER IF NOT EXISTS trg_research_experiments_no_delete
BEFORE DELETE ON research_experiments
BEGIN SELECT RAISE(ABORT, 'research_experiments is append-only'); END;
CREATE TRIGGER trg_research_experiments_guard_update
BEFORE UPDATE ON research_experiments
WHEN OLD.id <> NEW.id
  OR OLD.title <> NEW.title
  OR OLD.owner_session <> NEW.owner_session
  OR OLD.created_by <> NEW.created_by
  OR OLD.created_at <> NEW.created_at
  OR OLD.evaluation_module <> NEW.evaluation_module
  OR OLD.subject_key <> NEW.subject_key
  OR OLD.spec_json <> NEW.spec_json
  OR OLD.hypothesis <> NEW.hypothesis
  OR IFNULL(OLD.parent_experiment_id, '') <> IFNULL(NEW.parent_experiment_id, '')
  OR OLD.attempt_index <> NEW.attempt_index
  OR OLD.is_reproduction <> NEW.is_reproduction
BEGIN SELECT RAISE(ABORT, 'research_experiments content is append-only'); END;

-- runs：完全 insert-only
CREATE TRIGGER IF NOT EXISTS trg_research_runs_no_delete
BEFORE DELETE ON research_runs
BEGIN SELECT RAISE(ABORT, 'research_runs is append-only'); END;
DROP TRIGGER IF EXISTS trg_research_runs_no_update;
CREATE TRIGGER trg_research_runs_no_update
BEFORE UPDATE ON research_runs
BEGIN SELECT RAISE(ABORT, 'research_runs is append-only'); END;

-- verdicts：可改 = final_verdict/reasoning/confirmed_by/confirmed_at
CREATE TRIGGER IF NOT EXISTS trg_research_verdicts_no_delete
BEFORE DELETE ON research_verdicts
BEGIN SELECT RAISE(ABORT, 'research_verdicts is append-only'); END;
DROP TRIGGER IF EXISTS trg_research_verdicts_guard_update;
CREATE TRIGGER trg_research_verdicts_guard_update
BEFORE UPDATE ON research_verdicts
WHEN OLD.id <> NEW.id
  OR OLD.experiment_id <> NEW.experiment_id
  OR OLD.generated_at <> NEW.generated_at
  OR OLD.baseline_json <> NEW.baseline_json
  OR OLD.evidence_json <> NEW.evidence_json
  OR OLD.warnings_json <> NEW.warnings_json
  OR OLD.report_json <> NEW.report_json
  OR OLD.suggested_verdict <> NEW.suggested_verdict
  OR OLD.holdout_touched <> NEW.holdout_touched
  OR OLD.attempt_index_snapshot <> NEW.attempt_index_snapshot
  OR IFNULL(OLD.supersedes, '') <> IFNULL(NEW.supersedes, '')
  -- 已落定的 final_verdict 不可改写（评审 DS-P3-1：历史永不改写——库层守卫）
  OR (OLD.final_verdict IS NOT NULL AND OLD.final_verdict <> NEW.final_verdict)
BEGIN SELECT RAISE(ABORT, 'research_verdicts content is append-only'); END;

-- topics：可改 = status/conclusion/conclusion_grade/conclusion_summary_json/concluded_at
CREATE TRIGGER IF NOT EXISTS trg_research_topics_no_delete
BEFORE DELETE ON research_topics
BEGIN SELECT RAISE(ABORT, 'research_topics is append-only'); END;
DROP TRIGGER IF EXISTS trg_research_topics_guard_update;
CREATE TRIGGER trg_research_topics_guard_update
BEFORE UPDATE ON research_topics
WHEN OLD.id <> NEW.id
  OR OLD.title <> NEW.title
  OR OLD.question <> NEW.question
  OR OLD.owner_session <> NEW.owner_session
  OR OLD.created_by <> NEW.created_by
  OR OLD.created_at <> NEW.created_at
BEGIN SELECT RAISE(ABORT, 'research_topics content is append-only'); END;

-- sessions：可改 = label
CREATE TRIGGER IF NOT EXISTS trg_research_sessions_no_delete
BEFORE DELETE ON research_sessions
BEGIN SELECT RAISE(ABORT, 'research_sessions is append-only'); END;
DROP TRIGGER IF EXISTS trg_research_sessions_guard_update;
CREATE TRIGGER trg_research_sessions_guard_update
BEFORE UPDATE ON research_sessions
WHEN OLD.session_id <> NEW.session_id
  OR OLD.kind <> NEW.kind
  OR OLD.channel <> NEW.channel
  OR OLD.created_at <> NEW.created_at
BEGIN SELECT RAISE(ABORT, 'research_sessions content is append-only'); END;

-- 策略版本不可变（详设 §5.5：改一个字就是新版本；软删除走 strategies.retired_at）
CREATE TRIGGER IF NOT EXISTS trg_portfolio_strategy_versions_no_delete
BEFORE DELETE ON portfolio_strategy_versions
BEGIN SELECT RAISE(ABORT, 'portfolio_strategy_versions is append-only'); END;
DROP TRIGGER IF EXISTS trg_portfolio_strategy_versions_no_update;
CREATE TRIGGER trg_portfolio_strategy_versions_no_update
BEFORE UPDATE ON portfolio_strategy_versions
BEGIN SELECT RAISE(ABORT, 'portfolio_strategy_versions is immutable'); END;

CREATE TRIGGER IF NOT EXISTS trg_portfolio_strategies_no_delete
BEFORE DELETE ON portfolio_strategies
BEGIN SELECT RAISE(ABORT, 'portfolio_strategies is append-only'); END;
DROP TRIGGER IF EXISTS trg_portfolio_strategies_guard_update;
CREATE TRIGGER trg_portfolio_strategies_guard_update
BEFORE UPDATE ON portfolio_strategies
WHEN OLD.id <> NEW.id
  OR OLD.is_benchmark <> NEW.is_benchmark
  OR OLD.is_blank_base <> NEW.is_blank_base
  OR OLD.created_by <> NEW.created_by
  OR OLD.created_at <> NEW.created_at
BEGIN SELECT RAISE(ABORT, 'portfolio_strategies content is append-only'); END;

-- holdout tokens：可改 = consumed_at
CREATE TRIGGER IF NOT EXISTS trg_holdout_tokens_no_delete
BEFORE DELETE ON holdout_tokens
BEGIN SELECT RAISE(ABORT, 'holdout_tokens is append-only'); END;
DROP TRIGGER IF EXISTS trg_holdout_tokens_guard_update;
CREATE TRIGGER trg_holdout_tokens_guard_update
BEFORE UPDATE ON holdout_tokens
WHEN OLD.id <> NEW.id
  OR OLD.granted_by <> NEW.granted_by
  OR OLD.purpose <> NEW.purpose
  OR IFNULL(OLD.experiment_id, '') <> IFNULL(NEW.experiment_id, '')
  OR OLD.created_at <> NEW.created_at
BEGIN SELECT RAISE(ABORT, 'holdout_tokens content is append-only'); END;

-- module_drafts：可改 = status/test_report_json/reject_reason/reviewed_at
CREATE TRIGGER IF NOT EXISTS trg_module_drafts_no_delete
BEFORE DELETE ON module_drafts
BEGIN SELECT RAISE(ABORT, 'module_drafts is append-only'); END;
DROP TRIGGER IF EXISTS trg_module_drafts_guard_update;
CREATE TRIGGER trg_module_drafts_guard_update
BEFORE UPDATE ON module_drafts
WHEN OLD.id <> NEW.id
  OR OLD.slot <> NEW.slot
  OR OLD.name <> NEW.name
  OR OLD.version <> NEW.version
  OR OLD.kind <> NEW.kind
  OR OLD.source <> NEW.source
  OR OLD.params_schema_json <> NEW.params_schema_json
  OR OLD.created_by <> NEW.created_by
  OR IFNULL(OLD.owner_session, '') <> IFNULL(NEW.owner_session, '')
  OR OLD.created_at <> NEW.created_at
BEGIN SELECT RAISE(ABORT, 'module_drafts content is append-only'); END;
"""
_RESEARCH_STACK_DDL = _RESEARCH_STACK_DDL.replace("__LIVE_LISTS_DDL__", _LIVE_LISTS_DDL)


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

    @contextmanager
    def connect(self):
        """公开的连接入口（`_connect` 的别名）。

        投研基建新栈（engine/portfolio/research）的 store 模块经此取连接——
        新 SQL 不再写入 db.py（God class 只维持存量与 DDL，访问逻辑归各层）。
        """
        with self._connect() as conn:
            yield conn

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
                -- 周K/月K（2026-09-12）：结构与日K逐列一致，time 为周期内最后
                -- 一个交易日（vendor 口径）。只存已收盘周期（未走完的当期 bar
                -- 不落库，见 core/bars.closed_bars），故非复权表是追加型的。
                -- 与日K的差别在复权口径：周/月 bar 内可能横跨除权日，同一个
                -- bar 的 OHLC 里各日用的因子不同，无法由“raw 周/月 bar × 单一
                -- 因子”本地物化 —— 所以 weekly/monthly 的 qfq 表直接存 vendor
                -- 前复权结果，除权日变化时按标的整段重取（service 层负责）。
                CREATE TABLE IF NOT EXISTS market_data_raw_weekly (
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
                CREATE TABLE IF NOT EXISTS market_data_qfq_weekly (
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
                CREATE TABLE IF NOT EXISTS market_data_raw_monthly (
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
                CREATE TABLE IF NOT EXISTS market_data_qfq_monthly (
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
                -- 拟合周/月K（2026-09-13）：由 qfq 日K 派生的「每日在途 bar 快照」——
                -- time 为交易日，行内容是「截至当日收盘、当日所在周/月的在途 bar」
                -- （open=周期首日、high/low=累计极值、close=当日、volume/amount 累计），
                -- 供回测/研究按 (symbol, time) 取「当日真实可见」的周/月K，杜绝前视。
                -- 派生表而非真源：除权因子变化时随 qfq 日K 整段重建（service 层负责；
                -- 价格水位随新因子平移，收益与趋势值不受影响——与 qfq 日K 同一性质）。
                -- period_start = 该周期的日历起点（周一/月初），免消费方重算 ISO 周。
                CREATE TABLE IF NOT EXISTS market_data_qfq_weekly_fitted (
                    symbol TEXT NOT NULL,
                    time TEXT NOT NULL,
                    period_start TEXT,
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
                CREATE TABLE IF NOT EXISTS market_data_qfq_monthly_fitted (
                    symbol TEXT NOT NULL,
                    time TEXT NOT NULL,
                    period_start TEXT,
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
                -- 滚动周/月趋势值（2026-09-13）：由 qfq 日K 派生的每日滚动锚定
                -- 周/月趋势值（core.rolling_bars.rolling_period_trend_series；
                -- 周 D=5 ATR8/ER4/vol8，月 D=22 ATR6/ER3/vol6，回看 K=16 根 bar）。
                -- time 为交易日；w_trend/m_trend 预热期内为 NULL（双 NULL 行不落库）。
                -- 派生表而非真源：除权因子变化时随 qfq 日K 整段重建（service 层负责）。
                CREATE TABLE IF NOT EXISTS trend_rolling_daily (
                    symbol TEXT NOT NULL,
                    time TEXT NOT NULL,
                    w_trend REAL,
                    m_trend REAL,
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
                    e_bias20 REAL,
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
                    stop_profile TEXT NOT NULL DEFAULT 'default',
                    atr_basis TEXT NOT NULL DEFAULT '',
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
                    round_trips_json TEXT,
                    round_trips_source TEXT,
                    r_mean REAL,
                    r_p5 REAL,
                    r_p25 REAL,
                    r_p75 REAL,
                    r_p95 REAL,
                    r_skew REAL,
                    tail_ratio REAL,
                    exit_efficiency REAL,
                    max_losing_streak INTEGER,
                    cvar_5 REAL,
                    ulcer_index REAL,
                    max_dd_duration_days INTEGER,
                    stop_exit_ratio REAL,
                    chandelier_exit_ratio REAL,
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
            conn.executescript(_RESEARCH_STACK_DDL)

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
            # E-BIAS（均线偏离度·减法版）：ln(C) - EMA(ln C, 20)。
            "e_bias20": "REAL",
        }
        batch_cell_columns = {
            "avg_holding_days": "REAL",
            "partial_window": "INTEGER NOT NULL DEFAULT 0",
            "avg_flat_days": "REAL",
            "benchmark_sharpe": "REAL",
            "benchmark_calmar": "REAL",
            "excess_sharpe": "REAL",
            "excess_calmar": "REAL",
            # 止损宽度诊断（2026-08-30 方案 §2.3/§3.1）：round_trips_json NULL = 旧批次未回填
            "round_trips_json": "TEXT",
            "round_trips_source": "TEXT",
            "r_mean": "REAL",
            "r_p5": "REAL",
            "r_p25": "REAL",
            "r_p75": "REAL",
            "r_p95": "REAL",
            "r_skew": "REAL",
            "tail_ratio": "REAL",
            "exit_efficiency": "REAL",
            "max_losing_streak": "INTEGER",
            "cvar_5": "REAL",
            "ulcer_index": "REAL",
            "max_dd_duration_days": "INTEGER",
            "stop_exit_ratio": "REAL",
            "chandelier_exit_ratio": "REAL",
        }
        batch_run_columns = {
            "stop_profile": "TEXT NOT NULL DEFAULT 'default'",
            "atr_basis": "TEXT NOT NULL DEFAULT ''",
        }
        research_experiment_columns = {
            # 复现标记（评审 DS-P1-2）：rerun 的实验与原始实验同 attempt_index，
            # 且不计入后续尝试计数——否则复现会反噬 DSR 的试验次数输入。
            "is_reproduction": "INTEGER NOT NULL DEFAULT 0",
        }
        targets = {
            "instrument_metadata": metadata_columns,
            "indicator_daily": indicator_columns,
            "trend_daily": cache_columns,
            "batch_backtest_cells": batch_cell_columns,
            "batch_backtest_runs": batch_run_columns,
            "research_experiments": research_experiment_columns,
        }
        with self._connect() as conn:
            # N1（2026-08-25）：删除与 PRIMARY KEY (symbol,time) 完全同列的
            # 冗余索引——rowid 表上 PK 已自动建同列索引，这三个白白放大
            # 百万行表的每次写入。
            for redundant_index in (
                "idx_market_data_raw_symbol_time",
                "idx_market_data_qfq_symbol_time",
                "idx_ex_factors_symbol_time",
                # engine 子表上两个与 UNIQUE 约束
                # 完全同列的冗余索引——持仓快照是引擎最大子表（满仓 800 标的
                # ×1250 日 ≈ 百万行），白放大每次写入。
                "idx_engine_positions_run",
                "idx_engine_daily_nav_run",
            ):
                conn.execute(f"DROP INDEX IF EXISTS {redundant_index}")
            # portfolio_live_lists 的 user 维度（R14B-F2）：旧定义的
            # UNIQUE(list_date, strategy_version_id) 无用户维度——两用户同策略同日
            # 会互相覆盖，且任何登录用户都能从台账页读到操作者的实盘计划。
            # SQLite 不能改约束，故"新建→搬运→替换"（表为 0 行时是空操作）。
            live_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table'"
                " AND name='portfolio_live_lists'"
            ).fetchone()
            if live_row is not None and "user_id" not in (live_row["sql"] or ""):
                _logger.warning(
                    "migrating portfolio_live_lists to per-user schema (rebuild)"
                )
                conn.execute(
                    "ALTER TABLE portfolio_live_lists RENAME TO portfolio_live_lists_old"
                )
                conn.executescript(_LIVE_LISTS_DDL)
                conn.execute(
                    "INSERT INTO portfolio_live_lists (list_date, strategy_version_id,"
                    " user_id, as_of, engine_run_id, target_json, reconcile_json,"
                    " reconciled_at, created_at)"
                    " SELECT list_date, strategy_version_id, 1, as_of, engine_run_id,"
                    " target_json, reconcile_json, reconciled_at, created_at"
                    " FROM portfolio_live_lists_old"
                )
                conn.execute("DROP TABLE portfolio_live_lists_old")

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
    def market_data_version_name(symbol: str, price_mode: str = "qfq", period: str = "1d") -> str:
        return f"{Database._market_table(price_mode, period)}:{symbol}"

    # ------------------------------------------------------------------
    # market_data
    # ------------------------------------------------------------------
    # (period, price_mode) → 表名。周期值经 core.bars.normalize_period
    # 规范化，故 'weekly'/'w'/'1w' 均落到同一张表；分钟线（'1m'）不在周期
    # 表内，误传即 ValueError，不会静默写成月K。
    _PERIOD_TABLE_SUFFIXES = {"1d": "", "1w": "_weekly", "1M": "_monthly"}

    @staticmethod
    def _market_table(price_mode: str = "qfq", period: str = "1d") -> str:
        value = str(price_mode or "qfq").strip().lower()
        if value in {"qfq", "forward", "forward_additive"}:
            base = "market_data_qfq"
        elif value in {"raw", "none", "unadjusted"}:
            base = "market_data_raw"
        else:
            raise ValueError(f"unsupported market data price_mode: {price_mode}")
        canonical = normalize_period(period)
        return f"{base}{Database._PERIOD_TABLE_SUFFIXES[canonical]}"

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

    def save_market_data(self, symbol: str, df, price_mode: str = "qfq", period: str = "1d") -> None:
        if df.empty:
            return
        table = self._market_table(price_mode, period)
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

    def save_market_data_many(
        self, items, price_mode: str = "qfq", period: str = "1d"
    ) -> dict[str, int]:
        """一次连接批量 upsert 多个标的：(symbol, df) 列表 → {symbol: 写入行数}。

        周/月K 日更要为 800+ 标的各写 raw+qfq 两张表 —— 逐标的调用
        save_market_data 每次新建连接 + 独立事务，一晚能吃掉几十分钟。这里
        合成一次连接 + 一个事务，与 load_market_data_many 的批量读思路对称。

        data_versions 仍按「每标的 +1」并「表级 +1」推进，口径与单标的写入
        完全一致（否则按标的失效的缓存会漏掉更新）。

        行情为空、或整段被防御性丢弃（非正价格）的标的既不写入、也不出现在
        返回 dict 中，调用方据此判断实际落库情况。
        """
        table = self._market_table(price_mode, period)
        records: list[tuple] = []
        written: dict[str, int] = {}
        for symbol, df in items or []:
            if df is None or df.empty:
                continue
            symbol_records, _ = self._market_records(symbol, df, table)
            if not symbol_records:
                continue
            records.extend(symbol_records)
            written[str(symbol)] = len(symbol_records)
        if not records:
            return {}
        with self._connect() as conn:
            conn.executemany(
                f"""INSERT OR REPLACE INTO {table}
                   (symbol, time, open, high, low, close, volume, amount, provider, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))""",
                records,
            )
            self._bump_data_version_conn(conn, table)
            for symbol in written:
                self._bump_data_version_conn(conn, f"{table}:{symbol}")
        self._market_symbols_cache.pop(table, None)
        return written

    def replace_market_data(self, symbol: str, df, price_mode: str = "qfq", period: str = "1d") -> int:
        """同事务全量重写一个标的的行情（本地物化 qfq 用）。返回写入行数。"""
        table = self._market_table(price_mode, period)
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

    @staticmethod
    def _market_rows_to_df(rows) -> pd.DataFrame:
        """market_data 行 → DataFrame 的统一转换（load_market_data /
        load_market_data_many 共用，dtype 口径一致）。"""
        df = pd.DataFrame([dict(r) for r in rows])
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        for col in ("open", "high", "low", "close", "volume", "amount"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    # ------------------------------------------------------------------
    # 拟合周/月K（market_data_qfq_*_fitted）：由 qfq 日K 派生的每日在途快照，
    # 聚合逻辑在 core.bars.fitted_period_rows，本层只负责存取。
    # ------------------------------------------------------------------

    @staticmethod
    def _fitted_table(period: str) -> str:
        canonical = normalize_period(period)
        if canonical == "1d":
            raise ValueError("fitted tables exist only for weekly/monthly periods")
        return f"market_data_qfq_{'weekly' if canonical == '1w' else 'monthly'}_fitted"

    @staticmethod
    def _fitted_records(symbol: str, df) -> tuple[list[tuple], int]:
        """拟合行 → upsert 记录（向量化；非正价格行拦截，与 _market_records 同防御）。"""
        if df is None or df.empty:
            return [], 0
        frame = pd.DataFrame(
            {
                "time": pd.to_datetime(df["time"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S"),
                "period_start": df["period_start"].astype(str) if "period_start" in df.columns else None,
            }
        )
        for col in ("open", "high", "low", "close", "volume", "amount"):
            frame[col] = pd.to_numeric(df[col], errors="coerce") if col in df.columns else None
        valid = ~frame["time"].isna() & ~(
            frame[["open", "high", "low", "close"]].astype("Float64") <= 0
        ).any(axis=1)
        dropped = int((~valid).sum())
        frame = frame[valid]
        frame = frame.astype(object).where(frame.notna(), None)
        records = [
            (symbol, *row, "local_fitted")
            for row in frame.itertuples(index=False, name=None)
        ]
        return records, dropped

    def save_fitted_period_bars(self, symbol: str, df, period: str) -> int:
        """upsert 拟合周期行（同事务）。返回写入行数。"""
        table = self._fitted_table(period)
        records, dropped = self._fitted_records(symbol, df)
        if dropped:
            _logger.warning("Dropped %d invalid rows for %s (%s)", dropped, symbol, table)
        if not records:
            return 0
        with self._connect() as conn:
            conn.executemany(
                f"""INSERT OR REPLACE INTO {table}
                   (symbol, time, period_start, open, high, low, close, volume, amount, provider, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))""",
                records,
            )
            self._bump_data_version_conn(conn, table)
            self._bump_data_version_conn(conn, f"{table}:{symbol}")
        return len(records)

    def save_fitted_period_bars_many(self, items, period: str) -> dict[str, int]:
        """一次连接批量 upsert 多标的的拟合周期行（回填用，与 save_market_data_many 对称）。"""
        table = self._fitted_table(period)
        records: list[tuple] = []
        written: dict[str, int] = {}
        for symbol, df in items or []:
            symbol_records, _ = self._fitted_records(symbol, df)
            if not symbol_records:
                continue
            records.extend(symbol_records)
            written[str(symbol)] = len(symbol_records)
        if not records:
            return {}
        with self._connect() as conn:
            conn.executemany(
                f"""INSERT OR REPLACE INTO {table}
                   (symbol, time, period_start, open, high, low, close, volume, amount, provider, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))""",
                records,
            )
            self._bump_data_version_conn(conn, table)
            for symbol in written:
                self._bump_data_version_conn(conn, f"{table}:{symbol}")
        return written

    def replace_fitted_period_bars(self, symbol: str, df, period: str) -> int:
        """同事务整段重写一个标的的拟合周期行（除权随 qfq 重建用）。返回写入行数。"""
        table = self._fitted_table(period)
        records, dropped = self._fitted_records(symbol, df)
        if dropped:
            _logger.warning("Dropped %d invalid rows for %s (%s)", dropped, symbol, table)
        with self._connect() as conn:
            conn.execute(f"DELETE FROM {table} WHERE symbol = ?", (symbol,))
            if records:
                conn.executemany(
                    f"""INSERT OR REPLACE INTO {table}
                       (symbol, time, period_start, open, high, low, close, volume, amount, provider, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))""",
                    records,
                )
            self._bump_data_version_conn(conn, table)
            self._bump_data_version_conn(conn, f"{table}:{symbol}")
        return len(records)

    def load_fitted_period_bars(
        self, symbol: str, period: str, start: str | None = None, end: str | None = None
    ) -> pd.DataFrame:
        """按标的读拟合周期行（含 period_start 列），可选 [start, end] 日期闭区间。"""
        table = self._fitted_table(period)
        sql = (
            f"SELECT time, period_start, open, high, low, close, volume, amount, symbol, provider"
            f" FROM {table} WHERE symbol = ?"
        )
        params: list = [symbol]
        if start:
            sql += " AND time >= ?"
            params.append(f"{str(start)[:10]} 00:00:00")
        if end:
            sql += " AND time <= ?"
            params.append(f"{str(end)[:10]} 23:59:59")
        sql += " ORDER BY time"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        if not rows:
            return pd.DataFrame()
        return self._market_rows_to_df(rows)

    # ------------------------------------------------------------------
    # 滚动周/月趋势值（trend_rolling_daily）：由 qfq 日K 派生的每日滚动锚定
    # 周/月趋势值，计算逻辑在 core.rolling_bars，本层只负责存取。
    # ------------------------------------------------------------------

    @staticmethod
    def _rolling_trend_records(symbol: str, df) -> list[tuple]:
        """趋势行 → upsert 记录：w/m 至少一个非 NaN 才成行（预热期双 NaN 行
        不落库），单侧 NaN → NULL。time 统一 'YYYY-MM-DD HH:MM:SS' 19 字符。"""
        if df is None or df.empty:
            return []
        frame = pd.DataFrame(
            {
                "time": pd.to_datetime(df["time"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S"),
                "w_trend": pd.to_numeric(df["w_trend"], errors="coerce"),
                "m_trend": pd.to_numeric(df["m_trend"], errors="coerce"),
            }
        )
        valid = ~frame["time"].isna() & (frame["w_trend"].notna() | frame["m_trend"].notna())
        frame = frame[valid]
        frame = frame.astype(object).where(frame.notna(), None)
        return [(symbol, *row) for row in frame.itertuples(index=False, name=None)]

    def save_rolling_trend_many(self, items) -> dict[str, int]:
        """一次连接批量 upsert 多标的的滚动趋势行（增量维护/回填用）。

        items 为 (symbol, df) 序列，df 含 time/w_trend/m_trend 列。
        """
        records: list[tuple] = []
        written: dict[str, int] = {}
        for symbol, df in items or []:
            symbol_records = self._rolling_trend_records(symbol, df)
            if not symbol_records:
                continue
            records.extend(symbol_records)
            written[str(symbol)] = len(symbol_records)
        if not records:
            return {}
        with self._connect() as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO trend_rolling_daily
                   (symbol, time, w_trend, m_trend, updated_at)
                   VALUES (?, ?, ?, ?, datetime('now','localtime'))""",
                records,
            )
            self._bump_data_version_conn(conn, "trend_rolling_daily")
            for symbol in written:
                self._bump_data_version_conn(conn, f"trend_rolling_daily:{symbol}")
        return written

    def replace_rolling_trend(self, symbol: str, df) -> int:
        """同事务整段重写一个标的的滚动趋势行（除权随 qfq 重建用）。返回写入行数。"""
        records = self._rolling_trend_records(symbol, df)
        with self._connect() as conn:
            conn.execute("DELETE FROM trend_rolling_daily WHERE symbol = ?", (symbol,))
            if records:
                conn.executemany(
                    """INSERT OR REPLACE INTO trend_rolling_daily
                       (symbol, time, w_trend, m_trend, updated_at)
                       VALUES (?, ?, ?, ?, datetime('now','localtime'))""",
                    records,
                )
            self._bump_data_version_conn(conn, "trend_rolling_daily")
            self._bump_data_version_conn(conn, f"trend_rolling_daily:{symbol}")
        return len(records)

    @staticmethod
    def _rolling_trend_rows_to_df(rows) -> pd.DataFrame:
        df = pd.DataFrame([dict(r) for r in rows])
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        for col in ("w_trend", "m_trend"):
            df[col] = pd.to_numeric(df[col], errors="coerce")  # NULL → NaN
        return df

    def load_rolling_trend(
        self, symbol: str, start: str | None = None, end: str | None = None
    ) -> pd.DataFrame:
        """按标的读滚动趋势行（time/w_trend/m_trend），可选 [start, end] 日期闭区间。"""
        sql = "SELECT time, w_trend, m_trend FROM trend_rolling_daily WHERE symbol = ?"
        params: list = [symbol]
        if start:
            sql += " AND time >= ?"
            params.append(f"{str(start)[:10]} 00:00:00")
        if end:
            sql += " AND time <= ?"
            params.append(f"{str(end)[:10]} 23:59:59")
        sql += " ORDER BY time"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        if not rows:
            return pd.DataFrame()
        return self._rolling_trend_rows_to_df(rows)

    def load_rolling_trend_many(
        self, symbols, start: str | None = None, end: str | None = None
    ) -> dict[str, pd.DataFrame]:
        """多标的批量读滚动趋势行：单连接 + chunked IN 查询（与 load_market_data_many
        同型）。无数据的 symbol 不出现在返回 dict 中。"""
        unique = [s for s in dict.fromkeys(str(s or "").strip().upper() for s in symbols) if s]
        if not unique:
            return {}
        sql_extra = ""
        params_tail: list = []
        if start:
            sql_extra += " AND time >= ?"
            params_tail.append(f"{str(start)[:10]} 00:00:00")
        if end:
            sql_extra += " AND time <= ?"
            params_tail.append(f"{str(end)[:10]} 23:59:59")
        grouped: dict[str, list] = {s: [] for s in unique}
        with self._connect() as conn:
            for i in range(0, len(unique), 500):
                chunk = unique[i : i + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"""SELECT symbol, time, w_trend, m_trend FROM trend_rolling_daily
                       WHERE symbol IN ({placeholders}){sql_extra}
                       ORDER BY symbol, time""",
                    chunk + params_tail,
                ).fetchall()
                for row in rows:
                    grouped[row["symbol"]].append(row)
        return {
            symbol: self._rolling_trend_rows_to_df(rows)
            for symbol, rows in grouped.items()
            if rows
        }

    def load_market_data(self, symbol: str, price_mode: str = "qfq", period: str = "1d"):

        table = self._market_table(price_mode, period)
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT time, open, high, low, close, volume, amount, symbol, provider
                   FROM {table} WHERE symbol = ? ORDER BY time""",
                (symbol,),
            ).fetchall()
        if not rows:
            return pd.DataFrame()
        return self._market_rows_to_df(rows)

    def load_market_data_many(
        self, symbols, price_mode: str = "qfq", period: str = "1d"
    ) -> dict[str, pd.DataFrame]:
        """多标的批量读全量日K：单连接 + chunked IN 查询，按 symbol 分组返回。

        批量止损试算（calc_stop_loss_batch）一次需要上百只标的的全历史，
        逐只 load_market_data 每次新建连接、单条查询，连接/查询开销随标的
        数线性放大。这里改为单连接分块查询（每块 500 只，远离 SQLite
        变量上限）。无数据的 symbol 不出现在返回 dict 中。
        """
        return self.load_market_data_window_many(
            symbols, None, None, price_mode=price_mode, period=period
        )

    def load_market_data_window_many(
        self, symbols, start=None, end=None, price_mode: str = "qfq", period: str = "1d"
    ) -> dict[str, pd.DataFrame]:
        """窗口限定的多标的批量读（L1 面板批量读取能力，投研基建 L1.5 面板用）。

        与 load_market_data_many 同口径（单连接 chunked IN + 按 symbol 分组），
        但把 [start, end] 过滤下推到 SQL——全历史标的在窄窗口场景下
        （回测面板）避免整段载入。
        """
        table = self._market_table(price_mode, period)
        unique = [s for s in dict.fromkeys(str(s or "").strip().upper() for s in symbols) if s]
        if not unique:
            return {}
        clauses = ""
        window_params: list = []
        if start is not None:
            clauses += " AND time >= ?"
            window_params.append(str(start)[:10])
        if end is not None:
            clauses += " AND time <= ?"
            window_params.append(str(end)[:10] + " 23:59:59")
        grouped: dict[str, list] = {s: [] for s in unique}
        with self._connect() as conn:
            for i in range(0, len(unique), 500):
                chunk = unique[i : i + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"""SELECT time, open, high, low, close, volume, amount, symbol, provider
                       FROM {table} WHERE symbol IN ({placeholders}){clauses}
                       ORDER BY symbol, time""",
                    (*chunk, *window_params),
                ).fetchall()
                for row in rows:
                    grouped[row["symbol"]].append(row)
        return {
            symbol: self._market_rows_to_df(rows)
            for symbol, rows in grouped.items()
            if rows
        }

    def get_market_data_summary_many(
        self, symbols, price_mode: str = "qfq", period: str = "1d"
    ) -> dict[str, dict]:
        """多标的 {symbol: {rows, start, end}}——单连接 chunked IN + GROUP BY。

        与 list_market_data_summaries（全表 GROUP BY）不同，这里按给定标的
        过滤，走 symbol 索引，供批量新鲜度检查使用。
        """
        table = self._market_table(price_mode, period)
        unique = [s for s in dict.fromkeys(str(s or "").strip().upper() for s in symbols) if s]
        if not unique:
            return {}
        out: dict[str, dict] = {}
        with self._connect() as conn:
            for i in range(0, len(unique), 500):
                chunk = unique[i : i + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"""SELECT symbol, COUNT(*) AS rows, MIN(time) AS start, MAX(time) AS end
                       FROM {table} WHERE symbol IN ({placeholders}) GROUP BY symbol""",
                    chunk,
                ).fetchall()
                for r in rows:
                    out[r["symbol"]] = {
                        "rows": int(r["rows"] or 0),
                        "start": r["start"],
                        "end": r["end"],
                    }
        return out

    def get_data_versions(self, names) -> dict[str, int]:
        """多名称批量读 data_versions（单连接 chunked IN）；未写入的名称缺省为 0。"""
        unique = [n for n in dict.fromkeys(str(n) for n in names) if n]
        if not unique:
            return {}
        out: dict[str, int] = {}
        with self._connect() as conn:
            for i in range(0, len(unique), 500):
                chunk = unique[i : i + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT name, version FROM data_versions WHERE name IN ({placeholders})",
                    chunk,
                ).fetchall()
                for r in rows:
                    out[r["name"]] = int(r["version"] or 0)
        return out

    def list_market_symbols(self, price_mode: str = "qfq", period: str = "1d") -> list[str]:
        table = self._market_table(price_mode, period)
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

    def get_market_data_summary(self, symbol: str, price_mode: str = "qfq", period: str = "1d") -> dict:
        table = self._market_table(price_mode, period)
        with self._connect() as conn:
            row = conn.execute(
                f"""SELECT COUNT(*) AS rows, MIN(time) AS start, MAX(time) AS end
                   FROM {table} WHERE symbol = ?""",
                (symbol,),
            ).fetchone()
        if row is None or row["rows"] == 0:
            return {"rows": 0, "start": None, "end": None}
        return {"rows": row["rows"], "start": row["start"], "end": row["end"]}

    def list_market_data_summaries(self, price_mode: str = "qfq", period: str = "1d") -> dict[str, dict]:
        """全标的 {symbol: {rows, start, end}}——单条 GROUP BY（P2-18：
        标的管理列表接口原对 600+ 标的逐只 get_market_data_summary，
        每次新建连接；改单条聚合查询一次出结果）。"""
        table = self._market_table(price_mode, period)
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT symbol, COUNT(*) AS rows, MIN(time) AS start, MAX(time) AS end
                   FROM {table} GROUP BY symbol"""
            ).fetchall()
        return {
            str(row["symbol"]): {"rows": row["rows"], "start": row["start"], "end": row["end"]}
            for row in rows
        }

    def clear_market_data(self, price_mode: str = "qfq", period: str = "1d") -> int:
        table = self._market_table(price_mode, period)
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
            "e_bias20",
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
                    e_bias20,
                    price_mode, formula_version, data_version, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))""",
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

    def load_indicator_daily_many(
        self, symbols, columns=("time", "atr")
    ) -> dict[str, pd.DataFrame]:
        """多标的批量读 indicator_daily 指定列：单连接 + chunked IN，按 symbol 分组。

        批量止损试算只需要 ATR 序列；逐只 load_indicator_daily 是 SELECT *
        全历史（~25 列），这里只取需要的列且一次出结果。``columns`` 必须含
        "time"；列名经标识符白名单校验（防注入），非法列直接抛 ValueError。
        """
        cols = [str(c).strip() for c in columns]
        if "time" not in cols:
            cols.insert(0, "time")
        for c in cols:
            if not c.isidentifier():
                raise ValueError(f"invalid indicator_daily column: {c}")
        unique = [s for s in dict.fromkeys(str(s or "").strip().upper() for s in symbols) if s]
        if not unique:
            return {}
        select_cols = ", ".join(["symbol", *cols])
        grouped: dict[str, list] = {s: [] for s in unique}
        with self._connect() as conn:
            for i in range(0, len(unique), 500):
                chunk = unique[i : i + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"""SELECT {select_cols} FROM indicator_daily
                       WHERE symbol IN ({placeholders}) ORDER BY symbol, time""",
                    chunk,
                ).fetchall()
                for row in rows:
                    grouped[row["symbol"]].append(row)
        return {
            symbol: pd.DataFrame([dict(r) for r in rows])
            for symbol, rows in grouped.items()
            if rows
        }

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

    def indicator_cache_info_many(self, symbols) -> dict[str, dict]:
        """多标的批量版 indicator_cache_info：两张缓存表各一次 GROUP BY 查询。

        返回结构与 indicator_cache_info 相同；无任何缓存行的 symbol 不出现
        在返回 dict 中（调用方按 rows=0 处理）。
        """
        unique = [s for s in dict.fromkeys(str(s or "").strip().upper() for s in symbols) if s]
        if not unique:
            return {}
        out: dict[str, dict] = {}

        def _blank() -> dict:
            return {
                "indicator_rows": 0,
                "indicator_last": None,
                "indicator_version": None,
                "indicator_data_version": 0,
                "trend_rows": 0,
                "trend_last": None,
                "trend_version": None,
                "trend_data_version": 0,
            }

        with self._connect() as conn:
            for i in range(0, len(unique), 500):
                chunk = unique[i : i + 500]
                placeholders = ",".join("?" for _ in chunk)
                for r in conn.execute(
                    f"""SELECT symbol, COUNT(*) AS n, MAX(time) AS last,
                              MAX(formula_version) AS ver, MAX(data_version) AS dv
                       FROM indicator_daily WHERE symbol IN ({placeholders})
                       GROUP BY symbol""",
                    chunk,
                ).fetchall():
                    info = out.setdefault(r["symbol"], _blank())
                    info["indicator_rows"] = int(r["n"] or 0)
                    info["indicator_last"] = r["last"]
                    info["indicator_version"] = r["ver"]
                    info["indicator_data_version"] = int(r["dv"] or 0)
                for r in conn.execute(
                    f"""SELECT symbol, COUNT(*) AS n, MAX(time) AS last,
                              MAX(formula_version) AS ver, MAX(data_version) AS dv
                       FROM trend_daily
                       WHERE symbol IN ({placeholders}) AND param_set = 'default'
                       GROUP BY symbol""",
                    chunk,
                ).fetchall():
                    info = out.setdefault(r["symbol"], _blank())
                    info["trend_rows"] = int(r["n"] or 0)
                    info["trend_last"] = r["last"]
                    info["trend_version"] = r["ver"]
                    info["trend_data_version"] = int(r["dv"] or 0)
        return out

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
                    stop_profile, atr_basis, created_at)
                   VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))""",
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
                    batch.get("stop_profile", "default"),
                    batch.get("atr_basis", ""),
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
                    skipped_buys_json, monthly_nav_json,
                    round_trips_json, round_trips_source,
                    r_mean, r_p5, r_p25, r_p75, r_p95, r_skew, tail_ratio,
                    exit_efficiency, max_losing_streak, cvar_5, ulcer_index,
                    max_dd_duration_days, stop_exit_ratio, chandelier_exit_ratio,
                    created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))""",
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
                    cell.get("round_trips_json"), cell.get("round_trips_source"),
                    cell.get("r_mean"), cell.get("r_p5"), cell.get("r_p25"),
                    cell.get("r_p75"), cell.get("r_p95"), cell.get("r_skew"),
                    cell.get("tail_ratio"), cell.get("exit_efficiency"),
                    cell.get("max_losing_streak"), cell.get("cvar_5"),
                    cell.get("ulcer_index"), cell.get("max_dd_duration_days"),
                    cell.get("stop_exit_ratio"), cell.get("chandelier_exit_ratio"),
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
        " c.excess_annual_return, c.excess_sharpe, c.excess_calmar,"
        " c.r_mean, c.r_p5, c.r_p25, c.r_p75, c.r_p95, c.r_skew, c.tail_ratio,"
        " c.exit_efficiency, c.max_losing_streak, c.cvar_5, c.ulcer_index,"
        " c.max_dd_duration_days, c.stop_exit_ratio, c.chandelier_exit_ratio,"
        " c.round_trips_source"
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

    def get_batch_roundtrip_rows(self, batch_id: str) -> list[dict]:
        """ok 格子的 round-trip blob + 聚合所需语境（止损诊断/导出用）。"""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT c.symbol, c.symbol_name, c.strategy_id, c.strategy_name,
                          c.category_l1, c.asset_type, c.round_trips_json,
                          f.trend_score_avg
                   FROM batch_backtest_cells c
                   LEFT JOIN batch_backtest_symbol_features f
                     ON f.batch_id = c.batch_id AND f.symbol = c.symbol
                   WHERE c.batch_id = ? AND c.status = 'ok' AND c.round_trips_json IS NOT NULL
                   ORDER BY c.symbol, c.strategy_id""",
                (batch_id,),
            ).fetchall()
        return [dict(r) for r in rows]

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
        period: str = "1d",
    ) -> dict[str, int]:
        """Bar counts per symbol (single indexed GROUP BY) — batch ETA estimates.

        start/end 限定统计窗口（按交易日计数，供窗口批次 ETA 使用）。
        time 列为 'YYYY-MM-DD HH:MM:SS' 文本，end 补到当日末尾做闭区间比较。
        """
        table = self._market_table(price_mode, period)
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


def init_db(db_path: str | Path | None = None) -> Database:
    """进程级单例初始化。

    默认路径走 ``core.paths.default_db_path()``（项目根锚定 + 支持
    ``TREND_QUANT_HOME`` 覆盖），与 ``Database()`` 的缺省一致。此前这里是
    硬编码的 CWD 相对字符串 ``"data/trend_quant.db"``——与 core/paths 文档
    「已消除 CWD 相对路径」相矛盾，且让 ``TREND_QUANT_HOME`` 对主应用
    （app.main 的无参调用）完全失效：从非项目根目录启动会静默连到
    另一个库。
    """
    global _db_instance
    _db_instance = Database(db_path if db_path is not None else default_db_path())
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
