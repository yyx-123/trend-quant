# Round 4 审查报告（loop-review-ds4f）

> **状态：OPEN（待修复验收）**
>
> 日期：2026-09-25
> 审查对象：commit `90e48f8`（Round 3 闭合后全量代码）
> 审查方式：独立审查代理 R4A 做**新面审查轮**——分层与导入卫生、新栈与存量行情的
> 数据一致性、研究 worker 的并发与资源行为、台账 Web 端到端（含敌意表单）、验收脚本
> 可复现性。全部结论经真探针（AST 导入图 / 只读生产库 / TestClient 真表单轰炸 /
> 真 worker 并发 / `VACUUM INTO` 复制库跑验收脚本）。主审人复核并自行复现关键项。

## 总体结论

**FAIL（1 项 P1 + 1 项 P2 + 6 项 P3）**——前三轮聚焦数值与平台纪律，本轮换面后抓到
一个**改变了基准策略语义**的 P1（已发布的验收数字因此不可复现），以及一个**数据语义**
P2。

---

## P1

### R4A-P1-1 `target_weight@1` 的 schema 默认值与实现的条件默认相反 → `bench-60-40` 静默变成满仓

- 位置：`src/portfolio/slots/sizing.py:136-140`（schema `default: "equal"`）+
  `:92`（实现的**条件**默认：`weights` 非空 → `explicit`）+ `src/portfolio/registry.py:169-170`
  （`validate_params` 会**物化** schema 默认值）
- 事实：只要参数里写了 `weights` 而没写 `mode`，物化出来的 `mode='equal'` 就让实现走
  "成员均分"，**权重表被整体忽略**：
  ```
  validate_params(schema, {'weights': {'510300.SS': 0.6}})
    → {'weights': {...}, 'mode': 'equal'}     # 物化了与实现相反的默认
  TargetWeightSizing.size(...) → 1,000,000（100%）
  显式 mode='explicit' → 600,000（60%）
  ```
  影响链：①新库首次 seed 就把 `mode: equal` 写进 canonical `config_yaml`（YAML 源文件
  没有该键）→ 从第一天起就是错的；②在**生产库复制库**上重跑验收：`base-v1`/`buy-hold`/
  `random-entry` 的 NAV 2431/2431 **逐位相同**，`bench-60-40` **2431/2431 全不同**——
  年化 2.50%→**4.09%**、MDD −30.99%→**−43.78%**、Sharpe 0.251→**0.316**、成交 3→**10**；
  ③只有 `bench-60-40` 被铸了新版本（`@3`），其余 8 条线 hash 未变 → 差异只能来自配置；
  ④任意实验 diff 形态（`{"slot":"sizing","to":"target_weight@1","params":{"weights":{...}}}`）
  同样中招 → "单变量实验"实际改了两件事（研究纪律层面的错归因）。
- 引入点：`8f38c0a`（R3B-P3-4）——前三轮与 GLM53F 系列均未覆盖。
- 修复：删掉 `mode` 的 schema `default`（保留 `choices`），把条件默认留给实现；
  新增**机制性**钉子：对每个内置模块，用多组参数比较"原样实例化"与"经 schema 物化后
  实例化"的行为指纹必须一致（整类守卫，不针对单个模块）。
- 历史处置：版本不可变 → 已铸的 `bench-60-40@3` 保留为历史；修复后 seed 会按 YAML 重铸
  一个正确版本；验收产物需重跑（进待决策）。

---

## P2

### R4A-P2-2 `instrument_metadata.start_date` 被当上市日 → 新股无涨跌幅限制这条口径在真实库上整条失效

- 位置：`src/gateway/tradability.py:170-173`（`listing_dates` 取自元数据 `start_date`）+
  `_ipo_no_limit_days`
