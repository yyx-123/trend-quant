# Round 12 修复与回归（loop-review-ds4f）

> **状态：CLOSED（2026-09-26 闭合）** —— 审查报告见 `round12-review.md`
> 日期：2026-09-25/26

## 修复总览

| 项 | 级别 | 修复 | 触及文件 |
|---|---|---|---|
| **R12A-F1** 单跑路径的噪声直达前端（第 9 次复发） | P2 | **把闸门移到引擎 summary 出口**（单点收口）：新增 `sanitize_ratio_metrics()`，`engine.py` 对 `summary` 与 `benchmark_summary` 同时过闸 → single-run / 批量落库 / 导出 / 前端四条消费面一次覆盖，不必逐个消费面补 | `src/rule_backtest/metrics.py`、`src/rule_backtest/engine.py` |
| **R12A-F2** 因子守卫的幅值带挡不住带内脏因子 | P2 | 改为**两层判据**：① 经验合法带外（[0.05, 20]）直接跳过；② 带内但与观测跳变**严重**不一致（\|log cum − log implied\| > 0.6）跳过。`cum` 用**累乘后**的因子（停牌跨多除权日时观测跳变只对应乘积，逐因子比对会误杀第二个——本轮的 `累乘` 钉子正是这样抓到的） | `src/gateway/tradability.py` |
| **R12B-F1** 批量导出跨用户泄露实盘成交 | P2 | `_live_trades_frame(db, *, user_id=None)` 加 SQL 过滤；`export_batch_analysis(..., user_id=...)` 透传并在 manifest 声明 `live_trades_scope`；HTTP 端点要求登录并把**调用者 id** 传下去（admin 需显式 `live_all=true` 才取全量）；本地脚本保持 `None`=全体（单操作员） | `src/services/backtest_export.py`、`src/app/routers/batch_backtest.py` |
| **R12B-F2** 老持仓的止损被静默取消 | P2 | `pad_start` 前移到 **最早未平仓买入日 − 60 天**（覆盖 ATR 预热），使 `_rebuild_stop_state` 能重建状态 | `src/portfolio/live.py` |
| **R12B-F3** T+1 可卖量按最早入场日虚增 | P2 | 聚合时**逐笔留档** `lots=[(qty, buy_date)]`，可卖量 = 买入日严格早于决策日的那些笔之和（不再用最早入场日判整笔） | `src/portfolio/live.py` |
| **R12B-F4** 旧栈批量缺运行期冻结门 | P2 | 新增 `BatchBacktestService.run_batch_frozen()`（决策 A3 的冻结门），HTTP 与 `scripts/run_stop_sweep.py` 两个入口都改走它；代价（批次与日更重叠时顺延，`job_runs` 留痕）已在 docstring 写明 （**更正**：CLI 是独立进程，当时的冻结计数器是进程内实现 → 对 app 进程的日更不可见；Round 13 补跨进程哨兵文件后才真正生效）| `src/rule_backtest/batch_service.py`、`src/app/routers/batch_backtest.py`、`scripts/run_stop_sweep.py` |
| R12A backlog：calmar 不入闸无钉子 | P3 | 补 `sanitize_ratio_metrics` 语义钉子 + 年度块侧断言 | `tests/integration/test_loop_review_ds4f_r12.py` |
| R12A backlog：`engine_runs` 迁移传播无钉子 | P3 | 补迁移钉子（建库 → DROP 守卫 → 重开 → 守卫重装且 DELETE 被拒） | 同上 |
| R12A backlog：`manual_trade` 的幅值兜底是死代码 | P3 | 删除该分支（`is_degenerate_summary` 已含同键位判定） | `src/services/manual_trade.py` |
| R12A backlog：两处证据/说法不准 | P3（文书） | 更正 `round11-review.md` 的"年度噪声进夏普中位数聚合"（聚合读 `sharpe`，147 条全在 `benchmark_sharpe`）；更正 `manual_trade` 注释里不可复现的 57.4 样例 | `round11-review.md`、`src/services/manual_trade.py` |

## 新增钉子（6 条，全部变异实证）

