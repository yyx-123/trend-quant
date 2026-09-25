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

## V3 独立验收（第 3 个代理）：判 FAIL → 二次修复

V3 逐条复现并**推翻了 Round 2 的结论**，报 2 项 P1：

- **V3-P1-1（我引入的回归）**：R2 的"纯现金项"数量界比合法漂移更紧——合法漂移
  是**整手取整的量化随机游走**（实测 260 日 19 笔差异里 drift 恒为 1~2 手的整数倍），
  现金项低估约 3× → 首笔差 5 股即被拒，随后"一笔不归类 → 累积冻结 → 界停摆"
  形成连环误杀：260 日 7 笔 / 500 日 31 笔 / 1200 日 90 笔 / 2850 日 211 笔 /
  4000 日 294 笔假报警（父提交在这些尺度是 0）。
- **V3-P1-2**：NAV 轴在长窗口仍吸收 +100%（与父提交相同），Round 2 只加了标注
  并把余量从 3× 放宽到 5×；且新钉子只用手搓的"小额订单"形态（真实满仓形态下
  300 笔前缀的上界已是 0.9999…）——**"钉子形态 ≠ 真实调用形态"第六次复发**。

**二次修复（本轮最终口径）**：

1. 数量界 = **现金项 + 量化项**：`cum_extra_cost / price + lot_size × max(1, n_in_band)`；
2. 现金项对**所有**同日同向、价差在带内的差异累积（不再因数量检验失败而冻结）；
3. NAV 界 = `max(日计息×2, 2 × drag_ratio)`（余量从 5× 收到 2×，实测合法偏离 ≤ drag）；
4. `saturated` 判据**收紧**：`差异笔数 > 20 or 上界 > 0.05 or 曾吸收 >5% 的偏离`
   ——"未饱和"才真正等价于"判据有效"；
5. docstring 写明适用区间与实测锚点；
6. V3-P2-2（Round 2 漏网）：`_with_param` 改为**只写生效位置**，保证邻域点是
   **单参数**扰动（此前会新建 `to.params` 整体接管并丢掉其余 item 参数 →
   高原判定对着错误基准）；
7. V3-P3-3：`qty=None/NaN` 不再抛异常穿出，改判 `trade_shape_invalid`。

**实测结论（真引擎，260→6000 日全覆盖）**：

| 场景 | 结果 |
|---|---|
| 合法纯尾滑点 run（260/500/1200/2500/4000/6000 日） | `unexplained == []` **全部零假报警** |
| stage-1 尺度（260 日 / 0.001）：合法 | 未饱和、零假报警（判据有效） |
| stage-1 尺度 + 净值 +100% / +9%（注入真实 run 的末点） | **判超纲**、不饱和 |
| stage-1 尺度 + 数量 ×2 | **判超纲**、不饱和 |
| 长窗口（1200 日以上） | 零假报警 + `saturated=True` + 说明非空；验收判据为 `violations` |

**已确认无法两全的口径冲突（记录为待决策）**：长窗口下"合法偏离 47%~74%"与
"荒谬错误也被吸收"在数学上不可区分——任何绝对天花板都会误杀合法 run
（Round 2 试过 50% 天花板，实测 4000 日合法偏离 63% 被误杀）。最终口径取
"零假报警 + 饱和显式标注 + 该场景改用 violations 验收"。


## V4 独立验收（第 4 个代理）：再判 FAIL → 精确恒等式收口

V4 在独立复现后**驳倒了 Round 2 的一个核心论断**，并抓到我引入的回退：

- **B1（P1）工作树留着红灯**：两个用例必然失败——① `test_parity_attribution_rejects_large_qty_and_nav_errors`
  的 60% 数量漂移被吸收（量化项把它放宽过头）；② `test_plateau_items_accepts_dict_form_and_zero_values`
  因 `_with_param` 的 pop 抛 `KeyError: 'params'`（我改了语义没改旧钉子）。
- **B2（P1）"长窗口下不可区分"的论断被推翻**：V4 证明存在**精确**表述同时满足两个要求——
  用模型残差而非阈值。并给出真实 bug 反例：把新引擎的佣金费率翻倍（真实配置错误）
  会被我的设计**静默吸收**且判"未饱和"。
