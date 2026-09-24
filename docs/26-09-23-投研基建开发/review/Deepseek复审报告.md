# Deepseek 复审报告（第二轮 · 针对《Deepseek 盲审报告》6×P1 + 9×P2 的修复复核）

> 复审日期：2026-09-24
> 复审对象：修复轮后的工作区（最后修改 2026-09-24 14:44）
> 复审基准：`docs/26-09-23-投研基建开发/review/Deepseek盲审报告.md`（下称"上轮报告"）
> 以及 `docs/26-09-20-投研基建架构/`（架构稿/总体方案/后续TODO）。
> 复审身份：量化系统研究员 / 开发工程师 / 架构师 / QA（同一审查者，第二轮）。
> **只读声明：本轮未修改任何代码；所有结论来自我本人逐条复核与实证。**

---

## 0. 总判定

**有条件通过（1 项新引入的 P1 必须修，其余为上轮问题的残留 P3）。**

| 维度 | 结论 |
|---|---|
| 上轮 6 项 P1 | **6/6 已修复**，每项我都独立复核/实证（§1） |
| 上轮 9 项 P2 | **9/9 有对应修复与钉子**；其中 1 项的修法引入新问题（见下） |
| 上轮 P3 | 6 项已修（触发器守卫/阈值入配置/topics 路径/方向翻转/±2 邻居/唯一索引）；8 项残留（§4） |
| **本轮新引入** | **1 项 P1**：启动收割把等待人工确认的 `evaluating` 实验误判为失败（§3，已实证） |
| 回归 | 项目 venv 下全量 **1445 通过 / 1 失败**（唯一失败是改动前就存在的 Windows 临时目录 flake）；新增 43 项测试全绿 |
| 修复质量 | 好——修法对症、fail-loud 优先、附可失败的钉子；判定器口径的修复是本轮最大亮点（§6 我独立复算：假阳 3.0–6.5%、真实改进检出 93%） |

一句话：上轮六项阻断**全部真实修复**（不是"改了注释"），修复引入的新问题只有一处、但性质与上轮 P2-3 同类（**启动/批量动作误伤已完成的台账记录**），必须在交付前修掉。

---

## 1. 上轮 P1 的逐项复核（我亲自跑过）

### P1-1 涨跌停"新股无限价窗口"按运行窗口计 → **已修**

- 修法：`src/gateway/tradability.py:95-101` 改为按**交易日历**建轴
  （`cal_start = min(窗口起点, 最早上市日)`，`cal_ordinals` 由 `is_trading_day` 逐日生成），
  `:158-161` 的 `elapsed` 因此是"上市后第 N 个交易日"。
- 我的实证（直接调真实函数）：
  ```
  老股 600519.SS（上市 2001-08-27），窗口 2024-03-04 起：
     全部交易日 no_limit=False；带垫片前收时 limit_up=11.0（首日也有）
  新股 001234.SZ（上市 2024-03-04），窗口从上市日起：
     03-04..03-08 no_limit=True（无限价）→ 03-11 no_limit=False limit_up=11.0   ✅ 边界正确
  live 单日形态（dates=[2024-03-15]）：no_limit=False，limit_up=11.0           ✅ 不再恒失效
  ```
- 附注：窗口首日要出涨跌幅价，依赖 `load_raw_closes` 的 30 天垫片（生产路径有，实测成立）。
- 新增钉子：`tests/unit/test_review_gaps.py` 与 `tests/integration/test_review_k3ds.py` 相应扩展。

### P1-2 holdout 放行链路断裂 → **已修**

- 修法：`src/research/pipeline.py:33-36` 在调用方未传 token 时自动带出
  "绑定该实验或全局的未消费 token"（`_pick_unconsumed_token`，`:70-80`）；
  CLI 增加 `--token`（`scripts/research_cli.py:113`）。
- 端到端钉子：`tests/integration/test_review_k3ds.py:87-127`
  —— 无 token 触碰 holdout → failed；发 token 后同窗口跑通 → **token 已消费 + `holdout_touched=1` + warnings 带标记**。真实、非重言式。
