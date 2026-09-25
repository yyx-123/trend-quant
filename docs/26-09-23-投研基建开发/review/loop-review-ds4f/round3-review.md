# Round 3 审查报告（loop-review-ds4f）

> **状态：CLOSED（2026-09-25 闭合）**
> - 修复清单与验收记录：`round3-fixes.md`；验收代理 V7 **FAIL**（3 项阻断 + 3 项残留）
>   → 修复 → V8 **FAIL**（2 项阻断：服务面未透传 topics_dir；primary_horizon 误杀
>   bucket_analysis）→ 修复 → V9 **PASS**（含负向 A/B 对照；并抓到 1 项**既有**中等
>   缺陷：退役种子线会让所有回测 failed → 本轮一并修掉）；
> - 全量回归：1615 passed / 2 failed（失败全部为改动前即 flaky 的 Windows 临时文件用例）；
> - ruff：(file, rule) 集合与基线完全一致（新增 0 条）。
>
> 日期：2026-09-25
> 审查对象：commit `0a6bfe7`（Round 2 闭合后全量代码）
> 审查方式：独立审查代理 R3C 专项审计**平台/纪律/通道层**（前两轮集中在数值与引擎面），
> 全部结论经真代码路径实跑（真 CLI subprocess、真 service、真 MCP 工具函数、
> 真 FastAPI TestClient、真 DB 探针）；主审人复核并二次复现关键结论。

## 总体结论

**FAIL（4 项 P2 + 10 项 P3）**——全部为**新发现**（不含任何 R1/R2 已报项）。
前两轮把数值与引擎面审得很密，平台/纪律层留下了口径不一致：

- 研究纪律的三处**不变式**在"非主路径"上失守：晋升血缘（P2-1）、重复检测（P2-2）、
  退役线引用（P2-3）；
- 一处**能力缺口**（P2-4）：课题一旦关题，平台没有任何合法通路重跑同 spec。

---

## P2

### R3C-P2-1 晋升到同 config_hash 时静默复用他人版本行——实验血缘丢失

- 位置：`src/portfolio/library.py:109-116`（`_version_by_hash` 提前返回）+ `src/research/api.py:267-270`
- 事实（真 service 探针）：两个**不同**实验晋升到同一策略线且 resolved config 相同时，
  第二次晋升静默复用第一份版本行：
  ```
  promote#1 E0004 -> promo@1 experiment_id=E0004
  promote#2 E0005 -> promo@1 experiment_id=E0004   ← 第二份实验血缘未落库，调用方仍收 ok:True
  ```
  可达性：两实验窗口差 >10 天即不算重复（`_spec_similar` 放行）→ 各自独立
  `attempt_index`、各自 confirmed → 均可晋升。决策 8"入库是唯一的门、必须完整实验血缘"被绕过。
- 修复：同 hash 命中且既有行的 `experiment_id` 与本次不同 → `LibraryError`（fail-loud）；
  同 hash 同实验（幂等重放）仍允许。

### R3C-P2-2 重复检测被"显式写出平台缺省值"整体绕过

- 位置：`src/research/experiments.py:80-101`（只展开 window/universe）+ `:158-174`
- 事实（真 `propose_experiment` 探针）：省略 `initial_capital`/`window_mode`/`n_folds`
  与显式写出其缺省值，解析出的 run 参数**逐值相同**（`window_mode='static_holdout' /
  initial_capital=1000000.0 / n_folds=4`），但入口判为"另一个实验"
  （exact 不命中、similar 因键集不同直接 `return False`）→ 全部 ACCEPTED。
- 修复：`_expand_platform_defaults` 扩到**全部声明字段**，缺省值表
  `_MODULE_SPEC_DEFAULTS` 作单一真源（与各 runner 的 `spec.get(k, default)` 对齐）
  → "省略 ≡ 显式缺省"，exact 硬拒生效。

### R3C-P2-3 退役策略线仍可作新实验 base

- 位置：`src/portfolio/library.py:96-104`（退役只挡 `add_version`）vs 对照
  `src/research/modules.py:273-301`（模块退役同步摘注册表，"只禁新引用"）
