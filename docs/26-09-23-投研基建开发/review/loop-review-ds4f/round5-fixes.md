# Round 5 修复与回归（loop-review-ds4f）

> **状态：CLOSED（2026-09-25 闭合）**
> 验收：Round 6 的两个独立确认代理（R6A/R6B）复核本轮的 5 项修复与 3 项钉子缺口 →
> 本文件"R6 复核"节记录其结论与后续修复。原 Round 5 的报告见 `round5-review.md`。

> 日期：2026-09-25
> 对应审查：`round5-review.md`（新面审查轮：存量栈与跨栈原语 → 1 P1 + 3 P2 + 3 P3）

## 修复总览

| 项 | 修复 | 钉子 |
|---|---|---|
| **R5-P1-1**（P1）生产管理员仍用源码可见的默认引导密码 | **只做可见化**（改行为属运维/凭据决策）：`_ensure_builtin_admin` 检测到"内置默认密码仍在用"→ 启动即 `SECURITY:` 响亮告警；新建且未显式配置引导密码同样告警 | `test_default_admin_password_in_use_logs_a_warning`（含"已改密则不再告警"反向断言） |
| **R5-P2-2**（P2）退化腿 Sharpe ≈ 6e12 决定判定 | 新栈侧：`_nav_summary` 判退化（`std ≤ \|mean\|·1e-6`）→ `sharpe/sortino=None` + `degenerate_leg=True`；Δ 抽 `_deltas_from_summaries`，任一腿退化则 sharpe/sortino/calmar 的 Δ 记 None；warnings 落说明 | `test_degenerate_flat_leg_does_not_gate_the_verdict` + R6 追加的 4 条 |
| **R5-P2-3**（P2）因子先落库、qfq 物化失败则静默分叉且永不重试 | **记录在案**（存量数据管线行为，改写入顺序属存量变更）→ 待决策 R5-D-3 | — |
| **R5-P2-4**（P2）CI 跑不了 CLI 用例（硬编码 Windows venv） | 改 `sys.executable` | `test_cli_test_uses_the_current_interpreter` |
| R5-P3-5 | 共享 `compute_summary` 的退化口径（Sortino 下行偏差/Sortino·Calmar 零值 vs None/PF 哨兵/`total_return` 漏首日） | **记录在案** → 待决策 R5-D-4（改共享函数会重述全部存量数字） |
| R5-P3-6 | `research_runs` 无 role 列（候选腿与对照腿不可区分） | **记录在案** → 待决策 R5-D-5 |
| R5-P3-7 | `macd` 两模式不止掩码不同（DEA 种子不同 → 跨页面口径差） | docstring 如实说明 + 钉子断言 docstring 声明了该差异 |

## R6 复核（第 11/12 个代理）与其后续修复

Round 6 的两个独立确认代理均判 **NOT CLEAN**，抓到的问题与本轮直接相关：

| # | 项 | 证据 | 修复 |
|---|---|---|---|
| **R6-P1-1**（P1，**我的修复引入的回归**） | 退化腿修复把 `sharpe` 记 None，但两处消费点仍 `float()` 相减：`backtest.py:460`（wf 折 Δ）与 `:727-729`（高原探针 Δ）→ **恰好把被修的场景打成 `status=failed`（TypeError）**（A/B：HEAD failed / 父提交 evaluating） | 真引擎 + 真 pipeline 对照 | `_delta_or_none` 统一 None 语义并接入三处（wf 折 Δ / 探针 Δ / selected Δ）；`plateau_verdict` 新增 `skipped` 计数（被剔除的退化邻域点必须可见）；另在持久化 warnings 落 `degenerate_leg(...)`（R6-P3-3） |
| **R6-P2-2**（P2） | `head_to_head@1` 仍用原始 `compute_summary` 作差 → 同一类噪声决定其判定（实测正常腿 vs 全现金腿 → `rejected`、ΔSharpe −6.09e12、置信带全是噪声） | 真 pipeline 探针 | 同口径判退化腿 → 强制 `inconclusive` + `degenerate_leg(...)` 告警 + 置信带置 None |
| R6-P3-4（P3） | R5-P2-4 的 `import sys` 位置引入 ruff I001 | ruff 逐对比较 | 移入 stdlib 块（ruff 与基线持平） |
| R6 钉子缺口 1（P3） | engine 子表守卫只钉了 2/5 张（orders/unfilled/positions 的守卫删掉仍绿） | 变异 M11 存活 | 钉子扩展为**五张子表**逐表 UPDATE/DELETE |
| R6 钉子缺口 2（P3） | `stop()` 的"被取消 future 回灌"这条腿未钉（只断言集合、没断言 `_queue`）→ 删掉该行仍绿（正是 V10 抓到的缺陷形态） | 变异 M15 存活 | 钉子追加"回灌必须落回 `_queue`"的 FIFO 断言（变异后失败） |
| R6 钉子缺口 3（P3） | "已落定 final_verdict 不可改写"的**库层**守卫只被源码注释钉住（删掉 WHEN 子句后 3 条相关钉子仍绿，实测可改写已定论行） | 变异 M24 存活 | 新增**行为**钉子：已落定后直改 `final_verdict` 必须被库层拒绝，白名单列仍可写（变异后失败） |

