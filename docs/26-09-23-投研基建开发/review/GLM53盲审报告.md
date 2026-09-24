# GLM53 盲审报告（投研基建一期，阶段 0–5）

> 审查日期：2026-09-24
> 审查者：GLM53（独立盲审，未阅读 review/ 目录下任何既有评审报告，仅依据三份设计文档与代码本身）
> 审查对象：工作区全部未提交改动（7 个存量文件修改 + src/{gateway,engine,portfolio,research} 四个新包 + 20 个新测试文件 + 2 个脚本 + 2 个模板 + DDL）
> 审查身份：量化系统研究员 / 量化系统开发工程师 / 资深架构师 / 资深 QA 四重角色
> 方法：通读《2026-09-20 投研基建架构设计》《2026-09-21 总体方案设计》《2026-09-23 后续TODO》《2026-09-23 开发日志》全文 → 逐文件审读新栈全部核心源码（gateway 5 件、engine 9 件、portfolio 14 件、research 24 件择要全覆盖）与存量文件 diff → 静态扫描分层违规 → 实机运行全量测试套件

---

## 0. 总体结论

**PASS（有保留）**。

- **方案一致性**：与架构稿 20 条决策、详设 L1–L4 各节逐条对照，核心语义（成交口径、T+1、止损收编口径、七插槽、日循环八条写死语义、台账纪律、holdout 工程、判定器全家桶、实盘薄版）**全部落实且未见擅自变更**。发现的偏离均为文档内部张力或边角语义，无一处动到"会改变研究结论方向"的机制。
- **正确性**：未发现 P1 级缺陷（即会导致错误结论入台账、资金/持仓语义错误、数据损坏、PIT 前视洞、append-only 被破的问题）。发现 5 项 P2（行为与文档偏差 / 性能 / 边界可达性问题）与约 12 项 P3。
- **存量影响**：存量文件改动克制且向后兼容；实跑全量回归 **1476 通过 / 1 失败 / 24 deselect（slow）**，唯一失败为改动前即存在的 Windows 临时目录清理竞态（`test_instruments_bulk_backfill`，存量文件本次未触碰，失败点在 tempfile 删除，与新栈零交集）。
- **测试与质量保障**：新增约 6400 行测试、覆盖判定器 golden 对拍、引擎位级 golden、新旧引擎 parity 白名单归因、关键路径钉子与五轮审查钉子；质量纪律显著高于常见自研水平。测试缺口集中在少数"组合语义 × 边界"交叉点（见 §5.3）。

分项判定：方案一致性 A−；代码正确性 A−；存量隔离 A；测试完备性 A−；统计判定器 A（golden 纪律到位）；工程卫生 A−。

---

## 1. 审查范围与方法补记

| 范围 | 方式 |
|---|---|
| 设计文档 4 份（3272 行） | 全文通读，建立逐条决策清单作为对照基线 |
| 存量文件 diff（.gitignore / main.py / jobs.py / scheduler.py / db.py / server.py / base.html，+657/−14） | 逐行审读，重点评估对存量行为的改写风险 |
| src/gateway/（panel/tradability/metadata/audit/service/live_overlay） | 全文精读（PIT 卡控、涨跌停推导、留痕） |
| src/engine/（models/matcher/fees/stops/account/engine/store/parity/profiles） | 全文精读（撮合语义、费用、止损口径、落库） |
| src/portfolio/（backtester/context/live/reports/service/library/strategy/registry/seed + 七插槽全部模块） | 全文精读 |
| src/research/（experiments/lifecycle/verdict/verdict_rules/holdout/conclusion/worker/pipeline/modules/module_gate/dsl/api/recompute/topics/sessions/runs/ledger + evaluations 5 件 + stats 4 件） | 核心件全文精读，辅助件结构化审读 |
| 测试 20 文件（6429 行） | 用例清单盘点 + 关键文件（stats/golden/parity/critical_paths）逐行抽查断言强度 |
| 分层铁律 | 静态扫描 `from data.` / `import data.` / `from rule_backtest` 全量出现点 |
| 回归验证 | 实机运行 `pytest -m "not slow"`（8 分 39 秒） |

