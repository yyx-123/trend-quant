# A-复审-R3（评审子代理 A，终审）

> 日期：2026-09-23
> 终审范围：R2 唯一阻断项（A-P1-4 残留：event.py/bucket.py 未接线
> prepare_with_gateway）+ B 复审驱动的四处附带修改（知情核验，非本线阻断项）
> 方式：代码走读 + 功能探针实证 + 全量新测试复跑（只读评审，未改代码；
> 探针脚本一次性、用完即删、未入库）

---

## 总体结论

**通过。** R2 的唯一阻断项已修复并经功能探针实证：trend_score_cross 作为
event_study / bucket_analysis 事件源现在能产出真实事件（不再是静默 0 事件
伪装成合法 inconclusive 进台账），生产指标取数经受限句柄并留痕
（caller_layer=research、run_id=实验 id）。B 线四处附带修改核验无偏离。
新增/相关测试 143 项全绿。R1 的 6 项 P1 至此全部关闭；无未决 P0/P1。

## 阻断项复核：A-P1-4 残留 → 已修复

**代码证据**（与 backtester.py:186-188 同构，符合 R2 修复指引）：

- `src/research/evaluations/event.py:138-148`：`hasattr(module,
  "prepare_with_gateway")` → `Gateway(db).bind(as_of=窗口末 15:00,
  caller_layer="research", run_id=experiment.id)` →
  `module.prepare_with_gateway(_bound, symbols, start)` → `flush_audit()`；
  注释明示来源（评审 A-R2 的静默空结果风险）。
- `src/research/evaluations/bucket.py:121-131`：同构接线。
- `from datetime import datetime, time` 导入已在两文件头部（event.py:30 /
  bucket.py:11）。

**功能实证**（临时探针：合成 60 交易日 × 2 标的 + trend_daily 注入
threshold=5 的单次上穿）：

- event_study × trend_score_cross@1 → events=10（2 标的 × 5 个有效日，
  与模块 valid_days 语义一致），per-horizon 证据齐全（event_mean vs
  baseline_mean 同窗口径）；
- bucket_analysis × trend_score_cross@1 → events=10；
- gateway_audit 留痕正确：两条 `get_production_indicator:trend_score`，
  caller_layer=research、run_id 分别为探针实验 id。

至此 §6.1.3 双重身份闭环对生产指标类信号成立：同一注册模块在
回测（R2 已验）、实盘（R2 已验）、事件研究、分桶研究四条路径均可用。

## B 线附带修改的知情核验（非本线阻断项，确认无偏离）

1. **modules.py AST 预筛补 `__import__` 黑名单**（modules.py:152-156）：
   动态 `__import__("os")` 实测被拒（status=rejected，原因
   "name not allowed: __import__"）；import 语句经白名单库不受影响
   （`__import__` builtin 保留 + Name 黑名单拦截调用的配合设计合理）。
   信任模型已写死在文档（防误伤探针、非对抗沙箱），与 §6.7 层级一致。
2. **backtester 装配段独立 try/except**（backtester.py:178-216）：prepare/
   起算日等装配失败同样落 failed，与日循环段（218 行起）各自的 try 并列——
   不留 running 孤儿行，与台账"工程失败入表"口径一致。
3. **main.py lifespan 停 research worker**（main.py:326 `worker.stop()`）：
   进程退出不再遗留池线程，与并发池设计（崩溃只影响自身 run）同向。
4. **止损优先测试升级为价格可区分版**（止损跳空按开盘价成交 vs 信号尾盘价，
   执行序由价差区分）：强化的是 §5.4.2"止损优先"语义的回归精度，方向正确。

## 测试实证汇总

- 新增/相关套件复跑 **143 项全绿**（unit 105：stage0/stats/fees+matcher/
  golden/stops parity/gateway/portfolio_config/review_gaps；
  integration+api 38：backtester/parity/live/evaluations/stage5/ledger API）；
- 终审功能探针 1 项（上节）+ B 线 `__import__` 拒绝实证 1 项，均通过；
- 工作区无遗留探针/脚本垃圾。

## 三轮迭代的关闭台账（本线）

| 轮次 | 阻断项 | 结局 |
|---|---|---|
| R1 | 6 项 P1（PIT 绑定 / 分层 7 处 / 冻结门 / 死模块 / 测试门 / 槽位泄漏） | 全部确认 |
| R2 | 5 项修复实证通过；A-P1-4 残留（event/bucket 未接线） | 唯一阻断 |
| R3 | A-P1-4 残留修复并实证 | **全部关闭** |

P2 级记录项（60/40 口径替换、regime fallback、覆盖率警告、bucket
subject_key、换手成本入判定、force 路径冻结、元模块覆盖面、owner_session
写隔离、存储预算口径等）维持 R1/R2 清单，按章程不阻断，留待运行期评估。

A_VERDICT_R3: PASS