- 残留（P3，见 §4-5）：全局 token（`experiment_id IS NULL`）会被**任意**触碰 holdout 的实验按 `ORDER BY id` 抢先消费；建议优先绑定 token、或把 token 与实验在入口绑死。

### P1-3 walk_forward 必崩 → **已修**

- 修法：`src/research/evaluations/backtest.py:362-373` 拼接序列改用**真实日期**
  （`stitched_dates.extend(fold_dates)`），不再是 `oos-{i}`。
- 钉子：`tests/integration/test_review_k3ds.py:133`（端到端不崩）。
- 我复跑 `pd.to_datetime` 场景：旧写法必抛 `DateParseError`（上轮已实证），新写法不再触碰该路径。

### P1-4 `context_filter` 被静默忽略 → **已修（实现而非拒绝）**

- 修法：`src/research/evaluations/event.py:97-102` 入口校验规则取值；
  `:196-213` 逐日算 benchmark 的 `close vs SMA200` regime 掩码；`:236` 用它过滤事件样本。
- `transition_matrix` 改为 **fail-loud**：`event.py:245-248` 抛
  `NotImplementedError(...勿静默忽略)`。
- 钉子：`tests/integration/test_review_k3ds.py:156-182` 断言"带条件的事件数 < 无条件事件数"
  （真过滤），另有 `event_side=exit` 可达性钉（`:184`）。
- 残留（P3，见 §4-2）：`cf_mask` 与 regime 标签共用 120 天垫片，而 SMA200 需要 200 根 bar
  → 窗口前 ~118 个交易日的条件/regime 判定不可用（事件被静默排除，无警告）。上轮 P3-9 同源，未修。

### P1-5 策略入库门 `NameError` → **已修**

- 修法：`src/research/api.py:22` 新增 `from research.ledger import loads`（`:245` 使用）。
- 钉子：`tests/integration/test_review_k3ds.py:297`（确认实验 → 晋升成功 → 版本带血缘）；
  `tests/integration/test_critical_paths.py:96` 断言 MCP 侧已暴露 `research_promote_to_library`。
- 即"决策 8 唯一的门"现在**既可用又可调用**。

### P1-6 `confirmed` 判定门的统计口径 → **已修，且修得对**

- 修法：新增 `src/research/stats/paired.py`——对**日收益差序列**做检验
  （差序列 Sharpe、配对 t、环形块 bootstrap 95% 带、差序列 DSR）；
  `src/research/verdict_rules.py` 的判定改为 `配对 t ≥ 1.645 且 DSR(diff) > 0`
  （PSR 降级为展示件），阈值可由 `app_config: research.rules.*` 覆盖
  （`load_rules`，`:30-43`；调用点 `backtest.py:648` 已接）。
- 我的独立复算（用仓库自身代码，200 次/点，n=2500≈10 年，基准年化 Sharpe 0.8）：

  | 真实改进 ΔSharpe（序列级） | ρ=0（独立两臂） | ρ=0.5 | ρ=0.98（单槽现实） |
  |---|---|---|---|
  | 0（零边际，应 ≈5%） | **4.0%** | **6.5%** | **3.0%** |
  | 0.1 | — | — | 48% |
  | 0.2 | — | — | **93%** |
  | 0.3 | — | — | 100% |

  对照上轮：同样构造下 ΔSharpe=0.2/ρ=0.98 的 confirmed 概率是 **0%**，零边际假阳 12.3%。
  现在是**校准正确**的配对检验，且随相关性不再失真。
- 钉子：`tests/unit/test_paired_verdict.py`（7 项，含解析 t 值对照、零边际 ≤5%、门条件逐项可失败、同种子可复现、样本不足返回 None）。
- **一处口径澄清（非缺陷）**：该测试文件的说明把它自己的 `ΔSharpe` 定义为**差序列**的年化 Sharpe
  （故 "0.2 → t≈0.63 不可达" 成立，诚实检出下限 ≈0.5）。而上轮验收行里我写的"ΔSharpe=0.2"
  指**策略序列级**改进（0.8→1.0），对应差序列 Sharpe≈1.0 → 93%。两者不矛盾，
  建议开发日志把这行口径写明（"序列级 vs 差序列级"），避免后续误读为"验收未达成"。