---

## 2. 方案一致性核对

### 2.1 逐项符合清单（抽查实证，非仅看声明）

**架构稿层面：**

1. **分层铁律（决策 7/10）**：静态扫描证实 `engine/`、`portfolio/`、`research/` **零** `data.*` 直接导入（唯一例外在 `gateway/live_overlay.py`，L1.5 调 L1 合法）；L4 读 L2 存储经 `portfolio/service.py:24-49` 转发（load_fills/load_nav/load_positions/get_engine_run），未见跨级 import。
2. **成交口径（决策 1）**：信号 T 收盘计算、成交基准价 = T 收盘 × (1+基础滑点+尾盘滑点)（`engine/matcher.py:79,26`，`engine/fees.py:26,56`）；涨跌停/停牌 T 日卡控（`matcher.py:71-74,151-154`）。
3. **止损收编（决策 6）**：实际买入价基准（`account.py:29` entry_price=fill_price）、ATR 含当根（`stops.py` init 系列用 `atr_including_entry`）、持仓期 T 日判定用 T-1 状态（`stops.py:12-18` 日内路径口径写死于模块 docstring 与 `position_risk.py:130-134` 实现）；九个 position_risk 模块全部委托 engine/stops。
4. **实盘形态（决策 3）**：`portfolio/live.py` 只出清单不撮合；`evaluate_exits`/`select_entries`（`backtester.py:400-476`）为回测器与实盘运行器共享的同一条决策代码路径（`live.py:253-277` 消费）。
5. **无晋升门（决策 8）**：promote_to_library 血缘门 = verdicted + final confirmed + parent_version_id + experiment_id（`api.py:233-271`）。
6. **一期/二期边界（决策 19）**：未见因子 DSL 工业化、加仓 lot 账、数据线、meta-portfolio 等二期项的任何实现痕迹；`dsl.py` 仅信号表达式（架构稿为因子预留的位置未被僭越）。

**详设层面（择核心实证）：**

