# Round 2 审查报告（loop-review-ds4f）

> **状态：OPEN（待修复验收）**
>
> 日期：2026-09-25
> 审查对象：commit `82459c6`（Round 1 闭合后全量代码）
> 审查方式：独立审查代理 R2A 专项审计**Round 1 的修复代码本身**（逐行读 diff、
> 用 `git archive c986ad1` 建父树做 A/B 数值对照、真引擎/真端到端实验/真库探针）。
> 主审人复核全部 P1/P2 断言并自行复现关键结论。

## 总体结论

**FAIL（1 项 P1 + 2 项 P2 + 9 项 P3）**——全部集中在**Round 1 的修复代码**，其中
1 项 P1 是 R1-P2-7 的修复**在真实生产尺度上失效**（钉子只覆盖了 1 笔前缀）。

Round 1 的四个 P1 修复本身经中立性 A/B 复核**逐位正确**（见"中立性复核"节），
问题出在我为修 R1-P2-7 引入的新归因界。

---

## P1

### R2A-P1-1 parity 的 NAV 级联上限在生产尺度上发散——+100% 净值错误又被吸收

- 位置：`src/engine/parity.py:287`（`nav_cascade_bound = max(日计息×2, drag_factor×3)`），
  配合 `:253-266` 的 `drag_factor` 累积
- 事实（代理用**真引擎**复现）：`drag_factor` 是全 run 的**复合乘积** `Π(1+slip)`，
  笔数一多就发散；线性 ×3 之后上限被推到几百 %：
  ```
  2850 个交易日（生产窗口尺度）、macd 全进全出、零卡控、slippage_tail=0.001：
    mismatches=3036  final drag_factor=0.250 -> nav_cascade_bound=0.749
    注入 +100% 净值错误到末点 -> nav unexplained=0（全部归入 tail_slippage）
  ```
- 为何 R1 钉子没抓住：`test_parity_attribution_rejects_large_qty_and_nav_errors`
  的 case(a) 只有 **1 笔**前缀（bound=0.0033），钉子绿而缺陷在真实 run 上原样复活
  ——这正是"钉子形态 ≠ 真实调用形态"的第四次复发。
- 修复：级联上限改为**物理量**——"新引擎相对旧引擎累计多付/少收的现金"
  （`cum_extra_cost`，逐笔实测：买入 `(P_new−P_old)×qty_old`、卖出反向），
  NAV 上限 = `max(日计息×2, 5 × 累计额外成本/平均权益)`。

---

## P2

### R2A-P2-1 同一处修复引入假报警——"该笔数量的 1/2"硬帽把合法长 run 判成超纲

- 位置：`src/engine/parity.py:257-261`（`qty_cap = max(lot, |qty|//2)`）
- 事实：真引擎实测（零卡控、唯一差异是 `slippage_tail`）：
  ```
  2850 日 tail≤0.003 -> unexplained=0（生产窗口安全）
  4000 日 tail=0.003 -> unexplained_trade=76   ← 假报警
  6000 日 tail=0.002 -> unexplained_trade=101  ← 假报警
  6000 日 tail=0.003 -> unexplained_trade=229  ← 假报警
  同一输入在 c986ad1 上：unexplained_trade=0
  ```
  失败笔的 `drift` 700–900 股而 cap=|qty|//2=550–850 —— 现金差复利**确实**能让
  单笔漂移超过该笔数量的 1/2。该假报警与 `attribute_diffs` 文档"零卡控场景
  `unexplained` 必须为空"直接冲突。
- 修复：数量界同样改用物理量——本笔之前累计额外成本所能购买的股数
  （`cum_extra_cost / price + 一手`），不再用"该笔数量的比例"。

### R2A-P2-2 R2A-P1-1 修复后仍有 ~24–50% 的数量错配被吸收（量纲问题）

- 位置：同上
- 事实：代理合成 120 笔（每笔 slip 0.11%）——drift 10% 全吸收、24% 仍吸收。
- 处置：与 P2-1 同一处修复一并收口——吸收宽度改为"累计额外成本能买的股数"，
  这是**物理上唯一**的合法来源；同时在结果里输出 `drag_ratio` /
  `nav_cascade_bound` / `saturated`，把"判不动"与"判过没问题"显式区分。
- 实测校准（真引擎，纯尾滑点、零卡控、零逻辑差异）：合法净值偏离随窗口增长到
  260 日 2.2–6.3% / 1200 日 26% / 2500 日 47% / 4000 日 63% / 6000 日 74% ——
  说明**任何**绝对百分比阈值都会误判合法长 run，故不加绝对天花板，改以
  `saturated` 显式标注判别力边界（阈值 25%）。

---

## P3

