# Round 24 审查与修复（loop-review-ds4f）

> **状态：CLOSED（2026-09-26 闭合）**
> 日期：2026-09-26
> 代理：R24A（评估模块统计面：event/bucket/distribution + PBO 对齐 + bootstrap）、
> R24B（证伪 R23 的 7 项修复 + 相邻面）
> 主题：R23 明确留下的三个统计盲区 + 上一轮修复的对抗证伪

## 0. 本轮结论（TL;DR）

**两个 P1**：① R23 自己在判定主路径引入了 **scipy 依赖**，而项目部署环境 `.venv`
里没有 scipy → **所有 backtest 实验在该环境直接 failed**（我引入的回归，已修）；
② **event_study 的 p 与噪声带未计"事件按日成簇"的设计效应**（名义 5% → 实际拒绝率
**17.0%**，SE 低估 1.77×）。

**P2 若干**（均已修）：`_migrate_schema` 的 ADD COLUMN 竞态 + 段间不原子（10 进程
16/200 起不来；第二段失败会留下"30 表 0 触发器"）、`rejected` 分支无显著性要件（H0 下
20%~34% 判"证伪"）、plateau 的 `same_direction` 仍是符号硬币（近零区间误判 52%）、
bucket 置换 p 无下限/只 200 次 + 空桶语义错、基准被流动性过滤剔除导致 **regime 机制
整体失效**、PBO 按位置对齐（注入 1 天缺口 ΔPBO 0.143 无告警）、逐笔 bootstrap 用 iid
（终值带低估 1.77×）、同一载荷 round_trips（毛）与 summary（净）口径冲突。

**R23 修复的证伪结果**：配对门、净额口径、查重同事务、原子消费、MCP 去 token、
CLI 信封、DDL 段内原子 —— **机制成立**；但被攻破 1×P1 + 4×P2（上表）——其中 P1 是
R23 **新引入**的，另有多处"只在钉子覆盖的形状上成立"（钉子先建库 → 测不到 ADD COLUMN
竞态；夹具只用 ETF → 测不到印花税口径；只测同步分支 → 测不到 worker 信封）。
**"变异测试只验证钉子会红，证伪轮才能暴露钉子覆盖面不足"** 是本轮最重要的过程结论。

## 1. R24A 发现（评估模块统计面）

| 编号 | 级别 | 现象 | 复算证据 |
|---|---|---|---|
| **R24A-F1** | **P1** | `event_study` 的 p 值与噪声带用**事件级 iid** 重抽样（7393 事件当独立样本），而事件按日成簇（同日均值 ACF(lag1)=0.418）、80% 前瞻窗口重叠 | 真实面板复算：模块 p=0.548 与台账逐位一致；SE iid 0.001169 → 簇 0.002071（1.77×）→ 10 日区块 0.003848（3.29×）；零效应 MC（4000 次）实际拒绝率 **17.03%**（名义 5%）、1% 档 9.12%；解析设计效应 1+(3.03×0.72)=3.18 与 z 的 sd=1.768 吻合 |
| **R24A-F2** | P2 | `bucket_analysis` 置换 p 只 200 次、**无 (b+1)/(B+1) 下限**（真实运行报出 `p=0.0`） | 真实特征 `atr_pct` m=5 → p=0.0 而判定 inconclusive；`momentum_20` → p=0.015；独立 B=10000 复算同数据 p=0.0142（模块 0.03）；band95 比真 95 分位高 7.5% |
| R24A-F2b | P3 | 内部空桶时 `np.sign(NaN)` 被当"方向不一致"，spread 仍是有限数 → 可判 `rejected`（"倒挂"），与告警文案"不可计算→inconclusive"相反 | 合成并列特征：桶 n=[200,0,0,600,200]、monotonicity=0.000、spread=−0.00753（有限） |
| **R24A-F3** | P2 | 判定门与课题 FDR 家族的显著性水平/统计量不一致：event 门≈单尾 2.5% vs 家族 5%；bucket 门≈0.31% vs 家族 5%；h2h 额外要求置信带（≈2.5%）而 backtest 只要 5% | event 实测 `shift=0.0025 → p=0.025 significant` vs `shift=0.0020 → p=0.061 not` ；bucket 门零效应假阳率 0.0031（严 16×）；h2h vs backtest 在 t≈1.9 时门结论相反 |
| **R24A-F4** | P2 | 基准 510500.SS 被流动性过滤剔除（窗口前 20 日均成交额 0.93 亿 < 1 亿）→ `regime_labels` 全 unknown → §6.6.3 的 regime 机制在 event/bucket/wf 三路径**整体失效**，且告警把成因误报成"2431 日 SMA200 预热不足" | 真实台账 E0003：`regime_split={}` + `regime_warmup_unknown(2431 日)`（2431 = 窗口全部交易日）；bucket 6 次真实运行**零** regime 告警；E0010（wf）`regime={}` 无告警 |
| **R24A-F5** | P2 | PBO 变体矩阵按**位置**对齐（`r[-min_len:]`） | 真实 3 run 日期一致 → ΔPBO=0（当前不出错）；注入 1 天内部缺口 ΔPBO 0.043~**0.143**、起点晚 20 天 Δ0.129，**全程无告警** |
| **R24A-F6** | P2 | 逐笔 iid bootstrap 低估终值/回撤带（逐笔 PnL lag1..5 = 0.23/0.35/0.19/0.27） | 真实 E0002 腿：点值与台账逐位一致；iid 带终值 193,845 → 区块(10) 342,961（1.77×）；回撤下尾 iid −0.5116 vs 区块 −0.6006~−0.6472；AR(1) 覆盖率 0.932→0.838→0.667（ρ=0/0.3/0.6） |
| R24A-F7 | P3 | 区块 bootstrap 的块长/次数未落库；h2h 的退化清空清单与 backtest 不一致（`dsr_on_diff` 未清） | 真实日收益 ACF≈0.04 → √n 块长过长但保守；清空清单差异在精确退化下数值无害 |

