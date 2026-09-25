# Round 15 修复与回归（loop-review-ds4f）

> **状态：CLOSED（2026-09-26 闭合）** —— 审查报告见 `round15-review.md`
> 日期：2026-09-26

## 修复总览

| 项 | 级别 | 修复 | 触及文件 |
|---|---|---|---|
| **R15B-P1-1** 创业板涨跌停幅度无日期维度（2020-08-24 前应为 ±10%） | **P1** | 新增常量 `_CHINEXT_20PCT_SINCE = date(2020,8,24)`；`board_limit_pct(..., as_of=None)` 加日期入参（`as_of=None` = 当前口径，保持既有调用点行为）；`compute_tradability` 逐日选口径（`limit_pct_arr`），其余板块两值相同、与旧行为**逐位一致**；科创板（688/588）不随该日期变化 | `src/gateway/tradability.py` |
| **R15A-F1** 对账写回缺 user_id | P2 | `UPDATE portfolio_live_lists SET reconcile_json … WHERE … AND user_id = ?` | `src/portfolio/live.py` |
| **R15A-F2** 写入侧守卫只覆盖一个入口 | P2 | 守卫**下沉到 `rebuild_all`**（一处覆盖 HTTP 补齐后的重建 / 启动补偿 / 日更尾 pipeline）；启动补偿 `rebuild_if_needed` 改为 **best-effort 跳过**（返回 `skipped_frozen` + 日志），避免"一次启动恰好撞上另一进程的批次就中断启动" | `src/services/indicator_builder.py` |

## 新增钉子（4 条，全部变异实证）

| 钉子 | 断言 | 变异实证 |
|---|---|---|
| `test_chinext_band_has_date_dimension` | 创业板股票/ETF：2015、2020-08-21 → 0.10；2020-08-24、2024 → 0.20；科创板 2019 → 0.20；主板不受影响；缺省 = 0.20 | 去掉日期判定 → **红**（2 条断言） |
| `test_tradability_applies_chinext_band_by_day` | 端到端：`300059.SZ` 2015-01-12 前收 35.0 → 涨停 **38.5** 且 `is_limit_up=True`（正是当年那笔不可达买入的价）；2021 → 前收 30.0 → 涨停 36.0 | `compute_tradability` 退回单一幅度 → **红** |
| `test_reconcile_writeback_is_user_scoped` | 两用户同 (date, strategy) 两行：用户 2 的对账只写自己的行，用户 1 的行保持 None | 写回去 user_id → **红** |
| `test_rebuild_all_is_frozen_guarded` | 冻结中 `rebuild_all` 抛 `FrozenWritesError`；`rebuild_if_needed` 返回 `skipped_frozen`（best-effort） | `rebuild_all` 去守卫 → **红**；启动补偿不跳过 → **红** |

## 生产数据验证（主审人，只读）

| 验证 | 方法 | 结果 |
|---|---|---|
| P1 影响面 | 27 只创业板标的 × 2014-12~2020-08-21（37,692 行）新旧口径 A/B | **新判出真涨停 909 天 + 真跌停 528 天；反向（新口径丢掉的）0 天** |
| P1 点名案例 | 真实入口 `compute_tradability` 复算 | `300059.SZ 2015-01-12`：`limit_up=38.5`（=35.00×1.1）、`is_limit_up=True` ✓；`300033.SZ 2017-07-17`：`limit_down=53.37`（=59.30×0.9）、`is_limit_down=True` ✓ |
| 无回归 | 2020-08-24 之后（含科创板/主板全样本） | 与旧口径**逐位一致**（其余板块两值相同；科创不随该日期变化） |

## 回归结果

- 相关面：`tests/integration/test_loop_review_ds4f_r15.py`（4）+ `test_live_runner.py` +
  `test_gateway.py` + `test_review_gaps.py` + r11~r14 钉子文件全绿；
- 全量套件：**1691 passed / 0 failed**（首跑出现 1 个由我的钉子造成的跨测试污染，已修，见下）；
- ruff：`(file, rule)` 与基线 78/78 一致（新增 0 条）；
- 变异实证：本轮 **5 个**语义变异全部被抓住。

## 重要记录：这次 P1 为什么能躲过 14 轮

既有断言把**错误口径写死**了：`tests/unit/test_gateway.py:186` 与
`tests/unit/test_review_r3_unit.py:41` 的 `board_limit_pct("300750.SZ") == 0.20` 用的是
"无日期"调用形态——只要实现保持"30xxxx 恒 20%"，这些断言永远为真。本轮把断言扩展为
**带日期**的版本（旧断言保留为"当前口径"检查），并在端到端钉子里用真实历史价复核。
这与 Round 1 的除权基准同型：**错误口径被自己的钉子保护时，后续审查会被测试绿灯误导**。

## 测试卫生（本轮自查发现并修掉）

全量套件首跑出现 3 个失败（其中 1 个是**我的钉子造成的跨测试污染**）：
`tests/integration/test_review_r3.py::test_freeze_defer_spawns_same_day_catchup` 断言
"补跑哨兵 10 秒内退出"，但 R14 那条日更钉子让日更真的走了 deferred 分支 → **挂起了一个
补跑哨兵线程**（等 60s × 120 轮）→ 该线程泄漏到后续测试，被 r3 的断言误认成自己的哨兵。
修复：r14 钉子把 `jobs._spawn_same_day_catchup` 打桩记录（同时也让钉子更聚焦于"顺延"本身）。

## 待决策新增

- **R15-D-1（历史数字重基线）**：本次 P1 修正后，**2015-01-01~2020-08-21 期间所有创业板标的的
  回测结果都会变**（涉及已物化的 `base_v1_sample` 结论与任何含创业板标的的历史 run）。
  建议：① 决定是否重跑这些 run 并更新物化产物；② 物化产物加"引擎/口径版本"标注，
  使口径变更后的旧产物可被识别。这件事与 R1-D-2（除权基准修复后的重基线）**是同一类**，
  建议一并排期。
- **R15-D-2（ST ±5%）**：`st_status` 恒为 `unknown` → ST 按主板 ±10%（run 池内有 1 只 ST 名标的）。
  代码注释已声明为阶段 7 缺口，是否提前到近期需要你定。