7. **L2 存储六表**（§4.1）：`db.py` `_RESEARCH_STACK_DDL` 与详设逐字段对齐，另补 `run_params_json`（详设 §5.6 run 级参数的落实）、`engine_positions` UNIQUE(run_id,date,symbol)。
8. **撮合语义**（§4.2）：整手对齐、现金不足逐手递减语义（`fees.max_affordable_quantity` 解析式直达且与逐手校验同口径，`matcher.py:88-95`）、T+1 sellable（`account.py:49-57` 在 begin_day 滚动——修掉了初版 settle 滚动漏洞）、止损触发先于阻塞判定（跌停未触发不记 unfilled，`matcher.py:208-213`）、止损成交不叠滑点（§4.2 成交可行性确认的字面落实）、跳空按开盘（`matcher.py:204,215`）。
9. **market profile 参数化**（§4.2.1）：`profiles.py` cn_stock 全参数化，fees/matcher/account 无写死数字（复核过常量仅存在于 profile 定义）。
10. **七插槽 + §5.14 清单**：universe 3 + signal 8 + rank 5 + sizing 5 + portfolio_risk 6 + position_risk 9 + execution 4 + any_of/all_of 元模块——数量与名称逐一对上；策略 YAML 格式、config_hash 内容寻址、版本不可变（库层触发器 `trg_portfolio_strategy_versions_no_update`）。
11. **日循环八条写死语义**（§5.4.2）：先卖后买（回测器两阶段结构）、边卖边买拒绝（`backtester.py:456-461` exited_symbols 排除）、失败不递补（无递补逻辑 + 专项测试）、止损优先（evaluate_exits 先止损后信号）、整仓全清（`matcher.py:157`）、不加仓（`engine.py:68-72` EngineError fail-loud）、空仓计息 1% 常量（`account.py:69`）、窗口默认 2015 起（C1 决议落地）。
12. **基准策略 v1**：`base_v1.yaml` 与详设附录 A 逐字一致（含注释与参数）。
13. **L4 台账纪律**：骨架五条入口卡控且 rejected_intake 留痕（`experiments.py:185-297`）；状态机非法转移拒绝 + 三终态；烂尾巡检只提醒不关闭；verdict 可降不可升（VERDICT_RANK）+ distribution 固定 inconclusive（`allowed_finals`）；append-only 触发器列白名单 + 已落定 final 库层不可改写；attempt_index 平台赋值且排除 rejected_intake 与复现；重复检测两档（完全一致硬拒 / 相似须确认 / 未知字段一律按可疑相似）；holdout 窗口 [2015-01-01, 2024-12-31] + token 原子消费（TOCTOU 防护）+ enforced 默认开 + 触碰必留痕；课题必选归属、concluded 前平台量化摘要（verdict 计数含失败 / 效应量中位数与按假设方向一致率 / 课题内 BH-FDR / 警告聚合）、四级分级可降不可升 + supported↔refuted 平级翻转拒绝、不做跨实验 p 值合并；recompute supersedes 指针只写新记录。
14. **判定器全家桶**：PSR/DSR/MinTRL 公式与 Bailey 2012/2014 一致（golden 用 NormalDist 独立复算 + 论文代数性质对拍）；配对口径（差序列 t + 区块 bootstrap 带 + 差序列 DSR）作为 confirmed 门，PSR 降为展示件——与开发日志 DS-P1-6 修复一致；小样本 t 临界用 Cornish–Fisher 展开不退回 z；PBO/CSCV、MC 交易序列 bootstrap、walk_forward 选项、head_to_head 均在。
15. **通道与看板**：MCP 11 个研究工具注册、grant_holdout 未暴露（`trend_mcp/research_tools.py` 工具清单核对）；CLI 9 子命令；台账看板在 AuthWallMiddleware 登录墙内（`main.py:475-476` 豁免名单不含 /research-ledger）。
16. **冻结门 A3**：`run_freeze` 计数型冻结；日更等待 30 分钟→顺延留痕（job_runs）→当日一次性补跑哨兵（解冻即补、上限 2h、幂等）；顺延后 main.py 的 post-update pipeline 不抢跑（`main.py:213-217`）；force（启动补偿）同受冻结门约束（`jobs.py:182-186`）。哨兵补跑内部再次经过 `daily_market_update_job` 自身的冻结门，不存在"解冻瞬间补跑与新 run 并发写"的窗口——这点我专门推演过，闭环成立。
17. **长窗口三注记**（§6.6.4）抽成共享件 `long_window_annotations` 并接入 backtest/event/bucket/distribution 四路；sample 验收产物（`data/research/base_v1_sample/comparison.md`）实存且与开发日志数字一致。
18. **开发日志声明的五轮审查修复点抽查**：抽验 12 处（ETF 板块幅度 `tradability.py:41-62`、元模块 prepare_with_gateway 逐子转发 `execution.py:182-187`、live 止损 T-1 重建 `live.py:87-164`、canonical_verdict 全接线、涨跌停新股豁免按交易日历 `tradability.py:145-158`、配对日期交集 join `evaluations/backtest.py:536-549` 等）均属实，未见"日志声称已修、代码未修"的虚报。

### 2.2 偏离与文档张力（无 P1）

