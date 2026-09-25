# Round 23 审查（loop-review-ds4f）

> **状态：CLOSED（2026-09-26 闭合）** —— 修复与回归见 `round23-fixes.md`
> 日期：2026-09-26
> 代理：R23A（统计量数值正确性）、R23B（证伪 R22 修复 + 相邻面）
> 范围：R22 提交（815cf95）后的工作树；本轮主题=**研究栈的统计地基** 与 **上一轮修复的对抗证伪**

## 0. 本轮方法

- **R23A**：把"给结论定生死"的统计量逐个**独立复算**（论文闭式解 / scipy / 已知答案
  小例子 / MC 校准），公式对拍逐点比差；再查判定门与台账口径。
- **R23B**：对 R22 的 6 项修复逐条施加对抗攻击（真跨进程、栅栏加宽竞态、token 越权、
  异常路径、口径混用），并把发现的新面一并报出。

## 1. R23A 发现（统计量）

**好消息（复算得到，写入覆盖清单）**：PSR / DSR / MinTRL 与论文闭式解逐点差 **0.00e+00**；
BH-FDR 与教科书例差 0（1000 组随机对拍 0 不一致）；配对 t 与 `scipy.ttest_rel` 差 ≤1.1e-16；
CSCV 的枚举完整性 / 阈值语义（等价于论文 ω=(r+1)/(N+1)）/ 并列 mid-rank 处理正确；
年化与未年化无泄漏（全部传日频 SR + 日频矩 + n=日收益数）。**公式层面没有系数错误。**

问题出在**判定门与台账口径**：

| 编号 | 级别 | 现象 | 复算证据 |
|---|---|---|---|
| **R23A-F1** | **P1** | `head_to_head` 的 confirmed 门仍用**未配对**单序列 PSR（`psr_ab >= 0.95`）当 AND 门——平台已在 `verdict_rules` 明文点名那是口径错误（DS-P1-6），backtest 已改配对门，h2h 漏改 | n=2430、σ=1%/日、真 ΔSR=0.3、ρ=0.98（同窗同池的现实情形）：配对 t 命中 **99.9%**，该 PSR 门命中 **0.0%** → `confirmed` 恒不可达（只能落 insufficient-evidence）；ΔSR=0、ρ=0 时该门反而 12.6%（过松） |
| **R23A-F2** | P2 | `pbo_cscv` 在"只剩一个可用变体"时输出 `PBO=1.0`（伪造"必然过拟合"）且 `degenerate_variants=False`（无标注）——其余探针退化（零成交/全现金）时可达 | 主选正常 + 2 个退化探针 → `{'pbo': 1.0, 'degenerate_variants': False}`（逐组合核对 `usable=1`，走 `else 0.0` 分支） |
| **R23A-F3** | P2 | 课题级 FDR 的 p 值 = `1 - stats.psr`（单序列、未配对）——同一份 evidence 里 `paired.psr_on_diff` 现成可用 | 真实台账：E0002 `0.3774` vs 配对 `0.2009`（**1.88×**）、E0008 `0.5086` vs `0.3124`（1.63×）；方向随 ρ 变 |
| **R23A-F4** | P2 | BH-FDR 家族成员 = "能算出 p 的实验"，与 docstring"课题内全部实验"不符 → m 少计 → 校正偏松（m=8→10 阈值放大 1.25×）；h2h/distribution 的已定论实验不进族 | T001 实测 `n_tested=8`（共 10 个实验：2 个 failed 不入族 + h2h/distribution 无 p 不入族） |
| **R23A-F5** | P2 | plateau 的 Alvarez 1σ 判据与文档隐含语义不符：拿"不在邻域内的选定点与均值之差"比"由 k 个点估的 σ̂×1.0"，且 `same_direction` 是"逐点符号全一致" | MC（2~4 万次）：真高原误判 peak **50%~60%**；边际真实改进（ΔSharpe≈0.10 = E0002 量级）误判 **74%~91%**；而 `low_confidence`（<5 点）恰在此处静默 |
| **R23A-F6** | P2→**待决策** | DSR 的 `sr_var` 缺省用"单试验估计量方差"，文献的 `V[{SR_n}]` 是**跨试验**方差；docstring 曾声称"Bailey 2014 的做法" | 用台账跨试验 SR 复算：`blank-base` 线 sd=0.4815 > 估计量 0.0303（DSR 0.569 → 0.813）；`base-v1` 线 0.0517 < 0.0033…（方向随线的离散度变，最大差 0.24） |
| R23A-F7 | P3 | `same_direction` 用严格 `>0`：邻域 Δ 恰为 0（该参数在此取值不生效）被判"反向" → peak 并写台账 | `plateau_verdict(0.30, [0.0, 0.25])` → `peak` |
| R23A-F8 | P3 | `dsr(n_trials<=1)` 含 0/负数静默退化为 PSR(0)（= 不校正，方向是**放松门**） | `dsr(...,0)` 与 `dsr(...,1)` 输出完全相同 |
| R23A-F9 | P3 | `bh_fdr` docstring 判定口径写错（实现是 `adjusted_p ≤ q`） | 实现正确（1000 组对拍），仅文案 |
| R23A-F10 | P3 | `moments()` 零方差守卫太弱：常量序列 `std=1.8e-18 > 0` → 返回非物理矩（skew=1.19、kurt=0.43） | `moments([0.01]*10)` |
| R23A-F11 | P3 | `pbo_cscv` 的 `_usable` 用日频 Sharpe 比**年化**闸门 50（死条件，易被误读为有效防线） | 日频存活值恒 ≤3.15 |
| R23A-F12 | P3 | PBO 的零假设期望随变体数 N 变化（N=2→0.50、N=3→0.33…）而台账未标注 → "PBO=0.4" 会被读成"不过拟合"（N=3 时其实高于期望） | MC 实测与解析式一致（0.3387 vs 1/3） |

