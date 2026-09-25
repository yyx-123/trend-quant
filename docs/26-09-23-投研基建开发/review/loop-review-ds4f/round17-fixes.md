# Round 17 修复与回归（loop-review-ds4f）

> **状态：CLOSED（2026-09-26 闭合）** —— 审查报告见 `round17-review.md`
> 日期：2026-09-26

## 修复总览

| 项 | 级别 | 修复 | 触及文件 |
|---|---|---|---|
| **R17A-F1** `quantity` 路径现金递减后未复检最小申报（**已提交态存在**） | P2 | `match_buy` 在 `qty = min(qty, affordable)` 之后补复检：低于最小申报即 `unfilled(below_min_order)`，两条意图路径语义对齐；钉子断言由"∈{below_min_order, insufficient_cash}"收紧为 `== "below_min_order"`，并补"意图 200 股 + 现金只够 199 股"的精确用例 | `src/engine/matcher.py`、`tests/integration/test_loop_review_ds4f_r16.py` |
| **R17A-F2** 旧栈 `rule_backtest` 是第二个下单入口，336 笔 100 股科创板"成交" | P2 | `_resolve_buy_qty` 增加 `symbol`/`asset_type` 入参并按 `min_buy_qty` 判定；`_max_buy_qty` 支持 `min_qty`（递减到低于最小申报即返 0）；新增跳过原因 `below_min_order`（"现金只够 N 股 < 最小申报 M 股"）；调用点传 `request.symbol` + `execution.instrument_type` | `src/rule_backtest/engine.py` |
| R17A-B1 清单侧缺元数据时退化到 100（口径不对称） | P3 | `min_buy_qty` 改为**按代码判定**（688/689 一律 200，588 ETF 不受影响），与 `asset_type` 是否缺失无关 | `src/engine/profiles.py` |
| R17A-B2 `rebuild_all` 登记条件有死析取项且不看结果 | P3 | 改为 `if not partial and rebuilt > 0 and failed == 0:`（重建 0 只/有失败都不登记） | `src/services/indicator_builder.py` |
| 自查连带：parity 驱动器的定量 | — | `run_macd_parity` 的可负担定量低于最小申报时**两个引擎都不下**（否则新引擎拒单、旧引擎照成交 → 会被误读成引擎差异） | `src/engine/parity.py` |

## 新增/收紧钉子（2 条 + 1 条收紧，全部变异实证）

| 钉子 | 断言 | 变异实证 |
|---|---|---|
| `test_legacy_engine_respects_star_min_order`（新，r17 文件） | 旧栈：科创板现金只够 100 股 → `(0, below_min_order)`；主板同现金 → 100 股成交；科创板够 200+ → 成交且 ≥200；`asset_type=None` 仍按 200 判定 | 旧栈退回 symbol-blind → **红**；`min_buy_qty` 不按代码兜底 → **红** |
| r16 的 `test_star_min_order_qty_is_enforced`（收紧） | 新增"意图 200 股 + 现金只够 199 股 → `below_min_order`"精确用例；reason 断言收紧 | 去掉现金递减后的复检 → **红** |
| r16 的 `test_partial_rebuild_does_not_clear_drift_flag`（适配新语义） | 空库全量重建（rebuilt=0）**不登记**；有数据全量重建（rebuilt>0, failed=0）才登记 | 登记条件缺 `rebuilt>0` → **红** |

## 旧栈修复的数字影响（需重基线，记入待决策）

旧栈那 **336 笔**（5 只科创板标的：688802.SS 131 / 688795.SS 165 / 688498.SS 5 /
688809.SS 33 / 688111.SS 2）在**重跑批量回测后不会再出现** → 这些格子的
`trade_count/return/Sharpe/…` 与批量汇总会变化。已存在的格子是修复前的产物，
**需要重跑才会更新**（与 R15-D-1 的创业板幅度修复同属"重基线"范畴）。

## 回归结果

- 相关面：旧栈全套（137 项）+ 引擎/清单/r16/r17 钉子文件全绿；
- 全量套件：**1693 passed / 2 failed**（均为既有 Windows 临时文件 flake）；
- ruff：`(file, rule)` 与基线 78/78 一致（新增 0 条）；
- 变异实证：本轮 **4 个**语义变异全部被抓住。

## R17B 的 backlog 处置

R17B 判 CLEAN，其 9 条 backlog 均为文案/标注类（不阻断）。其中两条**建议尽快处理**、已进待决策：
① **日更漏跑**（库内最后一根 K 线 2026-09-23，而 09-24 是交易日 → 与"生产服务未重启"同因）；
② manifest 的 `calibers.fees` 文案与 `realized_pnl` 实为毛额矛盾（会误导 AI 离线分析）。
其余（仓位占比分母、`meta.rows` 语义、run description、blank-base 命名、pf=999 渲染、
`/cells` 缓存、重名 topics 目录）记入 backlog，待统一口径时一并修。

## 待决策新增

- **R17-D-1（旧栈的规则保真度：修还是冻结？）**：旧栈不仅缺"分品种最小申报"（本轮已补），
  还**完全不建模涨跌停/停牌/T+1**（R15B 已记录）。两条路：
  1. **逐步补齐**（最小申报已补；再接涨跌停/T+1）：旧栈数字会继续变化，需连续重基线；
  2. **冻结并显式标注**：在旧栈的 API/导出/manifest 上标注"该引擎不建模涨跌停/停牌/T+1，
     数字仅用于相对比较，不可用于执行口径判断"。
  **建议**：先做 2（零风险、立刻消除误读），当旧栈成为决策依据时再做 1。
