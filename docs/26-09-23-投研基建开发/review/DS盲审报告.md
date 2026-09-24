# DS 盲审报告：投研基建一期（阶段 0–5）实现审查

> 审查人：DS（独立盲审）
> 日期：2026-09-24
> 审查对象：git 工作区当前全部改动面——7 个存量文件（`.gitignore`、`src/app/main.py`、
> `src/core/jobs.py`、`src/core/scheduler.py`、`src/data/storage/db.py`、
> `src/trend_mcp/server.py`、`web/templates/base.html`）+ 新栈四包
> （`src/engine/`、`src/gateway/`、`src/portfolio/`、`src/research/`）+
> 13 个新测试文件 + 2 个脚本 + 2 个模板 + 1 个路由
> 审查基准：`docs/26-09-20-投研基建架构/` 三份文档
> （架构稿 = 约束 / 2026-09-21 总体方案设计 = 落地蓝本 / 2026-09-23 后续TODO = 分期边界）
> 方法：全量只读走读 → 独立数值复算（PSR/DSR/BH/PBO/费用/分位舍入）→ 实跑测试回归 →
> 9 组临时探针实证（脚本一次性、已删除、未入库）→ 生产库只读快照（`file:...?mode=ro`）
> **约束遵守声明**：本次审查未改动任何代码、未修改任何设计文档；未向任何数据库写入数据
> （只读打开 `data/trend_quant.db` 产生的空 `-wal`/`-shm` 已清理，主库文件字节数与
> mtime 与审查前一致：5,518,233,600 字节 / 09-23 20:59）。

---

## 0. 总体结论

**判定：FAIL（有条件）——5 项 P1 未决。**

按本项目审查章程（存在 P0/P1 未决即 FAIL），本次给出 **FAIL**，与前审三家
（A 方案一致性 R3 PASS、B 工程质量 R2 PASS、C 测试完备性 R2 PASS）结论不同。
差异**不是**因为前审判断有误（我复现了它们的全部关键实证：143 项新测试全绿、
DSL `ref(close,0-1)` 已拒、分层已收口、后台 running 孤儿行已修、live 持仓已并入取数集），
而是**审查面不同**：前审三家集中火力在「PIT 沙箱 / 分层铁律 / 代码执行安全 / 测试断言
有效性」四个面上；本次把重心放在「**交付物完整性、纪律机制的接线与真实性、运行期资源
行为、存量环境卫生**」四个前审未覆盖的面上，于是照出 5 项 P1 + 11 项 P2。

一句话概括：

> **引擎（L2）、数据门面（L1.5）、统计判定件（PSR/DSR/BH/PBO/bootstrap）本身质量高、
> 可直接用；组合层（L3）与投研层（L4）的机制件基本齐备；但「台账纪律链」有三处断线
> ——完整报告缺件（§6.5.0）、复现入口不存在（§6.4.1）、Δ换手是恒 0 假证据（§6.5.1）
> ——外加并发池在 AI 正常用法下会热自旋（实测 576 次/秒），以及重复检测会误杀
> plan 自己点名的合法实验形态。**

### P1 缺陷速览（详见 §3）

| # | 缺陷 | 面 | 证据强度 |
|---|---|---|---|
| P1-1 | §6.5.0「完整实验报告」未落地：`build_report` 全代码库零调用（死代码）；台账 report_json 缺 §5.4.3 多数指标与 §6.5.0 点名的风控拦截统计；页面再截断 4000 字符 | 方案一致性/交付完整性 | 代码走读 + grep 零调用 |
| P1-2 | 复现路径不存在：manifest 写明 `research rerun <id>`，但 CLI/MCP/服务面均无此入口；原样重提被判 `duplicate_of`；用 `allow_duplicate` 绕过则污染 `attempt_index` → 反噬 DSR | 方案一致性/功能 | 代码 + 探针实证 |
| P1-3 | `evidence.deltas_vs_base.delta_turnover` 恒为 `0.0`（假证据），真实换手实现存在但从未接入 | 证据真实性 | 探针：0.0 vs 0.009955 |
| P1-4 | ResearchWorker 调度器在同会话达并发上限时**紧循环**热自旋（实测 576 polls/s，期望 ~2/s），每次一个 SQLite 查询；MCP 通道天然命中该路径 | 资源/稳定性 | 探针实测 |
| P1-5 | 重复检测签名只读 `spec["diff"]`：event_study「同事件换条件」与 distribution「同指标换池/窗口」被判 `duplicate_of`，而详设 §6.5.2 明确这些是**另一个实验** | 纪律机制正确性 | 探针实证 |

---

## 1. 审查过程与证据留痕

| 动作 | 命令/方式 | 结果 |
|---|---|---|
| 静态盘点 | `git status` / `git diff --stat` / 逐包 `wc -l` | 存量改动 591+/14−；新栈约 11,000 行源码 + 2,000 行测试 |
| 方案对齐 | 通读架构稿（640 行）、总体方案（2,019 行）、后续TODO（133 行）、开发日志（295 行） | 建立条款级核对表（§2–§9、附录 A） |
| 前审复核 | 通读 review/ 下 7 份报告 | 确认前审 P1 修复有效；定位未覆盖面 |
| 新测试实跑 | unit 8 文件 / integration+api 5 文件 / stage5 | **105 passed + 25 passed + 13 passed = 143 全绿**（与 C-R2 记录一致） |
| 探针 A | `_nav_summary` vs `reports._turnover`（构造 nav+fills） | turnover 字段 0.0；真实换手 0.009955 → P1-3 |
| 探针 B | 自建 Panel + PanelView(day0) 读 `_panel` | `full_panel` 抛 PermissionError，但 `_panel.data["close"][-1] = 99.0`（未来值）→ P2-11 |
| 探针 C | 混合持仓 heat / 空仓 heat | 1/2 持仓有止损时 heat=1000（B 被静默忽略）；空仓 heat=None → P2-8 |
| 探针 D | TimeStopModule 逐日驱动（max_days=5） | 第 6 个交易日才离场 → P2-9 |
| 探针 E | `run_module_gate` 对「需 ctx.gateway 的 universe 模块」 | 干净模块 PASS；需 gateway 的模块被拒（AttributeError）→ P2-7 |
| 探针 F | 4 实验同会话入队 + 计数 `lifecycle.get_experiment` 调用 | **5 秒内 2881 次（576/s）**，期望 ~2/s → P1-4 |
| 探针 G | 创建型实验七槽全 `to: none` | 校验通过（空实验合法）→ P3-1 |
| 探针 H/I | 同事件换 regime / 同信号换特征 | H5 被拒 `duplicate_of`；bucket 因 subject_key 含 feature 而免于误杀 → P1-5 |
| 生产库快照 | 只读 URI 查新表行数 | 引擎/策略/审计表已有开发期产物（见 §5.3） |

