# Round 5 修复与回归（loop-review-ds4f）

> **状态：CLOSED（2026-09-25 闭合）**
> 验收：Round 6 的两个独立确认代理（R6A/R6B）复核本轮的 5 项修复与 3 项钉子缺口 →
> 其结论与后续修复见 `round6-review.md` / `round6-fixes.md`。原 Round 5 的报告见 `round5-review.md`。

> 日期：2026-09-25
> 对应审查：`round5-review.md`（新面审查轮：存量栈与跨栈原语 → 1 P1 + 3 P2 + 3 P3）

## 修复总览

| 项 | 修复 | 钉子 |
|---|---|---|
| **R5-P1-1**（P1）生产管理员仍用源码可见的默认引导密码 | **只做可见化**（改行为属运维/凭据决策）：`_ensure_builtin_admin` 检测到"内置默认密码仍在用"→ 启动即 `SECURITY:` 响亮告警；新建且未显式配置引导密码同样告警 | `test_default_admin_password_in_use_logs_a_warning`（含"已改密则不再告警"反向断言） |
| **R5-P2-2**（P2）退化腿 Sharpe ≈ 6e12 决定判定 | 新栈侧：`_nav_summary` 判退化（`std ≤ \|mean\|·1e-6`）→ `sharpe/sortino=None` + `degenerate_leg=True`；Δ 抽 `_deltas_from_summaries`，任一腿退化则 sharpe/sortino/calmar 的 Δ 记 None；warnings 落说明 | `test_degenerate_flat_leg_does_not_gate_the_verdict` + R6 追加的 4 条 |
| **R5-P2-3**（P2）因子先落库、qfq 物化失败则静默分叉且永不重试 | **记录在案**（存量数据管线行为，改写入顺序属存量变更）→ 待决策 R5-D-3 | — |
| **R5-P2-4**（P2）CI 跑不了 CLI 用例（硬编码 Windows venv） | 改 `sys.executable` | `test_cli_test_uses_the_current_interpreter` |
| R5-P3-5 | 共享 `compute_summary` 的退化口径（Sortino 下行偏差/Sortino·Calmar 零值 vs None/PF 哨兵/`total_return` 漏首日） | **记录在案** → 待决策 R5-D-4（改共享函数会重述全部存量数字） |
| R5-P3-6 | `research_runs` 无 role 列（候选腿与对照腿不可区分） | **记录在案** → 待决策 R5-D-5 |
| R5-P3-7 | `macd` 两模式不止掩码不同（DEA 种子不同 → 跨页面口径差） | docstring 如实说明 + 钉子断言 docstring 声明了该差异 |

## 后续轮次报告索引

Round 5 之后按"确认轮"继续推进（每轮 ≥2 个独立代理，代理不通过则继续修）。为避免早期
把多轮结论折叠进本文件造成检索困难，Round 6 起**每轮独立成文件**：

| 轮次 | 审查报告 | 修复与回归 | 审查对象 | 结论 | 该轮提交 |
|---|---|---|---|---|---|
| Round 6 | `round6-review.md` | `round6-fixes.md` | `f89c913` | NOT CLEAN（1 P1 + 1 P2 + 1 P3 + 3 钉子缺口） | `cd09ddc` |
| Round 7 | `round7-review.md` | `round7-fixes.md` | `cd09ddc` | NOT CLEAN（3 P2 + 3 P3） | `8a5aca2` |
| Round 8 | `round8-review.md` | `round8-fixes.md` | `8a5aca2` | R8B CLEAN / R8A NOT CLEAN（2 P2 + 3 P3） | `eb1650c` |
| Round 9 | `round9-review.md` | `round9-fixes.md` | `eb1650c` | NOT CLEAN（2 P2 + 1 P3） | `93a5583` |
| Round 10 | `round10-review.md` | `round10-fixes.md` | `93a5583` | R10B CLEAN / R10A NOT CLEAN（1 P2 + 2 P3 + 3 空钉） | `ccf52c7`、`1ce4f1b` |

**贯穿 R5~R10 的一条主线**：Round 5 首次发现的"退化腿噪声"缺陷类，先后在 Round 6/7/8/9/10
**复发 7 次**，每次都是"某个消费面未接闸门"。收口路径：
逐点修 → 单点判据（R7-F5）→ 幅值闸门 + 消费面全覆盖（R8）→ **两腿结构性入口**（R9）→
任一比值指标（sortino/IR 同闸）+ 空钉补强（R10）。相关取舍（闸门 50 vs 最短窗口长度）
进入待决策清单。

## 回归结果

- 全量（Round 5 修复后）：**1637 passed / 2 failed**（唯一失败为既有 Windows 临时文件 flake）；
- 新增钉子：4 项（默认口令告警 / 退化腿 / `sys.executable` / macd docstring），
  Round 6 起在 `tests/unit/test_loop_review_ds4f_r5.py` 上继续追加（见 `round6-fixes.md`）；
- ruff：`(file, rule)` 集合与基线**完全一致**（新增 0 条）。
