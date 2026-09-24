# A-复审-R2（评审子代理 A，第二轮）

> 日期：2026-09-23（provider 中断后恢复复核，当日完成）
> 复审基准：R1 报告 `A-方案一致性评审.md` 的 6 项 P1 + 17 项 P2；
> 设计基准不变（架构稿 20 条决策 / 总体方案 / 后续TODO）
> 方式：逐条读新代码 + 功能探针实证 + 测试复跑（只读评审，未改任何代码；
> 探针脚本为一次性临时文件，用完即删，未入库）

---

## 总体结论

**6 项 P1 中 5 项完全修复并经功能探针实证；A-P1-4 为部分修复**
（backtest/live 两条主路径已接线且可用；event_study/bucket_analysis 的
双重身份路径仍未接线——trend_score_cross 作为事件源仍静默产出 0 事件，
功能探针实证）。另有一项 R1 发现的次级残留（冻结门 force 路径）降级为
P2 记录。本轮无新引入的 P0/P1 偏离。

按评审章程（P0/P1 未决即 FAIL），本轮仍判 **FAIL**，唯一阻断项是
A-P1-4 的残留两条腿（修复量为event/bucket 各约 3 行，与 backtester 同构）。
其余全部一致，下一轮预期可直接通过。

---

## 逐项复核（原 6 项 P1）

### A-P1-1 PIT 沙箱洞 → **已修复（功能探针实证）**

- 代码证据：`src/portfolio/backtester.py:216-229`——日循环内逐日
  `bound = gateway.bind(as_of=day_as_of, ...)`（day_as_of = 当日 15:00），
  DayContext 挂当日句柄；run 级句柄仅用于 prepare_with_gateway 的一次性
  取数（见 A-P1-4），与日内决策隔离。
- 功能实证（临时探针，合成 40 交易日 × 2 标的实跑回测）：自定义 signal
  模块每日经 `ctx.gateway.get_panel(end=当日+15天)` 试探——21 个决策日
  可见最大日期全部 ≤ 当日（0 泄漏）；每日再试显式 `as_of` 覆盖——21 次
  全部被拒（GatewayViolation）且全部落 gateway_audit（violation:* 21 行）。
  结论：模块经 ctx.gateway 在 day t 物理上只能取 ≤t 数据，越权留痕。
- 备注：逐日 bind 每 run 增加约 2400 次 data_version 轻查询，性能无碍。

### A-P1-2 分层铁律 7 处 → **已修复（全量静态扫描复证）**

- `portfolio/library.py:14-22`：row_to_dict/rows_to_dicts 已内联，
  无 research import（L3→L4 反向边消除）；
- L4→L2 跨级直连消除：`portfolio/service.py:24-46` 新增
  `load_fills / load_nav / get_engine_run` 转发面；
  `research/evaluations/backtest.py:421,517` 与 `research/topic_files.py:115-119`
  全部改经 portfolio.service（注释明示"分层铁律：L4→L3→L2"）；
- 三处元数据绕过消除：`backtester._universe_symbols` 改收
  `gateway.metadata`（backtester.py:144,313 签名换为 metadata_svc）、
  `live._live_universe_symbols` 改 `MetadataService(db).enabled_symbols`
  （live.py:294-301）、`_common.resolve_universe_symbols` 同改
  （`_common.py:30-33`）；
- live overlay 下沉：`src/gateway/live_overlay.py`（L1.5 内复用
  intraday_service，L1.5→L1 合法）；`portfolio/live.py` 与 `core/jobs.py`
  不再 import data.service/data.intraday_service。
- 复扫确认：portfolio 包无 research/data import；research 包无
  engine.store import。残留说明（不判偏离）：`research/module_gate.py:69,127,137`
  import engine.models 的**纯类型**（Account/Position/OrderIntent/Fill）——
  这些是 L3 插槽协议的构成类型（§5.2 协议签名即含 OrderIntent/Fill），
  属协议类型共享而非执行/数据调用，类比 core 共享库，不判违反；
  `research/evaluations/backtest.py:119`、`head_to_head.py:128` 仍 import
  rule_backtest.metrics——R1 P2-14 记录的设计文档内部冲突（§1 vs §5.4.3），
  维持 P2 不升级。