> 探针脚本全部位于系统临时目录、用完即删，仓库无新增文件（已复核 `git status` 与审查前一致）。

---

## 2. 方案一致性审查（逐层对照）

### 2.1 已按方案落地的部分（确认项）

**L1.5 gateway（详设 §3）**——`panel.py` as-of 强制（`as_of` 无默认值、`end` 截到 as_of、
live 模式 provisional 标记）、`tradability.py` 停牌/涨跌停推导（板块幅度、除权基准
`raw_close(t-1)/f_t` 与项目因子语义自洽、分位 ROUND_HALF_UP、新股 5 日无限制）、
`metadata.py` 元数据透出、`audit.py` 留痕、`BoundGateway` 受限句柄（越权记
`violation:*` 并抛 `GatewayViolation`）。**与 §3 逐条对齐**。

**L2 engine（详设 §4）**——`Order/Fill/Position/Account` 字段与 §2.4 草样一致（含
`sellable_quantity` 的 T+1 一等表达、`unfilled_reason` 六枚举、费用分列）；
`matcher.py` 尾盘撮合（现金解析式递减、整手、`target_value`）、止损盘中撮合
（**先判触发再判阻塞**，与 docstring 一致——前审 C 的发现已修）、跳空按开盘、
停牌/跌停/T+1 三阻塞；`account.py` 日结 + 空仓计息（252 日基数、写死 1%）+
`rollover_t1` 在**次日开始**滚动（避免当日 sellable 虚增，前审发现已修）；
`profiles.py` `cn_stock` 参数化（佣金万 0.854/最低 5 元/印花税 0.05% 仅股票/整手 100/
T+1/1% 计息，全部来自 profile，无硬编码）；`store.py` 五表 + `engine_runs` 血缘
（实测生产库 run 行含 `engine_version=1.0.0`、`git_hash=d7d1487`、`data_version`、
`resolved_config_yaml`、`run_params`）；`parity.py` 薄适配对拍旧引擎。**与 §4 逐条对齐**。

**L3 portfolio（详设 §5）**——七插槽 + `any_of/all_of` 元模块 + §5.14 工具箱
（universe 3 / signal 8 / rank 5 / sizing 5 / portfolio_risk 6 / position_risk 9 /
execution 4）**数量与名称全部到位**；日循环五步、先卖后买、失败不递补、止损优先、
整仓卖出、不加仓、空仓计息、位级确定性（`run_seed = date.toordinal()`）均实现；
`strategy.py` 载入即拒非法配置（未注册模块/参数越界/元模块嵌套）；
`library + seed` 9 条策略（blank-base + base-v1 + 7 条 benchmark，幂等），
`base_v1.yaml` 与**附录 A 逐字一致**（`use_exit: false`、`hard_stop 1.5×ATR`、
`portfolio_risk: []`、滑点 0.002/0.001）；版本不可变由触发器强制。

**L4 research（详设 §6）**——骨架五条入口卡控（假设长度、模块注册、单变量、
引用合法、attempt_index 平台赋值）、状态机七态 + 非法转移拒绝 + 终态必填
`final_verdict + reasoning`、verdict 可降不可升（`VERDICT_RANK` + `allowed_finals`）、
append-only 触发器列白名单（内容字段拒改）、holdout 窗口 + 一次性 token（原子消费）、
重复检测、模块治理（DSL 白名单免测 + python 三门探针 + 人下架）、recompute campaign
（`supersedes` 只写新记录）、四评估模块 + 判定器全家桶（PSR/DSR/MinTRL/区块 bootstrap/
MC 带/FDR/PBO/head_to_head/walk_forward）、课题两级对象 + 平台量化摘要 + 文件夹物化、
通道无关服务面（§6.7 清单**全覆盖**，含 `append_experiment_to_topic`）、
台账看板 UI + MCP 10 工具 + CLI。

### 2.2 偏离与缺口（按严重度）

见 §3（P1）与 §4（P2/P3）。归纳成三类：

1. **交付物未接线**：报告完整件（P1-1）、复现入口（P1-2）、gate_log（P2-4）、
   manifest 的 data_version（P2-2）、晋升入库（P2-1）——代码写好了或设计写明了，
   但**没有一条链路把它端到端接通**；
2. **证据真实性**：Δ换手恒 0（P1-3）——台账上看起来是机器算的，实际是常量；
3. **机制与实际语义不符**：重复检测签名（P1-5）、模块门上下文（P2-7）、
   live 无新鲜度闸门（P2-5）、heat 静默（P2-8）、time_stop +1（P2-9）。

### 2.3 远期条款的落地口径（确认无偏离）

- **决策 19 一期边界**：二期项（因子 DSL 工业化、加仓 lot、数据线、meta-portfolio、
  组合优化 sizing、冲击成本、purged CV、分布式、Kelly、展示分支迁 L1.5）确实未开工；
  DSL 仅作为**模块治理手段**存在（`research/dsl.py` + 白名单），非因子生产，口径正确；
- **决策 20 / §6.4.2**：量化摘要四项齐备（verdict 计数、效应量中位数+方向一致率、
  课题内 FDR、警告聚合），分级可降不可升，明确不做跨实验 p 值合并——**唯一偏差**是
  FDR 只吃 backtest 的 p（P2-3）；
