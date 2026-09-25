# Round 22 修复与回归（loop-review-ds4f）

> **状态：CLOSED（2026-09-26 闭合）** —— 审查报告见 `round22-review.md`
> 日期：2026-09-26

## 修复总览

| 项 | 级别 | 修复 | 触及文件 |
|---|---|---|---|
| **R22B-F1** 并发下发 `attempt_index` 撞号 → DSR 试验次数退化且**事后不可修** | **P1** | ① COUNT 与 INSERT 收进**同一 `BEGIN IMMEDIATE` 事务**（先拿写锁再计数）；② 加部分唯一索引 `ux_research_experiments_line_attempt`（排除复现与入口被拒）——撞号当场报错而不是静默污染；③ 存量库若已撞号**不阻断启动**，日志列出冲突行交人工 | `src/research/experiments.py`、`src/data/storage/db.py` |
| **R22A-F1** 同屏两个"交易数"（成交笔数 vs 平仓回合数） | P2 | 标签显式化：汇总表/批量明细 → **成交笔数**（含 tooltip 说明），年度表/弹窗年度表 → **平仓笔数**；弹窗头改"成交 N 笔（买卖各计）" | `web/static/js/market_view.js`、`web/static/js/batch_backtest.js`、`web/templates/batch_backtest.html` |
| **R22A-F2** L4 组合报告 summary 交易类字段全 0（同载荷内有真值，已落库 6 份） | P2 | 新增 `_trades_from_fills(fills, round_trips)` 把真实成交适配成 `compute_summary` 认的交易列表（买卖各一笔；卖出笔取配对回合净额 `pnl_net`/`pnl`）→ 换成真实 trades 调用（核心函数零成交语义不动） | `src/portfolio/reports.py` |
| **R22B-F2** 运行结果语义错位 + CLI 失败退出码恒 0 | P2 | 服务层新增 `run_result_envelope(result)` 统一信封（`run_status`/`verdict`/`error`/`experiment_status`）；MCP 与 CLI 都用它；MCP 顶层 `status` 改成跑完后的**库内真实状态**；CLI `propose-experiment --run` / `rerun --run` / `run` 失败一律退出码 1 | `src/research/pipeline.py`、`src/trend_mcp/research_tools.py`、`scripts/research_cli.py` |
| **R22B-F3** 约束冲突（业务结果）被报成 internal error | P2 | `modules.propose_module` 的 INSERT 就地翻译 `sqlite3.IntegrityError`（`module_drafts`）为 `ResearchError("module already exists: name@version")` | `src/research/modules.py` |
| **R22B-F4** 复现链拿不到/用不了父实验的 holdout token | P2 | ① `_pick_unconsumed_token` 沿 `parent_experiment_id` 回溯（仅复现链）；② 绑定校验新增 `_token_covers_experiment`（只放行复现链，无关实验照旧拦）；③ MCP 两个工具加 `holdout_token` 参数，与 CLI `--token` 对称 | `src/research/pipeline.py`、`src/research/holdout.py`、`src/trend_mcp/research_tools.py` |
| **R22B-F5** `run=False` 攒批后没有任何派发入口（会卡关题） | P2 | 新增 MCP 工具 `research_run_experiment` 与 CLI 子命令 `run <experiment_id>`（非 queued 明确拒绝，防重复烧试次）；工具文档改为指向该入口 | `src/trend_mcp/research_tools.py`、`scripts/research_cli.py` |
| R22A-F3 Δ年化取"两个中位数之差" | P3 | 前端改为**逐格作差后取中位数**（与后端 `compare_batches`/`/api/compare` 同口径） | `web/static/js/batch_backtest.js` |
| R22A-F4 止损线忽略棘轮档 | P3 | 白名单补 `chandelier_stop_ratchet_price/_triggered`；前端新增 `effectiveStopPrice`（三档取高）与三行悬停算式；表头 title 同步 | `src/services/trade_records.py`、`web/static/js/market_view.js`、`web/templates/market_view.html` |
| R22A-F5 跳过原因缺 `below_min_order` | P3 | 补中文映射"低于最小申报数量" | `web/static/js/market_view.js` |
| R22A-F6 看盘页 MACD 与权威口径不同源 | P3 | `market_indicators` 改用 `macd(close, warmup=True)`（与 `indicator_daily`/`detect_macd_phase` 一致；短历史标的从"整段空白"变为有值） | `src/services/market_indicators.py` |
| R22B-F7 非法 verdict 枚举被误报为"不得升格" | P3 | 枚举校验下沉到 `confirm_verdict`：`invalid final_verdict: ... (allowed: ...)` | `src/research/verdict.py` |
| R22B-F8 CLI `--spec` 非对象时抛裸异常文本 | P3 | 先校验 JSON 与对象类型，给业务文案 + 退出码 1 | `scripts/research_cli.py` |
| R22B-F9 台账检索行缺样本外/复现标记 | P3 | `search_ledger` 行补 `holdout_touched` / `is_reproduction` / `error` | `src/research/api.py` |
| 观察项：仓位% 硬编码"全仓" | P3 | 后端按成交自身算 `position_pct = 市值/(市值+建仓后现金)`；前端展示真实值、缺字段显示"—" | `src/rule_backtest/metrics.py`、`web/static/js/market_view.js` |

