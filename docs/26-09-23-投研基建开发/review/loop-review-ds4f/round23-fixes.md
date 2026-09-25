# Round 23 修复与回归（loop-review-ds4f）

> **状态：CLOSED（2026-09-26 闭合）** —— 审查报告见 `round23-review.md`
> 日期：2026-09-26

## 修复总览

| 项 | 级别 | 修复 | 触及文件 |
|---|---|---|---|
| **R23B-F1** AI 可自授权样本外访问（R22 加的 MCP token 参数 + 4 位顺序号 + 未绑定 token 直接放行） | **P1** | ① **MCP 工具移除 `holdout_token` 参数**（放行只来自"人把 token 绑定到具体实验"，运行期自动带出）；② `check_window` 对"`experiment_id` 缺失"**fail-closed**（匿名消费不再允许） | `src/trend_mcp/research_tools.py`、`src/research/holdout.py` |
| **R23A-F1** `head_to_head` 用未配对单序列 PSR 当判定门（高相关场景 confirmed 恒不可达） | **P1** | 新增 `verdict_rules.paired_gate_ok`（配对 t + 差序列 DSR，正/负两向）作为**单一真源**；`backtest` 与 `h2h` 都改用它；h2h 的证据块补 `dsr_on_diff`/`delta_sharpe_annual`/`n_pairs`/`gate` | `src/research/verdict_rules.py`、`src/research/evaluations/head_to_head.py` |
| **R23A-F2** PBO 在"只剩一个可用变体"时伪造 1.0 | P2 | `len(_usable)<=1` 的组合**不产 λ**（与全退化同语义）+ 新增 `combinations_skipped`；`_sharpe_vec` 把"全 NaN/单观测"列也记为退化；早返回分支补齐字段形状 | `src/research/stats/fdr_pbo.py` |
| **R23A-F3** 课题 FDR 的 p 用未配对 PSR | P2 | p 值改取 `paired.psr_on_diff`（配对差序列，与判定门同零假设）；无配对证据时回退各模块自有口径；h2h 只给 t 时用单尾 t 的 p（新增 `stats/paired.t_sf_one_sided`） | `src/research/conclusion.py`、`src/research/stats/paired.py` |
| **R23A-F4** FDR 家族成员口径与 docstring 不符（m 少计 → 校正偏松） | P2 | 摘要补 `n_excluded`（未产出可比 p 的实验数：工程失败 + 设计上不出 p 的模块），docstring 如实说明家族构成 | `src/research/conclusion.py` |
| **R23A-F5** plateau 的 1σ 判据结构错误（真高原 50~60% 误判、边际改进 74~91%） | P2 | 判据改为 **95% 预测区间**（`t_{0.975,k-1}·σ̂·√(1+1/k)`）；`same_direction` 改用**邻域均值**同号；输出补 `deviation`/`pi_t_crit`/`pi_half_width`。MC 复校（同实现代码）：真高原误判 **6.0~7.2%**、边际改进 18~24%、真孤峰功效 62~99% | `src/research/verdict_rules.py` |
| **R23B-F2** worker（app 生产路径）分支没有运行信封 | P2 | queued 分支返回 `run_status="queued"` + `experiment_status` + `poll` 指引；同步分支不变 | `src/trend_mcp/research_tools.py` |
| **R23B-F3** 报告 `total_commission` 与 `cost.total_fees` 分母不一致（含税） | P2 | 修正**钉子**为断言 `total_trading_cost == cost.total_fees`（并断言 `total_commission + total_stamp_tax` 恒等）；新增含印花税的股票腿钉子 | `tests/integration/test_loop_review_ds4f_r22.py`、`r23.py` |
| **R23B-F4** `_trades_from_fills` 毛/净口径漂移（同一份 fills 相反胜率） | P2 | 改为**由成交自身**用 `pair_round_trips` 配平净额（不再依赖调用方传的回合口径）；同日双平仓按**顺序消费**配对（F9 一并收口） | `src/portfolio/reports.py` |
| **R23B-F5** 并发初始化 DDL 竞态 → 进程起不来 | P2 | `_init_tables` 的两段 DDL 脚本各自包进显式 `BEGIN IMMEDIATE … COMMIT`（要么全生效要么回滚，第二进程排队等待）。实测修复前形态 **10 进程 50/50 必失败**，修复后 0/30 | `src/data/storage/db.py` |
| R23B-F6 CLI run 分支无异常兜底 | P3 | `run` 与 `propose-experiment --run` 补 try/except → 结构化 `{"ok": false, "run_status": "failed", "error": …}` + 退出码 1 | `scripts/research_cli.py` |
| R23B-F7 NOT NULL 被误报"模块已存在" | P3 | 约束冲突翻译改为**精确匹配**唯一约束文案 | `src/research/modules.py` |
| R23B-F8 复现链深度不对称（一级 vs 八级）+ 匿名跳过校验 | P3 | 血缘收敛为 `holdout.experiment_lineage`（单一真源），自动带出与绑定校验同深度；`check_window` 对缺 `experiment_id` fail-closed | `src/research/holdout.py`、`src/research/pipeline.py` |
| R23B-F10 查重门跨连接 TOCTOU（并发同 spec 双落库） | P3 | `find_duplicates` 支持传入连接；查重与 INSERT 收进同一 `BEGIN IMMEDIATE` 事务 | `src/research/experiments.py` |
| R23A-F7 零效应邻域被判"反向" | P3 | 同向判定用邻域均值符号 + 零视为中性 | `src/research/verdict_rules.py` |
| R23A-F8 `dsr(n_trials<1)` 静默不校正 | P3 | 显式 `ValueError`（1 仍按"首次尝试"退化 PSR(0)） | `src/research/stats/psr.py` |
| R23A-F9 `bh_fdr` docstring 口径写错 | P3 | 改为 `significant ⇔ adjusted_p ≤ q` | `src/research/stats/fdr_pbo.py` |
| R23A-F10 零方差守卫太弱（非物理矩） | P3 | 守卫改为与均值同量级的相对判据 | `src/research/stats/psr.py` |
| R23A-F11 `_usable` 死条件（日频比年化闸门） | P3 | 去掉恒真条件，退化统一由 `_sharpe_vec` 表达 | `src/research/stats/fdr_pbo.py` |
| R23A-F12 PBO 零假设期望随 N 变化而不可见 | P3 | evidence 补 `n_variants` 与 `pbo_null_expected`（解析式，MC 校验一致） | `src/research/stats/fdr_pbo.py` |
| R23A-F6 DSR 的 `sr_var` 口径 | — | **未改逻辑**（待决策），docstring 改为如实陈述"缺省=估计量方差，不是文献的跨试验 V" | `src/research/stats/psr.py` |