---

## 2. 上轮 P2 的逐项复核

| # | 上轮问题 | 本轮修法（我核对过的落点） | 钉子 | 结论 |
|---|---|---|---|---|
| P2-1 | 重复买入静默覆盖持仓 | `engine/engine.py:66-70` 已有持仓 → `EngineError`（fail-loud，引 §5.4.2） | `test_review_k3ds.py:347`；我实测：第二次买入抛 `EngineError`，持仓/现金不再错账 | ✅ 已修 |
| P2-2 | 停牌日 tail 离场炸 run | `engine/engine.py:129-140` `bar_close is None` → `unfilled(suspended)`（走 §4.2 语义），`backend` 不再抛 | `test_review_k3ds.py:220`（单测）+ `:244`（集成：停牌 5 日中 time_stop 到期，run 完成且落 `unfilled(suspended)`）；我实测通过 | ✅ 已修 |
| P2-3 | recompute 抹掉已确认结论 | `verdict.latest_verdict` 改为 `ORDER BY (supersedes IS NULL) DESC`（`verdict.py:85-96`）→ 复核稿不劫持定论位；复核 verdict 自动落定 `final = suggested` 并标 `confirmed_by='platform'`（`recompute.py:110-118`）；且补了 CLI + 服务面入口（`research_cli.py:140`、`api.py:277`） | `test_review_k3ds.py:412`（定论不被劫持）+ `:469`（仅 human） | ✅ 已修 |
| P2-4 | 入口不校验参数域/`from` | `evaluations/backtest.py:88-105` 逐槽 `validate_params`；`:108-134` `diff.from` 与 base 实际模块一致性校验 | `test_review_k3ds.py:576`（越界参数 + from 不符均入口即拒） | ✅ 已修 |
| P2-5 | 长窗口三注记未进实验路径 | `evaluations/backtest.py:613-617`：窗口起点 < 2020 时自动追加 coverage/fee-era/survivorship-weighting 三条 | `test_review_k3ds.py:604` | ⚠️ 部分（event/bucket/distribution 仍未带，§4-1） |
| P2-6 | 冻结门旁路 / 静默顺延 / 永久 running | `core/jobs.py:143-166`：**force 同样受冻结门约束**且顺延写 `job_runs(status=deferred_backtest_running)`；`research/lifecycle.py:157-177` 新增启动收割；`worker.start()` 调用（`worker.py:62-68`） | `test_critical_paths.py:121`（真调用 job 函数 + job_runs 断言） | ⚠️ 已修但**过度**：收割谓词含 `evaluating` → 新 P1（§3） |
| P2-7 | 9 条"删掉也全绿"的关键路径 | 8 条补齐：冻结门（critical_paths:121）、`trend_score_cross`（module_behaviors）、plateau+判定规则（critical_paths:202、paired_verdict）、MCP 工具（critical_paths:75）、CLI（:101）、live T+1（:166）、walk_forward（k3ds:133）、v1 sample 验收（`@pytest.mark.slow`，本机通过）；另新增 `test_module_behaviors.py` 覆盖 24 个此前零覆盖的插槽模块 | — | ✅ 实质修复 |
| P2-8 | 四处假钉子 | `test_review_ds.py:116` 改 `delta_turnover != 0.0`；`:146` 改 `gate_rejections["slot_limit"] > 0` | — | ✅ 已修 |
| P2-9 | parity 归因白名单未实现 | `parity.py:177-206` 新增 `ATTRIBUTION_WHITELIST` + `attribute_diffs()`（含 `unexplained` 桶）；预热边界 `i >= warmup_bars`（`:90-91`）已与旧栈一致（上轮是 `warmup_bars-1`，差 1 根） | — | ⚠️ 部分（§4-4） |

---

## 3. 本轮新引入的问题（**P1，必须修**）

### 新 P1：启动收割把"已完成、等待人工确认"的 `evaluating` 实验判死

