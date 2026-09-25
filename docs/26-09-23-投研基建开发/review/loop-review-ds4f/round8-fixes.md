# Round 8 修复与回归（loop-review-ds4f）

> **状态：CLOSED（2026-09-25 闭合）** —— 审查报告见 `round8-review.md`
> 日期：2026-09-25
> 提交：`eb1650c` fix(loop-review-ds4f-r8)

## 修复总览（退化腿类定稿收口：幅值闸门 + 消费面全覆盖）

| 项 | 级别 | 修复 | 触及文件 |
|---|---|---|---|
| **R8-F1** `build_report` 绕过闸门 → 6.1e12 Sharpe + 394 条滚动噪声被发布 | P2 | 单点实现 `rule_backtest.metrics.is_degenerate_nav`（相对方差 + **幅值闸门 `\|sharpe\|>50`**）；`build_report` 清零 summary 与滚动 Sharpe；`_common` 委托同一实现 | `src/rule_backtest/metrics.py`、`src/portfolio/reports.py`、`research/evaluations/_common.py` |
| **R8-F2** PBO 全现金列赢下 CSCV → 假的 `pbo=0.0` | P2 | `_sharpe_vec` 退化列记 NaN；比较仅在有序列上进行；全退化 → `pbo=None` + `degenerate_variants=True` | `src/research/stats/fdr_pbo.py` |
| **R8-F3** 近失配带（`std/\|mean\|` 5.7e-6~1.8e-5）双向错判 | P3 | 幅值闸门接入 `_nav_summary`、regime 段、`build_report`；阈值收敛为 1 份实现 | `research/evaluations/backtest.py`、`portfolio/reports.py` |
| **R8-F4** up/down capture 除以噪声均值 | P3 | 与 beta/alpha 同口径：退化腿整组记 `None` | `src/portfolio/reports.py` |
| **R8-F5** `_regime_segment_metrics` 行为回退存活 1185 用例 | P3 | 新增 4 条**载荷级**钉子（幅值闸门 / build_report / PBO 退化列 / capture） | `tests/unit/test_loop_review_ds4f_r5.py` |
| 附带 | — | `head_to_head` 改为传**行字典序列**给共享判据（此前传 float 列表 → KeyError）；清理随之而来的未用 import 与 ruff 项 | `research/evaluations/head_to_head.py` |

## 关键设计决定：为什么是"幅值闸门 + 相对方差"两条一起用

- **只有相对方差**（`std ≤ |mean|·1e-6`）：存在**近失配带**——单调低波路径
  （`std/|mean| ≈ 5.7e-6~1.8e-5`）会逃逸，其 8.7e5 级 Sharpe 反过来**翻转判定**；
- **只有幅值闸门**：低回撤/短窗口上 `calmar` 可合法 > 50（实测一条合法序列
  calmar = 52.8），所以闸门只施加于 **sharpe/sortino**（后续 Round 9/10 明确），
  且必须与相对方差判据并存以覆盖"方差极小但幅值未超"的形态；
- 两者合并后**阈值只剩 1 份实现**（`rule_backtest/metrics.py`）。

## 回归结果

- 全量：**1646 passed / 2 failed**（均为既有 Windows 临时文件 flake）；
- ruff：`(file, rule)` 集合与基线完全一致（新增 0 条）；
- 新增钉子：4 项（载荷级）。
