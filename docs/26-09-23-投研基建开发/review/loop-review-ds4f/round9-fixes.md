# Round 9 修复与回归（loop-review-ds4f）

> **状态：CLOSED（2026-09-25 闭合）** —— 审查报告见 `round9-review.md`
> 日期：2026-09-25
> 提交：`93a5583` fix(loop-review-ds4f-r9)

## 修复总览

| 项 | 级别 | 修复 | 触及文件 |
|---|---|---|---|
| **R9-1** 滚动 Sharpe 噪声是窗口局部的，整序列闸门不触发 | P2 | 新增 `_rolling_sharpe_gated`：**逐窗口**判退化并剔除（`is_degenerate_summary(窗口, 窗口 Sharpe)`） | `src/portfolio/reports.py` |
| **R9-2a** `head_to_head` 判据没传 Sharpe 腿 → 合法小仓位腿被误判、判定被双向翻转 | P2 | 改走新入口 `is_degenerate_summary(rows, summary)`（自动补 Sharpe 腿） | `src/research/evaluations/head_to_head.py` |
| **R9-2b** `benchmark_relative` 的 beta/capture 只有相对腿 | P3 | 用**基准腿年化 Sharpe** 走新入口；capture 与 beta/alpha 同闸 | `src/portfolio/reports.py` |
| **定稿** 判据入口结构性收口 | — | 新入口 `rule_backtest.metrics.is_degenerate_summary(nav_rows, summary)`：summary 无 Sharpe 时**自行从 NAV 现算** `annualized_sharpe` → 调用点漏传在结构上不可能；`_nav_summary`/`head_to_head`/`build_report`/`benchmark_relative` 全部改走该入口；PBO 逐块日频 Sharpe **先年化**再比闸门 | `src/rule_backtest/metrics.py`、`research/evaluations/backtest.py`、`src/research/stats/fdr_pbo.py` |
| 测试夹具 | — | PBO `dominant` 夹具由 0.05/日（年化 ≈127，不可能序列）改为 0.006/日（≈9.5，诚实区间） | `tests/unit/test_research_stats.py` |

## 结构性收口的判据语义（最终口径）

1. **两条腿**：`summary` 里的比值型指标 + NAV 序列（缺失时现算年化 Sharpe）；
2. **两个判据**：幅值闸门（`|sharpe| / |sortino| > 50`）+ 相对方差判据
   （`std ≤ |mean|·1e-6`）；
3. **闸门为年化口径**，所有调用点（含 PBO 的日频块）必须先年化再比；
4. `calmar` **不**入闸门（低回撤/短窗口可合法 > 50，实测合法序列 calmar = 52.8）。

## 回归结果

- 全量：**1649 passed / 2 failed**（既有 Windows 临时文件 flake）；
- ruff：`(file, rule)` 集合与基线完全一致（新增 0 条）；
- 新增钉子：3 项（两腿入口 / 逐窗口滚动 / 基准两腿）；
- 过拦截审计：真实数据 200 ETF × 10117 窗口最大年化 |Sharpe| = 4.37 → 闸门有
  2.6~11 倍余量（详见 `round9-review.md`）。
