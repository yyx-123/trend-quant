# Round 14 审查报告（loop-review-ds4f）

> **状态：CLOSED（2026-09-26 闭合）**
> - 修复清单与验收记录：`round14-fixes.md`；
> - 全量回归（修复后）：**1687 passed / 0 failed**（既有 Windows flake 本次未触发）；
> - ruff：(file, rule) 集合与基线 78/78 一致（新增 0 条）。
>
> 日期：2026-09-26
> 审查对象：commit `17d9aea`（Round 13 闭合后的 HEAD）
> 审查方式：两个独立代理并行——**R14A「证伪 R13 三项修复 + 补两个验证缺口」**（报告 **CLEAN**，
> 无 P1/P2）与 **R14B「沿'同源未改的一半'系统扫五个口径」**（判 NOT CLEAN，2 项 P2）。

## 总体结论

**R14A：CLEAN。R14B：NOT CLEAN（2 项 P2）。**

R14A 的 CLEAN 是**本轮唯一一次"整个面零 P1/P2"**：R13 三项修复全部经探针/变异验证（见下）；
R14B 的两条 P2 又是"同源未改的一半"——① 冻结门只覆盖了调度面与批次侧，**写入侧**没闸；
② `portfolio_live_lists` 没有用户维度。

| # | 问题 | 位置 | 影响 |
|---|---|---|---|
| R14B-F1 | 冻结门的**写入侧**缺失：HTTP 触发的历史补齐 / qfq 物化 / 指标重建均不查冻结 | `src/app/routers/instruments.py`、`src/services/instrument_jobs.py`、`src/data/service.py`、`src/services/indicator_builder.py` | 批次（37~44 分钟、逐格读行情）运行中，任一登录用户点一次"补齐/新增标的/导入成分"即可整段重写该标的 qfq 与指标缓存（冻结态实测仍写入 indicator_daily/trend_daily 各 300 行）→ 先/后跑的格子落在两版价格上且血缘不可见。**这正是决策 A3 承诺的"顺延日更/指标重建"里的"指标重建"** |
| R14B-F2 | `portfolio_live_lists` 无 user 维度：任何登录用户可读操作者的清单（持仓 + 应买应卖 + 现金 + 热）；且两用户同策略同日互相覆盖 | `src/app/routers/research_ledger.py`（`SELECT *` 无 WHERE）、DDL（`UNIQUE(list_date, strategy_version_id)`） | 多用户部署下泄露实盘计划；写入用 `ON CONFLICT` 覆盖 → 静默丢清单。（链路当前未部署，属潜伏） |

## R14A 的验证结论（CLEAN 的证据）

- **R13A-F1 影子现金/逐笔 T+1**：探针实测 `qty=1000 / qty_total=2000 / sellable_qty=1000 /
  executable=True`、`cash_est = 88508.42` 与手算一致（= 原现金 + 可卖 1000 × 参考价）、
  不可卖余额仍在 `target_holdings` 且为**副本**（`rest is not orig`，`PRE_VS_POST_DIFF: {}`）；
  全仓 grep 无第二个"qty=全仓"假设；对账路径实测 `qty_mismatches: []`（旧口径会误报）。
- **R13A-F2 跨进程哨兵**：日更实跑（取数入口设绊线）→ `status == deferred_backtest_running`
  且留痕 1 条；把 `jobs` 6 处改回 `is_frozen()` → **0.7 秒变红**；(subprocess) 跨进程可见性通过。
- **R13B-F1 格子级读取面 + backfill**：出口清单逐一核对（列表/明细/compare/CSV×3/年度聚合/引擎出口/
  backfill 助手）；`sanitize_ratio_metrics` 只清 4 个键、`calmar/excess_calmar/tail_ratio/r_*` 原样、
  键集合不变；生产库 18,333 格中 `|sharpe|>50` 与 `|sortino|>50` 均 0 行（未误杀存量）；
  CI 只用 `r_mean`（退化时为 NULL）→ 未清噪列不进 CI。

**R14A 对我两处自检的更正（重要）**：
1. 我说"日更看哨兵的接线没有钉子"**不准确**——钉子存在（R13 提交里），但它的绊线打在
   `jobs.DataService`（**只在类型注解里出现**）→ 空转；R14A 给出改进版（钉真入口
   `get_data_service`/`_pool_symbols`/`get_strategy_config`）并验证变异 0.7s 必红。
2. 我说"策略腿出口闸门构造不出夹具"**是错的**——恒入场策略 + 单调 20%/日 的 70 根 bars
   实测原值 `sharpe=9899.52`、出口为 None。两条都按 R14A 的方案落成钉子。

**R14A 的 backlog（P3，不阻断）**：`cross_process_frozen` 的 `mkdir` 在 try 之外 → 路径被文件
占用时会**崩批次**（与 docstring 承诺相反，已修）；哨兵写入失败静默 fail-open（无日志）；
6h 过期阈值无心跳（生产批次实测 36.9/42.3/44.3 分钟，8 倍余量）；`sell_list` 无 per-symbol 去重
（多卖出源同日触发时清单行合计 > 持仓；当前部署口径下不可达）；新栈单跑（CLI/MCP）不写哨兵
（1~2 分钟级，暴露窗口极小）；影子副本共享 `StopState`（当前无害，future-proof 提示）。

## 主审人的处理

R14B 的两条按"补写入侧 + 加用户维度"修（见 `round14-fixes.md`）；R14A 的三条钉子建议全部采纳
（含把我那两条空转/缺失的钉子换成他们的实测版本），另把 R14A 指出的 `mkdir` 崩溃路径一并修掉。
处理原则仍是**补闭环**：这一次补的是"写入侧"与"归属维度"。
