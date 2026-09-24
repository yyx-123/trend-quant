# A-方案一致性评审（评审子代理 A）

> 日期：2026-09-23
> 评审基准：`docs/26-09-20-投研基建架构/` 三份文档（架构稿 20 条决策 = 硬约束；
> 总体方案 §1–§8 + 附录 A；后续TODO = 分期边界）
> 被审对象：一期（阶段 0–5）全部新增/改动代码（src/gateway、src/engine、
> src/portfolio、src/research、core/run_freeze、core/jobs、scheduler、app/main、
> routers/research_ledger、web/templates/research_*、trend_mcp 接线、scripts×2、
> db.py 新增 DDL 块）+ 开发日志自认的口径澄清点
> 方式：只读审查（未修改任何代码）+ 测试实证（新增 135 项测试全绿复跑；
> append-only 触发器冒烟实测；sample 窗口产物核对）

---

## 总体结论

**有条件通过。**

骨架是忠实于设计的：分层包结构、七插槽、四+1 评估模块、纪律流水线七件、
append-only 触发器（实测拦截有效）、课题两级对象、决策 20 量化摘要、判定器
全家桶、附录 A 逐字一致、sample 窗口 2015-01-01~2024-12-31 落实且产物可查、
二期项与永不做项一件未混入。但存在 **6 项 P1 级偏离**，集中在三条线上：

1. **PIT 沙箱对 AI 新模块不成立**——回测 DayContext 的受限句柄绑定的是
   run 末日的 as_of 而非当日 t（§5.2 明确要求 as_of = t 收盘）；
2. **分层铁律（决策 7/10）有 7 处直接违反**——L3 反向 import L4、L4 跨级
   import L2、三处绕过 gateway 直读 L1 元数据、L3 直接 import L1 数据服务；
3. **决策 A3 冻结门在日更被顺延后的 post-update pipeline 上有洞**——被冻结
   顺延的日更仍会继续跑股息修复（qfq 原位重写）与指标重建。

另有模块测试门对非 signal 槽跳过两门探针（决策 16 打折且开发日志自述与
实现不符）、trend_score_cross 首批预置模块注册了但无任何调用路径（§5.14
"全量可用"不成立）、backtester 模块实例化的槽位上下文泄漏共 3 项 P1。
无 P0。修复 P1 清单后即可转为"通过"。

---

## 逐项核对表

### 一、架构稿决策 1–20

