# C 复审报告 R2：测试完备性与有效性（复审第 2 轮）

审查对象：R1 判 FAIL 后的修复轮——4 项 P1 的修复 + 新增
`tests/unit/test_review_gaps.py`（8 条缺口补测）+ 被改实现
（`src/engine/matcher.py`、`src/research/holdout.py`）。
复审方式：读新测试与实现 diff + 对钉值做**零项目代码**独立复算
（numpy/pandas 手写事件扫描/分桶/bootstrap）+ 实跑全部相关文件。

## 总体结论

**通过。** 4 项 P1 全部修复且证据确凿；R1 覆盖缺口清单 7 项中 6 项完整
补齐，1 项（止损优先于信号的"优先序"）部分补齐（见残留观察 R2-1，
非 P0/P1）。未发现新增虚假/无效断言。全部 143 个相关用例实跑通过。

```
unit 批（test_review_gaps + test_research_stage0 + test_engine_stops_parity
  + test_portfolio_config + test_research_stats + test_engine_fees_matcher
  + test_engine_golden + test_gateway）：105 passed in 13.86s
integration 批（test_research_evaluations + test_live_runner
  + test_portfolio_backtester + test_engine_parity
  + tests/api/test_research_ledger_api.py）：25 passed in 41.56s
integration/test_research_stage5.py：13 passed in 54.35s
```

## 原 P1 逐项复核

### C-P1-1 空体测试恒过 → 已修复

`test_portfolio_config.py` 中 `test_meta_nesting_rejected_by_strategy_layer`
已不存在（grep 零命中）；嵌套拒绝断言保留在
`test_meta_module_any_of_position_risk`（:119-126）内，非空转。
unit 批通过数 105 = 98（R1）+ 8（新增 gaps）− 1（删除空体），账目吻合。

### C-P1-2 弱断言（枚举重言/名不副实） → 已修复，钉值独立复算成立

`test_research_evaluations.py` 现已钉死确定性 verdict 与数值锚。我**不 import
任何项目代码**，从测试夹具配方（seed=5、40 标的 × 420 交易日）重新生成
原始数据，手写 SMA20 金叉扫描（含"上穿后 5 日内且仍在均线上"的事件语义）、
前瞻收益、无条件基线、bootstrap 噪声带、ATR% 分桶与随机对照，复算结果：

| 钉值 | 测试断言 | 我的独立复算 | 结论 |
|---|---|---|---|
| event n_events | `== 2705` | **2705**（精确一致） | 成立 |
| event delta_mean(10) | `≈ -0.00615` (abs 1e-4) | **-0.0061504** | 成立 |
| event verdict | `== "rejected"` | 噪声带 [0.01552, 0.01946] 整体低于基线均值 0.02365 → 显著且方向与 expect=positive 相反 → **rejected** | 成立 |
| bucket monotonicity | `≈ 0.25` | **0.25**（5 桶均值 [0.02419, 0.02004, 0.01833, 0.01220, 0.01297]，4 个相邻对仅 1 个同向） | 成立 |
| bucket q_spread | `≈ -0.01104` (abs 1e-3) | **-0.011217**（差 1.8e-4，在容差内；微小差异源于我复算的 TR 首行 NaN 处理与 pandas `.max(skipna)` 的口径差） | 成立 |
| bucket random_band | `is not None and > 0` | **0.006043 > 0** | 成立 |
| bucket verdict | `== "inconclusive"` | monotonicity 0.25 不满足 ≥0.8 也不满足 ≤0.2 → **inconclusive** | 成立 |

钉值不是实现自我抄录：它们与独立复算在精确（n_events）或四位有效数字
（delta/spread）层面吻合。`test_bucket_analysis_monotonicity` 现在名副其
实（monotonicity 本体被钉）。注意 q_spread 的 1e-3 容差略宽于必要（实际
离散度 ~2e-4），但方向与量级已锁死，不构成无效断言。

### C-P1-3 裸 `pytest.raises(Exception)` → 已修复（4/4）

| 位置 | 现状 | 实现侧消息核对 |
|---|---|---|
| test_research_stage0.py:576 | `pytest.raises(PermissionDenied, match="human session")` | sessions.py:66 "operation requires a human session" ✓ |
| test_research_stage5.py:308 | `pytest.raises(PermissionDenied, match="human session")` | 同上 ✓ |
| test_research_stage5.py:414 | `pytest.raises(TopicError, match="exceeds platform suggestion")` | topics.py:114 "grade {g} exceeds platform suggestion…" ✓ |
| test_live_runner.py:128 | `pytest.raises(LibraryError, match="not found")` | library.py:170 "strategy version not found" ✓（LibraryError 为 ValueError 子类，真实类型） |

