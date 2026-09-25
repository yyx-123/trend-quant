# Round 24 修复与回归（loop-review-ds4f）

> **状态：CLOSED（2026-09-26 闭合）** —— 审查报告见 `round24-review.md`
> 日期：2026-09-26

## 修复总览

| 项 | 级别 | 修复 | 触及文件 |
|---|---|---|---|
| **R24B-F1** R23 在判定主路径引入 **scipy 依赖**：项目声明无 scipy（`.venv` 里也没有）→ 该环境下**所有 backtest 实验 failed**（我引入的回归） | **P1** | 新增**无第三方依赖**的 t 分布模块 `stats/tdist.py`（正则化不完全贝塔连分式 + 二分反演，与 scipy 对拍 t_sf 差 ≤1.5e-13、t_ppf ≤2.6e-10）；`plateau_verdict`、`paired.t_sf_one_sided` 改用它；`_t_critical_95` 一并统一（精度自 ~1e-3 提到 1e-10） | `src/research/stats/tdist.py`（新）、`verdict_rules.py`、`stats/paired.py` |
| **R24A-F1** event_study 的 p/噪声带用**事件级 iid** 重抽样（事件按日成簇 + 80% 窗口重叠 → 名义 5% 实际 17%） | **P1** | 新增 `_common.cluster_bootstrap_means`（**两阶段簇**重抽样：先抽事件日、再抽日内事件），p 与噪声带同用该分布；evidence 落 `clustering{n_events,n_event_days,events_per_day,design_effect,bootstrap}`；单事件日退化时退回 iid 并**显式告警** | `src/research/evaluations/_common.py`、`event.py` |
| **R24B-F2** `_migrate_schema` 的 ADD COLUMN 无锁 TOCTOU + 段间不原子（10 进程 16/200 起不来；第二段失败留"30 表 0 触发器"；第三条留下半迁移的 `portfolio_live_lists`） | P2 | 整段迁移包进一个 `BEGIN IMMEDIATE` 事务（要么全生效要么整体回滚，后到进程排队）。实测：全新库 10 进程×5 轮 **0/50**（修复前 16/200）、既存库 0/50 | `src/data/storage/db.py` |
| **R24B-F4** `rejected` 分支无显著性要件（H0 下 20%~34% 判"证伪"，且台账只能降不能升） | P2 | 与 confirmed 对称：恶化也须过**配对负向门**（`paired_gate_ok(direction="negative")`）；无配对证据（样本 <30/无基准腿）→ `inconclusive`（fail-closed） | `src/research/verdict_rules.py` |
| **R24B-F3** plateau 的 `same_direction` 是符号硬币（近零区间误判 52%）、σ̂ 与 t 自由度不同源、浮点刀锋 | P2 | 方向腿**降级为诊断量**（不再当门——方向相反必然被偏离腿抓住；容差化避免 1e-17 翻转）；σ̂ 与 t 自由度**同源**（都用去重后的邻域值）。MC 复校（同实现代码，2 万次/档）：H0(μ=0) **4.95%~5.13%**、真高原 4.95%~5.24%、边际改进 4.7%~5.15%（此前 24%~52%）；真孤峰功效 k=2 25%、k=3 63%、k=4 83%、k=5 92%、k=8 98% | `src/research/verdict_rules.py` |
| **R24A-F2/F2b** bucket 置换 p 无下限/只 200 次；内部空桶语义错（可写"rejected"） | P2 | p 加 `(b+1)/(B+1)` 下限；置换次数 200 → **2000**；单调性只统计**有限**相邻差并落 `n_comparable_diffs`；端桶为空 → `spread=None`（inconclusive）；判定门与家族**统一到 p<0.05**（原联合门≈0.31%，confirmed 结构性难达） | `src/research/evaluations/bucket.py` |
| **R24A-F4** 基准 510500.SS 被流动性过滤剔除 → regime 机制在 event/bucket/wf 三路径整体失效 + 误归因文案 | P2 | 基准 `DEFAULT_REGIME_BENCHMARK` **豁免**流动性过滤；`regime_labels` 支持 `warnings_out`：基准缺失给 `regime_unavailable(...)`、部分未知给 `regime_unknown_days(n/total, reason)`（区分预热 vs 行情缺口）；event/bucket 的告警并入各自 warnings | `_common.py`、`event.py`、`bucket.py` |
| **R24A-F5** PBO 变体矩阵按位置对齐（注入 1 天缺口 ΔPBO 0.143 无告警） | P2 | 新增 `_daily_rets_with_dates`，矩阵按**日期交集**重建；落 `alignment="date_join"` 与 `days_dropped`；交集 <60 天 → `pbo_insufficient_overlap`；异常不再静默吞成 None（`pbo_unavailable(...)`） | `src/research/evaluations/backtest.py` |
| **R24A-F6** 逐笔 bootstrap 用 iid（终值带低估 1.77×、回撤尾带低估 17%~27%） | P2 | 改为**循环区块重抽样**（块长按 n^(1/3) 与 AR(1) 积分相关时间取大者，下限 5）；落 `method/block/acf1`。合成 AR(1) ρ=0.26 实测：带宽比 1.333（理论 1.363），回撤下尾 −0.349 vs iid −0.261 | `src/research/stats/bootstrap.py` |
| **R24B-F5** 同载荷 `round_trips`（毛额、无费）与 `summary`（净额）口径冲突 | P2 | `pair_round_trips` 补 `fee_total`；新增 `_round_trips_with_net` 统一补齐 `pnl_net`/`fee_total` 并把 `pnl` 归一到净额（毛额保留在 `pnl_gross`）；载荷落 `pnl_basis="net"` | `src/portfolio/reports.py`、`evaluations/backtest.py` |
| **R24A-F3** 判定门与课题 FDR 家族的显著性水平不一致 | P2（部分） | h2h 的置信带**降级为展示证据**、判定与 backtest 统一到 `paired_gate_ok`（5%），并落 `gate_alpha`/`gate`；bucket 门与家族统一到 0.05；event 落 `gate_alpha`。**家族 p 与门的口径差异**（家族是"证据强度"）记入待决策项 | `head_to_head.py`、`bucket.py`、`event.py` |
| R24B-F6 | P3 | `check_window` 对 `experiment_id` 先 strip、绑定校验后**校验实验存在**（空白/编造 id 不得消费全局 token） | `src/research/holdout.py` |
| R24B-F7 | P3 | `LINEAGE_MAX_DEPTH` 8 → **32** | `src/research/holdout.py` |
| R24B-F8 | P3 | `library.add_version` 的 `MAX(version)+1` 与 INSERT 收进 `BEGIN IMMEDIATE`（并发晋升撞唯一约束的窗口） | `src/portfolio/library.py` |
| R24B-F9 | P3 | `_traded_amount` 接受 `fill_price/price` 与 `quantity/qty` 两套键名；`cost_drag` 的费用在缺 `fee_total` 时回退 `commission+stamp_tax` | `src/portfolio/reports.py` |
| R24B-F10 | P3 | 无 verdict 的实验（工程失败）计入 `n_excluded`，让 `n_tested` 可解释 | `src/research/conclusion.py` |