| # | 决策 | 结论 | 证据 |
|---|---|---|---|
| 1 | 成交口径 T 日尾盘/收盘 + 尾盘滑点 | 通过 | `engine/matcher.py:79,116-127`（fill=close×(1+base+tail)）；信号用截至 t 数据（`portfolio/context.py:27-81` PanelView 限窗）；涨跌停 T 日卡控（`matcher.py:71-74,151-154`） |
| 2 | 年度行业龙头动态池（数据线） | 通过（未实现=正确，属二期慢线） | 全库无 yearly_leaders 实现；`gateway/metadata.py:47` 注释明示二期 |
| 3 | 实盘薄版（清单+对账，不碰券商） | 通过 | `portfolio/live.py:133-278` 只出清单；`live.py:297-364` 对账；`core/scheduler.py:118` 交易日 14:05；`core/jobs.py:231-278` |
| 4 | AI 接入与隔离（声明式+服务面+并发池+测试门） | **部分通过** | 声明式 spec+并发池+会话归属均有（`research/worker.py:24-125`）；但测试门对非 signal 槽跳过两门探针（`research/module_gate.py:123-128`）——见 P1-5 |
| 5 | 基准策略 v1 = 实盘打法 1:1 | 通过 | `src/portfolio/strategies/base_v1.yaml` 与附录 A 逐行一致（见末节核对） |
| 6 | 止损口径：实际买入价基准、ATR 含当根 | 通过 | `engine/account.py:29`（entry_price=fill_price）；`engine/stops.py:47-54` + `portfolio/slots/position_risk.py:75-84`（init 用含当根 ATR）；日内路径 T-1 判定（`stops.py:148-159` + `position_risk.py:125-145`） |
| 7 | 分层铁律（执行面逐层、数据面共享门面） | **偏离（P1）** | 7 处违反：`portfolio/library.py:13` L3→L4；`research/evaluations/backtest.py:395`、`research/topic_files.py:115` L4→L2；`portfolio/backtester.py:302`、`portfolio/live.py:287`、`research/evaluations/_common.py:30` 绕过 gateway 直读 L1 元数据；`portfolio/live.py:372-373` L3 import L1 数据服务——见 P1-2 |
| 8 | 无晋升门（回测随便跑，入库要血缘） | 通过（附 P2 注意） | 实验→回测无门槛（`research/pipeline.py`）；晋升通道未暴露=血缘规则由通道缺失兜住；`library.add_version` 本身不校验血缘（P2-12） |
| 9 | 展示/应用分支不动 | 通过 | `web/templates/base.html` 仅加导航链接；看板/单标的/MCP 查询未动 |
| 10 | L1.5 唯一数据咽喉 | **部分通过** | as-of 强制只在 `gateway/panel.py:135-159`、可交易性只在 `gateway/tradability.py` 实现 ✓；但存在三处元数据绕过与 live overlay 驻留 L3（同决策 7 条目） |
| 11 | 纪律平台流水线强制（8 项机制） | 通过 | schema 入口卡控（`experiments.py:81-131`）、verdict 平台生成（`pipeline.py:48-58`）、高原自动补跑（`evaluations/backtest.py:446-484`）、holdout 工程化（`holdout.py`）、attempt_index+DSR（`experiments.py:112-121`、`stats/psr.py`）、重复检测（`experiments.py:38-55,123-131`）、判定规则平台持有（`verdict_rules.py`）、多插槽三级处置（`evaluations/backtest.py:85-98`） |
| 12 | 因子工业化推迟；轻量评估不推迟 | 通过 | event/bucket/distribution 已实现（`research/evaluations/`）；无因子库/IC 流水线（grep 全库无） |
| 13 | 仓位管理功能移除（准备阶段） | 通过 | `src/rule_backtest/sizing/` 只剩 `__pycache__`（前序 commit 8ff23c4 完成）；equal_risk 数学已打捞进 `portfolio/slots/sizing.py:19-41` |
| 14 | 台账 append-only、全员只读 | 通过 | `db.py:365-486` 14 个触发器；**实测**：内容列 UPDATE 被拒、生命周期列放行、DELETE 被拒；`research/api.py:136-160` 检索无归属过滤 |
| 15 | 实验=固定骨架+可插拔评估方法 | 通过 | 骨架五条在 `experiments.py`；评估模块注册表 `evaluations/base.py`；首批四个+预留 head_to_head 均注册 |
| 16 | 模块治理无人审必经门（自动测试门+DSL 免测） | **偏离（P1）** | DSL 免测 ✓（`modules.py:63-71`）；python 三门仅 signal 槽全跑，非 signal 槽跳过确定性+前缀稳定性（`module_gate.py:123-128`）；开发日志阶段 5 自述"python 模块过契约/确定性/前缀稳定性三门"与实现不符 |
| 17 | 课题文件夹制 | 通过 | `research/topic_files.py`（TOPIC.md + experiments/<id>/REPORT.md + report.json + manifest.json，含 data_version 注记）；conclude/confirm 时物化（`api.py:197-207`） |
| 18 | 两级对象（课题必选归属、强制终态结论） | 通过 | `experiments.py:79` require_open_topic；`topics.py:80-150` 关题校验（终态+引用+结论必填） |
| 19 | 一期=阶段 0–5；二期项不做 | 通过 | 二期项全未实现（因子工业化/加仓 lot 账/meta-portfolio/组合优化 sizing/冲击成本/purged CV/分布式/Kelly/展示分支迁移/数据线——grep+逐包核查）；一期阶段 5 清单（walk-forward/Sortino 推断/head_to_head/recompute/看板 UI/判定器 golden）全在 |
| 20 | 课题结论量化纪律 | 通过 | `research/conclusion.py:39-115`（verdict 计数含失败/效应量中位数与方向一致率/课题内 BH-FDR/警告聚合/四档分级可降不可升/明示不做跨实验 p 值合并） |