R6 同时确认：R5 的 5 项修复在端到端层面成立（真引擎零成交场景现在记 None 并落警告）、
`target_weight.mode` 与机制钉、五表守卫、`listing_known`、表单上限、MCP 无 SQL、
`sys.executable` 全部通过；另做了 4 类随机化不变量对拍（150 随机 NAV × 18 指标、
400 随机订单的费用模型、200 随机序列的指标原语、150 随机序列的统计件），**0 不匹配**；
跨进程确定性（NAV 2400 行 × 6 字段位级一致）、106 个敌意 HTTP 请求（无 5xx）、
135 个敌意 MCP 调用、9 个敌意 CLI 调用全部符合预期；与 `f93031e` 的 A/B 显示
12000/12000 NAV 单元格位级一致。


## Round 7 确认轮（两个独立代理）与其后续修复

两个确认代理均判 **NOT CLEAN**——抓到"退化腿"这一类修复**仍未覆盖全部消费面**（同类
问题第四次显形，根因是"逐点修"而不是"单点收口"）：

| # | 项 | 证据 | 修复（本轮：单点收口） |
|---|---|---|---|
| **R7-F1**（P2） | `head_to_head` 的退化分支只把**局部变量** `d_band` 置 None，而 `evidence["paired"]["delta_sharpe_band"]` 与 `summary_a/b` 的 6.1e12 Sharpe **仍被持久化**（与父提交逐字节相同），且该修复只有**源码文本**钉子 | 真 pipeline：正常腿 vs 全现金腿的持久化证据里噪声置信带原样存在 | 清零动作移到 **evidence/report 组装之前**（`d_band/psr_ab/两侧 summary 的 sharpe·sortino` 全清）；钉子改为**顺序断言 + 载荷字段断言** |
| **R7-F2**（P2） | 退化腿的噪声不止在 summary：`stats.psr/dsr/mintrl_days/sharpe_bootstrap` 同样 6e12 级，经 `conclusion.py` 的 `1-psr` 作 p 值 → 课题级 BH-FDR 把**零成交**实验算成**显著**（实测 `still_significant=2`，并写进 `TOPIC.md`） | 真 pipeline + 物化产物 | `stats.*` 退化时统一记 None；`evidence["degenerate_legs"]` 机器可读；`conclusion` 跳过退化腿的 p 值；`benchmark_relative` 的 beta/alpha 加相对方差守卫（此前 beta=8.2e12） |
| **R7-F3**（P3） | 同类第三处：`_regime_segment_metrics` 的段内 Sharpe 在"整段零成交"时是 1.7e13 噪声 → 经 collapse 门把 confirmed 压成 inconclusive | 真 run：段 ΔSharpe −1.88e13 | 段内退化判定 → `sharpe=None` + `sufficient_sample=False`（复用 R1-P3-15 的排除机制） |
| **R7B #1**（P3） | `pbo_cscv` 把 OOS 名次并列计为"更差"→ λ=0 → `pbo=1.0`（"必然过拟合"），与同批证据里的 `plateau=plateau` 自相矛盾（含"所有变体是同一条 run"的极端） | 真 pipeline：4/6 探针 NAV 与主选逐位相同 → 持久化 `pbo=1.0` | 并列按半步计（λ=0.5），新增 `degenerate_variants` 标记；钉子覆盖"全并列"与"真过拟合"两侧 |
| **R7-F4**（P3） | 三处钉子失效：engine 守卫钉用了**违反 CHECK** 的值（测的是列约束不是触发器，2/10 腿可删而测试仍绿）；退化告警与 h2h 只用**源码文本**断言；`_pool is None` 回灌点无钉子 | 变异实证 | engine 钉改合法值（`status='rejected'`/`reason='limit_down'`）；h2h 钉改顺序+载荷断言；补 h2h/告警/FDR 的载荷钉子 |
| **R7-F5**（P3） | 退化判定公式在两个模块各抄一份（同类已复发三次） | 代码 | 抽 `_common.is_degenerate_leg` / `null_degenerate_metrics` 单点实现 |

