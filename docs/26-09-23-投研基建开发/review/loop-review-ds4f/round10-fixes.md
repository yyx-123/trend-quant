# Round 10 修复与回归（loop-review-ds4f）

> **状态：CLOSED（2026-09-25 闭合）** —— 审查报告见 `round10-review.md`
> 日期：2026-09-25
> 提交：`ccf52c7` fix(loop-review-ds4f-r10) + `1ce4f1b` chore(lint)

## 修复总览

| 项 | 级别 | 修复 | 触及文件 |
|---|---|---|---|
| **R10-F1** `sortino` 逃逸闸门（分母为"负收益子集 std"，与 Sharpe 不同源） | P2 | `is_degenerate_summary` 改为检查摘要里**任一比值型指标**（`_RATIO_METRIC_KEYS = ("sharpe","sortino")`）超闸；**刻意不含 calmar** | `src/rule_backtest/metrics.py` |
| **R10-F2** `information_ratio` / `tracking_error` 未过闸（实测 1.4e13） | P3 | 与 beta/alpha/capture 同闸（`_SHARPE_GATE` + `_bench_degenerate`） | `src/portfolio/reports.py` |
| **R10-F3** 过拦截与文案失真（5 日窗口合法上限 49.98，对闸门仅 1.00× 余量；10 条有成交腿被错误解释为"零成交或全现金"） | P3 | 文案改为"该腿的 Sharpe/Sortino/PSR/DSR 不可用（零成交/全现金、**近零方差或极短窗口**等）"（`backtest._assemble_result` 与 `head_to_head.run_head_to_head` 两处） | `research/evaluations/backtest.py`、`head_to_head.py` |
| 空钉 1（滚动闸门夹具零方差） | P3 | 夹具改为微小**非零**抖动，并**断言未加闸路径确实产出噪声** | `tests/unit/test_loop_review_ds4f_r5.py` |
| 空钉 2/3（h2h 与 PBO 年化闸门只有文本断言） | P3 | 改**载荷级**断言 | 同上 |
| lint 收口 | P3 | I001/F401/F811 三项（新增 0 条 vs 基线） | 相关测试/源码文件 |

## 本轮的语义结论：为什么闸门要"任一比值指标"而不是"Sharpe"

- Sharpe 与 Sortino 的分母**不同源**：Sharpe 用全样本 std，Sortino 用负收益子集的 std。
  因此存在"Sharpe 正常、Sortino 极大"的真实腿（实测 5 日窗口 → 1.58e5 已落库）。
- `calmar` 的分母是最大回撤，**低回撤/短窗口上可合法 > 50**（实测一条合法序列
  calmar = 52.8）→ 纳入闸门会误杀诚实策略，所以**不在**闸门键位里。
- 代价（记为待决策 R10-D-1 / 与 R9 的 50 闸门同源）：5 日窗口的合法 |年化 Sharpe|
  上限实测 **49.98**，与闸门 50 只差 **1.00×** 余量 → 极短窗口上存在"合法即被排除"
  与"噪声仍可逃逸"的窄带，需要口径决策（建议：短窗口腿改用更短的置信窗口或直接
  禁止 < 20 日窗口的评价腿）。

## 回归结果

- 全量：**1652 passed / 2 failed**（均为既有 Windows 临时文件 flake）；
- ruff：`(file, rule)` 集合与基线完全一致（新增 0 条）；
- 新增钉子：3 项（空钉补强）。