- **B3（P2）**验收尺度的静默盲区：末点净值 +5.9%、末笔数量 +21 手可在 `saturated=False` 下被吸收。
- **B4（P3）**"所有长度零假报警"的表述过宽：tail=0.008 且 n ≥ 4000 的 4/72 真实 run 因**成交清单结构错位**
  （极端滑点下新侧权益衰减 ~91%，逐笔索引不再对齐）产生差异——应表述为"对齐前提"而非"长度范围"。
- **B5（P4）** ruff 新增 1 条（`test_loop_review_ds4f_r2.py` 的 F401）。

**收口（本轮最终设计，第三版）**：把承重判据从"阈值"换成**精确恒等式**：

1. **每侧内部一致**：`equity == cash + 持仓市值`。合法 run 残差实测 **0.0**；任何伪造/漂移的净值
   （+5% / +100%）在**任意窗口长度**（260 / 2500 / 4000 / 6000 日）都被立刻判超纲
   （`nav_identity_broken`）。
2. **新侧仓位一致**：`持仓市值 == Σ(成交清单推出来的持仓量) × 当日收盘价`（收盘价取旧侧 nav）。
   任意伪造成交量（×1.6 / ×2）都被立刻判超纲（`nav_position_identity_broken`）——
   这条同时补上了 V4-B3 的"末笔 +21 手"盲区。
3. 白名单数量界保留为**归类**用途（现金项 + 量化项），不再是荒谬错误的主防线。
4. `saturated` 只表示"白名单归类的精度受限"，**不再**承担"抓不住伪造"的责任——
   伪造由恒等式无条件抓住。
5. 文档如实声明前置条件：两侧除尾盘滑点外**配置相同**（费率/计息差异属"配置对账"，
   不在归因器判别范围——V4-B2 的佣金 bug 属这一类，已如实写入 docstring）。

**实测（真引擎，逐项）**：

| 场景 | 结果 |
|---|---|
| 合法 run（260/1200/2500/4000/6000 日，tail 0.001~0.008） | `unexplained == []`（恒等式残差 0.0） |
| 伪造净值 ×1.05 / ×1.1 / ×2（260/2500/4000/6000 日） | **全部判超纲**（`nav_identity_broken`） |
| 伪造成交量 ×1.6 / ×2 | **全部判超纲**（`nav_position_identity_broken`） |
| 超滑点带的价差 | 判超纲（`trade_mismatch`） |
| 极端滑点（tail=0.008）× 超长窗口 | 成交清单结构错位 → 差异计入 `unexplained` 且 `saturated=True`（如实标注，不是假报警：该场景的逐笔对齐前提已不成立） |

**修掉的回退**：① 旧的 R1 数量漂移钉子改为**引擎形态**（nav 带 cash/持仓市值）并断言恒等式路径；
② plateau 旧钉子改为断言**解析后的有效值**（`_with_param` 已改为单参数扰动）；
③ ruff F401；④ 删掉冗余的 `nav_identity_residual_high` 标志（逐行 `nav_identity_broken` 已覆盖）。

**变异反证（新增）**：恒等式两条腿各关掉 → 对应用例失败；量化项删除 → 验收尺度假报警用例失败；
`_with_param` 回退为多参数 → 对应用例失败。共 8 项，6 项被捕获，2 项经复核确认属**冗余保护**
（`nav_identity_residual_high` 已删除；`n_trade_diffs > 20` 已补钉子直接钉住）。

## 变异反证（Round 2 钉子）

15 项定向变异中 **14 项被抓住**（唯一未命中的"累积冻结"在当前量化项足够宽时
不可观测，属冗余保护而非承重逻辑）：
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

- 全量（V4 修复后终跑）：**1593 passed / 1 failed**（失败仍全部落在改动前即 flaky 的
  `tests/test_instruments_bulk_backfill.py`，父提交上同样失败）；
- 新增钉子：`tests/unit/test_loop_review_ds4f_r2.py` 9 项；
- ruff：与基线逐条对比新增 0 条。