**未修（记录为待决策/下轮）**：
- R24A-F3 的**家族口径决策**（BH-FDR 的 p 是"证据强度"还是"结论口径"；是否让家族 α 随模块门变化）；
- plateau 的 **k=2 功效天生低**（生产邻域全是 2 点：功效 25%）——是否扩邻域（±10/20/30% → k=4~6，功效 83%~92%）属口径决策；
- "邻域含 1 个极端离群值时真孤峰 100% 漏检"（需稳健尺度或邻域一致性判据，属口径决策）；
- `low_confidence` 是否**阻断** confirmed（当前只告警）；
- walk-forward 拼接细节（折边界日期重复/缺口）与 DSR 的 `V[{SR_n}]` 口径（R23A-F6 仍未决）。

## 新增钉子（14 条）

文件：`tests/integration/test_loop_review_ds4f_r24.py`

| 钉子 | 断言要点 |
|---|---|
| `test_verdict_path_has_no_scipy_dependency` | 判定链路源码无 `from scipy import`；`tdist` 对拍解析已知值；**屏蔽 scipy 后** plateau/paired 仍可跑 |
| `test_event_cluster_bootstrap_restores_nominal_size` | 簇口径零效应拒绝率 <9%（名义 5%），且 iid 口径至少是其 2 倍 |
| `test_event_evidence_records_design_effect` | 真实验的 evidence 落 `clustering.bootstrap == "cluster_by_event_day"` 与事件日数 |
| `test_migrate_schema_is_transactional_and_concurrency_safe` | 10 进程并发初始化全成功；`_migrate_schema` 源码含 `BEGIN IMMEDIATE` |
| `test_plateau_rule_calibrated_in_near_zero_regime` | 近零区间误判率 <9%（k=3/5）；σ̂ 与 t 临界值**同源**（[1.0,1.0,3.0] → 去重 2 点、t=12.706）；真孤峰功效 >0.8 |
| `test_rejected_requires_paired_significance` | 无配对证据/配对不显著 → inconclusive；配对显著为负 → rejected |
| `test_bucket_permutation_p_has_floor_and_gate_is_five_percent` | p>0 且等于 `(b+1)/(B+1)`；置换 2000 次；`n_comparable_diffs` 在位 |
| `test_regime_benchmark_is_exempt_from_liquidity_filter` | 基准在 keep 列表；`regime_unavailable` / `regime_unknown_days` 告警在位 |
| `test_pbo_matrix_aligns_by_date` | 日期对齐 + `alignment="date_join"` + 交集不足与异常两条告警 |
| `test_trade_bootstrap_uses_blocks_and_reports_dependence` | `method="circular_block"`、块长 ≥5、带宽随 ρ 变宽 >1.15×、回撤尾带更深 |
| `test_report_round_trips_carry_net_basis` | `pnl_basis="net"`；回合 `pnl == pnl_net`、`pnl_gross` 保留、`fee_total` = 两端费用合计；summary 与之一致 |
| `test_report_accepts_both_fill_key_forms_and_fee_fallback` | 内存键名可取价/量；缺 `fee_total` 时费用回退佣金+印花税 |
| `test_conclusion_counts_excluded_experiments` | 无 verdict 的实验计入 `n_excluded` |
| `test_holdout_token_requires_real_experiment` | 空白/不存在的 id 被拒；血缘深度 32 |

