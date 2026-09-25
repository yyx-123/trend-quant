# Round 2 修复与回归（loop-review-ds4f）

> 日期：2026-09-25
> 对应审查：`round2-review.md`（1 项 P1 + 2 项 P2 + 9 项 P3，全部集中在 **Round 1 的修复代码**）

## 修复总览

| 项 | 修复 | 钉子 |
|---|---|---|
| **R2A-P1-1**（P1）parity NAV 上限在生产尺度发散 | 级联上限改为**物理量**：`cum_extra_cost`（逐笔实测的"新引擎相对旧引擎多付/少收的现金"）÷ 平均权益 × 5。旧口径 `Π(1+slip)×3` 在 3036 笔时到 0.749，把 +100% 净值错误吸收 | `test_parity_physical_bound_rejects_absurd_nav_error_on_long_prefix`（300 笔前缀 + 100% 净值 → 必须判超纲） |
| **R2A-P2-1**（P2）数量硬帽引入假报警 | 数量界同改物理量：本笔之前累计额外成本能买的股数 `cum_extra_cost/price + 一手`。实测 4000~6000 日合法 run 的 76~229 笔假报警应当消失，同时 +100% 数量漂移仍被拒 | `test_parity_physical_bound_rejects_absurd_qty_drift`（双向断言：+100% 判超纲 / 合法漂移必须归类） |
| **R2A-P2-2**（P2）吸收宽度量纲不当 | 同上一并收口；新增 `saturated` + `attribution_note` + `nav_cascade_bound` + `drag_ratio` 输出，显式区分"判不动"与"判过没问题"；docstring 写明适用区间（短/中窗口 ≲200 笔）。真引擎校准：合法净值偏离 260 日 2.2~6.3% / 1200 日 26% / 2500 日 47% / 4000 日 63% / 6000 日 74%——**任何绝对阈值都会误判合法长 run**，故不加天花板 | `test_parity_reports_saturation_flag` |
| R2A-P3-1 | `_plateau_items` 的字典形态参数取值改为与 `apply_diff` **逐字一致**（整体取或，不是逐键合并） | `test_plateau_items_matches_apply_diff_param_semantics`（**真实内置件** hard_stop@1 + 真实 `apply_diff` 对照） |
| R2A-P3-2 | wf 高原探针的 `engine_runs.window_kind` 显式覆盖为 `plateau_probe` | `test_wf_plateau_probe_records_window_kind` |
| R2A-P3-3 | heat_cap 留痕原因三态化（no_stop_estimate / no_price_estimate / both） | `test_heat_cap_gate_log_reason_is_truthful` |
| R2A-P3-4 | 哨兵预算**按轮次**扣减；`deferred` 且未冻结时退避一轮（消除忙循环） | `test_catchup_sentinel_does_not_hot_spin`（同步驱动哨兵，断言调用次数有界且确有退避） |
| R2A-P3-5 | `window_touches_holdout` docstring 如实声明"判定只依赖 end" | 文档（与 `test_holdout_window_*` 一并覆盖） |
| R2A-P3-6 | purpose 非空校验**下沉到 `holdout.grant_token`**（覆盖 Web/service/未来通道） | `test_grant_token_rejects_empty_purpose_at_source` |
| R2A-P3-7 | PBO 修复的理由改为实情（父提交 wf 下 PBO 是算出来的，真改进是"错基准→同基准"） | 注释更正 |
| R2A-P3-8 | lifespan 钉子补强：断言 `after_update` 是 callable + 成功/顺延两路的 pipeline 次数 | `test_daily_update_passes_after_update_and_runs_pipeline_once` |
| R2A-P3-9 | `live_bars` × 真停牌（合成 bar 对停牌标的也会兜底给价 → 可能把真停牌改判可交易） | **待决策**：需数据侧确认 TickFlow 停牌标的是否返回非空报价；建议合并条件加 `volume > 0`。不在本轮静默改（属口径选择） |

## 变异反证（Round 2 钉子）

10 项定向变异 **10/10 被抓住**：
parity 旧复合乘积界 / parity 旧滑点上界数量界 / saturation 恒 False /
plateau 逐键合并 / wf 探针 window_kind 还原 / heat_cap 原因还原 /
哨兵预算还原（忙循环）/ grant_token 校验删除 / after_update 未传 / 成功路径 pipeline 双跑。

## 中立性（A/B 对照，代理实证）

Round 1 的修复**未改变应当不变的数字**：static 与 wf 的
`deltas_vs_base`/`experiment_summary`/`base_summary`/`walk_forward.folds`/
static 高原探针 delta/static PBO/`event_study.per_horizon`/bucket 的
`p_value·monotonicity·q_spread` 全部与父提交逐位一致。差异项全部是"应改"
（wf 的费用/未成交可用性、wf 高原探针基准、wf PBO 基准、bucket 由 failed 转成功、
event regime 口径——旧值保留为 `delta_mean_vs_global`）。

## 回归结果

- 全量：**1588 passed / 1 failed**（失败仍全部落在改动前即 flaky 的
  `tests/test_instruments_bulk_backfill.py`，父提交上同样失败）；
- 新增钉子：`tests/unit/test_loop_review_ds4f_r2.py` 9 项；
- ruff：与基线逐条对比新增 0 条。
