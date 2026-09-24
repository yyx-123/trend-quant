# Deepseek 复审报告 R3（第三轮 · 复核上轮新 P1 与 8 项残留 + 本轮新增修复的独立验证）

> 复审日期：2026-09-24（本轮修改覆盖 16:49–17:59）
> 复审基准：`Deepseek盲审报告.md`（R1）、`Deepseek复审报告.md`（R2）、
> `docs/26-09-20-投研基建架构/`（架构稿/总体方案/后续TODO）。
> 复审身份：量化系统研究员 / 开发工程师 / 架构师 / QA（同一审查者，第三轮）。
> **只读声明：本轮未修改任何代码；实证均在进程内或临时库完成，生产库新表全程 0 行（已复核）。**

---

## 0. 总判定

**通过（PASS）。** 上轮我提出的新 P1 已按建议语义修复并经我三场景实证；
R2 的 8 项残留中 7 项已修、1 项（全局 holdout token）被判为有意不改且已文档化；
本轮（另一条评审线 GLM53F 驱动）新增的修复我逐项独立复核，未发现新缺陷。
全量回归 **1476 通过 / 1 失败**（唯一失败为改动前即存在的 Windows 临时目录 flake）。

| 检查面 | 结论 |
|---|---|
| R2 新 P1：启动收割误杀 `evaluating` | **已修**，语义与我的建议一致（§1，附三场景实证） |
| R2 残留 §4-1…§4-8 | 7 项已修，1 项裁决为"有意"+文档化（§2） |
| 本轮新增修复（ETF 板块幅度/IPO 分时代/live T-1/元模块转发/补跑哨兵/草稿隔离/小样本 t/方向口径/去重归一/`to` 字典） | **10/10 复核通过**（§3） |
| R1 六项 P1 有无回归 | 无——安全关键件（涨跌停、配对判定）我本轮**再次实证**（§4） |
| 全量回归 | 1476 passed / 1 failed（既有 flake），新增 31 项测试全绿 |
| 残留 | 仅 8 项 P3（建议项，不阻断）+ 1 项部署前检查（§5） |

---

## 1. R2 新 P1 的复核：启动收割不再误杀"跑完待确认"的实验

**修法（`src/research/lifecycle.py:171-177`）**：谓词收窄为
```sql
WHERE status = 'running'
   OR (status = 'evaluating' AND id NOT IN (SELECT experiment_id FROM research_verdicts))
```
正是我建议的语义：`running` 一律收割；`evaluating` 仅在**没有平台 verdict**
（pipeline 在取证途中死亡）时收割；已有 verdict 的 `evaluating` 原样保留。

**我的实证（进程内临时库）**：
```
收割: {'experiments': 2, 'engine_runs': 0}
  A(evaluating+有verdict) -> evaluating     ← 存活
  B(evaluating+无verdict) -> failed         ← 收割（进程死在取证途中）
  C(running)              -> failed         ← 收割（进程死亡孤儿）
A 确认成功 ✅                                 ← confirm_verdict 可用，结论不丢
```
钉子：`tests/integration/test_review_r3.py:104`（保留）与 `:127`（只收割孤儿）——
两个方向都有，避免了"只钉一半"。

---

## 2. R2 残留项的逐项复核