**两处既有钉子按新语义同步更新**（都是判据/门变更的必要同步，非放宽）：
`tests/unit/test_loop_review_ds4f.py`（置换 p 的下限语义）、
`tests/unit/test_paired_verdict.py`（rejected 需配对负向门——并新增"配对显著为负 → rejected"的对照）。
`tests/unit/test_loop_review_ds4f.py` 的 2 点邻域用例沿用 R23 已更新的校准口径。

## 回归结果

- **全量套件（`.venv`，项目声明环境）：`1765 passed / 0 failed`** —— 这是本轮的关键一步：
  R23 的 `1708 passed` 是在**带 scipy 的系统解释器**下取得的，而 `.venv` 下 R23 是红的；
- 全量套件（系统解释器）：见提交说明（同批代码，两条解释器都要绿）；
- ruff：`(file, rule)` 与 R23 前基线相比 **无新增**（顺带清掉 5 条既有项）；
- 并发/数值实证：迁移 0/50 失败、簇 bootstrap 拒绝率 3.8%（iid 35.5%）、
  plateau 校准 5.0%±0.2、区块 bootstrap 带宽比 1.333（理论 1.363）。

## 数字影响与运维提示

1. **判定面会变**：event/bucket/h2h 的 p 与判定口径已统一到配对/簇口径与 5%；
   `rejected` 变得**更难**（需配对显著为负）；plateau 的 peak 变得**更少误判**
   （真高原误判 52% → 5%），但 k=2 的真孤峰也更容易漏（功效 25%）——
   **是否扩邻域请见待决策清单**。
2. **带/区间会变宽**：逐笔 MC 带（区块口径）与 event 噪声带（簇口径）都比旧口径宽
   （分别 ~1.3×/1.8×），这是修正而非保守化过度——旧口径的名义 95% 实际只有 84%。
3. **部署**：`init_db` 现在是单事务（大库首次迁移仍快：229 MB 库重复 init 0.014s），
   多进程同时拉起安全；R22/R23/R24 的修复都需要在重启后跑**重定基线批量**。
4. **测试环境纪律**：`CLAUDE.md` 指定的 `.venv` 从此是**必须**通过的全量环境
   （R24B-F1 的教训：只在带 scipy 的解释器下跑会漏掉生产环境的硬依赖问题）。