- 事实（真探针）：`retire_strategy` 后 `resolve_experiment_config(退役版本)` 仍 **ALLOWED**，
  `propose_experiment(base=退役版本)` 仍 **queued**；而 `list_strategies()` 默认**隐藏**
  退役线 → 台账看不到这条线，实验却还在它上面生长。`retire_strategy` 在 `tests/` 中零引用。
- 修复：①`resolve_experiment_config` 拒绝退役线的版本（运行期）；②**入口**即拒
  （`portfolio_backtest` 的 `_spec_errors` 检查 base 所属策略线是否退役——避免烧掉一次
  attempt 才落 failed）。

### R3C-P2-4 已关课题的结论在平台上无法再执行/复现（三扇门全闭）

- 位置：`src/research/experiments.py:330-333`（重复拒绝文案）+ `:405-410`（rerun 卡 open topic）
  + `src/research/api.py:125-144`（append 仅 queued）
- 事实（真 service 探针）：
  ```
  rerun BLOCKED: TopicError: topic already concluded: T001
  新课题重提同 spec BLOCKED: IntakeRejected: duplicate_of: E0001（…请修改 spec 或用 rerun 复现）
  append BLOCKED: only queued experiments can move topics
  ```
  三句错误互相指向死路；唯一残留通道是未文档化的 `recompute --old X --new X`。
  这一缺口直接影响 R1-D-2（重跑已发布数字）与"数据重述复核"。
- 处置：**能力缺口 + 设计选择**，不在本轮静默改行为。本轮只把三处错误文案改成
  可行动（说明"复现请新建课题并显式声明为复现"）。**通路本身进待决策清单**
  （建议：允许在已关课题上创建 `is_reproduction` 实验，或提供
  `reproduce_into_topic`；保持"关题不带在途实验"不变式）。

---

## P3（本轮择修，其余记录在案）

| # | 位置 | 问题 | 处置 |
|---|---|---|---|
| R3C-P3-1 | `scripts/research_cli.py:157-181` | `rerun --run` / `recompute` 未包 `run_freeze.frozen_writes()`，与 propose --run / MCP / worker 口径不一（实测冻结态 `frozen=False`） | 修：两条通道补齐包装 |
| R3C-P3-2 | `src/core/jobs.py:141` + `tests/unit/test_loop_review_ds4f_r2.py:298` | 哨兵**钉子本身失效**：`_spawn_same_day_catchup` 在函数体内 `import threading` → `monkeypatch.setattr(jobs, "threading", …)` 永不生效，钉子实际起真线程并与断言竞态（实测假 Thread 被调用 0 次） | 修：`threading` 提为模块级引用（+断言钉子）；修复后实测假线程生效、调用/退避各 120 次有界 |
| R3C-P3-3 | `scripts/research_cli.py:180-181` | CLI recompute 丢掉 `skipped`（R1-P3-18 修好的"哪些目标没复核"在唯一通道不可见） | 修：输出 skipped |
| R3C-P3-4 | `src/research/topic_files.py:72-82` vs 路由下载 | 物化 `report.json` 只写 `latest["report"]`（仅 experiment_summary），而 HTTP 同名端点带 baseline/evidence/warnings/定论与理由 → 同一份"完整报告"两种内容 | 修：抽 `verdict.verdict_envelope` 单一真源，两处共用 |
| R3C-P3-5 | `src/research/recompute.py` | 复核 campaign 追加 supersedes verdict 后**从不重新物化**课题文件夹 → 审计文件夹与 DB 长期不一致 | 修：campaign 结束对受影响 topic 去重物化（失败只告警） |
| R3C-P3-6 | `src/data/storage/db.py:145-228` | engine **子证据表**（orders/fills/unfilled/positions/daily_nav）不受 append-only 保护（实测 `UPDATE engine_daily_nav SET equity=…`、`DELETE FROM engine_fills` 均 ALLOWED），与 `engine_runs` 的既有守卫口径不一 | 修：5 张子表补 `_no_delete` + 全列 `_no_update` 守卫（含存量库传播） |
| R3C-P3-7 | `src/data/storage/db.py:212-228` | 冗余索引：`idx_engine_positions_run` 与 UNIQUE 同列、`idx_engine_daily_nav_run` 与 UNIQUE 同列（持仓快照是最大子表） | 修：删索引 + 迁移路径 `DROP INDEX IF EXISTS` |
| R3C-P3-8 | `src/research/verdict.py:38-65` | `insert_platform_verdict` 不校验实验状态 → verdict 可挂 queued/rejected_intake/failed（不变式只在调用方各自保证） | 修：只允许 evaluating/verdicted（+钉子） |
| R3C-P3-9 | `src/research/verdict.py:157-165` | confirm 的落定 UPDATE 无行级守卫 → 并发/双击 confirm 若给相同 final，第二条会覆写 `reasoning`/`confirmed_by`（第一位作者的留痕丢失） | 修：`AND final_verdict IS NULL` + rowcount 拒（+钉子断言留痕不被覆写） |
| R3C-P3-10 | `src/trend_mcp/research_tools.py:174-181` | `research_get_experiment` 对不存在实验返回无 `error` 字段（同族工具都有）→ 客户端无法区分"不存在"与内部错误 | 修：统一错误口径 |

