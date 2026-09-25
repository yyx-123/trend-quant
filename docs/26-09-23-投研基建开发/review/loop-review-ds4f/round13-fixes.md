# Round 13 修复与回归（loop-review-ds4f）

> **状态：CLOSED（2026-09-26 闭合）** —— 审查报告见 `round13-review.md`
> 日期：2026-09-25/26

## 修复总览

| 项 | 级别 | 修复 | 触及文件 |
|---|---|---|---|
| **R13A-F1** 影子账户按全仓入账（含不可卖的当日买入） | P2 | 影子只对 **sellable** 部分入账；不可卖余额以**副本**留在影子持仓（不动调用方对象）；`sell_list` 的 `qty` 改为**本次可执行股数**并新增 `qty_total`（旧口径 `qty=全仓 + executable=true` 会让人工下出不可卖的量） | `src/portfolio/live.py` |
| **R13A-F2** CLI 批次的冻结门对 app 进程不可见 | P2 | `run_freeze` 新增**跨进程哨兵**：`cross_process_frozen()`（写 `data/run_freeze.lock`，退出删除；崩溃残留按 `_STALE_SECONDS=6h` 过期，不永久卡日更）、`file_frozen()`、`is_frozen_anywhere()`；`run_batch_frozen` 双层冻结（进程内 + 哨兵）；`core/jobs.py` 的日更/哨兵 6 处判据改走 `is_frozen_anywhere()` | `src/core/run_freeze.py`、`src/rule_backtest/batch_service.py`、`src/core/jobs.py` |
| **R13B-F1（写入半）** backfill 脚本零闸门（R11A-F3 未落地） | P2 | `_benchmark_sharpe_calmar` 走 `sanitize_ratio_metrics`（与引擎出口/HTTP/CSV 同一闸门） | `scripts/backfill_batch_excess_metrics.py` |
| **R13B-F1（读取半）** 格子级比值列在读取面无闸 | P2 | 四处出口统一过 `sanitize_ratio_metrics`：`/cells` 列表端点、`_parse_cell_blobs`（明细）、`export_batch_analysis`（cells.csv/cells_alt.csv）、`compare_batches`（入参先清噪 → delta 不再是噪声作差） | `src/app/routers/batch_backtest.py`、`src/services/backtest_export.py`、`src/rule_backtest/batch_service.py` |
| backlog：`run_base_v1_sample` 遇 None 格式化崩溃 | P3 | 控制台与 markdown 表格对 None 写 `n/a` | `scripts/run_base_v1_sample.py` |
| backlog：迁移钉子钉不住"定义被改弱" | P3 | 钉子加强：把库里的守卫改成**弱定义** → 重开库必须恢复强定义（这才区分 DROP+CREATE 与 IF NOT EXISTS）；round12-fixes 的相关记载同步更正 | `tests/integration/test_loop_review_ds4f_r12.py`、`round12-fixes.md` |
| backlog：`test_offline_scripts_gate_noise_ratios` 是空钉 | P3 | 该钉子里的 backfill 部分删除（指向新的行为级钉子），只保留 `run_base_v1_sample` 的行为断言 | `tests/unit/test_loop_review_ds4f_r11.py` |
| 报告失真两处 | — | `round11-fixes.md`「两个脚本都已过闸」→ 更正为"backfill 当时未落地"；`round12-fixes.md`「HTTP 与 CLI 两个入口都覆盖」→ 更正为"CLI 当时只覆盖一半" | 两份报告 |

## 新增钉子（6 条，全部变异实证）

| 钉子 | 断言 | 变异实证 |
|---|---|---|
| `test_offline_backfill_helper_gates_noise`（integration） | backfill 助手：70 根一字板 → `sharpe is None`（原值 28775.93）、calmar 保留；200 根带波动 → 保留且 \|v\|<50 | 助手去闸 → **红** |
| `test_cell_ratio_columns_are_gated_on_read_paths`（integration） | 明细端点解析器：`benchmark_sharpe/excess_sharpe` 置 None，`sharpe/calmar/annual_blocks` 保留 | 明细端点去闸 → **红** |
| `test_compare_batches_does_not_difference_noise`（integration） | 噪声格子的 `base_sharpe` 为 None、`delta_sharpe` 为 None；合法侧保留；`delta_calmar` 仍可算 | compare 入参去闸 → **红** |
| `test_cross_process_freeze_is_visible`（integration） | 哨兵存在时**子进程**也判冻结；退出即删；过期文件视为未冻结 | 哨兵不落盘 → **红** |
| `test_shadow_cash_excludes_unsellable_same_day_lot`（integration） | 打桩"必止损"后：昨买+今买 的 `cash_est` **严格小于** 昨买+昨买，差额 = 不可卖那 1000 股 × 清单参考价 | 影子退回全仓入账 → **红** |
| `test_engine_runs_delete_guard_migrates_to_existing_db`（加强） | 缺失 → 重开补装；**改弱 → 重开恢复强定义** | 实现改 `IF NOT EXISTS` → **红** |

## 回归结果

- 相关面：r11/r12/r13 三个钉子文件 + `test_live_runner.py` + 旧栈/API 全套全绿；
- 全量套件：**1677 passed / 2 failed**（均为既有 Windows 临时文件 flake）；
- ruff：`(file, rule)` 与基线 78/78 一致（新增 0 条）；
- 变异实证：本轮 **7 个**语义变异全部被抓住。

## 诚实记录（本轮暴露的流程问题）

1. **R11A-F3 的修复从未落地**：当时对 `scripts/backfill_batch_excess_metrics.py` 的编辑失败
   （"File has not been read yet"），我改完另一个脚本后**没有复核文件**，却在提交信息与报告中
   写成"两个脚本都已过闸"。**教训**：编辑失败后必须复核目标文件，报告里的"已修"必须以文件内容为准。
2. **Round 12 的冻结门只覆盖了 app 进程**：`run_batch_frozen` 的实现是对的，但"CLI 入口也覆盖"
   这句结论把"改了调用点"当成了"跨进程生效"，忽略了冻结计数器本身是进程内实现。
3. **同源面容易只改一半**：T+1 只改了 `sellable_quantity`、漏了影子现金；年度块只加了读取面闸门、
   漏了同一行的格子列。**教训**：修一个口径时，先列出该口径的**全部**消费者与兄弟字段。

以上三条已写入 `final-report.md` 的流程改进项。