| # | 级别 | 事项 | 证据 | 判读 |
|---|---|---|---|---|
| D1 | P3 | 新栈 import `rule_backtest.metrics.compute_summary`（3 处） | `portfolio/reports.py:21`、`evaluations/backtest.py:180`、`head_to_head.py:128` | 详设 §5.8 明文"复用 metrics.compute_summary"，但 §1 依赖规则又说新代码不得反向依赖并行分支——**文档自相矛盾，实现取了 §5.8**。工程上可接受（旧分支永不退役的假设下），建议后续把 compute_summary 迁 core/ 消除张力 |
| D2 | P3 | worker 并发位硬编码 2 | `main.py:307` `ResearchWorker(db, ..., max_workers=2)` | 开发日志遗留 P2 写"并发位默认 2（可配 2~4）"——实际无配置入口，改并发要改代码 |
| D3 | P3 | promote_to_library 权限口径 | `api.py:238-241` docstring 引"2026-09-24 用户决策：AI 全流程闭环自动晋升"，无 human 门 | 开发日志第九轮记录的是"仅 human + 待用户确认"。代码 docstring 表明用户已拍板放开，但**开发日志未回写这条最终决策**——留痕链有个缺口 |
| D4 | P3 | verdict_rules 口径注记与实现字段不完全对应 | `verdict_rules.py:23-26` 说"判定门的 ΔSharpe 指差序列年化 Sharpe"；实现中 `suggest_backtest_verdict` 读的 `deltas_vs_base.delta_sharpe` 是**策略序列级**差值（`evaluations/backtest.py:445-453`），差序列口径由 paired t/DSR 承担 | 判定逻辑本身自洽（序列级 Δ>0 作方向门 + 差序列配对检验作显著性门），但 docstring 会误导读者以为同一字段是差序列值；rejected 阈值 −0.2 实际作用在序列级差上。属文档口径问题，不是判定错误 |

**结论：未见"未声明的方案偏离"。四处张力三处是文档滞后/矛盾，一处是硬编码。**

---

## 3. 缺陷清单

分级定义：P1 = 会导致错误结论入台账 / 资金与持仓语义错误 / PIT 或 append-only 被破 / 存量业务损坏；P2 = 行为与文档偏差、边界条件下的性能或可达性故障、证据质量缺口；P3 = 卫生、一致性、边角语义。

### 3.1 P1：无

专门排查过的高危面全部关闭（逐项复核而非采信声明）：

- **DSL 前视洞**：`dsl.py:84-93` ref 的 k 只认非负整数字面量，`ref(close, 0-1)` 算术绕行在编译期被拒（AST 白名单无表达式作参数的通路——`ast.BinOp` 出现在 args 里虽不被节点白名单排除，但 k 参数类型检查 `isinstance(k_arg, ast.Constant)` 直接拒绝非字面量）✓
- **PIT 双保险**：面板限窗（PanelView 截到 upto）+ 受限句柄逐日重绑 as_of=当日收盘（`backtester.py:240-243`）；BoundGateway 的 as_of/data_version 覆写被拒并记 audit ✓
- **append-only**：触发器列白名单 + 已落定 final_verdict 库层不可改 + 状态机代码层强制 ✓
- **holdout token TOCTOU**：`UPDATE ... WHERE consumed_at IS NULL` + rowcount 校验原子消费 ✓
- **T+1**：begin_day 滚动、日结快照如实记 sellable=0、live 重建当日买入 sellable=0 ✓
- **不加仓静默覆盖**：EngineError fail-loud ✓
- **现金少记**：买入校验含预估费用，最低佣金场景由 while 循环兜底 ✓
- **复核稿劫持定论**：canonical_verdict（supersedes IS NULL 优先）在 conclusion/api/看板/下载端全接线 ✓

### 3.2 P2（5 项）

**P2-1 buffered_rotation 绕过行动门控（语义与 §5.11 偏差）**
`backtester.py:434-436` 在 evaluate_exits 中**无条件**调用 `exec_mod.rotation_policy`；`select_entries` 的 `allows_action` 门（`backtester.py:451-452`）只管买入。`RebalanceBandExecution.rotation_policy` 自查了 `allows_action`（`execution.py:126`），但 `BufferedRotationExecution` 没有（`execution.py:96-111`）。后果：`buffered_rotation + action_gate: {freq: monthly}` 配置下，"踢仓"每日发生而"新开仓"每月一次——与详设 §5.11"行动门控决定哪些日子允许行动（新开仓/**轮换**/再平衡）"不符。修复方向：rotation_policy 调用前统一过 allows_action，或 BufferedRotation 内自查（与 RebalanceBand 对齐）。注：v1/benchmark 均不用 buffered_rotation，不影响已产出结论。