**未修（记录为待决策点）**：R23A-F6（DSR 的 V[{SR_n}] 口径 + 是否把 `min_dsr_on_diff`
提到论文的 0.95，与 R18-D-1 合并决策）；R23A 的盲区（`event_study`/`bucket_analysis`
的 p 值本身、PBO 变体矩阵的**日期位置对齐**、逐笔 bootstrap 的 iid 假设）；
R23B 的盲区（批次页走 DB 形态成交时 `position_pct` 可能缺失）。

## 新增钉子（16 条，变异实证 18/18 变红）

文件：`tests/integration/test_loop_review_ds4f_r23.py`

| 钉子 | 断言要点 | 变异实证 |
|---|---|---|
| `test_paired_gate_is_the_single_source` | 配对门正/负两向 + 小样本 t 临界 + DSR 门生效；h2h 与 backtest 都走单一真源，且 h2h 不再出现 `psr_ab >= 0.95` | h2h 回到旧门 / 门恒真 → **红** |
| `test_pbo_single_usable_variant_is_degenerate_not_one` | 单可用列 → `pbo=None` + `degenerate_variants=True` + 跳过计数；3 可用列时 `pbo_null_expected=1/3` | 单可用列回到 λ=0 → **红** |
| `test_sharpe_vec_marks_no_observation_columns_degenerate` | 全 NaN / 单观测列必须记 NaN | 去掉该判据 → **红** |
| `test_topic_fdr_uses_paired_p_and_reports_excluded` | 课题 p 取配对口径（源码 + 数值）+ `n_excluded` 如实上报 | 回到未配对 / 删 n_excluded → **红** |
| `test_plateau_rule_is_prediction_interval_calibrated` | 4000 次 MC：真高原误判 peak < 15%；1000 次真孤峰功效 > 0.6 | 回到 1σ + 逐点符号 → **红** |
| `test_plateau_zero_neighbor_is_not_opposite_direction` | 零邻域 → plateau；邻域贴零的真孤峰仍 → peak | 同上 → **红** |
| `test_dsr_rejects_nonpositive_trials_and_moments_guard` | `n_trials=0/-3` 抛错；`n_trials=1` 仍 PSR(0)；常量序列矩为 (0,0,3) | 两处各自 → **红** |
| `test_mcp_tools_do_not_accept_caller_tokens` | 三个工具签名无 token 参数；匿名消费被拒；具名仍可用 | MCP 重新加 token 参数 / 匿名放行 → **红** |
| `test_reproduction_lineage_auto_backfill_spans_multiple_levels` | 三级复现链：血缘 = [e3,e2,e1]；二级复现也能带出链首 token；显式传同链 token 放行 | 血缘只回溯一级 → **红** |
| `test_worker_dispatch_envelope_is_explicit` | queued 分支给 `run_status/experiment_status/poll`；同步信封形态不变 | 去掉 run_status → **红** |
| `test_init_db_is_concurrency_safe` | 10 进程 × 3 轮并发打开同一库全部成功 | 去掉事务边界 → **红**（变异后 ERROR） |
| `test_report_fee_identity_includes_stamp_tax` | 含印花税时 `total_trading_cost == total_fees` 且 `total_commission ≠ total_fees` | —（钉子口径修正） |
| `test_report_pnl_is_net_basis_regardless_of_round_trip_source` | 传入毛额回合不得改变胜率/盈亏比（净额由成交现算） | 回到毛额优先 → **红** |
| `test_module_notnull_is_not_reported_as_duplicate` | `source=None` 不得报"模块已存在" | 回到子串匹配 → **红** |
| `test_concurrent_identical_specs_only_one_lands` | 两线程同 spec 并发：恰 1 条落库 + 另一条 `duplicate_of` 拒绝 | 查重移出事务 → **红** |
| `test_cli_run_branch_has_failure_fallback` | CLI run 分支必须有异常兜底与结构化信封 | 去掉兜底 → **红** |