### A-P1-3 冻结门洞 → **已修复（主洞）；force 路径残留降级 P2**

- 代码证据：`src/app/main.py:213`——
  `if payload.get("status") in ("skipped_non_trading_day",
  "deferred_backtest_running"): return`，冻结顺延后 post-update pipeline
  （除权检测+qfq 重写+指标重建）不再抢跑。R1 的主攻击面封死。
- 残留（降级 P2，不阻断）：`core/jobs.py:143` 的 `force=True` 路径
  （启动补偿 `_daily_update_catchup`）仍完全绕过冻结门——启动时刻 research
  worker 亦启动并提交 queued 实验，存在窄竞态窗口。建议后续给 force 路径
  加冻结等待或冲突日志。R1 该发现的主向量（deferred 顺延仍跑写任务）已消除，
  故本条判已修复、残留单列。

### A-P1-4 trend_score_cross 死模块 → **部分修复（残留阻断本轮 PASS）**

- 已修复部分：`backtester.py:186-188` 与 `live.py:177-180` 均接线
  `prepare_with_gateway`（run 级 as_of 一次取数，模块 scan 内
  `series[series.index <= ctx.date]` 逐日因果过滤）——回测/实盘两条主路径
  可用，§5.14 清单在 L3 工具箱意义上已全量可用。
- **未修复部分**：`research/evaluations/event.py:135` 与
  `research/evaluations/bucket.py:117` 仍只调 `module.prepare(panel)`，
  未接 `prepare_with_gateway`——TrendScoreCrossSignal 无 prepare 方法，
  `_series` 恒为空，scan 静默返回空事件。
- 功能实证（临时探针）：以 trend_score_cross@1 为事件源跑 event_study，
  返回 `events = 0`——静默空结果会披上"该信号无事件/inconclusive"的
  合法外衣进台账，正是平台纪律要防的安静撒谎形态。R1 该发现的证据明确
  覆盖"回测/实盘/事件研究三条路径"，双重身份（§6.1.3：同一注册模块同时
  是 event_study/bucket 的合法事件源）是设计闭环的一部分，故判部分修复。
- 修复指引：event.py/bucket.py 各加与 backtester.py:186-188 同构的三行
  （bound = Gateway(db).bind(as_of=窗口末, caller_layer="research",
  run_id=experiment.id) → hasattr 检查 → prepare_with_gateway）。

### A-P1-5 模块测试门 → **已修复（功能探针实证）**

- 代码证据：`research/module_gate.py` 重写——`_SLOT_PROTOCOLS` 覆盖七槽
  （20-28 行），`_make_ctx` 提供带持仓/账户的完整 DayContext 脚手架
  （67-94 行），`_probe` 每槽独立探测动作（113-160 行），确定性 +
  前缀稳定性对**全部插槽**运行（170-219 行，无非 signal 跳过分支）。
- 功能实证：position_risk 槽提交前视模块（prepare 用全史最高价定止损）→
  prefix_stability=False 被拒；同槽干净模块 → 三门全过 reviewed。
- 固有边界（非新偏离，设计已认领）：前缀稳定性探针只能抓住"未来数据
  实际改变 ≤t 输出"的前视（本探针中 nanmin 型前视因随机漫步数据特性
  未显现而漏过）——决策 16 已明确此类"自洽的错误"靠 golden+人抽检兜底。
- 模块装载侧顺带加固（超出本发现范围的正向改动）：
  `modules._load_python_module` 加 AST 预筛+受限 builtins+白名单库，
  与 §6.7 沙箱第 5 层同向。

### A-P1-6 槽位泄漏 → **已修复**

- 代码证据：`backtester.py:104`——gates 循环显式
  `registry.require(binding.module, slot="portfolio_risk")`，不再依赖
  泄漏的循环变量与 fallback 巧合。

