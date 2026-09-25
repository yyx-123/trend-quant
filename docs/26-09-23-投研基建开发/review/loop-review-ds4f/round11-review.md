# Round 11 审查报告（loop-review-ds4f）——确认轮 6

> **状态：CLOSED（2026-09-25 闭合）**
> - 修复清单与验收记录：`round11-fixes.md`；
> - 全量回归（修复后）：**1666 passed / 1 failed**（唯一失败为既有 Windows 临时文件 flake：
>   `tests/test_instruments_bulk_backfill.py::InstrumentAddJobManagerTest`，在基线上同样失败）；
> - ruff：(file, rule) 集合与基线 78/78 一致（新增 0 条）。
>
> 日期：2026-09-25
> 审查对象：commit `1ce4f1b`（Round 10 收口后的 HEAD；含 `43a079d` 的审查痕迹清理）
> 审查方式：三个独立代理并行——
> **R11B「全新视角」**（独立重实现 + 真实数据对拍 + 老流程 A/B + 文档抽检，122 次工具调用、
> 34 分钟）、**R11C「钉子变异扫荡」**（56 个语义变异，逐条确认钉子是否真的变红）与
> **R11A「退化腿类第 8 次搜捕」**（178 次工具调用、32 分钟；因长时间静默被主审人催收口）。

## 总体结论

**NOT CLEAN（3 项 P1 + 2 项 P2 + 4 项 P3 + 6 条空钉）**。本轮出现了**两个新机制缺陷面**（此前
5 轮全是同一个"退化腿"缺陷类的不同消费面）：

1. **R11B-F1（P1）**：ETF 的最小变动单位是 0.001，而代码按 **0.01** 舍入限价与收盘价，
   产出假涨停/假跌停；
2. **R11A-F1/F2（P1×2）**：**旧引擎链（`rule_backtest` 单跑 + 批量回测）与"逐年度"粒度
   从未接入退化闸门**——噪声已落进生产库（`batch_backtest_cells.annual_returns_json` 中
   **147 条** `|benchmark_sharpe| > 50`，最大 **16332.48**），并经 HTTP/前端/CSV 外显。
   这是退化腿类的第 8 次复发，且第一次证明**噪声已经污染生产数据**（前 7 次都只是把合成
   噪声写进新产物）。

---

## P1（三：一个 ETF 报价口径 + 两个"判据从未接线"）

### R11B-F1 ETF 限价按 0.01 舍入——39 天假涨停 + 28 天假跌停，并已造成真实误拒

- 位置：`src/gateway/tradability.py`（`_round_fen` / `_round_fen_vec`）
- 事实（生产库 + 独立重实现三重实证）：
  1. **tick 实证**：2024-01 起 ETF 的 `close` 第 3 位小数 0~9 均匀分布（各约 1.1 万条），
     同期股票 `close` 第 3 位**恒为 0** → ETF 报价精度 0.001，股票 0.01；
  2. 634 个 ETF bar 的 high/low 精确等于 `round_half_up(前收×(1±幅度), 0.001)`，其中
     555 个落点**不在 0.01 网格上** → 交易所限价确为 0.001 精度；
  3. 影响面（主审人在 enabled 池 202 只 ETF × 2024-01-01 起 133,724 行上 A/B 复现）：
     **39 天假涨停**（旧判 True、真值不是涨停）、**28 天假跌停**，另有 **8 天真跌停被旧口径
     漏判**（0.01 舍入对跌停价是"抬高"，同样会漏），涉及 34 只标的、20 个交易日。
- 为什么是 P1：`is_limit_up` 直接让买单 `unfilled`、`is_limit_down` 阻塞卖出与盘中止损——
  假信号 = 漏开仓/漏止损，直接改 NAV 与实盘动作；且 ETF 是本系统唯一实际交易品种。

## P2

### R11B-F2 创业板系 ETF 的 ±20% 判据漏掉"创业大盘"这类名称

- 事实：判据要求名称含"创业板"或"科创"，而 `159814.SZ 创业大盘ETF西部利得` 的两字之差
  导致按 ±10% 出限价（实测 2024-09-30：前收 0.364 → 真实涨停 0.437 = +20.05%，10% 口径
  不可能达到）。
- 数据驱动复核（qfq 序列，2024-01 起，enabled 池 202 只）：**20 只** ETF 的日收益幅度
  超过 10.5%（= 必须 ±20%）；修复后判据对这 **20/20 全部给出 20%**，修复前会漏 1 只。

## P3