- **§6.6.4 生效时点**：`is_enforced` 默认 `"1"`（一行默认开），建设期/测试显式关闭 ✓
  与「阶段 5 起生效」一致；
- **§6.5.0 各模块证据规格**：四个模块的证据字段基本齐（per_horizon/路径统计/regime、
  bucket 表+单调性+随机带+Q 利差、distribution 分位+分组、backtest Δ+regime+plateau+
  stats+mc+pbo），缺口集中在「报告成品的汇总与展示」而非各模块证据本体。

---

## 3. P1 缺陷详述

### P1-1 §6.5.0「完整实验报告」未交付；`build_report` 是死代码

- **事实**：
  - `src/portfolio/reports.py:168 build_report()`——**全代码库零调用点**（grep `src/ scripts/ tests/ web/`，
    含其组成部分 `rolling_sharpe:63` / `drawdown_durations:79` / `return_distribution:98` /
    `cost_drag:114`，均只被 `build_report` 自己引用）；
  - 实验落库的 `report_json` 由 `src/research/evaluations/backtest.py:554-561` 组装，
    内容 = `{spec, resolved_config_yaml, window, engine_runs, evidence, warnings}`；
    `evidence` 含 Δ指标（6 个）、regime 拆分、stats、mc_bands、fee_total、交易数、
    `unfilled_by_reason`、plateau、pbo ——**缺**：全指标表（胜率/盈亏比/持仓天数/费用分列/
    滚动 Sharpe 6m·12m/回撤持续期/收益分布/成本拖累）、`round_trips` 明细、
    **`gate_rejections`（风控拦截统计）**、heat/exposure 曲线、槽位利用率、逐年分解；
  - 页面 `src/app/routers/research_ledger.py:103-104` 把 evidence/report
    `[:4000]` 截断，且无全量下载/导出端点 → **Web 面看不到全量报告**。
- **不符合的条款**：详设 §6.5.0「backtest 含全指标表 + regime 逐段数值 + 高原各点 +
  **交易/未成交/风控拦截统计**；一个都不许摘要化」；§5.4.3「业界通用指标的补充清单」
  （相对基准统计/滚动 Sharpe/回撤时长/收益分布/成本拖累/heat/槽位利用率/round trips）；
  §5.2.5「被风控拦下的候选…落 run 日志」（`gate_log` 生产于
  `portfolio/backtester.py:315` 并被测试断言，但**在 L4 组装时被丢弃**）。
- **影响**：台账的核心承诺是「AI 能审计前人实验」；现在读台账的 AI 看不到
  regime 逐段以外的绝大多数明细，也看不到风控拦截分布——**报告完整性这一条护栏事实性缺失**。
  同时 `reports.py` 数百行成品代码无人调用，属"实现了但没接线"。
- **修复方向**（低成本）：`_assemble_result` 里改为调用
  `portfolio.service.build_run_report(run_id)`（L4→L3 转发，符合分层），把 `build_report`
  的输出整块塞进 `report_json`；`gate_log` 随 `run_backtest` 返回值透传进 report；
  页面加「下载 report.json」端点或在详情页直接渲染结构化明细。

### P1-2 复现入口不存在，且被重复检测与尝试计数反噬

- **事实**：
  - `src/research/topic_files.py:139` 的 `manifest.json` 写着：
    「复现 = spec 本身：`research rerun <experiment_id>`（声明式实验）」；
  - 但 `scripts/research_cli.py:38-62` 只有 6 条命令（topics / propose-topic /
    propose-experiment / ledger / confirm / conclude）；MCP 工具 10 个无 rerun；
    `ResearchService`（`research/api.py` 全部公开方法）无 rerun → **该命令不存在**；
  - 用 `propose_experiment` 原样重提会被 `find_duplicates`
    （`research/experiments.py:38-55`，同评估模块 + 同 subject_key + 同 diff 签名）
    判 `duplicate_of` → `rejected_intake`（探针实证）；
  - 唯一绕行是 `allow_duplicate=True`，而 `attempt_index` 定义为
    「同 subject_key 非 rejected_intake 实验数 +1」（`experiments.py:115-121`），
    该值直接进 `dsr(..., n_trials=attempt_index)`（`evaluations/backtest.py:456`）
    → **复现一次 = 试验次数 +1 = 研究线显著性折扣被动收紧**。
- **不符合的条款**：详设 §6.4.1「实验是声明式的，所以"复现代码"= spec 本身
  （`research rerun <experiment_id>` 即可重跑）」+ 骨架第 5 条 / 决策 20 的
  「尝试计数」口径；架构稿 §2.6「台账一切下游功能（重复检测、显著性折扣、血缘审计）
  可信的前提」。
- **影响**：审计面（阅读/复现）本应是文件夹制的另一半价值，现在是**空承诺**；
  且"想复现就得污染尝试计数"这个组合会让后续 AI 在两者之间做错误取舍。
- **修复方向**：加一个 `rerun_experiment(db, experiment_id, session_id)`
  服务方法（复制 spec/hypothesis/topic、`parent_experiment_id=原实验`、
  标记 `is_reproduction` 或 subject_key 加后缀使 `attempt_index` 不计入；
  或显式 `count_attempt=False`），CLI/MCP 各加一个薄通道；manifest 的 note 与实现对齐。

### P1-3 `delta_turnover` 是恒 0 的假证据

- **事实**：`evaluations/backtest.py:118-122`
  ```python
  fills_turnover = 0.0
  return compute_summary(nav_rows, trades=[], turnover_total=fills_turnover)
  ```
  而 `rule_backtest/metrics.py:136` 的 `turnover = turnover_total / avg_equity`
  → `experiment_summary.turnover = base_summary.turnover = 0.0` →
  `_DELTA_METRICS`（`backtest.py:115`）含 `"turnover"` → **`deltas_vs_base.delta_turnover ≡ 0.0`**。
  同一文件另有 `turnover_total` 的真实实现 `reports._turnover`（探针：同一 nav/fills
  组合实算 0.009955），但从未被引用。