| R2 § | 事项 | 本轮落点（我核对过） | 结论 |
|---|---|---|---|
| §4-1 | 长窗口三注记只覆盖 backtest | 抽成 `_common.long_window_annotations(start)`（`:162-173`），backtest/event/bucket/distribution 四模块全接线（`backtest.py:636`、`event.py:337`、`bucket.py:253`、`distribution.py:97`） | ✅ 已修（+2 项集成钉子） |
| §4-2 | 垫片 120 天 < SMA200 预热，静默丢样本 | 评估面板垫片 → **300 自然日**（`_common.py:60`）；tradability 前收垫片 → **250 自然日**（`tradability.py:118-120`，兼顾长期停牌复牌）；并新增**预热可见化警告** `regime_warmup_unknown(n 日)` / `context_filter_warmup_excluded(n 日)`（`event.py:343-347`） | ✅ 已修（超出我提的建议：把静默排除变成显式告警） |
| §4-3 | 配对检验未按日期对齐 | `backtest.py:538-543` 改为 `pd.concat([...], axis=1, join="inner")` 后取交集再配对 | ✅ 已修 |
| §4-4 | parity 归因未实现/未测、价差无幅度校验、长度截断 | `parity.py` 新增 `ATTRIBUTION_WHITELIST` + `attribute_diffs()`：违反卡控 → `violations`；`tail_slippage` 要求**价差在尾盘滑点界限内**（`:216-223`）；`count_mismatch` 仅归 limit_card（卡控日）；NAV 长度不等 → `length_mismatch` 进 `unexplained`（验收判据"unexplained 非空即失败"）；`test_engine_parity.py` + `test_review_r3_unit.py` 均调用 | ✅ 已修 |
| §4-5 | 全局 token 会被无关实验抢先消费 | 裁决为**不改**：只自动带出**绑定本实验**的 token，全局 token 须显式透传（`pipeline.py:71-86`，含决策注释）；钉子 `test_review_r3.py:190/216` 双向验证 | ✅ 已裁决+文档化（我给一条 UX 建议，见 §5-4） |
| §4-6 | 方向一致率是"多数占比"、效应量混单位 | `conclusion.py:89-97` 改为**与假设（`spec.expect`）同向的占比**；`:136-141` 增加 `by_module` 分族中位数，median 明示"跨族混合口径"；钉子 `test_review_r3.py:277` 用 [−0.2,−0.1,+0.05] 区分新旧口径（1/3 vs 2/3） | ✅ 已修（钉子设计正确） |
| §4-7 | `REPORT.md` 证据截断无标记 | `topic_files.py:93-94` 截断处追加"…（截断，完整证据见 report.json）" | ✅ 已修 |
| §4-8 | CLI 无 `--db`；`BACKTEST_RULES` 死别名；L4→L2 类型 import | `research_cli.py:38` 增 `--db`；`BACKTEST_RULES` 已删；`module_gate.py` 仍引 `engine.models`（仅类型构造） | ⚠️ 2/3 已修，剩余 1 项见 §5-6 |

---

## 3. 本轮新增修复的独立复核（10 项）

1. **ETF 涨跌幅跟随标的板块**（`tradability.py:40-58`）：科创板 ETF（沪 588xxx）±20%；
   名称含"创业板/科创"的 ETF ±20%；其余 ETF ±10%。我的复跑：
   `600519.SS → 11.0`、`510300.SS(沪深300ETF) → 11.0`、`159915.SZ(创业板ETF) → 12.0` ✅
   （**这是我 R1/R2 都没抓到的问题**，见 §6 诚实披露；修法正确，残留见 §5-1）
2. **新股豁免天数分板块/分时代**（`tradability.py:63-80`）：ETF 首日即受限；科创板 5 日；
   创业板 2020-08-24 起 5 日（此前首日）；主板 2023-04-10 起 5 日（此前首日，44% 上限按
   no_limit 近似并注明）。钉子 `test_review_r3_unit.py:114`（ETF 首日有限制）✅
3. **live 止损状态重建排除当日 provisional bar**（`live.py:88-105`）：末根为盘中合成 bar 时
   状态截至 T-1，与回测侧"用 T-1 状态判定"同口径；棘轮按持仓期逐日累计 max 重建 ✅
4. **元模块 `prepare_with_gateway`/`prepare` 逐子转发**（`execution.py:183-188`、`:200-209`）：
   组合腿不再静默零信号（GLM53F-P1-2）；成员参数过 schema 校验（`execution.py:170-180`）；
   钉子 `test_review_r3_unit.py:130` 断言顶层 `hasattr` 命中 ✅
5. **判定器小样本 t 临界**（`verdict_rules.py:43-54,149`）：`t_crit = max(1.645, t_{0.95}(n_pairs−1))`，
   Cornish–Fisher 展开。我数值校核：df=5/10/20/30/60/100/2500 与标准表误差 <0.001 ✅
   （df<5 虽不准，但 `paired_sharpe_comparison` 在 n_pairs<30 时返回 None → 不可达）
6. **冻结顺延后当日一次性补跑哨兵**（`jobs.py:127-160,210`）：解冻即补跑（最长再等 2h），
   并写 `job_runs(status=deferred_backtest_running)`——我 R2 提的"顺延=静默饿一整天"闭环 ✅
   钉子 `test_review_r3.py:495`