### R11B-F3 `engine_runs` 缺 DELETE 守卫（兄弟表全有）

- 事实：13 张兄弟表（engine 子表 / 研究台账 / 组合版本）的 `no_delete` 触发器实测全部
  ABORT，唯独 `engine_runs` 的 DELETE **成功**。run 头的 `config_hash`/`data_version`/
  `git_hash`/`resolved_config_yaml` 是 §5.6 可复现性锚点——一条 DELETE 即可抹掉口径来源
  而子证据行仍在，事后无法复核该 run 的出处。

### R11B-F4 parity 的 `tail_slippage` 白名单对方向不敏感

- 事实：判带用 `abs(价差)`，"系统性更有利"的价差（滑点符号写反/取错参考价的典型形态）
  在 ≤1.1% 带内会被静默归入白名单而非判超纲。
- 主审人实测（510300.SS 2015-2024，真实引擎双跑，2430 bar/191 笔）：**不利 191 / 有利 0**
  → 加方向约束不会误杀合法 run。

### R11B-F5 脏数据守卫缺口

- `close<=0` 未挡 → `is_limit_down = (0 <= 跌停价)` 恒真，产出"像真值的坏数字"；
- 极端因子（`1e12`/`1e-12`）通过了"有限且 >0"检查 → `limit_up=0.0` 且 `is_limit_up=True`。
  生产库当前无此类数据（raw close 无 ≤0，f∈[0.2, 9.97]）。

## 空钉（R11C，56 个语义变异中抓到 6 条）

**系统性风险**：其中 5 条属于"**只断言源码字符串/出现顺序**"这一类——把实现语义改坏
（`if X:` → `if False:`、阈值放大 1000 倍）后，**119 条钉子全绿**。

| # | 空钉 | 变异后 | 它本该守住什么 |
|---|---|---|---|
| W1 | 退化腿警告"必须落 warnings"只查 `"degenerate_leg(" in getsource(...)` | `if _degen_labels:` → `if False:` **仍绿** | verdict 记录里 sharpe/sortino/psr 全 null 却无任何解释 |
| W2 | h2h 退化清零只按"文本出现顺序"检查 | `if _degenerate:` → `if False:` **仍绿** | 6e12 级噪声置信带/PSR/两侧 summary 原样持久化（R7-F1 根因复活） |
| W3 | 段内退化标记只查源码含 `degenerate_segment` | `bool(` → `bool(False) and bool(` **仍绿** | 1e12 级段内 Sharpe 重新进 collapse 门 Δ |
| W4 | reasoning 上限只查文案 `"max 4000 chars"` | 阈值 `4000 → 4_000_000` **仍绿** | 70k 字可落库/进 HTTP 与 UI 载荷 |
| W5 | wf 探针 `window_kind` 只查调用点字面量 | `if window_kind:` → `if False:` **仍绿** | engine_runs 记回 sample，与 research_runs 两本账矛盾 |
| W6 | 课题 FDR 跳过退化腿只查 `evidence_all.get("degenerate_legs")` 字样 | `... and False:` **仍绿** | 零成交实验的 `1-psr` 噪声重回 FDR 家族被算成显著 |

（R11C 另报一条**信息性**结论：`DEGENERATE_REL_TOL` 在 `len≥3` 时被幅值闸门数学蕴含，
该 tol 取值本身无判别意义——非缺陷，仅提示。）

## R11A——退化腿类第 8 次复发：旧引擎链与"逐年度"粒度**从未接闸**

**根因**：闸门（`is_degenerate_summary` / `is_degenerate_nav`）只被**研究栈**调用。
`grep -rn degenerate src/rule_backtest/` 除定义处**零命中**——旧引擎（单策略 API + 批量回测）
与逐年度比值指标完全裸奔。

### R11A-F1（P1）批量回测落库路径把噪声当"策略指标/超额夏普"

- 位置：`src/rule_backtest/batch_service.py`（`extract_cell`）、`engine.py`（summary/benchmark_summary）、
  `service.py`（HTTP 透传）
- 事实（真实构造 + 真实引擎复现）：IPO 一字板窗口的买持腿 |年化 sharpe| = **17022**，
  被写成 `benchmark_sharpe = 17022.016639082012`、`excess_sharpe = −17022.016639082012`
  ——而 `excess_*` 的构造正是判据注释**明文禁止**的"用噪声作差"。
- 消费面：`web/static/js/batch_backtest.js`（列定义/排序）、`market_view.js:1377`
  （`valueWithDiff(r.sharpe, r.benchmark_sharpe)`）、`services/backtest_export.py`（CSV）、
  `app/routers/batch_backtest.py`（API）。