另有两处**既有钉子按新口径更新**（都是判据变更的必要同步，非放宽）：
`tests/unit/test_loop_review_ds4f.py` 的 2 点邻域用例（校准后 k=2 不足以判孤峰，
改为"低置信 + 极值仍能识别"）；`tests/integration/test_loop_review_ds4f_r22c.py`
的算术夹具改为按**成交净额**给出期望（并显式断言"传入的毛额回合不得改变口径"）。

## 回归结果

- **全量套件：`1708 passed / 6 skipped / 0 failed`**（R22 基线：1692 passed / 6 skipped / 0 failed）；
- ruff：`(file, rule)` 与 R22 前工作树基线 **129/129 一致**（新增 0 条；本轮一度多出 5 条已清）；
- 变异实证：**18 个语义/结构变异全部变红**（含并发、血缘深度、判据校准、DDL 事务边界）；
- 生产库只读，未写入。

## 数字影响与运维提示

1. **研究结论面会变**（这是修复的目的）：
   - h2h 实验从此**能**通过 confirmed（此前高相关场景恒不可达）；已落库的 h2h 判定
     （若有）不受影响（append-only），但**新**判定会与旧的不同；
   - 课题 FDR 的 p 值与 `still_significant` 计数会变（T001 当前结论不翻：两种口径下都 0 显著）；
   - plateau 判定会变：此前被判"疑似过拟合"的真高原（50~60%）现在不再误判，反之
     极端孤峰仍会被抓；**已落库的 `plateau_peak` 记录不追改**。
2. **PBO 语义**：退化探针导致的 `pbo=1.0` 假象改为 `pbo=None + degenerate_variants=True`；
   且现在会同时给出 `n_variants` 与零假设期望——读 PBO 时必须对照期望值（N=3 时 0.4 其实偏高）。
3. **并发启动**：多进程同时打开库不再有 DDL 竞态（app + 独立 MCP/CLI 同时拉起安全）。
4. **MCP 通道**：`research_propose_experiment` / `research_rerun_experiment` 的
   `holdout_token` 参数**已移除**（若已有外部客户端在传该参数，会收到参数错误——
   这是有意的：放行只能由人绑定；工具 docstring 已写明流程）。
5. 生产服务仍未重启（日更缺失：最后 bar 2026-09-23）；R15/R17/R18/R20/R21/R22/R23 的
   修复都需在重启后跑一次**重定基线批量**，且 h2h/plateau/FDR 口径变化意味着
   **既有课题结论需要重跑一遍**才能与平台当前口径一致（是否重跑属待决策项，见最终报告）。
