# Round 21 修复与回归（loop-review-ds4f）

> **状态：CLOSED（2026-09-26 闭合）** —— 审查报告见 `round21-review.md`
> 日期：2026-09-26

## 修复总览

| 项 | 级别 | 修复 | 触及文件 |
|---|---|---|---|
| **R21A-F1** 守卫只拦住因子表、没拦住**返回值** → 日更仍把 qfq 整段写成不复权（真入口复现 −66.94% 假断裂） | **P1** | ① 拒绝覆盖时**`fetched[symbol]` 同步回退为本地存量**；② `rematerialize_qfq(symbol, [])` 与 `None` 同口径（空列表也回读库内因子） | `src/data/service.py` |
| **R21A-F2** 判据只看"列表为空"，上游**部分截断**（丢掉大因子）可绕过 | **P1** | 判据改为"**本地已有因子日期不得缺失**"：清空 / 部分截断 / 整批非 dict 同罪，`missing` 逐条进 warning | `src/data/service.py` |
| **R21A-F3** 多 heat_cap 门取第一个 → 告警按宽松阈值判而整条消失 | P2 | `heat_cap_of` 取**约束最紧**（`min(caps)`） | `src/portfolio/backtester.py` |
| **R21B-P2-1** 成交明细的持有天数/本次收益/浮盈/回撤由浏览器按**当前 K 线**现算（切周/月 K 静默错数、算不出时退化成自然日） | P2 | 口径搬到**数据源侧**：新增 `annotate_trade_display_metrics`，在 `slim_backtest_result` 出口用**回测自身日线**（`daily_nav` 下标 + `charts.kline` 的 high/low）算好随 trades 带出；前端删掉 66 行现算逻辑，只负责显示 | `src/rule_backtest/metrics.py`、`src/rule_backtest/service.py`、`web/static/js/market_view.js` |
| **R21B-P2-2** MCP 把策略/模块业务错误当"内部错误"→ 模型误判平台故障并盲目重试 | P2 | `_error_payload` 白名单加 `StrategyConfigError` / `ModuleRegistrationError`（`LibraryError` / `ServiceError` 既有），非业务异常仍回笼统文案 | `src/trend_mcp/research_tools.py` |
| R21-巡-1 集成测试漏出真实"当日补跑哨兵"线程 → `tests/integration` 整目录必红 | P3 | 源头用例打桩 `_spawn_same_day_catchup`（并断言**已请求挂哨兵**，比原先"真起线程"更强）；受害用例先清空单例、用完再清空 | `tests/integration/test_critical_paths.py`、`tests/integration/test_review_r3.py` |
| R21-巡-2 Windows 上 `test_instruments_bulk_backfill` 删临时库 `WinError 32` | P3 | `TemporaryDirectory(ignore_cleanup_errors=True)`（HEAD 基线同红的**环境**差异，不伪装成断言失败） | `tests/test_instruments_bulk_backfill.py` |

## 新增钉子（11 条，全部变异实证）

文件：`tests/integration/test_loop_review_ds4f_r21.py`