**现象**：`mark_interrupted_research_runs` 的谓词是
```sql
UPDATE research_experiments SET status='failed', error='interrupted: process died (startup sweep)'
WHERE status IN ('running', 'evaluating')          -- src/research/lifecycle.py:169
```
而 `evaluating` 按详设 §6.4 是**合法长驻态**：平台 verdict 已生成、等人工/AI
`confirm_verdict` 写 reasoning（烂尾巡检也只对超过 7 天的提醒，不自动关闭）。
该函数由 `ResearchWorker.start()` 在**每次 app 启动**时调用（`worker.py:62-68`，main.py 生产路径必走）。

**我的实证（临时库，进程内完成，无落盘污染）**：
```
重启前: evaluating | verdict 已生成: inconclusive
收割:   {'experiments': 1, 'engine_runs': 0}
重启后: failed
确认失败: LifecycleError experiment must be evaluating to confirm verdict (status=failed)
```
即：**一次普通重启（部署、重启、机器重启）会把所有"跑完待确认"的实验变成 failed，
且平台 verdict 永远无法确认**（`confirm_verdict` 要求 `status=='evaluating'`，
`_TRANSITIONS` 无 `failed → evaluating`）。后果：结论从台账消失、课题量化摘要把它计入失败、
DSR 的尝试计数被污染，人工只能手改 SQL 才能救回。

**根因**：修 P2-6 时把"进程死亡遗留的中间态"等同于"所有中间态"，没有区分
`running`（进程内计算中，死亡即失败）与 `evaluating`（无计算在飞，等人工）。

**建议修法**：`running` 一律收割；`evaluating` 仅在**没有平台 verdict**时收割
（`LEFT JOIN research_verdicts v ON v.experiment_id=e.id WHERE v.id IS NULL`），
或改为 `WHERE status='running' OR (status='evaluating' AND 无 verdict)`。
钉子：造一个"evaluating + 有 verdict"的实验 → 收割后仍为 evaluating 且
`confirm_verdict` 可成功；再造一个"evaluating + 无 verdict"（模拟 pipeline 中途死）→ 收割为 failed。

---

## 4. 残留问题（P3，均为上轮已记录项的未修部分或本轮新发现）

1. **长窗口三注记只覆盖 backtest**（`evaluations/backtest.py:613-617`）：
   `event_study`/`bucket_analysis`/`distribution` 的默认窗口就是 2015 起（sample 段），
   却仍只带通用 `survivorship_bias`。§6.6.4 的"必须带"应按窗口而非按模块生效。
2. **regime/条件掩码的 200 根预热不被垫片满足**：`_common.py:57` 垫 120 自然日（≈82 交易日），
   而 `regime_labels`/`cf_mask` 要 SMA200（`min_periods=200`）→ 窗口前 ~118 个交易日
   regime 标签为 `unknown`、条件掩码为 False，事件被**静默**排除（无警告、无计数）。
   建议：垫片提到 ~300 自然日，或对"被排除天数"出警告。
3. **`paired_sharpe_comparison` 的配对是位置对齐**（`backtest.py:528` 传入的两条序列
   各自 `dropna()`，未按日期 join）：同一窗口的两个 run 长度通常一致，但若基准腿
   因标的/数据差异少几天，配对会整体错位（差异进 `t_stat` 而不报警）。
   建议按日期取交集后再成对，或断言两侧日期一致。
4. **parity 归因仍是"半成品"**：`attribute_diffs`（`parity.py:180-206`）**没有任何测试调用**；
   且 `tail_slippage` 分支只查"同日同向同数量"，**不校验价差幅度**
   （注释写"价格差恰为尾盘滑点比例"）→ 任何价格差异都会被归入白名单，归因判据偏宽；
   `nav_divergence` 仍用 `zip`（`:169`），两序列长度不齐时截断无告警。
   建议：价格比校验 + 长度断言 + 一条"unexplained 非空即失败"的验收测试。
5. **全局 holdout token 可被抢先消费**：`pipeline.py:70-80` 的 `ORDER BY id LIMIT 1`
   使"发给实验 A 的全局 token"会被先运行的实验 B 用掉。
   建议：优先 `experiment_id = ?` 的绑定 token；或入口强制绑定。