**P2-2 live overlay 的全历史逐标的载入（14:05 任务性能）**
`gateway/live_overlay.py:29`：对每个面板标的执行 `db.load_market_data(symbol)`（**全历史** qfq 载入）只为取最后一根的成交量，另有全池 `fetch_latest_quotes`。实盘 universe 为全 enabled 池（约 874 只）时，`generate_daily_list` → `build_panel(mode="live")` → overlay 会对 874 只各做一次全历史查询。每日一次、SQLite 本地可能仍在秒级~十秒级，但这是纯浪费（一条 `ORDER BY time DESC LIMIT 1` 或窗口查询即可），且拉长 14:05 清单产出时延。建议改为限量查询。

**P2-3 recompute campaign 无法复核 holdout 窗口实验**
`recompute.py:89-92` 调 runner 时固定 `holdout_token: None`；若被复核实验的原窗口触碰 holdout（enforced 默认开），`check_window` 抛 HoldoutError → 该实验进 failed 清单。且 `recompute_campaign` 无 token 透传参数（服务面/CLI 均无）。当前台账尚无 holdout 实验，问题未显化；但机制上"复核历史 holdout 实验"这条路是断的。修复方向：campaign 增加 token 入参或对 recompute 场景豁免（复核不是新取证尝试——但这本身是个口径决策，需用户定）。

**P2-4 ma_cross 族"首次可算即事件"伪交叉（事件质量）**
`signal.py:130-137`（MaCrossSignal.prepare）：`above = close > ma`（ma 为 NaN 时 False），`cross_up = (~prev_above) & above`。当 SMA(n) 首次满足 min_periods（新上市第 n 根 bar）或长停复牌后的首个有效 bar，prev_above 必为 False——若当时价在线上即产生 entry 事件。这不是"上穿"而是"状态成立"，会让新上市/复牌标的多出一批非交叉语义的候选（valid_days=5 窗口内有效）。MACD 因 NaN 参与比较得 False 无此问题。对以 ma_cross 为事件源的 event/bucket 实验有样本污染（轻微、方向偏多）。修复方向：cross 判定要求 prev 行 ma 与 close 双双有效。

**P2-5 walk_forward 折段按自然工作日切分**
`evaluations/backtest.py:308` `fold_days = pd.bdate_range(start, end)`——周一至周五自然日，**非 A 股交易日历**。节假日（春节/国庆各约一周）使各折的实际交易日数不均，后段折相对偏长；fold 级 Δ指标与拼接序列的季节权重轻微失真。修复方向：用 `gateway.metadata.trading_days`（或 core.calendar）切折。

### 3.3 P3（12 项，择期处理）