| # | 位置 | 问题 | 处置 |
|---|---|---|---|
| R2A-P3-1 | `evaluations/backtest.py:828` | `_plateau_items` 对字典形态 `to` 用**逐键合并**，而 `apply_diff` 是**整体取或**（`to.params or item.params`）→ 枚举出的 selected 值是运行从未用过的值 | 修：与 `apply_diff` 逐字一致（整体取或）+ 真实内置件形态钉子 |
| R2A-P3-2 | `evaluations/backtest.py:631-643` | wf 模式的高原探针在 `engine_runs` 里被记成 `window_kind="sample"`（static 记 `plateau_probe`）→ 两本账矛盾 | 修：`_run_walk_forward_exp_leg` 支持 `window_kind` 覆盖 + 钉子 |
| R2A-P3-3 | `slots/portfolio_risk.py:96-100` | heat_cap 把 `price is None` 也写成 `no_stop_estimate` —— 审计留痕**说谎**，且 cap 被绕过的面比文档大 | 修：三态原因（no_stop_estimate / no_price_estimate / both）+ 钉子 |
| R2A-P3-4 | `core/jobs.py:156-192` | 哨兵预算只在 `sleep` 时递减；作业返回 `deferred` 而冻结已解除时变成**忙循环**（实测 10 秒 15532 次调用、线程不退） | 修：预算按轮次扣减 + 未冻结的 deferred 退避一轮 + 钉子（同步驱动哨兵并断言调用次数有界） |
| R2A-P3-5 | `holdout.py:72-87` | docstring 称"端点先经 parse_window_bound 规范化"，实际**只解析 end** | 修：docstring 如实声明（判定只依赖 end，start 由入口校验负责） |
| R2A-P3-6 | `research/api.py:300` | purpose 非空校验只在 Web 路由层，service 面可空 purpose 发放 token | 修：校验下沉到 `holdout.grant_token`（源头，覆盖所有通道）+ 钉子 |
| R2A-P3-7 | `evaluations/backtest.py:613-621` + `round1-fixes.md` | PBO 修复的**理由**失实：父提交 wf 下 PBO 是算出来的（`0.3143`），不是"静默丢失" | 修：注释与报告如实改为"变体矩阵从错基准（拼接 vs 全窗口单 run）改为同基准" |
| R2A-P3-8 | `tests/unit/test_main_lifespan.py:75/106/136` | lifespan 钉子的 fake 接受 `after_update` 却**从不断言**；"成功路径 pipeline 恰好一次"无钉子 | 修：新增 Round 2 钉子（断言 `after_update` 是 callable + 成功/顺延两路的 pipeline 次数） |
| R2A-P3-9 | `gateway/live_overlay.py:44-55` + `portfolio/live.py:335` | `live_bars` 的合并判据只是"库里当日无 bar"，而合成 bar 的来源对真停牌标的也会兜底给价 → 可能把真停牌改判为可交易 | **待决策**（需数据侧确认 TickFlow 对停牌标的是否返回非空报价）；建议合并条件加"成交量>0" |

---

## 中立性复核（A/B 数字，代理实证）

Round 1 的修复**没有**改变应当不变的数字：

| 路径 | 结果 |
|---|---|
| static `portfolio_backtest` 的 `deltas_vs_base`/`experiment_summary`/`base_summary`/`suggested_verdict`/`regime_split` | 与父提交**逐位相同** |
| static 高原探针 delta（三处邻域点） | 两树一致 |
| static PBO | 两树一致（`pbo=0.6714285714285714`）→ 内存 NAV 与 `load_nav(run_id)` 数值等价 |
| wf 的 `deltas_vs_base`/`experiment_summary`/`walk_forward.folds` | **逐位相同** |
| wf 探针窗口 | 与主路径折窗口一致（同折、同拼接） |
| `event_study.per_horizon` | 逐位相同；新 `delta_mean_vs_global` == 父树 `delta_mean` |
| `overlap_and_cluster_stats` vs 父树内联实现 | 两树一致 |
| bucket 的 `p_value`/`monotonicity`/`q_spread` | 两树一致 |
| `reports.turnover_total/turnover_ratio` | 货币额=货币额、比率=比率（R1-P1-4 真修好） |
| 8 个 `_guard_update` + 2 个 `_no_update` 触发器 | 全部 DROP+CREATE；其余 `_no_delete` 函数体未变 |

**按设计变化**（已逐项核对为"应改"）：wf 的 `fee_total 0→None`、
`unfilled_by_reason {}→None`、`no_trades→trade_details_unavailable`、
wf plateau `peak→plateau`、wf PBO `0.3143→0.2286`、bucket 由 failed→成功且
新增 `overlap_heavy` 注记、event regime 口径改为 regime 匹配（旧值保留为
`delta_mean_vs_global`，平台内无其他消费方）。

## 已验证无问题（Round 1 修复代码的正面结论）

- `plateau_warnings` 六种形态全对且无泄漏（`_nav` 键在进 evidence 前剥除）；
- `probes[*].run_id=None`（wf）仓库内无任何解引用；
- `validate_window_spec` 23 种形态穷举：畸形全拒、合法全放行，且 `tests/`、
  `scripts/`、`docs/` 与生产库（`research_experiments` 0 行）中无合法 spec 被误杀；
- `window_touches_holdout` end 端 32 例穷举：父树的两种漏判已封堵、垃圾输入 fail-closed；
- jobs/main 的"恰好一次"在正常/启动补偿/顺延哨兵三条路径实测成立；
- 属性黑名单收窄后**未重开**已实证逃逸链（`pd.io.common.os.system` 等仍拒），
  且不误杀 `to_dict`/`out.write`/`df.drop`；
- `audit.flush` 重试（行回灌、不抛、缓冲有界）、`PanelView` 只读（直写 ValueError）、
  `registry.unregister`、`reports` 双形态键名、`backtester` unfilled 去重全部符合预期。

## 待决策点（新增，最终报告统一提交）

1. **R2A-P3-9 `live_bars` × 真停牌**：需数据侧确认 TickFlow 报价对停牌标的是否返回
   非空 `price`。若会返回，现有合并条件会把真停牌改判为可交易。**建议**：合并条件
   加 `quote.volume > 0`（或要求报价时间戳为当日盘中）。
2. **parity 判据的口径取向**（R2A 提问）："宁可漏报也不能假报警" vs "宁可假报警也
   不能吸收"。本轮取前者（物理量界 + `saturated` 显式标注），请确认。
3. **`_plateau_items`/`apply_diff` 的单点化**：两形态语义已对齐为"整体取或"；若希望
   平台契约改成"逐键合并"，需同时改 `apply_diff`（影响所有历史 diff 的解释）。