- **不符合的条款**：§6.5.1「证据：实验 vs 基准同窗的 Δ年化/Δ回撤/ΔSharpe/ΔSortino/
  **Δ换手**/Δ费用」；§6.10.3 判定建议里的「换手增幅成本可解释」（该输入现在恒真）。
- **影响**：台账/verdict/报告三处都会向读者呈现"换手完全没变"的结论，且看起来是
  机器算出来的。统计件错了不崩溃、只会让结论安静地撒谎——这条正落在分期文档
  §0 原则 3 点名的那类风险里。
- **修复方向**：`_nav_summary(nav, fills)` 用 `reports._turnover(fills, nav)` 或直接
  传 `turnover_total`；补一条"证据字段非平凡"的回归测试（见 §6.2 建议 1）。

### P1-4 ResearchWorker 调度器热自旋（实测 576 polls/s）

- **事实**：`research/worker.py:93-97`
  ```python
  if self._active_by_session.get(owner, 0) >= self.per_session_cap:
      self._queue.put(experiment_id)     # 立刻放回队尾
      continue
  ```
  队列里只有同会话实验时，下一次 `self._queue.get(timeout=0.5)`
  **立即返回**该条 → 循环无阻塞。探针 F 实测：1 个 run 活跃 + 2 个同会话排队时，
  `lifecycle.get_experiment` 在 5 秒内被调用 2881 次（**576 次/秒，每次一个 SQLite 查询**），
  期望值 ~2 次/秒。
- **命中概率**：`max_workers=2`（`main.py:308`）+ `per_session_cap=1`（默认）
  + **MCP 通道只有一个 AI 会话**（`trend_mcp/research_tools.py:38-41`
  `get_or_create_ai_session(channel="mcp")`）→ AI 只要连续提两个以上实验，
  第二个开始的整个等待期都落在该路径上。这正是"AI 批量提实验"的正常用法。
- **影响**：app 进程（同进程内还跑着 Web、调度器、日更）被打满一个 CPU 核并持续
  读 SQLite；回测本身又要吃 CPU/内存（§7.1 单 run ≤2GB）。开发日志的遗留 P2 把它
  描述为「dispatcher 轮询间隔（0.5s）属保守值」，与实际的紧循环行为不符——
  **这条应该升级并修掉**。
- **修复方向**：cap 命中时不要立刻 requeue，改为 `self._stop.wait(1.0)` 后再放回，
  或用一个"被 cap 阻塞的会话"集合 + `Queue.get(timeout=1)` 的退避；最小改动是
  在 `continue` 前 `self._stop.wait(0.5)`。

### P1-5 重复检测签名过宽，误杀详设点名的合法实验形态

- **事实**：`experiments.py:24-35 _canonical_diff()` **只读 `spec["diff"]`**；
  `find_duplicates` 判等条件 = 同 `evaluation_module` + 同 `subject_key` + `diff` 签名相同。
  而 event/bucket/distribution 的 spec **没有 `diff` 键** → 签名恒为 `[]`：
  - `event_study`：`subject_key = 事件模块 ref`（`evaluations/event.py:98-100`）→
    同事件、不同 `horizons`/`context_filter`/`universe`/`window` 的实验全部同签名；
    探针 H5 实证：`{event: ma_cross@1, horizons:[20], context_filter: 牛市 regime}`
    被拒为 `duplicate_of E0004`；
  - `distribution`：`subject_key = metric` → 同指标、不同池/窗口同样被误判；
  - `bucket_analysis` 反而**没有**这个问题，因为它的 `subject_key` 拼了特征名
    （`bucket.py:81-83` 返回 `f"{ref}:{feature}"`）——说明修法在本仓库已有先例。
- **不符合的条款**：详设 §6.5.2 明文：「`context_filter` 是研究问题的定义…
  同一事件换条件 = **另一个实验**（各自独立入链，结论可对照："牛市跌破 vs 熊市跌破"
  自然成为一对实验）」；§6.9.5 的「6 轮牛市 99 次考验 MA20」研究线正是靠这种对照成对；
  §6.6.6 的原意是防"重复发现"，不是防"同事件的第二个条件"。
- **影响**：合法研究被平台的纪律件挡住；被挡者只能 `allow_duplicate=True`，
  而那会**整体关闭**该实验的去重检查（把真重复也一起放进来）。
- **修复方向**：把签名扩展为「diff（若有）+ 其余 spec 的规范化哈希」，
  或按模块声明「自由变量的键」参与签名（event → `event|context_filter|horizons`；
  distribution → `metric|universe|window`），bucket 沿用现做法。

---

## 4. P2 / P3 清单

### P2（应改，不阻断单人开发节奏，但必须在首个正式实验之前收口）

**P2-1 无晋升入库路径（决策 8 的"唯一的门"无处可走）。**
`library.add_version`（`portfolio/library.py:82`）的唯一调用方是 `seed.py`；
服务面（api/mcp/cli/web）无 promote 类方法；`portfolio_strategy_versions.experiment_id`
除 seed 外永远为 NULL。§2.6「唯一的门在策略库入库：必须有完整实验血缘」、
§6.1.4「想固化则晋升入策略库，成为新策略线 v1」——**该动作没有实现载体**。

**P2-2 非回测类实验的复现清单缺 data_version（诚实义务落空）。**
`research/topic_files.py:107-124`：`data_version/engine_version/git_hash` 只在
`run["engine_run_id"]` 非空时填充 → event/bucket/distribution 三个模块的
`manifest.json` 这三项**恒为 null**（快筛类实验恰是主力）。这些值其实已被
`gateway_audit` 记录（`load_eval_panel` 每次留痕含 data_version），只是没透出。
§6.4.1 明确要求 manifest 承担"数据被重述时显式标出差异"的诚实义务。

**P2-3 课题内 FDR 只覆盖 backtest 实验。**
`research/conclusion.py:75-77` 仅从 `evidence.stats.psr` 取 p 值（`1 − PSR`）；
event/bucket 实验不贡献任何 p 值 → 纯 event_study 课题的
`fdr = {"n_tested": 0, "still_significant": 0}`，决策 20 的家族错误校正对
最常见的快筛类课题等于没做。修法：event/bucket 的 `noise_band`/`random_band`
可换算成经验 p 值（或模块声明其 p 值字段）后并入同一 BH 家族。