## 2. R24B 发现（证伪 R23 + 相邻面）

| 编号 | 级别 | 现象 | 复现证据 |
|---|---|---|---|
| **R24B-F1** | **P1** | R23 的 `plateau_verdict` 在函数内 `from scipy import stats`（无回退），而项目声明无 scipy、`.venv` 里确实没有 → **所有 backtest 实验 failed** | `.venv` 下真跑：`ModuleNotFoundError: No module named 'scipy'`（R23 的钉子在该环境同样 2 failed）；同脚本指向 R22 代码则成功 |
| **R24B-F2** | P2 | `_migrate_schema` 的 ADD COLUMN 是"PRAGMA 读 → ALTER 写"的无锁 TOCTOU；两段 executescript 是两个事务；第三条（live_lists 重建）根本没包事务 | 全新库 10 进程×20 轮：**16/200 失败**（`duplicate column name`）；既存库+新增列 2/100；第二段注入错误 → 30 表 0 触发器（append-only 守卫全缺）；第三条失败 → `portfolio_live_lists` 整表消失、旧表永留 |
| **R24B-F3** | P2 | plateau 的 `same_direction` 用"邻域均值符号"，近零效应下是掷硬币；σ̂ 用含重复值的 n 点而 t 自由度用去重点数 k−1（[1.0,1.0,3.0] 半宽被放宽 3 倍）；含 1 个 10σ 邻域值时真孤峰**100% 漏检**；`mean == 0.0` 精确比较 → 1e-17 浮点尘翻转判定 | MC（2 万次）：真高原 μ=0.1/σ=0.2 误判 **41.9%**、零效应 μ=0/σ=0.1 **51.8%**（几乎全来自 same_direction）；k=2 功效 61%（生产邻域**全是 k=2**） |
| **R24B-F4** | P2 | `rejected` 分支只看 ΔSharpe 点估计（≤−0.2），不查配对显著性（confirmed 侧却要 pairing 门） | MC（H0、n=2430）：ρ=0/0.5/0.7 下判 rejected **33.5%/26.0%/20.5%**，其中 78%~87% 配对 t 不显著 |
| **R24B-F5** | P2 | 同一载荷 `round_trips`（引擎毛额、无费字段）与 `summary`（净额）并存 → 可给相反结论 | 最小例：`round_trips[0].pnl=+5.0`（判盈）vs `summary.win_rate=0.0/pf=0.0`（净亏 16）；真 run 上毛/净差 244.00（费 324.52） |
| R24B-F6 | P3 | `check_window` 的"具名"只判真值 + 从不校验实验存在 → `"  "`/不存在的 id 都能消费全局 token | 实测 1d/1e 两条都 RETURN True（token 被写 consumed_at） |
| R24B-F7 | P3 | `LINEAGE_MAX_DEPTH=8` 让 ≥9 级复现链两侧一致地失效（静默 None） | 17 级链：lineage 截断为 8；绑定 E0010 跑 E0026 被拒 |
| R24B-F8 | P3 | `library.add_version` 的 `MAX(version)+1` 与 INSERT 跨连接 TOCTOU（与 R22B-F1/R23 同型），经 MCP 映射成 `internal error` | 8 进程并发晋升同线：4/10 轮出现 `UNIQUE constraint failed: portfolio_strategy_versions...`；`_error_payload(IntegrityError)` → `internal error` |
| R24B-F9 | P3 | `_traded_amount`/`cost_drag` 只认 DB 键名与 `fee_total`（docstring 声称"两套都接受"），缺键时静默降级并让两个费用字段互相矛盾 | 内存键名 → `build_report` KeyError；缺 `fee_total` → `cost.total_fees=0` 而 `summary.total_trading_cost=21` |
| R24B-F10 | P3 | `find_duplicates` 新增 `conn` 后自带连接的分支成死码；`conclusion.py` 注释称 failed 计入 excluded 但实现先 `continue` | T001：`n_tested=8, n_excluded=0` 而课题有 10 个实验 |

## 3. 修复与回归

见 `round24-fixes.md`（含 14 条新钉子与逐项变异/N 进程实证）。