1. **bucket 与 event 的扫描桩不一致**：`bucket.py:141-151` `_ScanCtx.account = None`，而 `event.py:409-421` 提供 `_EmptyAccount`。`abs_momentum`（scan 内访问 `ctx.account.positions`）作为 bucket 的 signal_module 会通过入口校验但运行时 AttributeError → 实验 failed。fail-loud 不产假证据，但入口不该放进一个必然崩的 spec。
2. **创建型实验的 plateau 证据静默缺席**：`evaluations/backtest.py:581`（`if plateau_items and not is_creation`）与 `:642-645`（`plateau_evidence_absent` 警告仅对非创建型）——blank-base 七槽全填且带数值参数（如 risk_budget_pct）的实验既无高原补跑也无"证据缺席"警告；改进型换模块反而有警告。不对称，创建型首个参数选择完全无邻域检查。
3. **dict 形态 diff 的高原漏跑**：`_plateau_items`（`backtest.py:726-740`）只读 `item.get("params")`；`to: {module, params}` 字典形态（apply_diff 与 intake 均支持，GLM53F-P2-7 修复项）的数值参数不进高原补跑。
4. **课题 FDR 家族的 p 值口径混合**：`conclusion.py:99-106` backtest 贡献 `1−PSR`（正态近似推断），event/bucket 贡献经验 bootstrap p——异质口径进同一 BH 家族。设计允许"课题内全部实验"，但混合口径未在 note 中声明。
5. **HeatCapGate 死代码**：`portfolio_risk.py:70-72` `unstopped = ...` 赋值无任何用途（历史修复残留）。
6. **market_gate.breadth_mid 无形状校验**：`[0.3]` 这类残缺入参在 admit 时 IndexError → run failed（fail-loud 可接受，但 schema 层应拦）。
7. **分位舍入双实现**：`tradability.py:85-86`（Decimal HALF_UP）与 `:161-163`（floor(x×100+0.5+ε) 向量化近似）并存；一致性靠测试锚定，极端浮点下理论可差 1 分。
8. **by_freshness 用日历日**：`rank.py:17-21` `(ctx.date − day).days` 是日历日距离而非交易日——周末对所有候选一致漂移，跨周比较轻微失真（无碍公平性，口径未在 docstring 声明）。
9. **validate_params 整型静默截断**：`registry.py:166` `int(value)` 使 YAML 传入 `2.5` 变 2 而不报错（字符串 "2.5" 会报错——不一致）。
10. **R 倍数分母硬编码 1.5×ATR**：`backtester.py:535` round trips 的 R 分母写死 1.5（docstring 已声明"通用分母"），换用其他止损倍数的策略其 R 口径不忠实——作为展示件可接受，但报告未按策略实际 atr_mul 标注。
11. **promote 后配置解析用当前注册表**：`api.py:259-262` 晋升时重新 resolve diff——若 base 版本引用的模块后续被 retire（下架只禁新引用），resolve 抛错属正确行为；但若模块**参数 schema 收紧**，历史合法配置可能解析失败，晋升门被隐式收紧。低概率，记录在案。
12. **time_stop/无数据持仓的 live 边界**：持仓标的在面板中无任何 bar 时止损意图永不产生（bar None → not triggered），live freshness 闸门只兜当日——历史遗留持仓若标的数据缺失则静默失管（靠清单 caveats 不可见）。极边角，建议清单 caveats 中列出"无数据持仓"。

### 3.4 审查过并排除的疑点（记录在案，防后续复审重查）

- 冻结哨兵补跑与新回测并发：哨兵补跑走 `daily_market_update_job`，其头部自带冻结门（等 30 分钟/顺延）——无并发写窗口。
- 止损触发先于阻塞（跌停日未触发不落 unfilled）、触发价用 T-1 状态、跳空当日 open<stop 成交价=open、`0 < bar_open` 防脏数据——均正确。
- `latest_verdict` 的 `(supersedes IS NULL) DESC, id DESC` 与 `canonical_verdict` 同语义，四处消费点一致。
- 非交易日 force 补跑、调度 14:05 的 misfire_grace、worker 崩溃只收 queued 孤儿（不代判 running）、启动收割 evaluating 谓词收窄——与开发日志修复声明一致。
- `engine_positions` 双索引（UNIQUE + 普通）冗余但无害。
- `research_cli`/MCP/服务面三通道共用同一服务层，无逻辑分叉。

---

## 4. 存量业务影响评估

| 存量文件 | 改动 | 影响判读 |
|---|---|---|
| `db.py` | +482 行：新栈 DDL（19 表 + 14 触发器）+ `connect()` 公开别名 + `load_market_data_window_many`（`load_market_data_many` 变为其无窗口包装，行为不变）+ 引号风格统一 | 零破坏。`_init_tables` 内 `executescript` 建表幂等；存量表 DDL 无一字改动；批量读接口经全量回归验证 |
| `jobs.py` | +130 行：日更冻结门（force 同受约束）+ 顺延留痕 + 当日补跑哨兵 + live_daily_list_job | **行为变化点**：启动补偿在活跃回测期间会等待 ≤30 分钟再顺延——这是决策 A3 的有意变更，留痕完整（job_runs `deferred_backtest_running`），且有测试钉住 |
| `main.py` | +35 行：worker 随 lifespan 启停（测试模式禁用）、research_ledger 路由、deferred 分支 | lifespan 收尾 `worker.stop()` 防御化（FakeThread 无 join 已处理）；worker 启动带 `submit_all_queued` 补偿与启动收割 |
| `scheduler.py` | +13 行：14:05 live 清单任务 | 未配置部署策略时 `skipped_no_deployed_strategy` 跳过——对未启用实盘运行器的部署零感知 |
| `server.py` / `base.html` / `.gitignore` | 注册研究工具 / 导航链接 / 忽略 db-shm/-wal 与 data/research | 无风险；gitignore 补丁顺带修掉了 WAL 侧文件误跟踪的隐患 |