**P2-4 风控拦截统计（gate_log）在 L4 链路被丢弃。**
`backtester.py:315` 返回 `gate_log`，测试也断言了它
（`tests/integration/test_portfolio_backtester.py:206`），但
`evaluations/backtest.py:_assemble_result` 既不放进 `evidence` 也不放进 `report`
→ 台账、课题文件夹、页面三处都看不到"被风控拦下的候选"。
§5.2.5 称其为研究素材，§6.5.0 要求报告含风控拦截统计。

**P2-5 实盘清单没有数据新鲜度闸门（真金场景）。**
`portfolio/live.py:164-172` 以 `mode="live"` 取面板，当天那根 provisional bar
依赖 `live_overlay`（TickFlow 实时报价）。若报价失败/非交易时段重跑，
`all_days` 不含当日 → `t_idx` 指向**昨日** → 清单静默基于昨日收盘生成，
payload 仍写 `as_of=今天14:00`，`_live_caveats` 也只提 vol_target/drawdown。
建议：断言面板末日 == as_of.date()，否则告警/拒绝（本项目已有"新鲜度约定"传统）。
另：实盘清单不做涨停/停牌卡控（§5.7 输出里没有可交易性字段），与回测路径不对称。

**P2-6 实盘账户重建忽略 T+1。**
`portfolio/live.py:75` 对全部 open 持仓硬编码 `sellable_quantity=qty  # 历史买入均已过 T+1`，
没有 `buy_date == 当日` 的判定。当日录入的买入若已触及止损价，会被列入"应卖"清单
——现实中当日买入不可卖。与 §4.2「T+1 显式」和 §5.7「同一条决策代码路径」不一致。

**P2-7 模块自动测试门的运行上下文与真实运行不符（误杀合法模块）。**
`module_gate.py:88-94` 的 `_make_ctx` 里 `gateway=None`、`params={}`。任何需要
`ctx.gateway.metadata` 的模块（与**内置** `category_filter/liquidity_filter` 同形，
AI 提的 universe 模块几乎必然需要类目）在 determinism 段直接 AttributeError →
`passed: False`（探针 E2 实证）。同理，需要 `prepare_with_gateway` 的模块也过不了门
（`_instantiate:163-167` 只调 `prepare`）。**后果**：门挡的不是"前视/不安全"，
而是"和内置件同形的合法件"。

**P2-8 heat 在混合持仓下静默少计，空仓记为 None。**
`engine/models.py:136-151`：只要有**任一**持仓有 stop_price 就返回已累计值，
无止损持仓（`position_risk=none`）的部分被静默丢弃且不告警（探针 C1：
两持仓一件无止损 → heat=1000，第二件的风险敞口缺席）；全无止损时才返回 None。
§4.4 要求"无止损持仓记 None 并告警"，`heat_cap` 门据此放行会低估组合风险。
另空仓时 heat=None（应为 0，探针 C3）。

**P2-9 `time_stop` 的持有天数比参数多 1。**
`position_risk.py:336-349` + `engine/stops.py:184-188`：`held_days` 从 0 起、
在每日期末自增，而持仓在**买入日当天不参与 evaluate** → max_days=5 时在第 6 个
交易日才离场（探针 D1 实证：entry idx=10 → exit idx=16）。参数语义与
「持有 N 日强制离场」不符，且无测试钉住。

**P2-10 §7.1 存储预算被首个真实 run 打破；开发期产物已写进生产库。**
§7.1 写死「每 run ≈ 2 万行 ≈ 2MB，1000 次 run ≈ 2GB」；实测 base-v1 单次
sample run（10 年窗口）= 160,565 条 unfilled + 961 笔成交 + 约 2,440 行 nav
≈ **16.4 万行（8× 预算）**（`comparison.md` / `comparison.json` 的 `trades` /
`unfilled` 字段），1000 run 量级将到十几 GB。生产库 `data/trend_quant.db` 现含
`engine_unfilled=228,147`、`engine_daily_nav=11,660`、`engine_runs=8`（4–8 次
开发 run 的产物）+ 9 策略/11 版本 + 26 条 audit。新表全部 `IF NOT EXISTS` 纯新增
（前审 B 已实测零破坏），但"开发跑批落在生产库"这件事本身需要处置口径：
要么清空新表 + 把本地跑批指向独立库，要么在 §7.1 记录实际口径（unfilled 需聚合
存储，例如按 (run, date, reason) 计数而非逐行）。

**P2-11 「物理卡控」的表述与实现的边界不符（残余逃逸面）。**
- `portfolio/context.py:37-40`：`PanelView.full_panel` 抛 `PermissionError`，
  但 `PanelView._panel` 是普通槽位 + `__slots__` 只保护名字不保护访问 →
  `ctx.panel._panel.data["close"][-1]` 直接读到运行末日（探针 B2 = 99.0）；
- `gateway/service.py:173`：`BoundGateway._gateway` 同理可绕 as_of 绑定。
- **缓解事实**（因此判 P2 而非 P1）：python 模块的**前缀稳定性探针**确实能抓住
  "天真窥探"（`module_gate.py:202-215`：全量面板实例 vs 截断面板实例的输出必须
  相等，读未来行会破坏相等）；DSL 的 `ref` 位移已编译期限定为非负整数字面量。
  残余风险是"条件式窥探"（如仅在长窗口时窥探）与**内置/已注册模块**（不过门）。
- **建议**：把 `_panel`/`_gateway` 改名私有并加 `__getattr__` 拦截（或提供
  `PanelView.peek()` 明确拒绝），或在架构稿/详设把「物理上不可能绕过」改写为
  「门面强制 + 探针兜底 + 信任边界 = token 持有者」，让文档与实现同口径
  （B-R2 已在 python 模块侧做过同样的信任模型声明）。

### P3（观察/口径注记，不影响判定）