**本轮"单点收口"清单**（回答"为什么反复复发"）：退化判定 → `_common`；Δ 语义 →
`_delta_or_none` / `_deltas_from_summaries`；持久化可见 → `evidence["degenerate_legs"]` +
warnings；下游消费 → `conclusion`（FDR）、`_regime_split`（collapse 门）、
`benchmark_relative`（beta/alpha）、`head_to_head`（置信带/PSR/summary）。

**代理同时确认的正面结论**：生产库与 03:00 备份 **50/50 张表行数一致**、11 个
`trg_engine_*` 守卫与已落定 verdict 子句**均在生产库生效**、冗余索引已删；其独立实现的
数值复算（reports/parity/nav-summary/stats/tradability）**零不匹配**；ruff 新增 0 条；
两轮全量套件零漂移（1637/2 → 1642/2，失败均为既有 flake）。


## Round 8 确认轮（两个独立代理）与其后续修复

- **R8B：CLEAN**（零新增真实缺陷）。独立复核了：所有存量改动的逐 hunk 行为保持（含
  迁移路径：用 `f93031e` 代码建的库被 HEAD 打开后 11 个 engine 守卫装上、2 个冗余索引
  删除、旧数据 0 丢失）、跨进程逐字节确定性（NAV/fills/verdict/物化 4 文件全等）、
  ~1500 条随机不变量断言（费用守恒/报告指标/统计件/可交易性/指标原语）**零不匹配**、
  38 条失败模式探针（holdout 边界与 token/启动收割/坏 spec_json/入口拒绝）全符合预期、
  生产库 49 表行数与 03:00 备份一致且 29 个触发器定义与代码 DDL 逐字节相同。
- **R8A：NOT CLEAN**——退化腿类缺陷在第 5 次复发（三个新消费面）：

| # | 项 | 证据 | 修复（定稿：幅值闸门单点收口） |
|---|---|---|---|
| **R8-F1**（P2） | `build_report` 从 NAV **重新派生**指标，不经过 L4 的退化闸门 → 持久化/发布 6.1e12 级 Sharpe + 394 条滚动 Sharpe 噪声（普通触发条件：前 255 日无成交） | 真 run 的 `research_verdicts.report_json` + HTTP + 物化文件 | `is_degenerate_nav` 单点实现（`rule_backtest/metrics.py`，含**幅值闸门** `\|sharpe\|>50` + 相对方差判据）；`build_report` 清零 summary/滚动 Sharpe；`_common` 委托同一实现 |
| **R8-F2**（P2） | PBO 变体矩阵里全现金列的 1e12 "Sharpe" 赢下每个 CSCV 组合的 IS argmax 与 OOS 最优 → λ≡1 → `pbo=0.0`（假的"绝不拟合"） | 真 pipeline 持久化 `pbo=0.0` | `_sharpe_vec` 把退化列记 NaN；比较仅在有序列上进行；全退化 → `pbo=None + degenerate_variants=True` |
| **R8-F3**（P3） | 相对判据有**近失配带**（std/\|mean\| ∈ 5.7e-6…1.8e-5）：单调低波路径的 8.7e5 级 Sharpe **翻转判定为 rejected**、wf 折 +2.8e6、regime 段 Δ −3.9e6 且 `sufficient_sample=true` | 真 run 四处 | 幅值闸门（`\|sharpe\|>50` 即判退化）接入 `_nav_summary`/regime 段/`build_report`；4 处阈值只剩 1 份实现 |
| **R8-F4**（P3） | `up_capture`/`down_capture` 在退化基准上除以噪声均值（实测 11.4） | 探针 | 与 beta/alpha 同口径：退化腿整组记 None |
| R8-F5（P3） | 钉子缺口：`_regime_segment_metrics` 的行为回退（保留标识符）可存活 1185 个用例 | 变异 | 新增 4 条**载荷级**钉子（幅值闸门 / build_report / PBO 退化列 / capture） |