**回归实证**：`pytest -m "not slow"` → **1476 passed, 1 failed, 24 deselected（519s）**。失败项 `tests/test_instruments_bulk_backtest.py::test_rejects_second_add_job_while_running` 为存量测试文件（本次未修改），失败原因是 Windows tempfile 清理竞态（WinError 32），与新栈无代码交集，且开发日志已记录"git stash 对照证实改动前即存在"。存量 1248 项基线无回归。

**环境卫生**：生产库开发期产物已清（开发日志声明）；`data/research/` 进 gitignore；未见新栈向存量表写入的任何路径。

---

## 5. 测试与质量保障评估

### 5.1 规模与结构

新增 20 个测试文件、6429 行，分层标记（unit/integration/api/slow）与项目惯例一致；全量含 slow 约 1500 用例。

### 5.2 质量抽查（重点：断言强度与"删掉实现是否仍绿"）

- **统计判定器 golden（test_research_stats.py）**：PSR 手算 worked example（z=1.5804→Φ≈0.9430 逐值断言）、PSR(SR̂=SR*)=0.5 代数性质、DSR 与 NormalDist 独立复算逐值对拍（abs=1e-12）、DSR 多重折扣单调、MinTRL↔PSR 互逆、BH 手算、PBO 构造性已知答案（占优矩阵 PBO≡0）——**这是全套件里最硬的一块**，符合详设 §8"判定器不过 golden 即阻断"的验收判据。
- **引擎 golden（test_engine_golden.py）**：买卖持有 NAV 位级、T+1 当日不可卖、跳空/触及止损成交价、计息（252000 元一天恰 10 元）、heat 算例（含止损价高于现价计 0）——与详设 §4.4 算例同构。
- **parity（test_engine_parity.py + engine/parity.py 的 attribute_diffs）**：零卡控+零滑点位级一致；滑点为唯一差异时归因 tail_slippage；卡控日"新引擎不得成交对应方向"的机器判据 + unexplained 非空即失败——归因白名单真的有牙。
- **关键路径钉子（test_critical_paths.py）**：MCP 12 工具注册可调、CLI 9 子命令、冻结门 force 不绕过、live T+1、plateau 补跑真实落 research_runs、v1 sample 验收产物存在性。
- **五轮审查钉子（test_review_*.py，72 用例）**：扫描无 `assert True`、无裸 `pytest.raises(Exception)`；抽查 DS-R2-P2-7 形态复刻钉（无参 instruments()）、GLM53F-P1-5 concentration_cap 真断言——钉子普遍复刻真实调用形态（开发日志的流程教训确已内化）。

### 5.3 覆盖缺口（对应 §3 缺陷——缺陷与测试缺口互为镜像）

1. 无 buffered_rotation × action_gate 组合用例（P2-1 因此漏网）；
2. 无 ma_cross 新上市/复牌首根有效 bar 的事件语义用例（P2-4 漏网）；
3. 无 recompute holdout 场景用例（P2-3 漏网）；
4. walk_forward 折段均等性无断言（P2-5 漏网）；
5. live overlay 性能无观测点（P2-2）；
6. slow 标记的 10 年窗口验收默认 deselect——常规回归不含端到端长窗口（有产物存在性钉子兜底，但产物内容与当次代码的对应关系依赖人工触发）。

### 5.4 一个流程观察

五轮外部审查 + 修复 + 钉子的迭代闭环执行得很认真（日志与代码可互证）；但本轮盲审仍抓出 5 项 P2，且全部位于**跨机制组合语义**（门控×轮换）、**边角数据形态**（首根有效 bar、节假日切分）与**未被任何用例走过的路径**（recompute×holdout）——印证"审查面错位才能抓真问题"的经验，也提示下一轮补测应从组合覆盖入手，而非加深已有单点。

---

