# Round 18 修复与回归（loop-review-ds4f）

> **状态：CLOSED（2026-09-26 闭合）** —— 审查报告见 `round18-review.md`
> 日期：2026-09-26

## 修复总览

| 项 | 级别 | 修复 | 触及文件 |
|---|---|---|---|
| **R18A-F1** 旧栈基准腿仍以 100 股为科创板建仓 | P2 | `_buy_and_hold_benchmark` 增加 `symbol`/`asset_type` 入参并按 `min_buy_qty` 判定：可负担量 < 最小申报 → `qty=0`（基准记为**全程现金**，净值恒为初始资金）；调用点与离线 backfill 脚本一并透传 | `src/rule_backtest/engine.py`、`scripts/backfill_batch_excess_metrics.py` |
| **R18B-P2-2** plateau 判据在邻域点 <3 时双向失效 | P2 | `plateau_verdict`：邻域**去重后**少于 3 点时返回 `unknown` + `reason=neighbor_points_insufficient(n)` + `insufficient_neighbors=True`（可见告警、不再给随机答案）；≥3 点保留 Alvarez 1σ 判据 | `src/research/verdict_rules.py` |
| **R18B-P2-1** DSR 门恒真却像"已做校正" | P2（**决策项的一半**） | 机械且无损的一半：规则注释写明"阈值 0 下该门恒真、平台无多重检验折扣"；新增 `dsr_gate_binding()` 并把它落进 verdict 证据（可审计）。阈值本身属研究纪律 → **R18-D-1**（含建议） | `src/research/verdict_rules.py`、`src/research/evaluations/backtest.py` |
| R17B backlog（本轮一并处理） | P3 | ① manifest 的 `calibers.fees` 如实标注 `live_trades.realized_pnl` 为毛额；② 持仓表"仓位占比"分母改为**全部未清仓持仓**（与悬停文案一致，避免时间筛选导致占比虚高） | `src/services/backtest_export.py`、`web/static/js/manual_trade.js` |

## 新增钉子（3 条，全部变异实证）

| 钉子 | 断言 | 变异实证 |
|---|---|---|
| `test_legacy_benchmark_respects_star_min_order` | 科创板：10 万资金买不起 200 股 → 基准 `qty=0`、净值恒为初始资金；20 万 → `qty≥200`；主板不受影响（100 股） | 基准退回 100 股建仓 → **红** |
| `test_plateau_insufficient_neighbors_is_not_a_verdict` | 1 点/2 点邻域 → `unknown` + `insufficient_neighbors` + 原因串；3 点起：邻域同向 → `plateau`、孤立峰 → `peak` | 去掉邻域点数量闸门 → **红** |
| `test_dsr_gate_binding_is_auditable` | 默认规则下 `dsr_gate_binding()` 为 False；阈值 0.95 时为 True | 让该函数恒返 True → **红** |
| 既有钉子适配 | `test_loop_review_ds4f.py` 的 peak 分支改用 3 点邻域并新增"2 点/1 点 = 证据不足"断言；r5 的 `skipped` 钉子同步适配 | — |

## 回归结果

- 相关面：研究栈 + 旧栈 + 引擎 + JS 语法（`node --check`）+ r11~r18 钉子文件全绿；
- 全量套件：**1697 passed / 1 failed**（既有 Windows 临时文件 flake）；首跑另有 1 项 `test_critical_paths.py::test_plateau_probes_actually_run` 因 plateau 语义修正而失败（它断言 2 点邻域必须判 plateau/peak）→ 已按新语义改为 unknown + insufficient_neighbors，并保留“探针照常跑”的断言；
- ruff：`(file, rule)` 与基线 78/78 一致（新增 0 条）；
- 变异实证：本轮 **3 个**语义变异全部被抓住。

## 数字影响（重基线范畴）

旧栈基准腿的修复会改变 **42 个生产格子**（`688795.SS`/`688802.SS` × 21 策略）的
`benchmark_total_return/benchmark_annual_return/benchmark_sharpe/benchmark_calmar/excess_*`
——其中 `688795.SS` 至少 1 格 `excess_annual_return` **变号**。需重跑才会更新
（与 R15-D-1、R17-D-2 同属"重基线"批次）。

## 待决策新增

- **R18-D-1（DSR 阈值：研究纪律的实质选择）**：现阈值 0.0 使该门恒真 → 平台不做多重检验折扣。
  三条路：
  1. **采纳论文口径**（`min_dsr_on_diff = 0.95`）+ 用邻域/探针 Δ 提供跨试验方差 `sr_var`
     → 折扣真实生效；代价：按现量级（E0001 的 DSR≈0.45）多数实验将停在 inconclusive；
  2. 保持 0.0，但**在课题/报告里显式写明"未做多重检验折扣"**（本轮已做一半：证据里带标记）；
  3. 折中：用 `attempt_index` 做 **Bonferroni/Holm 式**的 p 值收紧（把 N 折进 t 门），
     比 DSR 更直观、也更容易向使用者解释。
  **建议**：先做 2（已实现）+ 在**下一个课题**上试 3（把 N 折进 t 判据），把 DSR 作为展示性指标；
  若你希望严格对齐论文，再切到 1。
- **R18-D-2（E0002 卡 running）**：本次审计期间代理跑真实验留下的、未收口的 `running` 行。
  平台的**启动收割**会在下次服务启动时把它标成 `failed`（与"日更漏跑"同一次重启即可解决）。