## 本轮新增项核验（R1 P2 缺口的主动补齐）

- `research/api.py:103-120`：get_run_status（run_status 别名）/
  get_run_report（experiment → report + runs 血缘）——**已核验存在**。
  注意：R1 P2-1 的严格字面是 §5.6 的 **L3** 服务面 run_id 键控接口；
  本轮补在 L4 服务面（experiment 键控），portfolio/service.py 侧由
  get_engine_run(run_id) 覆盖状态查询。P2-1 判"实质补齐、字面残留"，
  维持 P2 不阻断。
- `research/api.py:122-141`：append_experiment_to_topic——**已核验**：
  仅 queued 状态可改挂（开跑冻结血缘），目标课题须 open；db.py:368-381
  触发器 WHEN 子句已将 topic_id 移出内容白名单（归属不是证据内容，
  生命周期字段由代码层守卫）。与设计"append-only + 状态机代码层强制"
  的分层一致，不判偏离。

## 新引入偏离检查

- 全量静态复扫（import 边、直连 db 读取）：无新增跨层边；
- 一次性 codemod 脚本 `_fix_layering.py` 已在两轮之间从仓库根清除，
  工作区无遗留垃圾（本评审的探针脚本亦即用即删）；
- backtester 新增 try/except 崩溃落 failed（消除 running 孤儿行）与
  live._live_universe_symbols 并入当前持仓（防止止损失管）——均为
  他评审驱动的一致性增强，与既有设计同向，无冲突；
- 存量回归：jobs/scheduler/freeze 相关 45 项过，唯一失败仍为
  test_instruments_bulk_backfill.py 的 Windows 临时目录清理竞态——R1 已
  用 git stash 对照证实为改动前即存在的环境性 flake，非本次引入。

## 测试实证汇总（本轮复跑）

- 新增/相关套件 143 项全绿：unit 105（含 test_review_gaps.py 新增的
  评审缺口回归）+ integration/api 38；
- 功能探针 2 项：A-P1-1 逐日绑定 PIT 实证（21 日 0 泄漏 + 21 次越权全拒
  全留痕）；A-P1-5 非 signal 槽前视探针实证（前视拒/干净过）；
- A-P1-4 残留实证：event_study × trend_score_cross → events=0（静默空）。

## 残留清单（阻断 1 项 + 不阻断记录）

| 项 | 级别 | 状态 |
|---|---|---|
| A-P1-4 残留：event.py/bucket.py 未接 prepare_with_gateway（趋势相位类模块的双重身份路径静默为空） | **P1（阻断）** | 未修复 |
| A-P1-3 残留：force=True（启动补偿）绕过冻结门 | P2 | 未修复，窄启动竞态 |
| R1 P2 清单其余各项（60/40 口径替换、regime fallback、bucket subject_key、覆盖率警告、换手成本入判定、元模块覆盖面、max_swaps、owner_session 写隔离、存储预算口径、MCP 工具数等） | P2 | 维持，不阻断 |

## 给开发者的修复指引（唯一阻断项）

`research/evaluations/event.py` 与 `research/evaluations/bucket.py` 的
runner 中，`module = spec_obj.factory(...)` 之后补（与
`portfolio/backtester.py:186-188` 同构）：

```python
if hasattr(module, "prepare_with_gateway"):
    from gateway.service import Gateway
    bound = Gateway(db).bind(as_of=<窗口末 15:00>, caller_layer="research",
                             run_id=experiment["id"])
    module.prepare_with_gateway(bound, list(panel.symbols), <面板起点>)
```

并补一条"trend_score_cross 经 event_study 产出非空事件（或数据缺失时
显式告警而非静默空）"的回归测试。元模块嵌套场景（any_of 内含
trend_score_cross 子模块）的 prepare_with_gateway 透传可一并考虑
（当前 AnyOfSignal.prepare 只透传 prepare，P2 级）。

---

A_VERDICT_R2: FAIL