7. **模块草稿装载失败隔离**（`modules.py:250-262`）：单条 reviewed 草稿依赖漂移不再拖垮全站启动
   （我 R1 转述的同类风险）✅ 钉子 `:475`
8. **去重签名类型逃逸**（`experiments.py:60-76`）：`"2.0"≡2.0`、`"true"≡True` 归一化后再比较，
   堵住"换个 JSON 类型就绕过两档检测"的通道 ✅
9. **`to` 字典形态在 resolve 端同口径支持**（`strategy.py:175-200`）：intake 接受而 resolve 不认会
   白烧 attempt_index + 落脏 failed，已对齐 ✅
10. **结论定量摘要的定论语义**（`verdict.py:99-105`、`api.py:115`、`research_ledger.py:77`）：
    复核稿（`supersedes` 非空）不进定论展示位 ✅

---

## 4. 安全关键件无回归（我本轮再次实证）

| 件 | 方法 | 结果 |
|---|---|---|
| R1-P1-1 涨跌停窗口口径 | 三形态复跑（老股/新股/ETF 板） | 全部正确 ✅ |
| R1-P1-6 配对判定门 | 用仓库代码重跑 MC（120 次/点） | 零边际 ρ=0.98 → **2.5%**；ΔSR=0.2 → **95%**；ΔSR=0.3 → 100% ✅（t 临界修正后略有提升） |
| R2-P1 启动收割 | 三场景（有 verdict/无 verdict/running） | 语义正确、确认可用 ✅ |
| 全量回归（venv） | `pytest -m "not slow"` | **1476 passed / 1 failed**（既有 Windows flake `test_rejects_second_add_job_while_running`） |
| 生产库卫生 | 只读查询新表 | `engine_runs/research_experiments/research_verdicts/holdout_tokens` 均 **0 行**（测试未污染实库）✅ |

---

## 5. 残留（P3，建议项，不阻断交付）

1. **ETF 板别判定依赖名称关键词**（`tradability.py:52-56`）：名称不含"创业板/科创"但实际跟踪
   创业板/科创板指数的 ETF（或以"创业""双创"等命名者）会退回 ±10%——**静默**。
   建议：①资产元数据补 tracked_index 字段（数据线，一劳永逸）；②过渡期在
   `asset_type=etf` 且名称缺关键词时写一条 `etf_board_heuristic` 警告（与"可交易性标注唯一出口"
   的诚实原则一致）。
2. **注册制前的"首日 44% 上限"按 no_limit 近似**（`tradability.py:71-72`）：首日实际有 44% 带
   而系统按"无限制"处理（方向偏乐观）。已注明，建议在 verdict/report 侧对"含 2023 年前上市标的"
   的 run 附一条注记（与长窗口三注记同机制）。
3. **`_t_critical_95` 在 df<5 时不准确**（Cornish–Fisher 展开局限）：当前被 `n_pairs≥30` 挡住，
   不可达；建议注释里写明"df<10 仅供兜底，调用方保证 n≥30"。
4. **全局 holdout token 的 UX**：语义已裁决（须显式透传），但页面 `/holdout/grant` 的
   `experiment_id` 仍可留空 → 发出来的 token 对队列路径无效，实验会以 `HoldoutError` 失败。
   建议：留空时给出提示文案（"未绑定实验的 token 需在 CLI/服务面显式传入"），或在错误信息里
   指引绑定方式。
5. **`research_promote_to_library` 在 MCP 上对 AI 会话必然失败**（human-only 门，`research_tools.py:183-199`）：
   工具仍暴露、错误以 `{"ok": false, ...}` 返回。功能上安全（门有效），但"暴露一个本通道不可用的工具"
   易误导 AI；建议工具文档置于描述首句（现已在 docstring 说明）或直接不注册。
6. **分层**：`research/module_gate.py:69,127,137` 仍从 L4 引 `engine.models`（仅构造探针类型），
   影响极小；建议顺手改经 `portfolio.service` 转发或把探针类型下沉。