### R11A-F2（P1）"逐年度"块是独立消费面，且**生产库已落库**

- 位置：`src/rule_backtest/metrics.py`（`_annual_sharpe_map` / `compute_annual_returns`）
- 事实（主审人在生产库只读复核）：`batch_backtest_cells.annual_returns_json` 共 18,333 格，
  其中 **147 条**年度块 `|benchmark_sharpe| > 50`（最大 **16332.48**；样例 2015 年块
  `benchmark_return=3.1637` 配 `benchmark_sharpe=16332.48`）；**同格子的整段
  `benchmark_sharpe` 列完全正常**（全库 0 条 > 50）→ 任何 summary 粒度闸门都会放行。
  这些块经 HTTP 取出、被前端显示、被 CSV 导出。**更正**（R12A 复核）："夏普（中位数）"聚合读的是 `sharpe` 字段，而生产库这 147 条噪声全在 `benchmark_sharpe` 上——故该聚合在真实数据上**从未**被污染（聚合读取面清噪仍是必要的防御，钉子里的噪声是塞在 `sharpe` 上的合成构造）。

### R11A-F3（P2）两个离线脚本直接写库/写文件，无闸门

`scripts/backfill_batch_excess_metrics.py`（写回 `batch_backtest_cells` 的基准列）与
`scripts/run_base_v1_sample.py`（写 `data/research/base_v1_sample/comparison.json|md`）。

### R11A-F4（P3）h2h 的 `evidence.paired.t_stat` 未随退化清零

实测两条同利率全现金腿给出 `t_stat=0.0`、`dsr_on_diff=0.5`——**不可利用**（差序列在收益空间、
退化腿的 diff 只是浮点残差），属"同类未一次清干净"的保存面。

### R11A-F5（P3）手工交易 HTTP 面的比值指标无闸门

按 `compute_manual_trade` 的 `metric_nav` 构造实测 `sharpe = 57.383`（> 闸门 50）后
`round()` 进 `/manual-trade/api/evaluate` 响应（前端不渲染，但 API/MCP 消费者会拿到）。

### R11A-F6（P3）判据 docstring 与实现自相矛盾

`metrics.py` 的 `is_degenerate_summary` docstring 写"sharpe/sortino/**calmar**"，而
`_RATIO_METRIC_KEYS` 刻意只含 sharpe/sortino（R10 结论：calmar 可合法 > 50）。

### R11A 的空钉发现

- **旧引擎整条链没有钉子**：把 `is_degenerate_summary` 改成 `return False`（判据彻底失效）后，
  批量回测/指标/导出/手工交易/研究 等 8 个文件 **132 条测试全绿**；
- 四类判据变异（去 sortino / 阈值 1e18 / 判据恒 False / NAV 判据恒 False）在 20 个相关测试
  文件里的失败**全部**落在 `test_loop_review_ds4f_r5.py` 单文件 → 判据覆盖单文件化；
- `test_sortino_noise_is_gated_alongside_sharpe` 的端到端断言写作
  `assert degenerate is None or sortino is None`，正常序列下第一支恒真 → **空钉**（把
  `_nav_summary` 的闸门改成 `if False` 仍通过）；
- `test_rule_backtest_metrics.py` 的年度断言只查存在性，从不断言过闸。

## 主审人的复核与修正

- **R11B 的 F2 证据里有一处不成立**：代理称 `159814.SZ` 在交易池内（`enabled=1`），
  实测它**不在** `instrument_metadata`（`SELECT COUNT(*) ... = 0`），因此不在本次 A/B 的
  202 只标的里。主审人改用"数据驱动 + qfq 口径"复核整池（20/20），结论不受影响但证据换掉。
- R11B 的 F1 影响数字（39/28/8）由主审人在生产数据上独立复现，并用
  `round_half_up(前收×(1±幅度), 0.001)` 逐日核对新限价（39/39、8/8 全部相符，0 反向）。
- R11A 的 F1/F2 由主审人在生产库与真实引擎上独立复现（147 条 / 最大 16332.48 / 整段列 0 条超标）。
- R11A 的 F4 被主审人降级确认：机制不同（收益空间 vs 比值空间），不可利用，仅"未收口"。

## 闭合

修复见 `round11-fixes.md`，提交 `TERMINAL_PLACEHOLDER`；新增钉子 7 项（全部变异实证）
+ 6 条空钉改写为行为级（同样变异实证）。