6. **课题量化摘要的两处口径**（`conclusion.py:25-36, 89-92`）：
   `direction_consistency` 仍是"多数占比"（构造上恒 ≥0.5，配 `≥0.7` 门槛近乎恒真），
   不是 §6.4.2 的"与假设同向的占比"；`effect_sizes.median` 仍把 ΔSharpe、Δ均值(%)、
   q_spread 混在一个中位数里（数字不可解释）。建议按模块分组给中位数，或明示"混合口径"。
7. **`REPORT.md` 证据 JSON 仍截断 6000 字符且无截断标记**（`topic_files.py:102`），
   与 §6.5.0"一个都不许摘要化"冲突（`report.json` 完整，仅人读面残缺）。
8. **`scripts/research_cli.py:31` 仍无 `--db`**（直连默认库）；`run_base_v1_sample.py`
   已有 `db_path`。另 `module_gate.py:69,127,137` 仍从 L4 引 `engine.models`
   （仅类型构造，影响小）；`verdict_rules.BACKTEST_RULES`（`:27`）现为无引用别名（可删）。

（上轮 P3 其余项已修：verdict 触发器加"已落定 final 不可改写"守卫（`db.py:411-412`）、
阈值入配置、`topics_dir` 改绝对路径（`api.py:25`）、课题/verdict 方向翻转拦截
（`conclusion.py:133-140`、`base.py:53-56`）、`plateau_neighbors` 剔除选定值
（`verdict_rules.py:56-60`）、`engine_positions` 唯一索引、`is_reproduction` 入 DDL。）

**部署注记（低风险，建议上线前确认）**：`db.py:192-194` 新增的
`CREATE UNIQUE INDEX IF NOT EXISTS idx_engine_positions_unique` 会在**存量**
`engine_positions` 有重复行时于 `_init_tables` 抛错 → 该库启动失败。
本地生产库该表 0 行（我实测），故当前无风险；若线上已跑过本工作区早期版本，
上线前先查 `SELECT run_id,date,symbol,COUNT(*) ... GROUP BY 1,2,3 HAVING COUNT(*)>1`。

---

## 5. 测试与回归取证

- **全量回归（项目 venv，权威口径）**：`1 failed, 1445 passed, 24 deselected in 465s`。
  唯一失败 `tests/test_instruments_bulk_backfill.py::test_rejects_second_add_job_while_running`
  = 改动前即存在的 Windows 临时目录清理竞态（`PermissionError: [WinError 32]`），与本次无关。
  通过数较上轮（1402）净增 43，全部来自本轮新增钉子。
- **新增测试文件（43 项）**：`tests/unit/test_paired_verdict.py`(7)、
  `tests/unit/test_module_behaviors.py`(12)、`tests/integration/test_review_k3ds.py`(16)、
  `tests/integration/test_critical_paths.py`(6，含 1 项 slow 验收)。
  我单独跑过：单元 126 项、集成/API 73 项、slow 1 项，均绿。
- **环境澄清（避免误记）**：我用**系统 Python**跑全量时出现 3 个失败
  （`test_mcp_requires_bearer_token`、`test_mcp_invalid_token_401` 返回 404，
  以及 `test_freeze_gate_applies_even_for_force`）。原因：系统 Python **未安装 `mcp` 包**
  → `app/main.py:605-622` 走 "MCP package not installed – skipping /mcp endpoint" 分支
  → `/mcp/*` 无路由。用 `.venv/Scripts/python.exe`（mcp 1.29.1）复跑：28 项全绿。
  **这三项不是回归**，但说明"测试必须用项目 venv 跑"——建议在 `Makefile`/文档里写明。
- **修复轮的新测试质量**：抽查的 8 项都能在实现回退时失败（含真实数据/真实 job 调用/job_runs 断言），
  未再发现 `abs(x) >= 0`、`isinstance(x, dict)` 这类重言式。

---

## 6. 我确认修复有效的关键实证汇总

