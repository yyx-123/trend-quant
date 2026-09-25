# Round 5 修复与回归（loop-review-ds4f）

> **状态：CLOSED（2026-09-25 闭合）**
> 验收：Round 6 的两个独立确认代理（R6A/R6B）复核本轮的 5 项修复与 3 项钉子缺口 →
> 本文件"R6 复核"节记录其结论与后续修复。原 Round 5 的报告见 `round5-review.md`。

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

## R6 复核（第 11/12 个代理）与其后续修复

Round 6 的两个独立确认代理均判 **NOT CLEAN**，抓到的问题与本轮直接相关：

| # | 项 | 证据 | 修复 |
|---|---|---|---|
| **R6-P1-1**（P1，**我的修复引入的回归**） | 退化腿修复把 `sharpe` 记 None，但两处消费点仍 `float()` 相减：`backtest.py:460`（wf 折 Δ）与 `:727-729`（高原探针 Δ）→ **恰好把被修的场景打成 `status=failed`（TypeError）**（A/B：HEAD failed / 父提交 evaluating） | 真引擎 + 真 pipeline 对照 | `_delta_or_none` 统一 None 语义并接入三处（wf 折 Δ / 探针 Δ / selected Δ）；`plateau_verdict` 新增 `skipped` 计数（被剔除的退化邻域点必须可见）；另在持久化 warnings 落 `degenerate_leg(...)`（R6-P3-3） |
| **R6-P2-2**（P2） | `head_to_head@1` 仍用原始 `compute_summary` 作差 → 同一类噪声决定其判定（实测正常腿 vs 全现金腿 → `rejected`、ΔSharpe −6.09e12、置信带全是噪声） | 真 pipeline 探针 | 同口径判退化腿 → 强制 `inconclusive` + `degenerate_leg(...)` 告警 + 置信带置 None |
| R6-P3-4（P3） | R5-P2-4 的 `import sys` 位置引入 ruff I001 | ruff 逐对比较 | 移入 stdlib 块（ruff 与基线持平） |
| R6 钉子缺口 1（P3） | engine 子表守卫只钉了 2/5 张（orders/unfilled/positions 的守卫删掉仍绿） | 变异 M11 存活 | 钉子扩展为**五张子表**逐表 UPDATE/DELETE |
| R6 钉子缺口 2（P3） | `stop()` 的"被取消 future 回灌"这条腿未钉（只断言集合、没断言 `_queue`）→ 删掉该行仍绿（正是 V10 抓到的缺陷形态） | 变异 M15 存活 | 钉子追加"回灌必须落回 `_queue`"的 FIFO 断言（变异后失败） |
| R6 钉子缺口 3（P3） | "已落定 final_verdict 不可改写"的**库层**守卫只被源码注释钉住（删掉 WHEN 子句后 3 条相关钉子仍绿，实测可改写已定论行） | 变异 M24 存活 | 新增**行为**钉子：已落定后直改 `final_verdict` 必须被库层拒绝，白名单列仍可写（变异后失败） |

R6 同时确认：R5 的 5 项修复在端到端层面成立（真引擎零成交场景现在记 None 并落警告）、
`target_weight.mode` 与机制钉、五表守卫、`listing_known`、表单上限、MCP 无 SQL、
`sys.executable` 全部通过；另做了 4 类随机化不变量对拍（150 随机 NAV × 18 指标、
400 随机订单的费用模型、200 随机序列的指标原语、150 随机序列的统计件），**0 不匹配**；
跨进程确定性（NAV 2400 行 × 6 字段位级一致）、106 个敌意 HTTP 请求（无 5xx）、
135 个敌意 MCP 调用、9 个敌意 CLI 调用全部符合预期；与 `f93031e` 的 A/B 显示
12000/12000 NAV 单元格位级一致。

## 回归结果

- 全量：**1633 passed / 1 failed**（唯一失败为既有 Windows 临时文件 flake）；
- 新增钉子：`tests/unit/test_loop_review_ds4f_r5.py` 8 项 + 三条守卫钉子（跨文件）；
- ruff：`(file, rule)` 集合与基线**完全一致**（新增 0 条）。