**至此"退化腿"类的单点收口链**：判据 → `rule_backtest.metrics.is_degenerate_nav`
（唯一实现）；Δ 语义 → `_delta_or_none`/`_deltas_from_summaries`；持久化可见 →
`evidence["degenerate_legs"]` + warnings；全部消费面 → summary/stats/滚动 Sharpe/
regime 段/collapse 门/benchmark_relative（beta·alpha·capture）/head_to_head/PBO/课题 FDR。


## Round 9 确认轮（第 6 次复发）与定稿修复

R9 判 **NOT CLEAN**：退化腿类第 6 次复发——根因是**"判定有两条腿（NAV + Sharpe）、
每个调用点都要记得传全"**，调用点一多必然漏。

| # | 项 | 证据 | 修复（结构性收口） |
|---|---|---|---|
| **R9-1**（P2） | `build_report` 的闸门是**整序列**的，而滚动 Sharpe 的噪声是**窗口局部**的："前 300 日无成交、之后正常"的腿整序列 Sharpe 正常 → 闸门不触发，但落在无成交段里的窗口仍是 6.3e12 级 → 实测 **392 条**噪声窗口被持久化/发布 | 引擎精确复现（前缀 255/300/385/645 日 → 132/222/392/912 条噪声） | 新增 `_rolling_sharpe_gated`：**逐窗口**判退化并剔除（`is_degenerate_summary(窗口, 该窗口的 sharpe)`） |
| **R9-2a**（P2） | `head_to_head` 的判据调用**没传 Sharpe 这条腿** → **合法可配置**的极小仓位腿（`weights`/`risk_budget_pct` 下限可达 1e-4 量级 → 年化 \|Sharpe\| 85~287）被判"未退化"，其噪声直接**双向翻转判定**（正常腿被 confirmed/rejected、ΔSharpe ±286、置信带 ±16~20） | 真 `run_head_to_head`（仅打桩数据源） | head_to_head 改走新入口 `is_degenerate_summary(rows, summary)` |
| **R9-2b**（P3） | `benchmark_relative` 的 beta 守卫与 `_bench_degenerate` 同样只有相对腿 → 近失配基准给出 beta −195…−67,470、capture 46.9 | 引擎精确复现 | 用基准腿的**年化** Sharpe 走新入口；capture 与 beta/alpha 同闸 |
| **定稿** | 判据入口结构性收口：`rule_backtest.metrics.is_degenerate_summary(nav_rows, summary)` 自动取两条腿（summary 缺 Sharpe 时**自行从 NAV 现算** `annualized_sharpe`）→ 调用点漏传在结构上不可能；`_nav_summary`/`head_to_head`/`build_report`/`benchmark_relative` 全部改走该入口；PBO 的逐块日频 Sharpe 先年化再比闸门 | — | 三条新钉子（两腿入口 / 逐窗口滚动 / 基准两腿） |

**R9 同时确认（正面）**：独立复算 4 组（判据 128 序列 + 边界 10 例、build_report 40 组
对 `ebcd0f6` 零差异、benchmark_relative 200 组零差异、PBO 40 组零差异）；**过拦截审计**：
平台真实数据 200 只 ETF × 10117 个窗口的最大年化 \|Sharpe\| 为 **4.37**（20 日窗口最大
19.07）→ 闸门 50 有 2.6~11 倍余量，当前数据上不可达（构造性 58.5 的合法序列会被排除
——记为文档口径注记，非缺陷）；两轮全量零漂移（1647/1）；ruff 新增 0 条；`.tmp_*`
未入库、工作树干净。

## 回归结果

- 全量（Round 9 修复后终跑）：**1649 passed / 2 failed**（唯一失败为既有 Windows 临时文件 flake）；
- 新增钉子：`tests/unit/test_loop_review_ds4f_r5.py` 8 项 + 三条守卫钉子（跨文件）；
- ruff：`(file, rule)` 集合与基线**完全一致**（新增 0 条）。