7. **live 账户重建仍是手写近似**（`live.py:64-85`）：成本不含费用、现金按原始买卖价累计，
   与 §2.4"同一个引擎实例族"仅**决策函数**层成立（记账层是近似，dev log 已注明）。
   一期可接受；建议在清单/对账输出里带一条 `account_rebuild_approx` 注记，避免被当成精确账户。
8. **部署前检查（R2 已提，仍适用）**：`db.py` 的
   `CREATE UNIQUE INDEX IF NOT EXISTS idx_engine_positions_unique` 在存量 `engine_positions`
   有重复行时会让 `_init_tables` 抛错、应用起不来。本地该表 0 行（已复核），线上上线前建议先跑
   `SELECT run_id,date,symbol,COUNT(*) c FROM engine_positions GROUP BY 1,2,3 HAVING c>1`。

---

## 6. 我前两轮的遗漏（诚实披露）

本轮修复里有四项**我 R1/R2 没抓到**的问题，由另一条评审线（GLM53F）发现，我复核后确认它们是真问题：

1. **ETF 涨跌幅跟随标的板块**——我在 R1 明确把"板块幅度规则"列为"已验证正确"
   （当时只核了股票前缀 60/00/68/30，没核 ETF）；
2. **live 止损状态重建用了当日 14:00 provisional bar**——我在 R1 只审到"live 不驱动引擎"，
   没深入 `_rebuild_stop_state` 的当日 bar 语义；
3. **元模块不转发 `prepare_with_gateway` → 组合腿静默零信号**——我在 R1/R2 审的是单个模块接线，
   没测元模块顶层 `hasattr` 探测的边界；
4. **冻结顺延后当日无补跑**——我在 R2 指出"静默跳过无记录"，但没提"应当当日补跑"这一层。

这说明：**多评审线并行 + 每轮独立复算是有必要的**，单条线的"验证正确"清单存在盲区。
建议保持"≥2 条独立评审线 + 全部发现必须实证"的现行流程。

---

## 7. 结论与上线建议

**结论**：一期阶段 0–5 的基建在本轮之后达到"可交付"状态——
R1 的 6 项阻断、R2 的 1 项新 P1 全部修复且有可失败的钉子；1400+ 全量回归仅剩既有 flake；
台账侧（append-only/状态机/定论保护/收割语义/holdout 双向/token 绑定）经得起复核。

**上线前建议完成的三件事（都不是代码缺陷）**：
1. 部署前跑一次 §5-8 的重复行检查（唯一次高影响项）；
2. 用 `.venv/Scripts/python.exe` 跑验收（系统 Python 无 `mcp` 包会让 3 项 MCP 测试假失败，
   我 R2 已踩过）——建议把"必须用项目 venv"写进 `Makefile`/README；
3. 跑一次 `scripts/run_base_v1_sample.py` 并保留产物，作为阶段 2 验收的可回归证据
   （现有 slow 钉子在该产物存在时才断言）。

**关于残留**：§5 的 8 项均为建议级；其中 §5-1（ETF 名称启发式）与 §5-4（token UX）
影响日常使用，建议排进下一轮；其余可随运行期反馈处理。

---

## 附录：本轮锚点与取证

- **锚点（2026-09-24 17:59 修订）**：`src/research/lifecycle.py f01bf0c2…`、
  `src/research/pipeline.py 1f016888…`、`src/research/worker.py f82e150a…`、
  `src/research/evaluations/_common.py 7a6d9726…`、`src/research/evaluations/event.py 269fcf9c…`、
  `src/research/evaluations/backtest.py beecb8d3…`、`src/engine/parity.py 7034c1cb…`、
  `src/research/conclusion.py 24554fd6…`、`src/research/topic_files.py e6e4273f…`。
- **本轮新增测试**：`tests/integration/test_review_r3.py`（16）、`tests/unit/test_review_r3_unit.py`（15），
  合计 31 项，全部并入全量运行并通过。
- **命令**：`pytest -m "not slow"`（venv）→ `1 failed, 1476 passed, 24 deselected in 405s`；
  另单独跑过 `-m slow`（1 项，本机通过）。
- **实证脚本**：涨跌停三形态（股票/沪深300ETF/创业板ETF）、启动收割三场景、
  配对判定 MC（120 次/点）、`_t_critical_95` 标准表校核——均在进程内完成，
  临时库位于 `tempfile.mkdtemp()`，未写入仓库与生产库。