## 已验证无问题（本轮实跑核对）

1. **物化确定性**：同一 DB 二次 `materialize_topic` → 全部文件 sha256 逐位一致；内容全部
   来自 `sort_keys=True` 的落库 JSON 与确定性 SQL，无 rng/时间戳/集合序泄漏。
2. **随机基准可复现**：RNG 以 `(seed, date.toordinal())` 派生、成员序来自全序
   `ORDER BY … symbol` → 跨进程可复现；无未播种 RNG。
3. **verdict 选择器单一语义**：7 个消费点全走 `latest_verdict`/`canonical_verdict`
   （supersedes IS NULL 优先）→ 复核稿不劫持定论展示位。
4. **Web 台账通道**：含 `<script>` 的课题标题/问题/警告在台账页与详情页**都被转义**；
   404/409 正确；跨站向量全 403。
5. **CLI 退出码与错误路径**：非法 JSON/不存在实验等 → rc=1 + `{"ok":false,"error":…}`，
   无 traceback 穿出；`--verdict maybe` → rc=2。
6. **MCP 12 工具面**：恰好 12 个、`grant_holdout` 未暴露、全部经服务层、错误分类正确。
7. **DB 写入面与类型**：DDL 与写入逐条对齐；既有守卫触发器的 WHEN 清单与白名单式 UPDATE
   逐一匹配；`alloc_id` 并发自增无重复（实测 version 1/2）。
8. **attempt_index/DSR 输入不可轻易操纵**：计数按 `subject_key` 平台赋值，
   `is_reproduction`/`rejected_intake` 已排除，DSR 的 `n_trials` 真源一致。

## 待决策点（新增）

1. **R3C-P2-4 已关课题的"再验证"通路**（本轮最重要）：结论一旦关题，平台没有合法路径
   重跑同 spec。**建议**：允许在已关课题上创建 `is_reproduction` 实验（复现产物挂新课题
   亦可），并保持"关题不带在途实验"不变式——这也是 R1-D-2"重跑已发布数字"的前置。
2. **R3C-P2-3 退役策略线语义**：已按模块口径对齐为"禁新引用"；若刻意允许实验沿用退役线，
   需把语义写进设计文档（现 docstring 自相矛盾）。
3. **R3C-P3-6 引擎子表守卫的代价**：全列禁改意味着任何"回填/修正"子证据都要新开 run；
   本轮按 `engine_runs` 同制执行。若未来需要受控的回填通道，需显式加白名单。
4. **R3C-P2-2 缺省值单一真源**：已抽 `_MODULE_SPEC_DEFAULTS`；建议约定"新增 spec 字段必须
   同时登记缺省值"，否则此类"显式缺省即逃逸"会随新字段复发。
5. **R3C-P3-1 冻结口径**：CLI 已补齐 `frozen_writes()`；若刻意允许 CLI 豁免（进程内计数器
   不跨进程），需在设计文档写明。