- 事实：生产库 **874/874 行的 `start_date` 为 NULL** → `no_limit` 恒 False
  （874×4694 行 0 命中）；后果是把无涨跌幅限制的新股按 ±10/20% 判**假涨停/假跌停**：
  `688820.SS`（科创板，首根 bar 2026-04-21）2026-04-22 收盘 95.00（+23.9%，上市初期
  无限制）被判 `is_limit_up=True`（`limit_up_price=91.98`）→ L2 会拦掉合法买入。
  反方向：一旦该字段被写（导入/回填表单会写），窗口起点后 5 个交易日的卡控会**整段
  关闭**（fail-open）。
- 处置：该字段语义（上市日 vs 回填起点）**无法从代码判定**，两种口径的取舍需数据侧
  裁决 → 本轮**不改行为**，只做**可见化**：结果里新增 `listing_known` 列，把"上市日
  未知"如实标出（不再静默）；修复方向与取舍建议进待决策清单。
  - 已否决的更激进方案（记录备查）：用"该标的首根 bar"当上市日代理——实测会把
    窗口起点附近的所有标的都当成新股（40 个用例失败），因为首根 bar 受回填窗口裁剪。

---

## P3

| # | 位置 | 问题 | 处置 |
|---|---|---|---|
| R4A-P3-3 | `src/research/worker.py:122-123,160,184-186` | `stop()` 用 `cancel_futures=True` 取消"已派发未开跑"的 future，但这些 id 在派发时已从 `_queued_ids` 摘除、减计数只在 `_run_one` 里 → **丢派发**（实验永远停在 queued）＋**泄漏会话计数**（泄漏到 cap 后该会话实验永不执行）；另 `_pool is None → break` 路径同样丢 id 与计数 | 修：`_inflight` 跟踪 future→(id, owner)，stop 时把未开跑的取消并**回灌队列 + 回退计数**；drain 原始队列并回 `_queued_ids`；`_pool is None` 退出前同样还回 id/计数 |
| R4A-P3-4 | `src/research/worker.py:117-121` | `stop()` 的 `join(timeout=5)` 超时后照常置空 `_dispatcher`（旧线程仍活）→ `start()` 会起**第二个调度线程**共享状态 | 修：超时保留引用并告警，`start()` 见活线程即拒 |
| R4A-P3-5 | `src/trend_mcp/research_tools.py:98-105` | AI 通道**自己写库**（`INSERT OR IGNORE INTO research_sessions`），会话命名/归属策略落在通道里、绕过服务面 | 修：新增 `sessions.ensure_channel_session`（服务面），通道只调它；钉子断言通道内不得出现 INSERT |
| R4A-P3-6 | `src/core/jobs.py:366-367,373` | `core` 反向依赖新栈（`gateway.live_overlay` / `portfolio.live`），与 `main.py:251` 自陈的"core 不引 services 层"矛盾 | **记录在案**（属分层文档口径问题：`live_daily_list_job` 是编排作业，迁到 services 需动调度注册与既有钉子；与 R4A-P3-6 一起进待决策） |
| R4A-P3-7 | 台账 3 个表单 | 字段无长度上限、`experiment_id` 不校验存在性：70k 字 reasoning、200k 字 purpose、指向不存在实验的 token 都能落库 | 修：`grant_token` 加 purpose ≤200 与 experiment 存在性；`confirm_verdict` 加 reasoning ≤4000；配钉子 |
| R4A-P3-8 | `tests/integration/test_critical_paths.py:222-244` | 验收 slow 钉只断言结构 + `elapsed_s`，**从不重算** → 数字漂移 CI 无感（本轮 P1 就是数字漂移）；开发日志阶段 2 快照仍是修前数字，其中"随机入场 7.02%→6.50%、Sharpe 0.37→0.35"全仓无记录 | 处置见待决策（重跑产物 + 更新/标注日志 + 钉子升级为"重算一条线"或"产物 run_id == 最新 engine_run"） |

## 已验证无问题（本轮实跑核对）