## 2. R23B 发现（证伪 R22 + 相邻面）

**R22 修复的证伪结果**：`attempt_index` 并发/索引/事务三面**全部被挡住**（真跨进程 8/8 唯一、
异常路径无半截行与残留写锁、复现链与索引不冲突、`rejected_intake` 反复重提不误伤）；
约束冲突翻译（唯一约束正例）有效；MACD/Δ年化/枚举校验/CLI spec/仓位%/止损三档 6 处未被证伪。
**但攻破了 4 处**：

| 编号 | 级别 | 现象 | 复现证据 |
|---|---|---|---|
| **R23B-F1** | **P1** | **AI 可自授权样本外访问**：R22 给 MCP 加了 `holdout_token` 参数，而 token id 是 4 位顺序号（`H0001`…）且绑定校验对"未绑定 token"直接放行 → AI 猜中/传入一个全局 token 即可跑样本外窗口并**消耗人发的凭证** | 实测：无 token → `run_status=failed`（fail-closed 正确）；把 `H0001` 传进 MCP → `run_status=ran`、`holdout_touched=1`、`research_runs` 出现 4 条 `window_kind=holdout/plateau_probe`、`H0001.consumed_at` 被写；失败文案还构成存在性预言机 |
| **R23B-F2** | P2 | R22 的"统一运行信封"只覆盖**同步**分支；app 生产路径恒有 worker → `dispatch` 只剩 `{mode,queued}`，无 `run_status`/`error`，顶层 `status` 仍是运行前快照（工具文档却告诉模型"看 `dispatch.run_status`"） | 真 worker 实测：`{"status":"queued","dispatch":{"mode":"queued","queued":true}}`，随后实际 `failed` |
| **R23B-F3** | P2 | 报告 `summary.total_commission` 与 `cost.total_fees` **分母不一致**（差一个印花税额，股票必现；实测差 307.49、近 3 倍）；R22 的钉子把该错误等值固化（夹具只用 ETF） | 真管线（`asset_type=stock`）：`total_commission=144.95`、`total_stamp_tax=307.49`、`total_trading_cost=452.43 == cost.total_fees` ✓ |
| **R23B-F4** | P2 | `_trades_from_fills` 的 `pnl` 在**毛/净**两套口径间漂移：单 run 传进来的引擎富化回合只有 `pnl=qty×(exit−entry)`（费前），而 `compute_summary` 的契约是净额 → 同一份 fills 可得**相反胜率** | 同一份 fills：引擎形态 `win_rate=1.0/pf=999`（毛 +6.0 判盈）vs 报告形态 `win_rate=0.0/pf=0`（净 −9.0 判亏） |
| R23B-F5 | P2（新面） | 两进程同时打开同一库 → 初始化 DDL 竞态（`executescript` 逐语句提交，DROP/CREATE 触发器不原子）→ `trigger ... already exists`，**进程起不来** | 2 进程 1/10、6 进程 1/6 命中（R22 又往同一脚本加了一条 DDL，窗口只增不减） |
| R23B-F6 | P3 | CLI `run` / `--run` 无异常兜底：并发认领竞态时 stdout 全空 + 裸 traceback（MCP 同场景给结构化 JSON） | 两 CLI 进程抢同一实验：输家 `stdout=""`、`LifecycleError: illegal transition running -> running` |
| R23B-F7 | P3 | 约束冲突翻译按**表名子串**匹配 → `NOT NULL constraint failed: module_drafts.source` 被误报"模块已存在"（把入参非法误导成改名字再提） | `propose_module(source=None)` → `ResearchError: module already exists: probe_null@1` |
| R23B-F8 | P3 | 复现链只回溯**一级**（自动带出） vs 绑定校验回溯 8 级（不对称）：二级复现带不出 token（白烧一次运行）；且 `experiment_id` 缺失时绑定校验整体跳过 | 实测：一级 `_pick` 命中、二级 `None`；显式传同一 token 却能过 |
| R23B-F9 | P3 | `_trades_from_fills` 用 `(symbol, exit_date)` 当唯一键索引回合盈亏 → 同日双平仓互相覆盖 | 真值 [100, −300] → 两笔都拿 −300（当前单仓位语义下不可达，防御性） |
| R23B-F10 | P3 | 查重门仍是**跨连接 TOCTOU**（`find_duplicates` 在 `BEGIN IMMEDIATE` 之前）→ 两进程同时提同一 spec 会双双落库 | 栅栏版实测：同 spec/同研究线落库 2 条（attempt 1 与 2），"完全重复硬拒"在并发下失效 |

## 3. 判定

| 项 | 级别 | 结论 |
|---|---|---|
| R23A-F1 / R23B-F1 | **P1** | 成立（真实验复现：h2h 高相关场景 confirmed 恒不可达；AI 自授权样本外访问） |
| R23A-F2/F3/F4/F5、R23B-F2/F3/F4/F5 | P2 | 成立（均有复算/真调用证据） |
| R23A-F7~F12、R23B-F6~F10 | P3 | 成立，本轮一并收口 |
| R23A-F6（DSR 的 V 口径） | — | **记录为待决策点**（与 R18-D-1 的阈值决策合并；本轮先把 docstring 改成如实陈述） |

**过程性结论**：R22 的 P1（试次号）修得扎实，但同批的 P2 里有 3 条**只在钉子覆盖的
形状上成立**（同步分支信封、ETF 夹具的费用恒等式、引擎回合口径）——变异测试只验证
"钉子会红"，而钉子本身的**覆盖面**需要靠"证伪轮"来发现。这解释了为什么这个循环里
每轮仍能找到新问题，也说明"上一轮修复"必须有一轮专门的对抗复核。