**未修（记录为待决策点）**：R22B-F6（CLI 动作记成 `human` + 晋升/关题不记操作者）、
R22B-F10（"失败但未取证"的实验占用 DSR 试次并阻塞重提）——见最终报告的决策清单。

## 新增钉子（27 条，全部变异实证）

| 文件 | 钉子 | 断言要点 | 变异实证 |
|---|---|---|---|
| `test_loop_review_ds4f_r22.py` | `test_concurrent_proposals_get_distinct_attempt_index` | 6 线程并发下发 → 试次号恰为 1..6 且库内无重复 | 计数与插入拆回两个连接 → **红** |
| 同上 | `test_unique_index_blocks_duplicate_trial_number` | 同线同号插入被唯一索引挡（IntegrityError） | 删唯一索引 → **红** |
| 同上 | `test_report_summary_trade_fields_are_not_zero` | 真 pipeline 落库报告：`trade_count/closed/commission/avg_holding_days` 非 0，佣金与 `cost.total_fees` 同源，胜率/盈亏比与回合表一致 | `trades=[]` 旧形态 → **红** |
| 同上 | `test_mcp_dispatch_envelope_reports_run_failure` | MCP run 失败 → `dispatch.run_status=failed` + 原因含 holdout；顶层 status=库内真实状态；补 token 后同一步必须成功 | 信封退回只看 status / 顶层用旧快照 → **红** |
| 同上 | `test_holdout_token_follows_parent_for_reproduction` | 复现自动带出父 token + 显式传入被接受并消费 + 无关实验仍被拦 | 不回溯父实验 / 绑定校验不认复现链 → **红** |
| 同上 | `test_constraint_conflict_is_business_error_not_internal` | 竞态输家（查重看不到已有行）撞 UNIQUE → `ResearchError("module already exists")`，不得冒 sqlite 异常 | 去掉翻译 → **红** |
| 同上 | `test_cli_run_command_dispatches_parked_experiment` | `run <id>` 派发 parked 实验成功；非 queued 再派发给明确拒绝 + 退出码 1 | —（新入口） |
| `test_loop_review_ds4f_r22b.py` | `test_cli_run_failure_exit_code_is_nonzero` | `propose-experiment --run` 触碰 holdout → 退出码 1 + `run_status=failed` + 原因 | 退出码恒 0 → **红** |
| 同上 | `test_cli_rerun_failure_exit_code_is_nonzero` | `rerun --run` 失败同样非 0 且带原因 | 同上 → **红** |
| `test_loop_review_ds4f_r22c.py` | `test_trade_count_labels_disambiguate_fills_vs_round_trips` | 三个展示面都不得再出现无口径的"交易数"；口径标签齐备 | —（文案层） |
| 同上 | `test_batch_compare_delta_is_paired_per_cell` | Δ年化 必须逐格作差后再取中位数 | 回到中位数之差 → **红** |
| 同上 | `test_annotation_stop_fields_include_ratchet` | 白名单含棘轮；前端 `effectiveStopPrice` 三档取高；悬停文案随档数变化 | 白名单移除棘轮 → **红** |
| 同上 | `test_skip_reason_labels_cover_engine_codes` | 引擎每个跳过原因码都有中文映射 | 删映射 → **红** |
| 同上 | `test_market_view_macd_uses_authoritative_warmup` | 看盘页 MACD 与 `macd(warmup=True)` 逐元素一致，且短历史非空 | 回到 `warmup=False` → **红** |
| 同上 | `test_trade_position_pct_is_computed_not_hardcoded` | `position_pct = 市值/(市值+现金)`（<100%）；前端读该字段 | 前端硬编码"全仓" → **红** |
| 同上 | `test_report_summary_trade_stats_derive_from_round_trips` | 手工夹具（2 胜 1 负）：成交笔数 6/平仓 3/胜率 2⁄3/盈亏比 1870⁄915/平均持仓 16⁄3 | 回合盈亏零化 → **红** |
| 同上 | `test_invalid_final_verdict_is_reported_as_invalid` | 非法枚举报 `invalid final_verdict`，且不得出现 `downgrade only` | 去掉枚举校验 → **红** |
| 同上 | `test_search_ledger_rows_carry_holdout_and_reproduction_flags` | 检索行含两标记 | 去掉字段 → **红** |
| 同上 | `test_cli_rejects_non_object_spec` | `--spec '[1,2]'` → 退出码 1 + "必须是 JSON 对象" + 无裸异常文本 | 去掉类型校验 → **红** |

