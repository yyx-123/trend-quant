# 数据库变更明细（2026-08-25 CR 修复）

> 线上 DB 需执行的变更清单。本次修复中涉及数据库结构与行为的全部变更如下。
> **无新增表、无新增列、无数据改写**；唯一结构性变更是删除 3 个冗余索引。
> 所有变更均已内置在 `Database.__init__`（`_init_tables` + `_migrate_schema`）中，
> 应用启动时自动、幂等地完成，**线上无需手工执行任何 SQL**。

## 1. 结构性变更（自动迁移，启动时幂等执行）

### 1.1 删除 3 个冗余索引（附录 B · N1）

| 索引 | 所在表 | 删除原因 |
|---|---|---|
| `idx_market_data_raw_symbol_time` | `market_data_raw` | 与 `PRIMARY KEY (symbol, time)` 完全同列；rowid 表上 PK 已自动建同列索引，冗余索引白白放大百万行表的每次写入 |
| `idx_market_data_qfq_symbol_time` | `market_data_qfq` | 同上 |
| `idx_ex_factors_symbol_time` | `ex_factors` | 同上 |

- **执行方式**：`src/data/storage/db.py` `_migrate_schema()` 内 `DROP INDEX IF EXISTS`（幂等，可重复执行）；
  `_init_tables` 的 DDL 已同步移除（全新部署不再创建）。
- **线上操作**：部署新版代码并重启服务即可，启动时自动完成。
- **风险**：零。主键前缀查询（`WHERE symbol=? AND time>=?`）的执行计划不变（已在 2.7GB 真实库副本上验证迁移幂等，索引删除后不存在）。

## 2. 行为级变更（非结构，但影响数据读写语义）

| # | 变更 | 说明 |
|---|---|---|
| 2.1 | `PRAGMA foreign_keys=ON`（P2-23） | `_connect()` 全连接开启外键。生产库已实测 `manual_trades.user_id`/`sessions.user_id` **零孤儿行**，开启安全。若线上库存在历史孤儿行，后续删除 users 行将被外键阻止（当前代码无删用户路径，实际无影响）。 |
| 2.2 | 连接 `timeout=30`（busy_timeout，P2-16/P2-23） | 长事务/调度器并发下的锁等待从默认 5s 提升到 30s。 |
| 2.3 | 每日 03:00 自动备份（P1-2） | 调度器新增 `daily_db_backup` cron（Asia/Shanghai 03:00），`backup_to(keep=1)` 写入 `data/backups/trend_quant-*.db` 且只保留最新一份。**注意**：`data/backups/` 下不匹配保留策略的旧手工备份会被修剪——上线前如需保留 `pre-auth-wall-20260824.db` 等历史备份，请先移出该目录。 |
| 2.4 | 备份前显式 `wal_checkpoint(TRUNCATE)` + 目标路径单引号校验 | 保证备份包含 WAL 内全部写入。 |
| 2.5 | `job_runs` 新增「running → interrupted」语义（P2-9） | 三个 JobManager（instrument_bulk_backfill / instrument_add / etf_constituent_import）启动时落 `status=running` 行；应用启动清扫（`mark_interrupted_job_runs`）把无配对终态的孤儿 running 行标记为 `interrupted`。**影响**：这些 job_type 的 job_runs 行数将翻倍（running + 终态两行），如有按行数统计的看板需注意。 |
| 2.6 | `instrument_metadata` / `market_data_*` 无结构变更 | 所有行情/元数据表结构不变。 |
| 2.7 | `users` 表无结构变更 | `is_admin` 列已存在（P1-3 子代理核实），内置管理员 ensure 只做 `INSERT`（不存在时）或 `UPDATE is_admin=1`（存在时），线上已有的 yyx 行只会被补 is_admin，密码不动。 |
| 2.8 | `data_versions` / `ex_factors` 语义不变 | qfq 内容版本继续由写入侧 bump；`get_market_dashboard_revision` 不再 COUNT(*)（三元素 token：MAX(time) + metadata 最新更新 + 版本号），仅进程内缓存键，无持久化影响。 |

## 3. 数据兼容性

- 全部变更向后兼容：旧版本代码读写本库不会报错（冗余索引删除对旧代码透明；foreign_keys 开启是连接级行为）。
- 回滚方式：代码回滚即可；被删索引如需重建，执行原 DDL（见 git 历史）即可，无数据损失。
