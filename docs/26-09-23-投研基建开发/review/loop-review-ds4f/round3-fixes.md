# Round 3 修复与回归（loop-review-ds4f）

> 日期：2026-09-25
> 对应审查：`round3-review.md`（4 项 P2 + 10 项 P3，全部集中在**平台/纪律/通道层**）

## 修复总览

| 项 | 修复 | 钉子 |
|---|---|---|
| **R3C-P2-1**（P2）晋升静默复用他人版本行、血缘丢失 | `library.add_version` 同 hash 命中且既有行 `experiment_id` 与本次不同 → `LibraryError`（同实验幂等重放仍允许） | `test_promote_refuses_silent_version_reuse_across_experiments`（真内置件 config + 真 `add_version`） |
| **R3C-P2-2**（P2）重复检测被"显式写出平台缺省值"绕过 | `_expand_platform_defaults` 扩到**全部声明字段**，新增 `_MODULE_SPEC_DEFAULTS` 单一真源（与各 runner 的 `spec.get(k, default)` 对齐）：缺键或 None 一律取平台缺省 → "省略 ≡ 显式缺省" | `test_duplicate_detection_expands_all_platform_defaults`（`initial_capital`/`window_mode`/`n_folds` 及其组合都必须 `duplicate_of`） |
| **R3C-P2-3**（P2）退役策略线仍可作新实验 base | ①`resolve_experiment_config` 拒退役线版本（运行期）；②**入口**即拒（`portfolio_backtest._spec_errors` 检查 base 所属线 `retired_at`，避免烧 attempt 才 failed） | `test_retired_strategy_line_rejected_as_base` |
| **R3C-P2-4**（P2）已关课题无法再验证同 spec | **能力缺口 + 设计选择**：本轮只把三处错误文案改成可行动（说明"复现请新建课题并显式声明为复现"）；通路本身进待决策清单 | 文案断言并入既有用例（行为不变） |
| R3C-P3-1 | CLI `rerun --run` / `recompute` 补齐 `run_freeze.frozen_writes()` | `test_cli_recompute_surfaces_skipped`（源码口径）+ 与 propose/MCP 同形 |
| R3C-P3-2 | `threading` 提为**模块级引用**（原函数体内 import 使 `monkeypatch(jobs.threading)` 永不生效，钉子实际起真线程并竞态） | `test_sentinel_uses_module_level_threading_reference` + 实测（假线程生效：调用/退避各 120 次有界） |
| R3C-P3-3 | CLI recompute 输出 `skipped` | 同上 |
| R3C-P3-4 | `verdict.verdict_envelope` 作**单一真源**：HTTP 下载与课题物化的 `report.json` 同构（含 final_verdict/reasoning/baseline/evidence/warnings） | `test_materialized_report_json_carries_verdict_envelope` |
| R3C-P3-5 | 复核 campaign 结束对受影响课题去重**重新物化**（失败只告警） | 行为随 `test_review_ds`/服务面用例；campaign 返回值加 `rematerialized_topics` |
| R3C-P3-6 | engine 5 张**子证据表**补 append-only 守卫（`_no_delete` + 全列 `_no_update`，DROP+CREATE 传播存量库） | `test_engine_sub_tables_are_append_only`（真行下的 UPDATE/DELETE 全被拒 + `engine_runs` 白名单更新仍可用） |
| R3C-P3-7 | 删两个与 UNIQUE 同列的冗余索引（DDL + 迁移路径） | `test_engine_duplicate_indexes_removed` |
| R3C-P3-8 | `insert_platform_verdict` 只接受 evaluating/verdicted | `test_platform_verdict_requires_evaluating`（并把旧夹具改为走真实状态机） |
| R3C-P3-9 | confirm 落定改**行级原子认领**（`AND final_verdict IS NULL` + rowcount） | `test_confirm_is_row_level_atomic_claim`（把入口检查喂过期快照，直接验证行级腿 + 断言留痕不被覆写） |
| R3C-P3-10 | MCP `research_get_experiment` 错误口径统一（带 `error` 字段，`ok=False`） | 源码口径钉子（工具为闭包，注册面由 critical_paths 覆盖） |

## 变异反证

对 Round 3 的 10 条新钉子做定向变异：**7/10 被直接抓住**；3 条经复核**排除为无效变异**：

- `confirm` 行级守卫：初版钉子确实为空（被入口状态检查代挡）→ **已重写**为喂过期快照直测行级腿，
  重做变异后**被抓住**（实测：删掉 `AND final_verdict IS NULL` → 用例失败）；
- engine 子表守卫：变异只是**改名**（trigger 体不变 → 仍拦 UPDATE）→ 无效变异；改为删体后实测
  `UPDATE engine_daily_nav`/`DELETE FROM engine_fills` 在真行下均被拒（且 `engine_runs` 白名单更新仍可用）；
- 冗余索引：DDL 层恢复后仍被 `_migrate_schema` 的 `DROP INDEX IF EXISTS` 清掉（双保险），
  终态断言仍成立。

## 回归结果

- 全量：**1605 passed / 1 failed**（唯一失败仍是改动前即 flaky 的 Windows 临时文件用例，在父提交上同样失败）；修复过程中另有一次全量 **1606 passed / 0 failed**。
- 新增钉子：`tests/unit/test_loop_review_ds4f_r3.py` 11 项；
- ruff：与基线逐条对比新增 0 条（并把 `src/portfolio/service.py` 的存量 I001/PLC3002
  与 `src/research/recompute.py` 的 I001 一并修掉，属顺手收口）。
