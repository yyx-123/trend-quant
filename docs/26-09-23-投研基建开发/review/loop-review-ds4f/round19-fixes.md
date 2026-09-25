# Round 19 修复与回归（loop-review-ds4f）

> **状态：CLOSED（2026-09-26 闭合）** —— 审查报告见 `round19-review.md`
> 日期：2026-09-26

## 修复总览

| 项 | 级别 | 修复 | 触及文件 |
|---|---|---|---|
| **R19A-F1** 我 R18 的改动把离线回填脚本改坏（`sqlite3.Row.get` 不存在 + SELECT 缺列） | P2（**我引入的回归**） | 改用 `cell["symbol"]` 下标取值；SELECT 补 `asset_type`；新增**端到端**钉子（注入临时库真跑 `backfill()`，断言两列真的写回） | `scripts/backfill_batch_excess_metrics.py`、`tests/integration/test_loop_review_ds4f_r19.py` |
| **R19A-F2** plateau 闸门一刀切 `unknown` → 单参数实验"非孤峰"永不阻断（假安全） | P2 | 收敛口径：**1 点 → unknown**（σ 无从估计）；**2~4 点 → 仍按设计口径判定但标注 `low_confidence`**（告警带邻域点数与"按低置信处理"）；**≥5 点 → 设计口径**。另用 MC 证伪"改用 t 预测区间"（2 点比值重尾，H0 误判率 77.9%） | `src/research/verdict_rules.py`、`src/research/evaluations/backtest.py`、4 处钉子同步更新 |
| **R19B-F1** `heat_cap` 只在准入时卡控，事后越线无留痕 | P2 | 新增纯函数 `heat_cap_ex_post_warning(nav_rows, cap)`：越线时产出如实告警（天数/峰值/倍数 + "不要把该 run 的敞口读作受 cap 约束"），接进 `run_warnings`；docstring 写明 cap 仅准入时刻生效 | `src/portfolio/backtester.py` |
| **R19B-F2** `all_of` 的离场语义是"任一成立"，与 §5.2.8 相反 | P2 | 离场改为 **AND**（`set.intersection`）+ 类 docstring 同步（入场本来就是 AND） | `src/portfolio/slots/execution.py` |
| **R19B-F3** `buffered_rotation` 不受 `action_gate` 门控 | P2 | `rotation_policy` 首行加 `allows_action(ctx)` 检查（与 `rebalance_band` 同口径，§5.11 缺口① 覆盖轮换） | `src/portfolio/slots/execution.py` |

## 新增钉子（5 条，全部变异实证）

| 钉子 | 断言 | 变异实证 |
|---|---|---|
| `test_backfill_script_runs_end_to_end` | 注入临时库真跑 `backfill()`：不抛异常且 `benchmark_sharpe`/`excess_sharpe` 真的写回 | 退回 `.get(...)` → **红** |
| `test_all_of_exit_requires_all_legs` | 单腿确认 → 不离场；两腿同时确认 → 离场 | 离场退回并集 → **红** |
| `test_buffered_rotation_respects_action_gate` | 非月首日门关闭 → `rotation_policy` 返回 `[]`（夹具保证"若门缺失就会真的产生轮换卖出"） | 去掉频率门 → **红** |
| `test_heat_cap_ex_post_warning_is_honest` | 未越线 → None；越线 → 文案含天数/峰值/倍数与"不得读作受 cap 约束" | 告警恒 None → **红** |
| plateau 4 处既有钉子 | 1 点 unknown；2 点保留判定 + `low_confidence`；≥5 点设计口径；告警文案带点数 | 去掉邻域数量闸门 / 取消低置信标注 → **红** |

## 回归结果

- 相关面：插槽/回测器/评估/关键路径/r11~r19 钉子文件全绿；
- 全量套件：**1702 passed / 0 failed**；
- ruff：`(file, rule)` 与基线 78/78 一致（新增 0 条）；
- 变异实证：本轮 **6 个**语义变异（含上表 4 条 + plateau 2 条）全部被抓住。

## 数字影响提示

- `all_of` 离场语义由 OR 改 AND：**生产库无使用该组合的实验**（3 条 verdict 均为简单模块），
  故当前无数字翻案；未来用该组合的实验会按文档语义（离场更难触发）运行。
- `buffered_rotation` 加门控：种子策略无该模块，爆炸半径限未来实验。
- `heat_cap` 告警与 plateau 低置信标注都只**新增可见性**，不改数字。

## 待决策新增

见 `round19-review.md` 末节：R19-D-1（plateau 邻域宽度 ±20% → ±10/20/30%，代价是探针 run 数翻倍）、
R19-D-2（heat_cap 语义：改文档口径 vs 加部分卖出机制）、R19-D-3（R19B 的 8 条 backlog 处置顺序）。