### 二、详设 §4.1 / §5.4 / §5.5 / §5.6 / §5.9 / §5.14

| 项 | 结论 | 证据 |
|---|---|---|
| §4.1 六张 engine_* 表结构 | 通过 | `db.py:108-206`（runs/orders/fills/unfilled/positions/daily_nav 字段全覆盖，含 module_state_json、run_params_json 合理增补）；落库路径 `engine/store.py` |
| §5.4.1 日循环五步 | 通过 | `portfolio/backtester.py:204-273`（begin_day→evaluate_exits(止损先)→select_entries→尾盘成交→settle）；风控先于信号、卖先于买 |
| §5.4.2 八条写死语义 | 通过 | 先卖后买 ✓（卖单先执行现金可用）；边卖边买拒绝 ✓（`backtester.py:416-421` exited_symbols 过滤）；失败不递补 ✓（admitted 定稿后逐一直接下单，失败只记 unfilled）；止损优先 ✓（stop_intents 先于 signal_exits/rotation，`backtester.py:231`）；整仓卖 ✓（`matcher.py:157,215`）；不加仓 ✓（持仓标的 entry 被过滤）；计息 1% ✓（`engine/account.py:68-71`）；窗口默认 2015-01-01 ✓（`holdout.py:19-21` 为全局默认，backtester 不感知窗口） |
| §5.5 策略库+benchmark 怀疑阶梯 7 条 | **通过（附 P2 口径替换）** | 7 条+blank-base 全部入库（`seed.py:18-28` + strategies/*.yaml）；但 60/40 债腿改现金、动量轮动 518880→518850、equal_weight 改流动性 top-50——三处与 §5.5 字面配置不同（YAML 注释+开发日志已声明，P2-3） |
| §5.6 服务面 | **部分通过（P2）** | resolve_experiment_config ✓、run_backtest ✓（同步执行+L4 worker 异步，可接受）、只读目录 ✓；**缺 get_run_status/get_run_report**（`portfolio/service.py` 全文无） |
| §5.9 MVP 边界 | 通过 | 无加仓/无部分卖出/默认 hold_first 不主动轮换/无做空/无时点动态池（逐包核查；buffered_rotation 是 §5.14 要求的预置模块，非默认行为，不违反） |
| §5.14 首批预置模块清单 | **偏离（P1）** | 数量全量：universe 3 + signal 8 + rank 5 + sizing 5 + portfolio_risk 6 + position_risk 9 + execution 4 + 元模块 any_of/all_of，与清单逐项相符；但 **trend_score_cross 是死模块**——`prepare_with_gateway`（`slots/signal.py:288`）无任何调用方（backtester/live/event/bucket 只调 `prepare`），注册可用但永远产不出事件 |

### 三、详设 §6.3 / §6.4 / §6.5 / §6.6 / §6.7 / §7

| 项 | 结论 | 证据 |
|---|---|---|
| §6.3 台账五表 | 通过 | `db.py:249-332`（sessions/topics/experiments/runs/verdicts 字段全覆盖；topics.experiment_ids_json 以反查替代，更优）；append-only 实测有效 |
| §6.4 状态机与终态 | 通过 | `research/lifecycle.py:21-37` 合法转移表；三终态 verdicted/failed/rejected_intake；archived 为标记非状态；烂尾巡检 `lifecycle.py:125-139`；final 可降不可升 `verdict.py:136-141`；reasoning 必填 |
| §6.5 四+1 评估模块 verdict 规格 | **基本通过（附 P2 缺口）** | 统一信封 ✓；backtest 证据（Δ指标+regime+高原+判定）✓；event（前瞻收益差值+路径+bootstrap 噪声带）✓ 但缺 context_filter 与可选迁移矩阵（P2-8）；bucket（分桶+单调性+随机打乱）✓；distribution 固定 inconclusive ✓（`distribution.py:116` allowed_finals）；"换手增幅成本可解释"未入判定规则（P2-6） |
| §6.6.1 创建校验五条 | 通过 | `experiments.py:81-131`（假设≥10 字/评估模块已注册+spec 过模块 schema/单变量机器检查/引用合法/attempt 平台赋值）；创建型 blank-base 自动识别豁免（`evaluations/backtest.py:88-92`） |
| §6.6.2 对照平台生成 | 通过 | 基准同窗重跑由平台执行（`evaluations/backtest.py:298-343`）；无条件分布同窗（`_common.py:104-125`）；随机打乱（`bucket.py:196-211`） |
| §6.6.3 平台自动警告五条 | 通过（附 P2） | 样本少/重叠/截面相关/单一 regime/幸存者偏差五项全在 `_common.py:157-183`；幸存者恒带（当前无时点池，正确落实）；但 §6.6.4 注记①的 2015–2019 覆盖率警告未进 verdict（P2-5） |
| §6.6.4 holdout 工程化 | 通过 | sample=[2015-01-01,2024-12-31]、token 一次性、计数、holdout_touched 留痕、enforced 开关默认开（`holdout.py`）；非实验路径不拦截（`scripts/run_base_v1_sample.py` 直跑无卡控）✓；高原补跑同窗同 token 覆盖 ✓ |
| §6.6.5 尝试计数+DSR | **通过（附 P2 口径）** | attempt_index=同 subject_key 计数+1、rejected_intake 不计（`experiments.py:112-121`）；PSR/DSR/MinTRL 公式正确（`stats/psr.py`，golden 对拍 15 项绿）；bucket 的 subject_key=signal:feature 比设计细（P2-7） |
| §6.6.6 重复检测 | 通过 | `experiments.py:38-55,123-131`（同模块+同研究线+同 diff 签名→duplicate_of 含失败记录，allow_duplicate=True 放行） |
| §6.6.7 recompute campaign | 通过 | `research/recompute.py`（批量复核 verdicted 实验、新 verdict 带 supersedes 指针、旧行不改——与 v3 修订口径一致） |
| §6.7 权限与沙箱五层 | **部分通过（P1+P2）** | 触发器+列白名单 ✓（决策 2 澄清合理：纯无 UPDATE 与状态机矛盾，白名单是意图的忠实实现）；数据隔离受限句柄 ✓ 但**绑定时刻错误**（P1-1）；运行/状态/并发/逻辑四层 ✓；owner_session 写隔离偏弱（P2-17） |
| §7 存储预算 | **基本通过（附 P2）** | 性能实测达标：5 年×874 标的 79.4s、10 年 88.8s ≤ 2min（开发日志，与 comparison.json elapsed 一致）；但无约束 v1 单 run 16 万行 unfilled ≈ 预算 8 倍（P2-16，忠实复刻的诚实结果，预算口径需复核） |

### 四、分期边界（后续TODO.md）

| 项 | 结论 |
|---|---|
| 二期 10 项（因子工业化/加仓 lot 账/数据线/meta-portfolio/组合优化 sizing/冲击成本/purged CV/分布式/Kelly/展示分支迁移） | **全部未实现 ✓**（逐包+grep 核查；research/dsl.py 是决策 16 的模块治理 DSL，非因子 DSL，不属违规） |
| 永不做（做空/基本面因子） | **均未出现 ✓**（matcher 仅 buy/sell 多头；无财务因子代码） |

### 五、附录 A 与 sample 窗口

| 项 | 结论 | 证据 |
|---|---|---|
| base v1 YAML 与附录 A 逐行一致 | 通过 | `src/portfolio/strategies/base_v1.yaml`：universe category_filter@1(asset_type: all) / signal macd_cross@1(12,26,9,use_exit:false) / rank by_freshness@1 / sizing equal_risk@1(0.0075) / portfolio_risk [] / position_risk hard_stop@1(atr_mul:1.5) / execution tail_session@1(0.002,0.001)——逐字一致 |
| sample 窗口 2015-01-01~2024-12-31 | 通过 | `holdout.py:19-21` 全局默认；`data/research/base_v1_sample/comparison.json` 实测 window=['2015-01-01','2024-12-31']，v1+买入持有+60/40+随机入场四条曲线指标与开发日志数字完全一致 |

### 六、测试实证（本评审复跑）

- 新增测试全绿：unit 98 项（stage0 26 + stats 15 + fees/matcher 17 + golden 7 +
  stops parity 6 + gateway 14 + portfolio_config 11 + run_freeze 含内）+
  integration/api 37 项 = **135 项全过**；
- append-only 触发器冒烟实测：内容列 UPDATE 拒、生命周期列 UPDATE 放、DELETE 拒；
- 存量回归：`tests/` 主体 306 项过，仅 `test_instruments_bulk_backfill.py` 1–2 项
  失败——**已用 git stash 对照证实为改动前就存在的 Windows 临时目录清理竞态
  （WinError 32），与本次新栈无关**。

---

## 发现的偏离清单

### P1（应改，阻断"通过"评级）

**P1-1 回测 DayContext 的受限句柄绑定在 run 末日 as_of 上——PIT 沙箱对
AI 新模块不成立。**
设计出处：详设 §5.2（DayContext.as_of = t 收盘；"gateway L1.5 受限句柄
（as_of 已绑定，模块拿不到未来数据）"）+ §6.7 沙箱第 1 层。
代码位置：`portfolio/backtester.py:138`（`as_of = datetime.combine(end, 15:00)`，
end 为整个回测窗口末日）+ `backtester.py:202`（一次 bind 全程复用）+
`backtester.py:214-221`（每日 ctx 都挂这同一个 run 末日句柄）。
后果：内置模块全部走 PanelView（逐日限窗）所以当前无泄漏；但任何 AI 提的
python/DSL 模块若调用 `ctx.gateway.get_panel(start=…, end=…)`，可拿到当前
决策日 t 之后直到 run 末日的数据——未来函数通道。module_gate 的探针上下文
gateway=None（`module_gate.py:91`），也探不到这种用法。
修复：日循环内按日 bind（as_of=当日 15:00），成本极低。

**P1-2 分层铁律 7 处直接违反（决策 7/10，硬约束）。**
设计出处：架构稿 §2.1 依赖铁律两条；详设 §1 依赖规则。
代码位置：
- L3 反向依赖 L4：`portfolio/library.py:13`（`from research.ledger import
  row_to_dict, rows_to_dicts`——两个 10 行工具函数导致 portfolio→research
  反向边）；
- L4 跨级直连 L2：`research/evaluations/backtest.py:395`、`research/topic_files.py:115`
  （`from engine.store import EngineStore`——读 fills/nav/run 记录；执行面
  L4→L3→L2 禁止跨层）；
- L3/L4 绕过 gateway 直读 L1 元数据：`portfolio/backtester.py:302`、
  `portfolio/live.py:287`、`research/evaluations/_common.py:30`
  （`db.get_instrument_metadata_map()`；`gateway/metadata.py:46` 的
  enabled_symbols/instruments 是现成等价物）；
- L3 直接 import L1 数据服务：`portfolio/live.py:372-373`
  （`from data.intraday_service import build_synthetic_bar` /
  `from data.service import get_data_service`——§3.1 写明 live 模式的盘中
  合成 bar 由**门面**复用 intraday_service 实现，现合成逻辑驻留 L3 注入）。

**P1-3 决策 A3 冻结门有洞：日更被冻结顺延之后，post-update pipeline 照跑。**
设计出处：详设 §3.1 运行期数据冻结（"回测运行期间冻结数据写入……防止
qfq 原位重写导致一次 run 读到两版数据、血缘失真"）。
代码位置：`core/jobs.py:143-158`（冻结时日更返回
`status="deferred_backtest_running"`）→ `app/main.py:213-225`（只拦截
skipped_non_trading_day，deferred 状态继续执行
`run_post_update_pipeline`）→ `services/indicator_builder.py:199-200`
（symbols 为空时 fallback 到**全池**做股息断裂检测→`repair_broken_symbols`
qfq 原位重写 + `rebuild_all` 指标重建）——全部发生在回测 run 活跃期间，
正是 A3 要防的场景。另外 `jobs.py:143` 的 `force=True` 路径（启动补偿）
完全绕过冻结门，与 research worker 启动即提交 queued 实验存在竞态。
修复：deferred 状态直接 return 不跑 pipeline；pipeline 入口自查
`run_freeze.is_frozen()`；force 路径至少记录冻结冲突。

**P1-4 trend_score_cross 是死模块——§5.14"首批预置模块清单全量可用"不成立。**
设计出处：详设 §5.14（signal 首批 8 个含 trend_score_cross）+ §5.2.2
（趋势相位类信号经 L1.5 透传直消费）。
代码位置：`portfolio/slots/signal.py:288`（`prepare_with_gateway`）；
全部调用点只认 `prepare`：`backtester.py:179-180`、`live.py:170-171`、
`research/evaluations/event.py:134-135`、`bucket.py:116-117`。
后果：该模块 `_series` 恒为空，scan 永远返回空事件——回测/实盘/事件研究
三条路径全部静默无信号，且无任何测试发现。

**P1-5 模块自动测试门对非 signal 槽跳过两门探针，且开发日志自述失实。**
设计出处：架构稿决策 16 + 详设 §6.2.2（新模块过契约/确定性/前缀稳定性
三门方可执行，无槽位豁免条款）。
代码位置：`research/module_gate.py:123-128`（非 signal 槽 determinism 与
prefix_stability 直接标 "skipped (non-signal slot)" 并放行 reviewed）。
文档失实：开发日志阶段 5 自称"python 模块过契约/确定性/前缀稳定性三门
自动测试，前视模块被探针抓住打回"——对非 signal 槽不成立。一个带前视的
python position_risk 模块今天即可 reviewed 并被实验引用。
修复：或补齐非 signal 槽的探针脚手架（持仓/账户桩），或在模块治理层硬
限制一期 python 模块只接受 signal 槽（与 DSL 一致的口径），并修正日志。

**P1-6 backtester 模块实例化的槽位上下文泄漏（潜在实例化错误）。**
代码位置：`portfolio/backtester.py:91-108`——`instantiate_modules` 第二个
循环 `for binding in config.gates: spec = registry.require(binding.module,
slot=slot)` 中的 `slot` 是第一个循环的泄漏变量（恒为 "execution"）。
后果：内置 gate 名只在 portfolio_risk 槽注册，靠 registry 的全局 fallback
蒙对；若 gate 引用元模块（any_of@1 在 signal 与 position_risk 都注册），
会按 "execution" 上下文错误解析/fallback 到不确定实现。
修复：改为 `slot="portfolio_risk"`。

### P2（建议，不阻断）

1. **§5.6 服务面缺 get_run_status / get_run_report**（`portfolio/service.py`
   只有两个运行级接口；reports.build_report 存在但未挂服务面）。
2. **§6.7 服务面缺 append_experiment_to_topic**（实现改为创建时必填
   topic_id 且 topic_id 在 append-only 白名单外不可改，post-hoc 挂题不存在；
   设计服务面清单列了该操作——或补操作，或走修订流程从清单删除）。
3. **benchmark 三处口径替换**：60/40 债腿→现金（年化 1%）、动量轮动
   518880→518850、equal_weight→流动性 top-50（`strategies/bench_*.yaml`）——
   与 §5.5 字面配置不同，数据可得性驱动且 YAML/日志已声明；建议把替换说明
   同步进策略库 description 字段与对比报告（目前只在 YAML 注释与日志里）。
4. **regime 拆分 fallback 口径变形**：`evaluations/backtest.py:376`
   （`bench_nav_for_regime or base_nav`）——沪深300 bench 不可用时退化为用
   基准策略自身 NAV 的 SMA200 做 regime 标签（设计口径是市场基准 SMA200）；
   应降级为空 regime + 警告（警告已有，计算应停）。
5. **§6.6.4 注记①覆盖率警告未进 verdict**：collect_warnings 无"2015–2019
   段覆盖率约 1/7 池"条目；三注记目前只在阶段 2 验收产物
   （comparison.json caveats）里带，实验流水线 verdict 不带。
6. **§6.5.1 "换手增幅成本可解释"未入 confirmed 判定规则**
   （`verdict_rules.py:69-91` 无费用/换手项；fee_total 在 evidence 但不参与
   suggest_backtest_verdict）。
7. **bucket 的 subject_key 口径比设计细**：`evaluations/bucket.py:80-82`
   （signal:feature）vs §6.6.5（event/bucket = 信号模块 name）——同一信号
   换特征会另起研究线，attempt_index/DSR 的 N 被系统性低估。
8. **event_study 缺 context_filter（样本条件过滤）与可选迁移矩阵**
   （§6.5.2 示例参数与证据③；regime_split 只覆盖事后拆分）。
9. **panel 字段表硬编码**：`gateway/panel.py:24` BAR_FIELDS——§2 要求
   "新增数据类自带字段清单并注册到门面，门面不做字段硬编码"（预留未做，
   当前只有 K 线数据集）。
10. **新股 5 日无涨跌停限制属超设计行为**：`gateway/tradability.py:35`
    （§3.2"依赖上市日期元数据，同上"可读解为阶段 7 前按主板处理；实现
    更精确但超出字面，建议回写设计或注释标注依据）。
11. **元模块未覆盖全插槽**（§5.2.8"全插槽通用"：实现只有 signal 的
    any_of/all_of + position_risk 的 any_of）；`buffered_rotation` 的
    max_swaps 参数声明未使用（`slots/execution.py:79,337`）。
12. **library.add_version 不校验"实验 id 或 benchmark 标记"**（§5.5 入库
    规则）——目前晋升通道未暴露所以无实际缺口，规则靠通道缺失兜住。
13. **新模块治理的一期限定未回写设计**：DSL/python 均只有 signal 槽可装载
    （`modules.py:184-185`）；已退役模块在当前进程注册表内仍可被新实验
    引用直至重启。
14. **研究栈直接 import rule_backtest.metrics**（`reports.py:21`、
    `evaluations/backtest.py:119`、`head_to_head.py:128`）——详设 §1 禁止
    新栈反向依赖并行分支，§5.4.3/§5.8 又明确要求复用 compute_summary，
    **设计文档内部冲突**；实现遵从了 §5.4.3。建议按 §1 的"单向打捞"口径
    把 compute_summary 迁为共享件，或修订 §1 文字。
15. **开发日志小误差**：MCP 研究工具实为 10 个（research_tools.py），日志
    称 11 个。
16. **§7.1 存储预算口径**：无组合约束 v1 十年窗口单 run 产出 160,565 行
    unfilled（comparison.json 实测）≈ "每 run ≈ 2 万行"预算的 8 倍——忠实
    复刻的诚实结果，但预算口径需复核或在报告标注。
17. **owner_session 写隔离偏弱**：任何会话可 confirm 任何实验的 verdict
    （`verdict.py:105-152` 无归属校验）；通道默认共享会话使写入命名空间
    实际按通道而非按会话（§6.7"写按 owner_session 隔离"的弱化）。

### 开发日志自认澄清点的判定

- 决策 1（DDL 集中 db.py、访问逻辑归层 store）：**认可**，合"顺手拆、不
  单独立项"。
- 决策 2（append-only=列白名单触发器）：**认可**，纯无 UPDATE 与 §6.4
  状态机矛盾，白名单是意图的忠实实现，实测有效。
- 决策 3（涨跌停基准价 ÷f_t）：**认可**，§3.2 括号内"与交易所口径一致"
  为权威，"×因子"是宽松措辞，实现方向数学正确且有除权日单测
  （test_tradability_ex_dividend_base_price）。
- 决策 4（冻结进程内边界）：**部分认可**——进程边界本身一期可接受，但
  未覆盖 P1-3 的进程内 deferred-pipeline 洞。
- 决策 5/6（测试口径、本地全量数据原则）：**认可**。
- 阶段 1 各项（T+1 begin_day、逐手递减、停牌日照推涨跌停、MACD 预热
  对齐、计息隔离 parity、止损不叠滑点、止损 unfilled 语义、面板一次取数）：
  **认可**（止损 unfilled"触发才落"经调用路径核实成立——模块层无 bar 不
  产意图，matcher 防御分支不可达）。
- 阶段 2 各项（benchmark 替换、元模块注册键带槽、top-50 近似、unfilled
  量级、性能优化、ATR 预计算）：**口径替换类转列 P2-3**；其余认可。
- 阶段 2∥（幸存者恒带、spec.expect 机器可判腿、criterion 文本）：**认可**，
  expect 是对"符号与假设一致"判定条的合理补强。
- 阶段 4（现金重建近似、14:05、冻结门）：**现金口径认可；冻结门见 P1-3**。
- 阶段 5（判定器对拍、并发池、重复检测、recompute、模块治理、课题纪律、
  通道）：**模块治理自述失实，见 P1-5**；MCP 工具数误差见 P2-15；其余认可。

---

## 给开发者的修复建议

按优先级排序（P1 全部修复后即可转"通过"）：

1. **P1-1**：`backtester.py` 日循环内按日创建 BoundGateway
   （`gateway.bind(as_of=datetime.combine(day, time(15,0)), …)`），替换
   当前的 run 级单次 bind；补一条"模块经 ctx.gateway 取数不得见 t 之后"
   的 PIT 测试。
2. **P1-2**：把 `row_to_dict/rows_to_dicts` 挪到中立位置（如
   `data/storage/db.py` 或 core 工具）消除 portfolio→research 边；
   L4 读取 engine 运行记录改经 L3（在 portfolio.service 补只读接口，顺带
   补齐 P2-1 的 get_run_status/get_run_report）；三处元数据读取改用
   `gateway.metadata`；live overlay 的合成 bar 实现下沉到 gateway 包内
   （gateway 复用 intraday_service，live.py 只声明使用）。
3. **P1-3**：`_run_daily_update` 对 `deferred_backtest_running` 直接
   return；`run_post_update_pipeline` 入口自查 run_freeze；force=True 路径
   记录冻结冲突日志（或同样顺延）。
4. **P1-4**：给 TrendScoreCrossSignal 补标准 `prepare(panel)`（内部经
   ctx/bound gateway 取 trend_score），或在 backtester/live/evaluations 的
   prepare 调度处兼容 prepare_with_gateway；补一条该模块出信号的测试。
5. **P1-5**：二选一——为非 signal 槽补齐探针脚手架（持仓/账户桩），或
   在 `modules.propose_module` 硬限制一期 python 模块仅 signal 槽（与 DSL
   一致）并修正开发日志措辞。
6. **P1-6**：`instantiate_modules` gates 循环改 `slot="portfolio_risk"`。
7. P2 批次：regime fallback 改空+警告；bucket subject_key 回 §6.6.5 口径；
   覆盖率警告进 collect_warnings（窗口起点 < 2020 时携带）；60/40 等替换
   说明写进策略库 description；max_swaps 要么实现要么从 schema 删除；
   修正 MCP 工具数；评估 compute_summary 归属（打捞共享或修订 §1）。

---

A_VERDICT: FAIL
