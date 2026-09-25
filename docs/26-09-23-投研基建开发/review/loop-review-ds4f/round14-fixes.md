# Round 14 修复与回归（loop-review-ds4f）

> **状态：CLOSED（2026-09-26 闭合）** —— 审查报告见 `round14-review.md`
> 日期：2026-09-26

## 修复总览

| 项 | 级别 | 修复 | 触及文件 |
|---|---|---|---|
| **R14B-F1** 冻结门的写入侧缺失（HTTP 触发的补齐/物化/重建） | P2 | 新增 `data.service.assert_writes_unfrozen(action)`（冻结中抛 `FrozenWritesError`）；`backfill_daily_history`、`rematerialize_qfq`、`indicator_builder.rebuild_after_backfill` 三个整段重写入口加守卫；`instruments.py` 的补齐端点把该异常转 **HTTP 409**（"稍后重试"语义） | `src/data/service.py`、`src/services/indicator_builder.py`、`src/app/routers/instruments.py` |
| **R14B-F2** `portfolio_live_lists` 无用户维度（跨用户可读 + 同日互相覆盖） | P2 | DDL 加 `user_id INTEGER NOT NULL DEFAULT 1`、唯一键改 `UNIQUE(user_id, list_date, strategy_version_id)`；旧表（无 user_id）走**重建搬运**迁移（生产库该表 0 行、空操作；有数据则 user_id 归 1）；写入带 `user_id`、`ON CONFLICT(user_id, …)`；`reconcile_daily_list` 的读/写带 user_id；台账页 `_recent_live_lists(user_id=...)` 按调用者过滤（admin 看全量） | `src/data/storage/db.py`、`src/portfolio/live.py`、`src/app/routers/research_ledger.py` |
| R14A-B1 哨兵 `mkdir` 在 try 之外 → 会崩批次 | P3 | `mkdir` 移入 try（路径不可建时降级为纯进程内冻结，与 docstring 一致）；`cross_process_frozen`/`file_frozen` 接受 str 路径 | `src/core/run_freeze.py` |

## 新增钉子（7 条，全部变异实证）

| 钉子 | 断言 | 变异实证 |
|---|---|---|
| `test_write_side_freeze_guard_blocks_backfill_and_rebuild` | 未冻结放行；冻结中 `assert_writes_unfrozen`、`rebuild_after_backfill`、`backfill_daily_history`、`rematerialize_qfq` 四处**全部**抛 `FrozenWritesError` | 补齐去闸 → **红**；物化去闸 → **红**；重建去闸 → **红** |
| `test_daily_update_defers_on_cross_process_sentinel` | 前置断言"进程内计数为 0"（确保只靠哨兵触发）；哨兵存在 → 日更 `deferred_backtest_running`；真取数入口设绊线 | `jobs` 6 处改回 `is_frozen()` → **红**（0.7s，无网络/无落库） |
| `test_strategy_leg_ratio_gate_is_pinned` | 恒入场 + 单调 20%/日 70 根：1 笔成交、原值 \|sharpe\|>50（实测 9899.52）→ 出口为 None、calmar 保留 | 引擎出口策略腿去闸 → **红** |
| `test_live_lists_are_per_user` | 两用户同日同策略各自一行（不覆盖）；`_recent_live_lists(user_id=N)` 只回自己的；admin 口径全量 | 读取面去 user 过滤 → **红** |
| `test_live_lists_schema_migrates_old_table` | 旧 schema（无 user_id）重开 → 重建出 user_id、历史行归 1、唯一键含 user_id | 迁移分支禁用 → **红** |
| `test_sentinel_degrades_instead_of_crashing` | 父路径是文件 → 上下文正常进出、`file_frozen` 为 False、退出后计数归零 | mkdir 移出 try → **红** |
| `test_sentinel_staleness_boundary` | 半阈值内 → 冻结；超阈值 → 未冻结 | 阈值改 1 秒 → **红** |

（另：R14A 的两条建议钉子按原方案落地——真入口绊线版日更钉 + 策略腿夹具钉；
R13 那条打在 `jobs.DataService` 上的空转钉已由新钉取代。）

## 回归结果

- 相关面：r11/r12/r13/r14 四个钉子文件 + live_runner + API/旧栈全套全绿；
- 全量套件：**1687 passed / 0 failed**（既有 Windows flake 本次未触发）；
- ruff：`(file, rule)` 与基线 78/78 一致（新增 0 条）；
- 变异实证：本轮 **9 个**语义变异（M1/M1b/M2–M8）；其中 3 个首轮未被抓住，均因**我的钉子/变异脚本不够严谨**
  （写入侧钉子只测了助手与重建、jobs 钉子未前置断言计数为 0、过期边界按固定 60 秒偏移取值），
  加固后全部变红——这一过程如实记录，供后续轮次参考。

## 待决策新增

- **R14-D-1（补齐/新增标的类端点的权限）**：这些端点当前只要求登录（任何用户可触发全库
  874 只的批量补齐）。修复只加了"冻结中拒绝"，**没加** admin 门——是否改成 admin-only 需要你定
  （会改变现有操作者习惯）。
- **R14-D-2（实盘清单的共享/隔离口径）**：已按"逐用户隔离 + admin 可全量"实现。如果你的实际
  用法是"全队共享一份实盘计划"，说一声我改回共享并在页面显式标注共享口径。
