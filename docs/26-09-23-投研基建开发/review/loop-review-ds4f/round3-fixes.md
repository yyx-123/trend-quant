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


## V7 独立验收（第 7 个代理）：判 FAIL → 二次修复

V7 逐项反证，**判 3 项阻断**（全部是我的实现/钉子问题）：

| # | 项 | V7 证据 | 二次修复 |
|---|---|---|---|
| ND-1（P2） | 复核 campaign 的"重新物化"是**死代码**：`materialize_topic(db, topic_id)` 漏了 keyword-only 的 `root` → `TypeError` 被本地 except 吞掉、每个 campaign 都往 stderr 打 traceback、`rematerialized_topics` 恒空 | 真 CLI 跑出 traceback；`rematerialized_topics == []` | 从 `research.api.DEFAULT_TOPICS_DIR` 取根、`ResearchService.recompute_campaign` 透传 `self.topics_dir`；新增钉子断言"真的产出物化文件" |
| ND-2（P3） | 物化 `report.json` 与 HTTP 下载**内容不等价**：`spec` 在文件里是 `null`（原始实验行只有 `spec_json`，未解码） | 键集相同但值不同（`spec=null`） | `materialize_topic` 内解码 `spec_json → spec`；钉子断言 `payload["spec"]["base"] == version id` |
| ND-3（P2） | 缺省展开**不完整**：8 个声明字段仍是静默杠杆（backtest 的 `expect/mc_bands/is_compound/compound_reason/primary_horizon`、event 的 `primary_horizon/context_filter`、distribution 的 `criterion`），显式写出其"不改变行为的值"仍绕过重复检测 | 逐一 ACCEPTED | 补全 `_MODULE_SPEC_DEFAULTS`（含 `universe`；`primary_horizon` 按 runner 语义现算 = `horizons[0]`）；新增**机制性**钉子：表必须覆盖全部声明字段（排除必填集）——新增字段不登记即红灯 |
| ND-4（P3） | 退役线修复**过头**：既有实验的重跑（rerun/复核/高原探针/晋升）经 `resolve_experiment_config` 一并被拒 → 退役一个策略线会把已完成的实验全部变成 failed | `rerun` → runner → ServiceError → failed | `resolve_experiment_config(allow_retired=False)` 默认拒新引用，**复现/复核路径传 `allow_retired=True`**（rerun/探针/晋升）；钉子断言两条路径的行为差 |
| ND-5（P3） | 既有版本行**无血缘**（种子/人工版本，`experiment_id IS NULL`）时仍静默吸收晋升 | `promote` 到与 `base-v1@1` 同 config 的实验 → 返回 `base-v1@1`、血缘 None | 血缘不同（含 None）即 `LibraryError`；钉子覆盖 |
| ND-6（P3） | `LibraryError`/`ServiceError` 是 `ValueError` 家族 → MCP 通道把它归为 "internal error"，业务原因被吞 | 直接调用 `_error_payload(LibraryError(...))` | `_error_payload` 增加这两类的业务分类；钉子断言不含 "internal error" |
| 钉子缺口 | engine 守卫钉子只覆盖 2/5 张子表；CLI 钉子是源码 grep（删任一 `frozen_writes` 都抓不到） | 变异实证 | engine 钉子补全 5 张表的 UPDATE/DELETE（含真行）；CLI 的 `skipped` 改为真 subprocess 断言（V7 已独立验证行为），`frozen_writes` 的存在由源码断言 + 行为 spy 双重覆盖 |

**V7 已独立验证为正确**（无需改动）：晋升守卫行为、命名缺省等价、退役线 intake/resolve/历史可读、
engine 守卫与存量传播（含父提交建的库重开后获得守卫）、索引删除与 UNIQUE 完整性、
verdict 状态守卫与行级认领、CLI 冻结与 skipped、哨兵 monkeypatch 生效、MCP 错误形状。

**另记（非本轮引入）**：`tests/unit/test_db_path_anchoring.py::test_init_db_default_is_not_cwd_relative`
以无参 `init_db()` 打开并迁移**生产库**——每次全量测试都会对 `data/trend_quant.db` 跑一遍 DDL
（行数不变、效果即"迁移到最新 schema"）。属存量测试卫生问题，记入最终报告的记录项。


## V8 独立验收（第 8 个代理）：再判 FAIL（2 项阻断）→ 三次修复

V8 逐项复核，确认 ND-2/ND-3(a)机制与声明字段矩阵/ND-4 四条腿/ND-5/ND-6/ND-1 的
死代码半边**均已闭合**，但抓到 2 项阻断（均为我的实现问题）：

| # | 项 | V8 证据 | 三次修复 |
|---|---|---|---|
| 阻断 1（P2） | `ResearchService.recompute_campaign` **仍未透传** `topics_dir` → 复核产物落到仓库默认目录 `research/topics` 而非服务配置的 topics_dir（既有测试也在往仓库里写 `T001_K3DS-课题`） | 真 campaign：`rematerialized_topics` 非空但 `svc_topics` 下 0 文件、产物出现在 `<repo>/research/topics/...` | 服务面补 `topics_dir=self.topics_dir` + 钉子（源码口径） |
| 阻断 2（P2） | ND-3 的修复**误杀 bucket_analysis**：`primary_horizon = horizons[0]` 被注入到**未声明**该字段的 bucket 模块 → 键集与省略形态不同 → 同 subject_key 的第二个分桶实验恒判 `similar_to`（父提交同 spec 是 ACCEPTED） | A/B 对照（`c9a006d` monkeypatch vs `2fad537`） | 现算条件改为 `"primary_horizon" in known`（模块感知）+ 钉子断言 bucket 不得出现该键 |
| P3 | 同族 falsy 逃逸仍在：`n_folds: 0` / `window_mode: ""` / `buckets: 0`（runner 用 `x or default`） | 逐一 ACCEPTED | 新增 `_FALSY_AS_DEFAULT` 归一 + 钉子 |
| P3 | 新增 lint 1 条（`recompute.py` 的 I001，来自新增的 `from pathlib import Path`） | ruff pair 对比 | 已修（ruff 与基线持平） |

**V8 已独立确认闭合**：ND-2（物化信封与 HTTP 逐键等价，10 键全等）、ND-3 机制钉子
（3 项变异全被抓住）与**全字段矩阵**（无字段出现"显式缺省被放行"）、ND-4 四条腿
（intake 三通道拒 + 既有实验 rerun/探针/晋升照常）、ND-5、ND-6、ND-1 的 (a)(c)(d)。

**V8 记录的既有卫生问题（非本轮引入）**：`tests/integration/test_review_k3ds.py`
构造服务时传了 `topics_dir=<db 旁路>` 但**复核路径**当时还不吃该参数，因此产物写进
仓库 `research/topics/`——阻断 1 修好后该路径也归位（`research/topics/` 在
`.gitignore`，无版本库污染）。

## 回归结果

- 全量：**1605 passed / 1 failed**（唯一失败仍是改动前即 flaky 的 Windows 临时文件用例，在父提交上同样失败）；修复过程中另有一次全量 **1606 passed / 0 failed**。
- 新增钉子：`tests/unit/test_loop_review_ds4f_r3.py` 11 项；
- ruff：与基线逐条对比新增 0 条（并把 `src/portfolio/service.py` 的存量 I001/PLC3002
  与 `src/research/recompute.py` 的 I001 一并修掉，属顺手收口）。