| 钉子 | 断言 | 变异实证 |
|---|---|---|
| `test_engine_summary_exit_gates_noise_ratios`（integration） | IPO 3 根 bar 窗口：基准腿噪声 Sharpe 出引擎即为 None、年度块同为 None；120 根正常窗口两侧都保留且 \|v\|<50 | 去掉引擎出口两条闸门 → **红** |
| `test_sanitize_ratio_metrics_semantics` | 超闸 sharpe/sortino → None；`calmar=78.5` 与收益率字段原样保留（防过拦截与防扩大闸门） | 助手语义（calmar 纳入闸门）→ **红** |
| `test_export_live_trades_scoped_by_user`（integration） | ① 取数助手按 user_id 过滤、`None`=全体；② **端点**把调用者 id 传下去、admin 显式才全量、非 admin 越权无效 | 助手去过滤 → **红**；端点忘传 id → **红** |
| `test_engine_runs_delete_guard_migrates_to_existing_db` | 旧库重开 → 守卫重装且 DELETE 被拒 | 改成 `IF NOT EXISTS` → **红**（R12A 手工验证过，本钉子补回归锚） |
| `test_batch_run_frozen_enters_freeze_gate` | 批次执行必须进入冻结门且在异常时正确退出 | 直接调 `run_batch`（不走冻结门）→ **红** |
| `test_live_sellable_quantity_is_per_lot`（integration） | `昨日买 1000 + 今日买 1000` → `sellable == 1000` | 退回"最早入场日判整笔" → **红** |
| `test_live_panel_window_covers_old_positions`（integration） | 买在 400 天窗口之前 → 清单里不再出现"持仓无止损价" | 窗口退回固定 400 天 → **红** |
| 因子互证（r11 文件内的钉子改写） | 带外/带内严重不一致的因子一律跳过；跳变互证的合法因子与 0.2 折算照常生效 | 退回纯幅值带 → **红**；互证容差放到无效 → **红** |

（共 8 条断言面；R12A 的 backlog 已按上表处理 4 项。）

## 生产数据验证（主审人，只读）

| 验证 | 方法 | 结果 |
|---|---|---|
| 因子互证不误伤 | 生产库 **10,177** 条真实因子逐条跑新判据 | 仅 **2** 条被跳过（2005/2008 年两只股票，其 raw 序列自身矛盾）；**ETF 0 条** → 交易池无影响 |
| 引擎出口闸门不误伤 | 510300.SS 2015-2024 真实 run + 生产库 `batch_backtest_cells` 七个比值列 | 合法值原样（列内 max\|v\|=9.41，0 条 >50）；147 条历史噪声年度块在 HTTP 出口被清 |
| 导出权限（修复前复现） | 临时库副本 + 新建非 admin 用户调用导出端点 | 修复前：15 行覆盖他人用户；修复后：仅调用者（钉子覆盖两层） |

## 回归结果

- 相关面：`tests/integration/test_loop_review_ds4f_r12.py`（5）+ `test_live_runner.py`（7）
  + `tests/api/` + 旧栈全套 + `test_manual_trade_service.py` 全绿；
- 全量套件：**1671 passed / 2 failed**（均为既有 Windows 临时文件 flake：
  `tests/test_instruments_bulk_backfill.py::InstrumentAddJobManagerTest`）；
- ruff：`(file, rule)` 与基线 **78/78** 一致（新增 0 条）；
- 变异实证：本轮 **8 个**语义变异全部被抓住。

## 诚实记录的缺口（不阻断）

- 引擎出口对**策略腿**的接线没有独立夹具（要构造"有成交但净值单调"的策略腿），
  只由 `sanitize_ratio_metrics` 的语义钉子 + 基准腿接线钉子覆盖；改坏策略侧那一行
  不会被现有钉子拦住 → 记入 backlog。
- parity 在真实数据上多数 run 是 `saturated`、`unexplained` 会含合法项：这与本轮方向
  约束无关（去约束后逐项相同），按 docstring 该场景判据是 `violations` + 恒等式。
- R12B 的实盘清单链（F2/F3）当前**未部署**，修复后再部署时建议先跑一次对账
  （`reconcile_daily_list`）核对清单与真实持仓一致。
