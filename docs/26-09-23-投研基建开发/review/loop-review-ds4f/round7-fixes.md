# Round 7 修复与回归（loop-review-ds4f）

> **状态：CLOSED（2026-09-25 闭合）** —— 审查报告见 `round7-review.md`
> 日期：2026-09-25
> 提交：`8a5aca2` fix(loop-review-ds4f-r7)

## 修复总览

| 项 | 级别 | 修复 | 触及文件 |
|---|---|---|---|
| **R7-F1** h2h 只清局部变量，持久化证据仍有 6.1e12 | P2 | 清零前移到 evidence/report 组装**之前**：`d_band`/`psr_ab`/两侧 summary 的 sharpe·sortino 全清；钉子改**顺序 + 载荷**断言 | `research/evaluations/head_to_head.py` |
| **R7-F2** `stats.*` 噪声经 `1-psr` 污染课题 FDR 与 `TOPIC.md` | P2 | `stats.*` 退化记 `None`；`evidence["degenerate_legs"]` 机器可读；`conclusion` 跳过退化腿 p 值；`benchmark_relative` 的 beta/alpha 加相对方差守卫（beta 曾达 8.2e12） | `research/conclusion.py`、`research/evaluations/backtest.py`、`portfolio/reports.py` |
| **R7-F3** 段内 1.7e13 噪声经 collapse 门压 `confirmed` | P3 | 段内判退化 → `sharpe=None` + `sufficient_sample=False`（复用既有排除机制） | `research/evaluations/backtest.py` |
| **R7B #1** PBO 并列名次 → `pbo=1.0` | P3 | 并列按半步计（λ=0.5）；新增 `degenerate_variants` 标记；钉子覆盖全并列与真过拟合两侧 | `research/stats/fdr_pbo.py` |
| **R7-F5** 判据公式两处各抄一份 | P3 | 抽 `_common.is_degenerate_leg` / `null_degenerate_metrics` 单点实现 | `research/evaluations/_common.py` |
| **R7-F4** 三处失效钉子 | P3 | engine 守卫钉改用**合法值**（`status='rejected'`/`reason='limit_down'`）；h2h/告警钉改载荷断言；补 FDR/regime/PBO 载荷钉子 | `tests/unit/test_loop_review_ds4f_r3.py`、`test_loop_review_ds4f_r5.py` |

## 验收（本轮不再单设验收代理的说明）

Round 7 的修复在 Round 8 由**两个新的独立代理**复核：R8B 判 CLEAN 并逐 hunk 验证
了本轮改动的行为保持与迁移路径；R8A 判 NOT CLEAN 但抓到的是**新的**消费面
（build_report/PBO/capture），说明本轮修复本身成立、覆盖仍不全。详见
`round8-review.md`。

## 回归结果

- 全量：**1643 passed / 1 failed**（既有 Windows 临时文件 flake）；
- ruff：`(file, rule)` 集合与基线完全一致（新增 0 条）；
- 新增钉子：5 项（h2h 顺序+载荷、stats 置 None、FDR 跳过、regime 段、PBO 并列）。