| 验证点 | 方法 | 结果 |
|---|---|---|
| 涨跌停无限价窗口 | 调真实 `compute_tradability`（老股/新股/live 三种形态） | 老股不再误判；新股第 6 个交易日恢复限制；live 出限价 ✅ |
| 重复买入 | 进程内双买 | `EngineError`，现金/持仓不再错账 ✅ |
| 停牌 tail 离场 | 停牌日 `bar_close=None` 卖出 | `unfilled(suspended)`，持仓保留 ✅ |
| 判定器校准与功效 | 用仓库 `paired_sharpe_comparison` + `suggest_backtest_verdict`，200 次/点 | 零边际 3.0–6.5%；ΔSR=0.2@ρ=0.98 → 93% ✅ |
| holdout 闭环 | 读代码 + 新增端到端钉子 | 发 token → 运行 → 消费 → 留痕 ✅ |
| 长窗口注记 | 读 `backtest.py:613-617` + 钉子 | backtest 路径已带三注记 ✅（其它模块见 §4-1） |
| 台账定论保护 | 读 `latest_verdict` / recompute / 触发器 | 复核稿不劫持定论；已落定 final 不可改写 ✅ |
| **启动收割** | **造 evaluating+verdict → 收割 → 尝试确认** | **被误判 failed 且不可确认 ❌（新 P1）** |

---

## 7. 交付建议（最小清单）

**必修（1 项）**
1. §3 的启动收割谓词收窄为"`running` 全收；`evaluating` 仅在无 verdict 时收"，
   并补两条钉子（有 verdict 的 evaluating 存活且可确认 / 无 verdict 的 evaluating 被收割）。

**建议本轮一并修（低成本、消除口径歧义）**
2. §4-2 垫片提到 ~300 自然日（或对被排除天数出警告）——这直接影响 regime 拆分与
   `context_filter` 的样本量（默认 sample 窗口下前 5 个月都在被静默丢弃）。
3. §4-3 配对前按日期取交集。
4. §4-4 parity：价差幅度校验 + 长度断言 + "unexplained 非空即失败"的验收钉子（阶段 1 判据）。
5. §4-5 holdout token 优先绑定；§4-1 三注记按窗口下发到全部评估模块。

**验收判据（与上轮同标准：自动化、能失败）**
- 每条修复附"删掉实现即失败"的测试；
- 修完后重跑全量（项目 venv）：应仍为"1445+ 通过 / 仅存量 Windows flake"；
- 启动收割的修复必须同时覆盖"重启不伤已完成的实验"与"进程死亡遗留仍被收割"两个方向。

---

## 附录：复审方法与锚点

- **方法**：先按上轮报告逐条定位修复点（读代码），对可执行项**亲自跑最小实证**
  （涨跌停三形态、引擎双买/停牌卖、判定器蒙特卡洛、收割对 evaluating 的影响）；
  再核查修复引入的新代码（`stats/paired.py`、`pipeline` token 选择、`jobs` 冻结门、
  `lifecycle` 收割、`db` 守卫与索引）；最后跑全量回归并逐个解释失败项的归属。
  本轮共排除 2 项**貌似发现、实为误报**的候选（MCP 权限/MCP 3 项测试失败 = 环境差异）。
- **锚点（2026-09-24 14:44 修订）**：`src/research/api.py 0f5fa9b1…`、
  `src/research/verdict.py 1e5eb339…`、`src/research/recompute.py 8ced7d48…`、
  `src/research/worker.py c6074198…`、`src/research/evaluations/backtest.py 0ec144a7…`、
  `src/research/evaluations/event.py 8553f7ec…`、`src/gateway/tradability.py c7f7372e…`、
  `src/portfolio/live.py 549e5952…`（未改）、`src/portfolio/backtester.py 049d3da5…`。
- **只读声明**：本轮未修改任何代码/模板/测试；实证均在进程内或临时库完成
  （`tempfile.mkdtemp()`），未写入 `data/trend_quant.db`，未在仓库留下文件。
  测试运行产生的 `__pycache__`/`.pytest_cache` 已被 `.gitignore` 覆盖；
  上一轮遗留的 `research/topics/T001_probe/` 已被开发侧清理（本轮 `git status` 确认无该目录）。