1. **创建型实验"七槽全填"可被 `to: none` 满足**（探针 G1：七槽全 none 通过校验）
   ——`backtest.py:88-92` 只检查槽**是否出现**，不检查是否绑定了模块。
2. **元模块只注册了 3 处**（`signal:any_of`、`signal:all_of`、`position_risk:any_of`），
   与 §5.2.8/§5.14「全插槽通用」有差（前审 A 已记录）。
3. **`distribution` 仅 3 个 metric**（`atr_pct/momentum_20/er_10`），plan §6.9.2 举过
   `signals_per_day`；按"模块参数空间快照"口径可接受，记录在案。
4. **live 不写 `engine_runs(kind='live')`** → §4.1 的实盘运行档案缺失，实盘 run
   没有 run 级血缘锚点（清单只落 `portfolio_live_lists`）。
5. **bucket 单调性定义与 §6.5.3 文字不一致**：实现是"相邻差分方向与 `expect` 一致的
   占比"（`bucket.py:199-203`），文字是"相邻组符号一致的占比"；前者更强，但口径应写明。
6. **`match_intraday_stop` 在停牌日对未触发持仓也落 `unfilled(suspended)`**
   （`matcher.py:200-202`）：长时间停牌会按日灌 unfilled 表（与 P2-10 的存储量
   相互放大）。是否要区分"停牌且无法判定"与"触发但阻塞"值得再想。
7. **regime 拆分的代理基准会静默回落**：`evaluations/backtest.py:397`
   `bench_nav_for_regime or base_nav` ——当策略库中根本没有 CSI300 benchmark 行时
   （非异常路径）不产生任何告警，直接用 base 策略净值当 regime 线。
8. **课题摘要把 failed / rejected_intake / 在途统一计入 `no_verdict`**
   （`conclusion.py:64-69`），"含失败实验一个不许藏"在计数上成立，但读者分不清
   "工程失败"与"还没跑"。
9. **`execution._is_first_day_of_period("every_n:N")` 用面板行号取模**
   （`execution.py:27-29`）→ 行动日随窗口起点漂移，同配置换窗口不再是同一实验。
10. **`_universe_symbols` 对非 static_list 一律取全 enabled 池**（`backtester.py:320-331`），
    面板载入量与 universe 实际成员无关（性能冗余，非正确性）。
11. **前审已记录、本次复核仍成立**：attempt_index 并发撞号、`library` MAX+1 竞态、
    DSL 大常量、ST/新股涨跌停近似、孤儿表 `portfolio_backtest_runs`（0 行）、
    `src/**/__pycache__` 残留已删模块的 pyc。
12. **报告注记建议**：60/40 = "股现"（无债券 ETF 数据）、equal_weight 用流动性 top-50
    近似、base-v1 无组合约束导致 160k 未成交——这三条已在开发日志/YAML 注释，但
    **没有随 verdict 输出**；建议把它们写进相应策略的 warnings（读台账的人看不到 YAML 注释）。
13. **v1 跑输猴子基准这条结论的解读口径**：`comparison.md` 显示 base-v1 年化 0.70% /
    −71.6% vs 随机入场 7.02% / −69.3%。数据本身我认可（无 slot_limit 的 v1 在 2015 股灾段
    满仓硬扛 + 每日重试导致 16 万条 insufficient_cash，方向自洽），但**"v1 = 实盘当前打法
    1:1 复刻"这一句在缺少组合约束时会被读者误读**（用户实盘有事实上的持仓数上限）。
    建议在 v1 的 verdict/报告注记："本配置按附录 A 无组合约束如实复刻，与实盘习惯
    （约 ≤10 只）的差异由实验 T0x（slot_limit 扫描）回答"。

---

## 5. 存量业务影响评估

### 5.1 逐文件结论：未发现破坏性改动

| 文件 | 改动 | 复核结论 |
|---|---|---|
| `src/data/storage/db.py` | +423 行 `_RESEARCH_STACK_DDL`（18 表 + 14 触发器 + 索引）+ `connect()` 别名 + `load_market_data_many` 委托重构 + 类型注解去引号 | 全部 `IF NOT EXISTS`，纯新增；`executescript` 的隐式 COMMIT 发生在 `_init_tables` 末尾（其余语句均为 DDL），**无事务语义回归**；`load_market_data_window_many(symbols, None, None, ...)` 在 start/end 为 None 时不拼子句，与原 SQL 等价（前审 B 已实测二次构造幂等 + 生产库零增删改） |
| `src/app/main.py` | research worker 启停（受 `TREND_QUANT_DISABLE_SCHEDULER` 门控）+ `deferred_backtest_running` 与 skipped 同等 return + 新路由挂载 | 逻辑正确；研究 worker 随 lifespan 停止 ✓；新路由落在 AuthWall 之后（前审 B 实测匿名 303/POST 401） |
| `src/core/jobs.py` | `daily_market_update_job` 冻结门（轮询 ≤30min，超时顺延）+ 新增 `live_daily_list_job`（14:05） | 冻结门非 force 才生效（启动补偿不受阻）✓；`deferred_backtest_running` 不算 completed/partial → 启动补偿会兜底 ✓；`live_daily_list_job` 未配置策略时跳过 ✓、异常记 job_run failed 不炸调度器 ✓ |
| `src/core/scheduler.py` | `live_list_job` 可选参数 + 14:05 cron | 签名向后兼容 ✓ |
| `src/trend_mcp/server.py` | 末尾 `register_research_tools(mcp)` | 导入期只注册函数；Bearer 中间件对全部工具统一生效 ✓（存量 MCP 测试全过） |
| `web/templates/base.html` | 导航 +1 行 | 纯增量 ✓ |
| `.gitignore` | +`data/research/` | 合理（但注意 `data/*.db-shm/-wal` 未被忽略，见 §5.3） |

### 5.2 运行期新增的存量风险（真实存在的两条）

