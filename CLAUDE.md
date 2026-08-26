# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A-share ETF trend-tracking system (single-user, self-hosted). FastAPI + Jinja2 frontend, APScheduler for the daily 16:30 data job, SQLite as the sole store, MCP server for external tool access.

## Common Commands

- **Run dev server:** `PYTHONPATH=src .venv/bin/python -m uvicorn app.main:app --reload`
- **Tests:** `.venv/bin/python -m pytest tests/ -q`
- **Deploy:** `sudo systemctl restart trend-quant.service` (unit at `/etc/systemd/system/trend-quant.service`, runs uvicorn from `/srv/trend-quant/.venv`)
- **Disable scheduler in dev:** `TREND_QUANT_DISABLE_SCHEDULER=1`

## Architecture

```
src/
├─ core/            Pure domain logic: indicators (unified indicator lib),
│                   trend (trend score), symbols, calendar, benchmarks,
│                   strategy_config, settings, jobs (daily 16:30 update), scheduler
├─ data/            Storage & feeds: db.py (SQLite), indicator_store (cache
│                   read facade), service (TickFlow), intraday_service
├─ services/        Application services: market_indicators, dashboard,
│                   instrument_jobs, instrument_admin, indicator_builder
├─ rule_backtest/   Rule-based backtesting: engine, condition_engine,
│                   value_resolver (full-series memoization), registry, loader, service
├─ app/             HTTP layer: main + routers (orchestration only)
└─ trend_mcp/       MCP thin adapter (7 tools, Bearer token channel auth)
```

Dependency direction: `app / trend_mcp → services → core / data`. Never import
router modules from services/core/data.

## Hard Invariants (do not violate)

1. **One implementation per concept.** Indicators/trend/symbols live in `core/`
   only. Never add a second implementation — adapt at the call site.
2. **Cache is an accelerator only.** Every indicator read goes through
   `data/indicator_store.get_series` with live-compute fallback. Fallback is permanent.
3. **Intraday rows are never persisted.** Realtime overlay rows (synthetic bar)
   are view-only; backtests and stop-loss use EOD data only.
4. **Backtest results must stay bit-identical** when refactoring the engine —
   golden-master tests (`tests/unit/test_p13_memoized_golden.py`) are the gate.

## Key Modules

- `core/indicators.py` — the only indicator implementations (vectorized). `INDICATOR_FORMULA_VERSION`.
- `core/trend.py` — trend score: series is canonical, snapshot = last row. `TREND_FORMULA_VERSION`.
- `core/symbols.py` — symbol normalization (6-digit → .SS/.SZ, SH→SS).
- `data/indicator_store.py` — cache-first reads; `compute_intraday_row` for realtime overlay (exact recursion from cached state columns).
- `services/indicator_builder.py` — full-symbol cache rebuilds, param-set registry (hash of TREND_FORMULA_VERSION + normalized params), dividend detection + history re-pull, pre-rebuild `VACUUM INTO` backups to `data/backups/`.
- `data/storage/db.py` — all tables incl. `market_data_qfq/raw`, `instrument_metadata` (sole instrument store), `rule_strategies`, `job_runs`, `app_config`, `indicator_daily`, `trend_daily`, `trend_param_sets`. WAL mode.

## Configuration

- `config/app.yaml` — infra only (timezone, scheduler time, retry, TickFlow limits, logging).
- Strategy/indicator params — `app_config.strategy` row in DB, accessed via `core/strategy_config.get_strategy_config()` (code defaults as fallback/seed; 30s 进程内 TTL 缓存，写后调 `invalidate_strategy_config_cache()`).
- Instruments — `instrument_metadata` table (sole source; edited via `/instruments`).
- Secrets — `.env` (`TICKFLOW_API_KEY`、`TREND_MCP_TOKENS`；env 读取统一经 `core/env.py`，模板见 `.env.example`).

## 时区约定（P1-5，全仓统一东八区）

- 凡「交易日/会话语义」（交易日门控、数据新鲜度、盘中/盘后判定）一律
  `core.calendar.market_now()`（Asia/Shanghai 墙钟）；
- 凡「系统事件时间戳」（run_id、任务计时、session 过期）可 `datetime.now()`；
- 禁止再引入裸 `date.today()` 判定交易日。

## Testing Conventions

- pytest with markers; integration tests use tmp-path SQLite, never the real DB.
- Golden-master pattern: legacy algorithms frozen as reference copies inside tests
  (see `tests/unit/test_core_indicators.py`, `test_core_trend.py`, `test_p13_memoized_golden.py`).