## 6. 专项审查结论

### 6.1 统计判定器（量化研究员视角）

- PSR/DSR/MinTRL 实现与 Bailey & López de Prado 2012/2014 公式一致，矩估计用样本校正型（pandas 同构），Φ/Φ⁻¹ 双实现（math.erf vs NormalDist）golden 对拍到位。
- **配对口径是本套判定体系的正确选择**：同窗同池单槽 diff 的 ρ≈0.98 场景，差序列 t + 区块 bootstrap 带是标准做法；PSR 降为展示件避免了"把基准已实现 Sharpe 当已知常数"的口径错误。诚实检出下限 ≈0.5（差序列年化）已在 docstring 与测试钉中写明，不制造"0.2 也能显著"的假象。
- 区块 bootstrap 用环形块、块长 √n、seed 固定位级可复现——台账可复审的前提成立。
- 保留意见（P3-4）：课题级 BH 家族混合 1−PSR 与经验 p 两类口径；plateau 1σ 检查对 2 个邻域点的 std 意义有限（ddof=1 时两点 std=|差|/√2，检查退化为"选定值偏离两点均值 1.4σ 之外"——仍然有用但弱）。
- PBO 的变体矩阵 = 主选+邻域探针，N 很小（2~5），CSCV 的 λ 分布粗糙——作为展示件可接受，勿当强证据读。

### 6.2 PIT 与数据门面（架构师视角）

as-of 强制单点实现（build_panel 端截断 + BoundGateway 绑定 + PanelView 限窗 + 逐日重绑）；涨跌停推导的除权基准修正（raw_close(t-1)/f_t）与项目因子语义自洽且与交易所口径一致；新股豁免按交易日历（与窗口解耦）；ETF 板块幅度（588xxx/名称含创业板·科创 ±20%）处理了 159915 这类真实易错点。信任模型（内置模块免探针、prepare 全面板预计算靠因果性+前缀稳定性探针兜底）在文档中诚实声明，符合"逻辑隔离非容器"的定位。

### 6.3 安全与权限（QA 视角）

登录墙覆盖台账看板与全部 POST（confirm/conclude/grant 走默认人工会话 + X-Requested-With CSRF 二道防线）；MCP 通道 Bearer 鉴权、grant_holdout/retire/recompute 均要求 human session；python 模块 exec 有 AST 预筛 + 受限 builtins + 白名单 import，且信任模型明示"防误伤非对抗恶意"——单人本地系统定位下合理。DSL 白名单无属性/下标/字符串方法，负位移编译期拒绝。

---

## 7. 建议处置清单（按优先级）

| 优先 | 项 | 建议 |
|---|---|---|
| 1 | P2-1 | rotation_policy 统一过行动门控（回测器侧一行或模块自查），补组合钉子 |
| 2 | P2-4 | ma_cross cross 判定加"前一行 MA 与 close 均有效"前置，补新上市/复牌钉子 |
| 3 | P2-2 | live_overlay 前收量改限量查询（性能） |
| 4 | P2-3 | recompute campaign 增加 holdout token 透传或豁免口径（需用户决策） |
| 5 | P2-5 | walk_forward 折段改交易日历切分 |
| 6 | P3-1/2/3 | bucket 扫描桩对齐 _EmptyAccount；创建型 plateau_evidence_absent 警告补齐；_plateau_items 支持 dict 形态 params |
| 7 | D2/D3/D4 | worker 并发位入 app_config；开发日志回写 promote 权限最终决策；verdict_rules docstring 口径修正 |
| 8 | 长期 | D1：compute_summary 迁 core，消除对并行分支的反向依赖 |

---

## 8. 复核声明

- 本报告全部结论基于对当前工作区代码的直接审读与实机测试（`pytest -m "not slow"`，1476 通过/1 存量 flake/24 deselect），未采信任何未经代码验证的日志声明；开发日志仅用作修复历史线索，所引修复点均回码核实。
- 报告未阅读 review/ 目录既有评审（盲审独立性）；如与既往报告结论重叠，属独立收敛而非引用。
- 未改动任何代码与测试。