1. **分层与导入卫生（AST 图，148 文件）**：`research→data` 0、`portfolio→data` 0、
   `engine→{data,gateway,portfolio,research}` 0、`gateway→{engine,portfolio,research}` 0、
   `core→services` 0、`web/` 0 个 .py；5 个导入环全是同包 `__init__` 自环（良性）。
   例外清单逐条定性（历史基线 vs 真违规），其中 `research→engine` 3 处与
   `rule_backtest.metrics` 复用已有既有审查记录。
2. **qfq 物化恒等式**：19 标的（50 个除权因子、4693 根 bar）`market_data_qfq ==
   compute_qfq(raw, ex_factors)` **容差 0 零不一致**。
3. **除权可交易性基价**：19 标的 137 个样本逐条吻合（误差 < 0.011 分位取整），
   且**全池假涨跌停回归**（874 只、~4.1M 行）假涨停 0 / 假跌停 0 → R1-P1-1 的修复
   在真实尺度成立；多因子同 bar 累乘正确。
4. **缓存正确性**：9 标的的 `indicator_daily` 29 列 + `trend_daily` 5 列 +
   `trend_rolling_daily` 逐位相同（maxdiff 0）；`formula_version`/`param_set` 一致。
5. **worker 并发**：4 worker × 6 实验全部到 evaluating，0 次 `database is locked`、
   无重复派发、无残留线程。
6. **冻结/调度纪律**：未冻结即执行；冻结则 `deferred_backtest_running` 且
   `update_pool_daily` 零调用、哨兵解冻后自动补跑、`after_update` 由 app 注入、
   `run_freeze` 异常路径计数归零。
7. **台账 Web 端到端**：8 个 XSS 载荷零裸注入（autoescape）；**69 个敌意请求 0 个 5xx**
   （缺字段/错类型 422、领域错误 409）；"升级"拒 409、"降级"放行、二次 confirm 409 且
   第一位作者 reasoning 未被覆写；敌意 path id 全 404；匿名 3 条路由全 303 登录墙；
   跨站向量全 403；`report.json` 与 `verdict_envelope` 同源。
8. **验收产物真实性**：存量 `comparison.json` 的 4 个 run_id 与生产库 `engine_runs`
   对应、NAV 与 `engine_daily_nav` 逐位一致（4×2431 行）→ 产物非手改；复制库重跑
   4 条线中 3 条逐位复现（唯一破坏者即 P1）。

## 待决策点（新增）

1. **R4-D-1 `target_weight.mode` 的历史处置**：版本不可变 → `bench-60-40@3`（错误口径）
   保留为历史，修复后需**重铸正确版本 + 重跑验收产物**并把新数字写入开发日志（现日志
   阶段 2 快照是修前数字；随机入场 7.02%→6.50%、Sharpe 0.37→0.35 无任何记录）。
2. **R4-D-2 上市日数据源**：新增独立 `listing_date` 列 / 用首根 bar 推导（已否决：
   会被窗口裁剪污染）/ 是否把 `start_date` 明确定义为上市日并补齐 874 行？在裁决前，
   实盘与回测接受哪一侧的错（NULL = 新股前 5 日假涨跌停、fail-closed；被写值 =
   窗口起点后 5 日卡控关闭、fail-open）？本轮已加 `listing_known` 可见化。
3. **R4-D-3 验收钉与文档**：是否把 slow 钉升级为"重算一条线"或"产物 run_id ==
   最新 engine_run"，并更新开发日志的阶段 2 快照。
4. **R4-D-4 既有分层项是否本轮收口**：`core/jobs.py` 引 L1.5/L3（本轮新记）、
   `module_gate` L4→L2、新栈复用 `rule_backtest.metrics`、`data→services` 与 README
   单向依赖声明的矛盾——统一收口还是写进分层文档作为显式例外。
5. **R4-D-5 worker `stop()` 语义**：排空并回灌队列（本轮已实现，安全）还是保持取消（更快）？