残留（R1 已列为 P2、本轮未动，不构成阻塞）：
`test_research_evaluations.py:114`（raises(Exception) 但有 "event module
not registered" 消息校验）与 `:169`（raises(Exception) 无消息校验）仍偏宽，
建议顺手收紧为 IntakeRejected/LifecycleError。

### C-P1-4 生产库耦合 → 已修复

`test_engine_stops_parity.py:25-33` 新增 autouse 夹具
`monkeypatch.setattr("services.stop_loss.get_strategy_config", lambda: {...1.5/2.5})`。
核对调用链：`services/stop_loss.py` 以 `from core.strategy_config import
get_strategy_config` 值绑定导入，monkeypatch 打在该模块命名空间的名字上，
绑定正确；`compute_stop_loss` 全程使用显式传入的 `db=test_db` 与注入的
df/atr_series，`intraday=False` 不触实时报价——全链路不再触达全局
`get_db()`，干净机器上不会创建 `data/trend_quant.db`。修复有效。

## R1 覆盖缺口逐项复核（新增 test_review_gaps.py，8 条）

1. **matcher 触发先于阻塞 → 已修复（实现+测试双改）。**
   `matcher.py:204-213` 现为：先算 gap_through/touched，未触发 →
   `not_triggered`（不落 unfilled），触发后再判 is_limit_down/t1_block——
   与自身 docstring 一致。新测试双向钉住（跌停未触发 not_triggered；
   跌停+触发 unfilled(limit_down)）。R1 指出的"实现与 docstring 矛盾"消除。
2. **BoundGateway 全方法越权 → 已修复。** get_tradability /
   get_production_indicator 带 as_of 均 GatewayViolation，且 audit 留痕
   精确断言为 `["violation:get_tradability", "violation:get_production_indicator"]`。
3. **IPO 第 5/6 交易日边界 → 已修复。** 2024-03-04（周一）上市：
   3-08（第 5 日）no_limit=True、3-11（第 6 日）no_limit=False 且
   limit_up=11.0（10×1.1 手算一致）；`elapsed < 5` 的 off-by-one 被钉死。
4. **holdout token 错绑实验 + 原子消费 → 已修复（实现+测试双改）。**
   `holdout.py` 消费改为 `UPDATE … WHERE id=? AND consumed_at IS NULL`
   并校验 rowcount——真原子（R1 时点还是先查后写的 TOCTOU 两步）。
   错绑实验拒绝有 `match="bound to experiment"`。
5. **止损优先于信号 → 部分修复（见 R2-1）。**
6. **实盘对账卖出侧 → 已修复。** 用真实 DB 函数构造"应卖已卖"：
   diff_pct 钉 `9.90/9.97 − 1`（abs 1e-5，注释说明覆盖 round(...,5) 落库
   舍入——容差依据明确，不是拍脑袋宽容差）；"应卖未卖" →
   `missing_sells == ["BBB002.SS"]` 精确相等。
7. **DSR 绝对锚 → 已修复。** 钉 `0.5740092653864478`（abs 1e-9），与我
   R1 用 statistics.NormalDist 独立复算的值逐位一致。

R1 其余缺口（match_buy bar_close≤0、target_value reason 判别、20% 板块
端到端推导、get_production_indicator as-of 裁剪、除权×停牌组合、T+1 止损
顺延重试端到端）本轮未补——均为 R1"缺口/可加强"级，非 P0/P1，记录在案。

## 残留观察（非阻塞）

- **R2-1（止损优先序未真正钉住）**：
  `test_stop_priority_over_signal_same_day` 用 `always_entry` 信号——该
  信号只发 entry、**不产生任何 exit 事件**，所以同日根本不存在止损与
  信号退出的真实竞争；且 slippage=0 时止损跳空按开盘 7.5 成交与信号
  退出尾盘按收盘 7.5 成交价格相同，即使执行序反过来断言也照样绿。
  该测试实际钉住的是"同日只卖一次"（exited_today 去重），不是"止损
  优先"。要加强需用 `ma_cross(use_exit: true)` 构造真实死叉竞争，并选
  止损价 ≠ 当日收盘的行情（如盘中触及 9.5、收盘 9.8）使成交价能区分
  执行来源。R1 此项属覆盖缺口（非 P0/P1），本轮判定不影响通过。
- **R2-2**：仓库根目录出现 `_fix_layering.py`、`_review_r2_probe.py`
  两个临时脚本（未跟踪），与测试无关，建议清理。
- **R2-3**：evaluations.py:114/:169 两处宽 raises 残留（见 C-P1-3 末节）。

## 结论

4 项 P1 全部修复并有实现侧/复算侧证据；缺口补测真实有效且带动了两处
实现级修复（matcher 判定序、holdout 原子消费）——这正是测试该起的
作用。无 P0/P1 未决。

C_VERDICT_R2: PASS