1. **日更会被研究 run 推迟**：`run_freeze` 冻结期最长 30 分钟（其后顺延到下次触发点，
   依赖启动补偿兜底）。风险场景是"研究 run 恰好跨 16:30"以及"并发 2 个 run 接力"。
   冻结计数是**进程内**的（开发日志已记），独立 CLI 发起的 run 不冻结 app 调度器。
   建议：在台账/日志里把"日更被顺延"变成可见事件（现在只有日志行），
   并在 live 清单页面加"数据更新是否被推迟"的注记。
2. **app 进程内的 CPU/SQLite 争用**：worker 线程与 P1-4 的热自旋、以及回测本身
   （10 年 × 874 标的 88.8s，内存数百 MB）都在 Web 进程内。单人系统可接受，
   但 P1-4 修掉后这条才算真正可控。

### 5.3 环境卫生

- 生产库 `data/trend_quant.db`（5.5GB，用户的真实交易库）已被新栈写入
  **开发期产物**：8 个 `engine_runs`、2,199 fills、228,147 unfilled、11,660 nav、
  9 策略线/11 版本、26 条 `gateway_audit`（research_* 五表全为 0 行，干净）。
  新表纯新增、既有表零改动（前审 B 实测；我只读快照复核：`position_strategies`
  **表仍在但 0 行**——准备阶段的"删表"在本地只清了数据、未落 DROP，与详设 §8 给出的
  线上一次性命令口径需对齐；`portfolio_backtest_runs` 仍在但 0 行）。
- 未跟踪文件里出现过我审查期间由只读连接产生的 `data/trend_quant.db-shm/-wal`
  （0 字节 WAL）——**已删除**，主库未变（见文首声明）。建议把
  `data/*.db-shm`、`data/*.db-wal` 补进 `.gitignore`，避免以后每次只读审查都污染状态。

---

## 6. 测试完备性与质量保障审查

### 6.1 已确认（复核前审结论成立）

- **143 项新增/相关测试我实跑全绿**（unit 105 / integration+api 25 / stage5 13）。
- **无 P0 虚假断言**：golden 数值（10.03、50428.5、10.4685、252000×1%/252=10、
  heat 600、印花税 4.985、逐手递减 400 股）我逐一复算成立；DSL/分层/PIT 的修复
  都有对应的实证型测试。
- **统计件有真 golden 对拍**：PSR 有区间锚（0.94<Φ(1.5804)<0.95）、DSR 有绝对锚
  （`tests/unit/test_review_gaps.py:155` 钉 `0.5740092653864478`，abs=1e-9）、BH 手算、
  PBO 构造性（λ=1→PBO=0）。**我用 `statistics.NormalDist` 独立复算了这两个锚**：
  PSR z=1.580352 → Φ=0.942987（区间锚成立）、DSR=0.5740092653864478（与钉值逐位一致）。
  公式我再逐项对照 Bailey & López de Prado 2012/2014：PSR 分母
  `1 − γ3·SR̂ + ((γ4−1)/4)·SR̂²`、γ4 取非超额峰度、DSR 的
  `(1−γ)Φ⁻¹(1−1/N) + γΦ⁻¹(1−1/(N·e))`、MinTRL 的 `(z/ΔSR)²` 因子——**装配与论文一致**，
  且口径正确（PSR 输入是日频 Sharpe 而非年化）。
- **无循环论证**：stops 对拍双方、parity 新旧引擎两条独立路径、append-only 触发器
  用原生 SQL 直测。

### 6.2 仍未覆盖的缺口（本次新增，按重要性）

1. **证据字段的"非平凡性"契约测试缺席**——正因为没有一条断言"Δ换手随成交变化"，
   P1-3 才能静默通过 143 项测试。建议加：
   `assert evidence["deltas_vs_base"]["delta_turnover"] != 0.0`（构造有成交的合成 run）
   以及 turnover 与 `Σ|price×qty|/mean_equity` 的数值锚。**这是本次审查给出的
   最高性价比测试建议**：它把"证据字段是常量"这一类错误纳入回归面。
2. **报告完整性契约测试缺席**：应断言 `report_json` 含
   `gate_rejections / round_trips / window / engine_runs / warnings` 等 §6.5.0 明细键
   （现在缺件无人知）。
3. **复现（rerun）测试缺席**：应有一条"同一实验可复现且 `attempt_index` 不变"的用例。
4. **重复检测误判测试缺席**：应加"同事件换 `context_filter`/horizons **不被**判重复"
   与"同事件同条件**被**判重复"的双向钉子（现只有后者）。
5. **live 数据陈旧**：overlay 返回空时应拒绝或告警的用例。
6. **live 当日买入不可卖**：应断言当日 `buy_date` 的持仓不出现在 sells。
7. **worker 资源行为**：cap 命中时的 requeue 退避（可用"调用次数上限"型断言，
   如 5 秒内 dispatcher 循环 ≤ 20 次）——正是 P1-4 的钉子。
8. **time_stop 暴露天数**：应钉"max_days=N → 第 N 个交易日离场"。
9. **创建型校验**：`to: none` 七槽是否应放行需与文档对齐后钉住。
10. **manifest 完整性**：event/bucket 实验的 manifest 应断言 data_version 非空
    （P2-2 的钉子）。
11. **heat 混合告警**：无止损持仓混在组合里时应产生告警。
12. 前审 C-R2/R2 记录但未补的：`match_buy` 的 `bar_close<=0`、除权×停牌组合、
    T+1 止损顺延重试端到端、holdout 非实验路径留痕（决策 C3）、
    `get_production_indicator` 的 as-of 裁剪。

### 6.3 质量保障流程评价

- 开发流程（设计期四轮修复 → 三代理并行一审 → 修复 → R2 复审 → R3 终审 → 三家一致
  才终止）**在形式上完整且可追溯**，审查报告与修复映射表齐全，这是本项目最值得保留的做法；
