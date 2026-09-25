# Round 15 审查报告（loop-review-ds4f）

> **状态：CLOSED（2026-09-26 闭合）**
> - 修复清单与验收记录：`round15-fixes.md`；
> - 全量回归（修复后）：**1691 passed / 0 failed**；
> - ruff：(file, rule) 集合与基线 78/78 一致（新增 0 条）。
>
> 日期：2026-09-26
> 审查对象：commit `c0f5e66`（Round 14 闭合后的 HEAD）
> 审查方式：两个独立代理并行——**R15A「证伪 R14 三项修复」**（判 NOT CLEAN，2 项 P2）
> 与 **R15B「材质性缺陷专项（A 股规则/费用/守恒/研究结论/时区）」**（判 NOT CLEAN，**1 项 P1**）。

## 总体结论

**NOT CLEAN（1 项 P1 + 2 项 P2）**。

**R15B 的 P1 是 14 轮以来第一次落在"A 股交易规则自身"上，且是材质性的**：
创业板涨跌停幅度没有日期维度。代码把所有 `30xxxx`（股票与其跟踪 ETF）无条件当 ±20%，
而 **2020-08-24 注册制改革之前创业板是 ±10%**——真实涨停/跌停日因此被判为可成交。

### R15B-P1-1 创业板涨跌停幅度无日期维度（±20% 从 2020-08-24 起）

- 位置：`src/gateway/tradability.py::board_limit_pct`（`if code.startswith("30"): return _LIMIT_CHINEXT_STAR`）
- 同业自相矛盾：同文件的 `_ipo_no_limit_days` **早已**用 2020-08-24 作分界（创业板注册制），
  只有幅度漏了日期维度。
- 事实（真实入口 + 生产库独立复算）：
  - `Gateway.get_tradability` 实测 `300059.SZ 2015-01-12 → limit_up=42.00`（=35.00×1.2），
    而当日 raw close **38.50 = 35.00×1.1 正是真实涨停价**（差 1.2 倍）；
  - **生产库 `engine_fills` 全量 1276 笔逐笔比对：13 笔不可达成交**——4 笔买在真涨停收盘价上
    （本应 `unfilled(limit_up)`）、9 笔卖在真跌停收盘价上（本应 `unfilled(limit_down)`）；
  - 受影响面：4 个生产 run 池内 28 只创业板标的中 27 只有真实 10% 涨跌停日，
    2015-01-01~2020-08-21 共 2266 个 symbol-day（本次复算覆盖 1437 个）；
    落在真实涨跌停日的订单 329 笔。
- 用户可感知影响：① 买在封板日 = 顺着涨停次日惯性的有利偏差；② 卖在跌停日 = 躲过本该继续
  下跌的一天——**双重抬高回测收益**，而承受这条 NAV 的正是已物化的
  `data/research/base_v1_sample/comparison.md`（base-v1 年化 7.47%、对应 run 终值 1,996,725.61）。
- **为什么 14 轮没抓到**：既有断言 `board_limit_pct("300750.SZ") == 0.20`（`test_gateway.py:186`、
  `test_review_r3_unit.py:41`）把"无日期口径"写死了——**测试把错误口径钉住了**，与 R1 的除权基准
  同类（当时也是旧钉子把错误前提当既定事实）。

## P2

### R15A-F1 `reconcile_daily_list` 的**写回**缺 user_id（同键下跨用户串写）

- 位置：`src/portfolio/live.py` 的 `UPDATE portfolio_live_lists SET reconcile_json …`
  （SELECT 在 Round 14 已加 `user_id`，UPDATE 漏了——我的 `round14-fixes.md` 却写成"读/写带 user_id"）
- 事实（临时库两行同 (list_date, strategy_version_id)、用户 1/2）：`UPDATE` rowcount = **2**
  → 用户 2 的成交明细被写进用户 1 的 `reconcile_json`；影响隐私（对手方实盘成交）+ 台账数字
  （`reconcile_json` 正是 `slippage_tail` 的标定样本）。

### R15A-F2 写入侧守卫只覆盖 `rebuild_after_backfill`：启动补偿仍能在冻结态整段重写缓存

- 位置：`src/services/indicator_builder.py`（`rebuild_if_needed → rebuild_all → rebuild_symbol → save_indicator_daily`）
- 事实：哨兵冻结 + 进程内计数 0 时，`rebuild_if_needed` 仍返回 `{'rebuilt': 1}` 并写入
  `indicator_daily=120`、`trend_daily=120` 行（单标的；生产为全池千行级）。前置条件较窄
  （需参数漂移 + 重启 + 恰好有批次在跑），故 P2 而非 P1。

## R15A 的其余核验（正面结论）

- **409 路径成立**：冻结 → 补齐端点抛 `HTTPException 409`，且 `market_data_*`/`indicator_daily`/`job_runs`
  全部 0 写入（守卫是 `backfill_daily_history` 的第一条语句，连网络请求都没发生）。
- **迁移在有数据的旧库上正确**：3 行数据重开后行数不变、`user_id=1`、`reconcile_json` 保留、
  新唯一键生效（同用户重复 → `IntegrityError`，不同用户 → 插入成功）、无残留表。
- **冻结门不误拒合法流程**：`research/trend_mcp/engine/strategy` 对补齐/物化/重建的调用点为 0。
- 哨兵 str/Path 入参、父路径为文件时的降级、多级目录创建与清理均正确。

## R15B 的其余核验（正面结论，样本量）

- **费用与滑点**：生产库 `engine_fills` 全量 1276 笔逐笔复算（佣金 `max(gross×8.54e-5,5)`、
  印花税股票卖出 0.05%/ETF 0、成交价 = base×(1±slip)）→ **0 差异**。
- **资金与持仓守恒**：4 个 run × 2431 日 = 9724 行 NAV，`cash + positions_value = equity`
  逐日 **0 违反**；按引擎顺序重放 1276 笔成交的现金链 → 0 违反。
- **T+1**：`engine_positions` 122,022 行，当日买入 `sellable=0` **零例外**。
- **PIT/时区/日历**：`end > as_of` 截断、春节窗口剔除、交易日判定（2026-09-25 中秋）均正确。
- **研究结论可信度**：生产库 `research_verdicts/topics/experiments` 全 0 行 → 无已物化课题结论
  被污染；唯一物化结论就是上面那份被 P1 影响的 `base_v1_sample`。
- 其余 A 股规则（整手、停牌、除权基准、T+1）除 P1 本体外**未发现其它不符**。

## 主审人处理

修 P1（幅度加日期维度 + 逐日选口径）、修两条 P2（对账写回加 user_id；守卫下沉到 `rebuild_all`，
启动补偿改为 best-effort 跳过），并在生产数据上复算验证：**新判出 909 天真涨停 + 528 天真跌停、
反向 0**；R15B 点名的两笔（`300059.SZ 2015-01-12`、`300033.SZ 2017-07-17`）已分别翻成
`is_limit_up=True` / `is_limit_down=True`。