| 钉子 | 断言 | 变异实证 |
|---|---|---|
| `test_sync_returns_stored_factors_when_upstream_shrinks` | 上游回空 → `changed == []` **且返回值非空**（回退本地存量） | 去掉回退 → **红** |
| `test_partial_upstream_does_not_shrink_factors` | 上游只回后一条（丢 2.0）→ 被拒 + 本地 2 条原样保留 | 判据退回"列表为空" → **红** |
| `test_rematerialize_with_empty_factors_reads_back_stored` | `rematerialize_qfq(symbol, [])` 回读库内因子：qfq 全序列 ≈50（连续），**首根不是 100** | 空列表不再回读 → **红** |
| `test_heat_cap_of_takes_binding_min_gate` | 两门 `0.25 / 0.06` → `heat_cap_of == 0.06` | 取 `caps[0]` → **红** |
| `test_trade_display_metrics_use_backtest_own_series` | 持有天数 = 净值下标差（6，不是自然日 8）；浮盈 8%、回撤 −12.037%；入场前一根与出场后三根的尖刺/深坑**不得**进场 | 改自然日 / 扫全序列 / 左右边界外扩 → 三种变异全 **红** |
| `test_trade_display_metrics_degrade_to_dash_not_calendar_days` | 序列对不上时留 `None`（显示 "-"），**不**拿自然日冒充 | — （反向钉子，防退化） |
| `test_slim_result_carries_annotated_trades` | slim 剥掉 `daily_nav`/`charts` 的同时，trades 带齐四个展示字段；顶层 backward-compat trades 与首个策略**同一份** | 去掉标注 → **红** |
| `test_slim_trade_metrics_match_engine_round_trips` | **真引擎**交叉核对：260 根 MACD 探针 4 个 round trip，slim 明细的 `holding_days`/`max_profit_pct` 与引擎 `round_trips.holding_days`/`mfe_pct` 逐笔一致 | 改自然日 / 扫全序列 / 不标注 → **红** |
| `test_trade_table_does_not_compute_from_chart_payload` | 前端成交明细区块**不得出现 `currentPayload`**，且直接展示后端字段 | 重新引入 `currentPayload` 现算 → **红** |
| `test_mcp_promote_errors_are_not_reported_as_internal` | `StrategyConfigError` → 透真实原因（含 `atr_mul`）；`RuntimeError` → 仍 `internal error` | 白名单去掉该类 → **红** |
| `test_catchup_spawner_tests_do_not_leak_real_threads` | 顺延用例必须打桩哨兵生成；受害用例必须先清空单例 | 删掉打桩 → **红** |
| `test_missing_keys_are_tolerated`（既有，回归） | 缺 `trades` 键的结果不造空键（本轮改动一度破坏，已修回） | — |

## 回归结果

- 相关面：数据服务 / 网关 / 关键路径 / 回测器 / 评估 / r1~r21 钉子文件全绿；
- **全量套件：`1673 passed / 6 skipped / 0 failed`**（改动前同一条命令：`4 failed / 1668 passed`；
  `--collect-only` 报 1675 项，与运行计数差 4 属 pytest 记账口径，不由本改动引起）；
- ruff：`(file, rule)` 与 HEAD 工作树基线 **105/105 一致**（新增 0 条；本轮一度多出 1 条 `RUF059` 已就地修掉）；
- 变异实证：本轮 **10 个**语义变异 + 3 个边界变异全部被抓住（含"删掉打桩"这类测试卫生变异）；
- 生产库未被测试污染（核实：最后 bar 仍 `2026-09-23`，`job_runs` 末行 `2026-09-23 17:40`）。

## 数字影响与运维提示

1. **旧栈前端显示值会变**（这是修复的**目的**）：成交明细的持有天数/浮盈/回撤
   从此按**回测自身日线**计算——日 K 下与旧值一致的场景占多数，但
   （a）图表区间与回测区间不一致、（b）切周/月 K、（c）买卖日不在图表序列内
   这三种情形下，旧值本身就是错的（月 K 实例：2/3/3 → 44/60/60）。
   历史批量快照里的 `trades` 无这些字段，前端显示 "-"（不再显示错数）。
2. **因子链路**：`sync_ex_factors` 现在会对"上游因子日期变少"发
   `ex-factor shrink refused for ...` warning 并保留本地因子、返回值同步回退。
   若上游某次**合法**减少因子（例如数据源改口），这条 warning 就是人工确认点
   （本轮无法自动区分"数据源缺口"与"数据源更正"，属已知取舍）。
3. 生产服务仍未重启（日更缺失：最后 bar `2026-09-23`；库内有一个
   `running` 态 experiment 待启动收割），与 R15/R17/R18/R20 的旧栈修复一样，
   需在重启后跑一次**重定基线批量**。