- **但本轮暴露了该流程的一个结构性盲点**：三家审查的清单里没有"**交付物 vs 详设条款的
  交付物清单逐项核对**"这一项（A 的 6 项 P1 集中在 PIT/分层/冻结/死模块/测试门/槽位泄漏，
  都是"机制实现是否正确"，没有一项是"详设要求的成品是否存在"），也没有"**运行期资源
  行为实测**"。建议在审查章程里固定加两张表：
  （1）「详设条款 → 代码位置 → 端到端可达路径」交付物核对表（本次 P1-1/P1-2/P2-1/P2-2
  全在这张表上）；（2）「机制件的运行期行为实测」清单（本例的 P1-4 一次 10 行探针就能抓到）。

---

## 7. 修复建议（按优先级）

### 7.1 阻断项（建议在首个正式实验前完成，全部为小改动）

1. **P1-3**（1 行 + 1 测试）：`_nav_summary` 接入真实 turnover。
2. **P1-4**（1–3 行 + 1 测试）：cap 命中时 `self._stop.wait(0.5)` 退避。
3. **P1-5**（~15 行 + 2 测试）：签名纳入 spec 其余字段（或模块声明自由变量键，bucket 已有先例）。
4. **P1-2**（~40 行 + 1 测试）：新增 `rerun_experiment` 服务方法 + CLI/MCP 薄通道 +
   不计入 attempt_index；manifest note 与实现对齐。
5. **P1-1**（~30 行）：`_assemble_result` 改为调用 `build_report`（经 L3 服务面），
   `gate_log` 透传入 report；页面加全量 report 出口（下载或结构化渲染）。

### 7.2 P2 建议顺序

先做"台账可读性/正确性"三件（P2-4 gate_log、P2-3 FDR 覆盖面、P2-2 manifest
data_version）→ 再做"实盘真金"三件（P2-5 新鲜度、P2-6 T+1、P2-11 逃逸面收口）→
最后做"治理与预算"（P2-1 晋升路径、P2-7 门上下文、P2-10 预算与环境卫生）。

### 7.3 建议新增的回归钉子（每条 P1 一条）

- `test_evidence_turnover_is_not_constant`
- `test_dispatcher_backs_off_when_session_capped`
- `test_event_study_same_event_different_regime_not_duplicate`
- `test_rerun_keeps_attempt_index`
- `test_backtest_report_contains_gate_rejections_and_round_trips`

---

## 8. 遗留风险与运行期观察项（不阻断）

- **§6.6.4 长窗口三注记**：覆盖率/费用时代错配/幸存者偏差加权已写进脚本输出与
  comparison.md，且事件类评估恒带 survivorship 警告 ✓；但 regime 拆分的 2015/2018
  两段样本量（127 只子集）没有量化呈现在 verdict 里，建议在 verdict 的
  `regime_split` 旁附该段"参与标的数/占总池比"。
- **实盘并行期**（阶段 4）尚未开始：`live.py` 的三个 P2（新鲜度/T+1/卡控）在开始
  真金并行前必须收口，否则"清单→手工执行→对账"会带着已知偏差起步。
- **holdout 生效后**：默认 enforced=1 ✓，但"非实验路径触碰必留痕"（决策 C3）在
  实现上无载体（`holdout.check_window` 只在评估模块内被调用；非实验路径如
  `scripts/run_base_v1_sample.py` 没有可写 holdout_touched 的地方）。
  若认为该条必须落地，需要一个 `holdout_events` 表或等效机制。
- **PBO 的变体池偏小**：`evaluations/backtest.py:508-522` 的 CSCV 变体 = 主选 +
  高原邻域探针，单参数 diff 时只有 **3 个变体**（T 足够长所以会正常算出结果，不是
  None）——PBO 在 3 个变体上的 λ 分辨率很粗（相对秩只取 0/0.5/1 三档），
  建议在 evidence 里显式记录 `n_variants`，并考虑把高原探针扩为 4–6 点
  （参数高原判定本身也已按 ±20% 两点做，扩点对"孤峰"判定同样有利）。

## 9. 本次审查未覆盖的范围（诚实声明）

1. 未重跑 2015–2024 全窗口回测（只复核既有 `data/research/base_v1_sample/*` 产物与
   生产库 run 快照；v1 的 0.70%/−71.6% 未逐笔复算，仅做口径合理性判断）；
2. 未做 MCP 端到端（需带外 token）与浏览器端页面交互验证（只读代码 + 前审 B 的
   TestClient 实证）；
3. 未验证 `research/topics/` 实际物化产物的内容质量（无 verdicted 实验可物化——
   生产库 research_* 五表 0 行）；
4. 未评估 2∥/阶段 5 各评估模块在**真实全池数据**上的统计行为（例如 event_study 在
   874 只 × 10 年上的事件密度、重叠率、bootstrap 稳定性）——这属于首次实验的执行面。

---

### 附：本次审查的关键证据索引

- 测试实跑：`105 passed`(unit 8 文件) / `25 passed`(integration+api 5 文件) / `13 passed`(stage5)
- P1-3 探针：`_nav_summary.turnover = 0.0` vs `reports._turnover = 0.009955`
- P1-4 探针：5.0 秒内 `lifecycle.get_experiment` 调用 2881 次（576/s）
- P1-5 探针：`duplicate_of: E0004`（同事件 + 不同 horizons/context_filter）
- P2-2/P2-7/P2-8/P2-9 探针：manifest data_version=None（代码）；gate 拒 gateway 依赖模块；
  heat=1000（部分静默）；time_stop max_days=5 → 第 6 日离场
- 生产库快照（只读）：`engine_unfilled=228147`、`engine_daily_nav=11660`、
  `engine_runs=8`、`portfolio_strategies=9`、`portfolio_strategy_versions=11`、
  `research_*=0`、`position_strategies=0`、`portfolio_backtest_runs=0`
- 死代码证据：`build_report` / `rolling_sharpe` / `drawdown_durations` /
  `return_distribution` / `cost_drag` 在 `src/ scripts/ tests/ web/` 内零外部引用
- 复现入口证据：`research_cli.py` 6 条子命令；`research_tools.py` 10 个 `@mcp.tool`；
  `ResearchService` 无 rerun；全库 grep `rerun` 只命中注释/manifest 文本

**DS_VERDICT: FAIL（5 项 P1 未决）**