变异实证：本轮 **17 个**语义变异（含并发、逐格口径、token lineage、报告算术）全部被抓住；
其中"M17 回合盈亏零化"最初未被端到端钉子抓住（夹具恰好全为亏损回合），
据此补了算术面钉子后变红——**钉子盲区是被变异测试自己发现的**。

## 回归结果

- **全量套件：`1692 passed / 6 skipped / 0 failed`**（R21 基线：1673 passed / 6 skipped / 0 failed；
  本轮新增 19 条钉子）；
- ruff：`(file, rule)` 与 R21 提交（5a69f02）工作树基线 **129/129 一致**（新增 0 条；本轮一度多出
  7 条 —— `SIM102`×1 / `RUF100`×1 / `C408`×1 / `PLW1510`×4 —— 已就地清掉）；
- **唯一索引暴露了一处既有测试数据违反不变量**：`tests/unit/test_loop_review_r1.py` 的种子
  数据把 3 条实验插成同一 `subject_key` + 同一 `attempt_index`（此前无约束，写起来随意）；
  已改为逐行递增的试次号（语义上本就该如此），非放宽索引；
- 生产库未被写入（只读访问）；唯一索引在生产库上的创建是**幂等且无冲突**的（实测
  `(subject_key, attempt_index)` 重复组数 = 0，10 个实验）。

## 数字影响与运维提示

1. **研究台账**：`attempt_index` 从此是研究线内严格唯一的原子序号；历史产物若有撞号，
   启动时日志会列出冲突行（该列 append-only，需人工处置；本轮核查生产库**无**撞号）。
2. **报告口径**：新落库的 `full_run_report.summary` 交易类字段不再是 0——**已落库的 6 份
   旧报告仍是 0**，需要重跑那 6 个实验（或接受历史产物不改，它们只影响阅读面）。
3. **前端显示**：交易日 K 线不变，但（a）两处"交易数"标签改为成交/平仓笔数、
   （b）跨批次 Δ年化 数值会变（改为逐格口径）、（c）短历史标的的 MACD 副图从空白变为有值、
   （d）棘轮价位更高的持仓，止损线会上移到真实最早触发价——这四类变化都是**修正**，
   但用户会看到与历史截图不同。
4. **MCP 通道**：多出 `research_run_experiment` 工具；`research_propose_experiment` /
   `research_rerun_experiment` 新增 `holdout_token` 参数；运行结果里新增
   `dispatch.run_status`（自动化脚本若要判成败，应改用该字段而不是顶层 `ok`）。
5. 生产服务仍未重启（日更缺失：最后 bar 2026-09-23），与 R15/R17/R18/R20/R21 的修复
   一样，需在重启后跑一次**重定基线批量**。
