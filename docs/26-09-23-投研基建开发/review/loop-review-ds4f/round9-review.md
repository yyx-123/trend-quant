# Round 9 审查报告（loop-review-ds4f）——确认轮 4

> **状态：CLOSED（2026-09-25 闭合）**
> - 修复清单与验收记录：`round9-fixes.md`；
> - 全量回归（修复后终跑）：1649 passed / 2 failed（均为既有 Windows flake）；
> - ruff：(file, rule) 集合与基线完全一致（新增 0 条）。
>
> 日期：2026-09-25
> 审查对象：commit `eb1650c`（Round 8 修复后的 HEAD）
> 审查方式：两个独立确认代理，围绕"退化腿类是否结构性收口"复检，并附**过拦截审计**
> （合法策略是否被闸门误杀）与**独立复算**（判据 / build_report / benchmark_relative / PBO
> 四组对拍）。所有结论均在真 run 的持久化记录与物化产物上复现。

## 总体结论

**NOT CLEAN（2 项 P2 + 1 项 P3）**——退化腿类**第 6 次复发**。

**根因升级诊断**：Round 8 的判据已经是单点实现，但仍然复发，原因是**判据有两条腿**
（NAV 判据 + Sharpe 判据），而**每个调用点都要记得把两条都传全**——调用点一多必然漏。
本轮据此把入口改成**结构性入口**：调用点无法漏传。

---

## P2

### R9-1 `build_report` 的闸门是整序列的，而滚动 Sharpe 噪声是**窗口局部**的

- 位置：`src/portfolio/reports.py` 的滚动 Sharpe 计算
- 事实（引擎精确复现）："**前 300 日无成交、之后正常**"的腿，整序列 Sharpe 正常 →
  整序列闸门**不触发**，但落在无成交段里的窗口仍是 **6.3e12** 级 → 实测 **392 条**
  噪声窗口被持久化并经 HTTP/物化发布。
  前缀 255 / 300 / 385 / 645 日的扫描分别产生 **132 / 222 / 392 / 912** 条噪声窗口。
- 修复：新增 `_rolling_sharpe_gated`——**逐窗口**判退化并剔除
  （`is_degenerate_summary(窗口, 该窗口的 sharpe)`）。

### R9-2a `head_to_head` 的判据调用没传 Sharpe 这条腿 → 合法小仓位腿被误判"未退化"

- 事实（真 `run_head_to_head`，仅打桩数据源）：**合法可配置**的极小仓位腿
  （`weights` / `risk_budget_pct` 下限可达 1e-4 量级 → 年化 |Sharpe| 85~287）被判"未退化"，
  其噪声**双向翻转判定**（正常腿被判 `confirmed`/`rejected`、ΔSharpe ±286、置信带 ±16~20）。
- 修复：`head_to_head` 改走新入口 `is_degenerate_summary(rows, summary)`。

## P3

### R9-2b `benchmark_relative` 的 beta 守卫与 `_bench_degenerate` 同样只有相对腿

- 事实（引擎精确复现）：近失配基准给出 `beta −195 … −67,470`、`capture 46.9`。
- 修复：用基准腿的**年化** Sharpe 走新入口；capture 与 beta/alpha 同闸。

## 定稿：判据入口结构性收口

```
rule_backtest.metrics.is_degenerate_summary(nav_rows, summary)
    └─ 自动取两条腿：summary 的比值指标（sharpe/sortino）+ 缺 Sharpe 时自行
       从 NAV 现算 annualized_sharpe
```

调用点漏传在**结构上不可能**：`_nav_summary` / `head_to_head` / `build_report` /
`benchmark_relative` 全部改走该入口；PBO 的逐块日频 Sharpe 先**年化**再比闸门
（闸门是年化口径）。

## 过拦截审计（本轮新增的负向证据）

- 平台真实数据：**200 只 ETF × 10117 个窗口**，最大年化 |Sharpe| = **4.37**
  （20 日窗口最大 **19.07**）→ 闸门 50 有 **2.6~11 倍**余量，当前数据上不可达。
- 结论：闸门在真实数据上不会误杀。构造性序列（年化 58.5）会被排除——**记为文档口径
  注记，非缺陷**，相关取舍进待决策清单。
- 测试夹具修正：PBO 的 `dominant` 测试夹具原用 0.05/日（年化 ≈127，本身是不可能序列）
  → 改为 0.006/日（年化 ≈9.5，诚实区间）。

## 代理同时确认的正面结论

- 独立复算 4 组，**零差异**：判据（128 序列 + 10 个边界例）、`build_report`
  （40 组 vs `ebcd0f6`）、`benchmark_relative`（200 组）、PBO（40 组）。
- 两轮全量套件零漂移（1647/1）；ruff 新增 0 条；`.tmp_*` 未入库；工作树干净。

## 闭合

修复见 `round9-fixes.md`，提交 `93a5583`；回归 1649 passed / 2 failed；新增钉子 3 项。
