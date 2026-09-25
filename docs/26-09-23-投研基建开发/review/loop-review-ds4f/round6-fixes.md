# Round 6 修复与回归（loop-review-ds4f）

> **状态：CLOSED（2026-09-25 闭合）** —— 审查报告见 `round6-review.md`
> 日期：2026-09-25
> 提交：`cd09ddc` fix(loop-review-ds4f-r5/r6)

## 修复总览

| 项 | 级别 | 修复 | 钉子 |
|---|---|---|---|
| **R6-P1-1** 退化腿 `sharpe=None` 后两处 Δ 直接相减 → 零成交场景变 `failed` | P1（**我引入的回归**） | 新增 `_delta_or_none` 统一 None 语义，接入三处：walk-forward 折 Δ、高原探针 Δ、selected Δ；`plateau_verdict` 增加 `skipped` 计数（**被剔除的退化邻域点必须可见**，不能静默消失）；持久化 warnings 落 `degenerate_leg(...)` | `test_loop_review_ds4f_r5.py` +6 |
| **R6-P2-2** `head_to_head@1` 用原始 `compute_summary` 作差 | P2 | 与主路径同口径判退化腿 → 强制 `inconclusive` + `degenerate_leg(...)` 告警 + 置信带置 `None` | `test_loop_review_ds4f_r5.py` h2h 载荷钉子 |
| 钉子缺口 1：engine 子表守卫只钉 2/5 | P3 | 钉子扩展为五张子表逐表 UPDATE/DELETE（合法值，不靠 CHECK 约束代劳） | `tests/unit/test_loop_review_ds4f_r3.py` 扩展 |
| 钉子缺口 2：`stop()` 回灌只断言集合 | P3 | 追加"回灌必须落回 `_queue`"的 FIFO 断言 | `tests/unit/test_loop_review_ds4f_r4.py` 扩展 |
| 钉子缺口 3：已落定 `final_verdict` 库层守卫无行为钉子 | P3 | 新增行为钉子：直改 `final_verdict` 必须被库层拒绝，白名单列仍可写 | `tests/unit/test_loop_review_ds4f_r3.py` 新增 |
| R6-P3-4 ruff I001 | P3 | `import sys` 移入 stdlib 块 | — |

## 为什么这三条钉子缺口值得单独修

三条的共同形态是"**删掉实现，测试仍然全绿**"，也就是钉子钉在了错的地方：

1. engine 守卫钉用了**违反 CHECK 约束**的值 → 实际测的是列约束而不是触发器
   （2/10 条腿可任意删改而测试通过）；
2. `stop()` 的回灌钉子只断言 `_queued_ids` 集合——而 V10 抓到的真缺陷恰恰是
   "进了集合、没进队列"（任务被孤立），所以它守卫的是**另一件事**；
3. "已落定 verdict 不可改写"只有源码注释与文本断言，删掉库层 WHEN 子句后
   实测可改写已定论行——这是研究纪律的核心不变式。

## 回归结果

- 全量：**1637 passed / 2 failed**（均为既有 Windows 临时文件 flake）；
- ruff：`(file, rule)` 集合与基线完全一致；
- 新增钉子：8 项（r5 文件）+ 3 项守卫钉子（跨文件扩展）；
- 文档：新增本文件与 `round6-review.md`（Round 5 的 `round5-fixes.md` 此前缺失，
  已在 Round 5 收口时补齐）。
