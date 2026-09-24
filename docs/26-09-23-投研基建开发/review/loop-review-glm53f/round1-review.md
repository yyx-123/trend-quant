# Round 1 审查报告（loop-review-glm53f）

> **状态：CLOSED（2026-09-24 闭合）**
> - 修复清单与回归结果：`round1-fixes.md`；
> - 验收子代理 V1（P1 实证，含双线程竞态复现）：**V1_VERDICT: PASS**；
> - 验收子代理 V2（P2/P3 + 回归审查）：首轮 **FAIL**（R1-P2-2 修复无效——
>   any_of 成员实例缺 `_registered_key`，递归重建落兜底分支）→ 补一行修复
>   （`_MetaBase.__init__` 打成员键）+ 新增 any_of 重建钉子 + 顺修 live ATR
>   ffill 口径与 auth mcp 用例 importorskip → **V2_REVERDICT: PASS**；
> - 最终全量回归：1470+ 通过 / 0 新增失败（详见 fixes 文档回归结果节）。
>
> 日期：2026-09-24
> 审查对象：commit `f93031e`（投研基建一期全量实现：src/engine、src/gateway、src/portfolio、src/research、tests、scripts、web、7 个存量文件修改）
> 审查依据：`docs/26-09-20-投研基建架构/`（架构稿 20 条决策 + 总体方案设计 §1–§10 + 附录 A + 后续TODO 分期边界）、`2026-09-23-开发日志.md`
> 审查方式：主审人通读全部设计文档（约 3300 行）并主审 L2 引擎（fees/matcher/stops/account/models/engine/store）、L3 backtester 日循环与 context、L1.5 panel/tradability、live 实盘运行器、统计件（psr/paired/bootstrap/fdr_pbo/verdict_rules）、lifecycle/holdout/run_freeze/db DDL 触发器；另派 3 个独立审查代理分别覆盖 research 包、portfolio+gateway 包、存量影响面。所有 P1/P2 断言均经主审人逐条复核源码确认后才列入本报告。
> 测试基线：全量套件 1456 passed / 4 failed / 4 skipped（4 个失败全部为改动前即存在：2 个 auth_wall 失败 = 当前环境缺 `mcp` 包（已在 f93031e~1 worktree 对照复现），2 个 `test_instruments_bulk_backfill` = 已知 Windows 临时目录 flake）。**无新增存量回归。**

## 总体结论

**FAIL（5 项 P1 + 8 项 P2 + 一批 P3）**。核心数值路径（日循环八条语义、撮合/费用/止损力学、PIT 卡控、涨跌停推导、统计件公式、append-only 触发器主链）经逐项验证**合格**；但存在 1 项基准策略行为性错误（60/40 再平衡方向反了，直接污染怀疑阶梯）、1 项并发纪律洞（状态机非原子认领）、1 项启动可用性回归风险（worker 启动块无保护）、1 项调度互斥绕过（补跑哨兵绕开单飞锁）、1 项实盘清单数值错误（买入不整手）。

---

## P1（修复后才允许闭合本轮）

### R1-P1-1 rebalance_band 把"低于目标权重"也判为偏离并清仓——60/40 基准在暴跌后割底空仓
- 位置：`src/portfolio/slots/execution.py:144`（`abs(actual - target) > self.band`）；与 backtester.py:460（`exited_symbols` 当日禁回补）叠加
- 事实：underweight（actual < target）同样发**整仓 exit**；MVP 无加仓/部分卖出，被卖标的当日不能回买 → 暴跌年后 60/40 变成"割底 + 全年空仓、永不回补"（子代理合成数据探针实证：2024 年 −40% 后 2025-01-01 清仓、2025 全年 exposure=0）
- 影响：`bench-bond-stock-60-40` 是怀疑阶梯第 2 级；真实 60/40 的定义是"跌了买回"。开发日志发布的 60/40 对比数字（3.32%/-31.0%）被系统性扭曲；`bench-equal-weight-universe` 同受波及（程度轻）
- 修复：exit 仅在 **overweight**（`actual - target > band`）时触发；underweight 显式 no-op（MVP 无加仓/部分卖出语义下唯一正确动作）；docstring/YAML 同步注记；重跑 sample 产物并注记数字变化

### R1-P1-2 实验状态机"先读后写"无原子认领——同一 queued 实验可被 worker 与 CLI 双通道各跑一遍
- 位置：`src/research/lifecycle.py:81-85`（`UPDATE ... WHERE id = ?` 无 `AND status = ?` 守卫）+ `src/research/pipeline.py:26-39`（先读 status 再 transition）
- 事实：`db.connect()` 每次新连接，读与写之间无事务连续性；app 启动 `submit_all_queued()` 与 CLI `--run` 并发时双双通过 status 检查 → 同一实验执行两遍、双 verdict、research_runs 双份
- 修复：`transition()` 的 UPDATE 加 `AND status = ?`（from_status 参数），rowcount==0 时重读并抛 LifecycleError（原子认领）

### R1-P1-3 main.py lifespan 的 research worker 启动块无异常保护——失败拖垮整个 app 启动（存量业务不可用）
- 位置：`src/app/main.py:299-315`（`_ensure()` / `load_reviewed_modules` / `submit_all_queued` / `worker.start()` 裸跑于 yield 之前）
- 事实：任一抛异常（SQLite busy、磁盘满、坏草稿等）→ 异常穿出 lifespan → 应用启动失败；改动前启动段无此依赖，属本次提交引入的 SPOF
- 修复：整块 try/except，失败 `logger.exception` 并降级 `research_worker=None`（与测试分支同形），保证存量路由照常启动

### R1-P1-4 日更补跑哨兵绕过 `_update_job_lock` 单飞锁 + 哨兵单例判断非原子
- 位置：`src/core/jobs.py:156`（`_watch` 直接调 `daily_market_update_job`）+ `jobs.py:138-139`（check-then-act 无锁）
- 事实：存量唯一单飞互斥在 main.py:151-161 闭包内，哨兵绕过它；冻结场景下启动补偿与定时触发可各自超时顺延、各自 spawn 哨兵 → 两路并发全池日更（改动前该并发不可能发生）
- 修复：模块级 `_DAILY_UPDATE_LOCK` 下沉到 jobs.py 由 `daily_market_update_job` 自守（non-blocking，占用时返回 skipped_already_running）；哨兵单例判断改在锁内完成

### R1-P1-5 实盘清单买入数量不整手（quantity 意图路径）——base-v1 默认中招
- 位置：`src/portfolio/live.py:306-308`
- 事实：equal_risk 产 `intent_type="quantity"` 任意浮点股数；回测引擎在 matcher 里 `// lot * lot` 对齐，live 清单路径只 `int()` 截断（子代理探针实证输出 16259 股）——A股买入必须整手，清单给出不可执行数量，对账还产生 qty_mismatch 噪声
- 修复：quantity 分支同样按 lot（profile.lot_size）取整

## P2

### R1-P2-1 live 账户重建对同标的重复 open 的 manual_trades 静默覆盖丢仓
- 位置：`src/portfolio/live.py:78`（循环 `account.positions[symbol] = Position(...)` 覆盖）
- 事实：存量 manual_trades 允许同标的多次 open（db.create_manual_trade 无唯一约束）；重建时现金扣了两笔成本、持仓只剩最后一笔 → 权益/heat/止损监控失真、被覆盖仓位失管
- 修复：按 symbol 聚合（qty 求和、加权成本、entry_date 取最早）

### R1-P2-2 live 止损重建不覆盖 any_of 组合止损——heat 静默 None、组合止损丢失、无 caveat
- 位置：`src/portfolio/live.py:134-164`（`_rebuild_stop_state` 只认 hard_stop/chandelier/ratchet/ma_stop，any_of 落兜底 stop=None）
- 修复：any_of 逐成员重建后取 max(stop_price)；重建不出止损价时清单 caveats 显式标注

### R1-P2-3 重复检测可被"省略有平台缺省值的字段"绕过（window/universe 缺省逃逸）
- 位置：`src/research/experiments.py:144-147`（`_spec_similar`：键集不同即 False=另一个实验）
- 事实：跑过 `window=[2015-01-01,2024-12-31]` 后，重提一个**不带 window 键**的同 diff 实验——非 exact、非 similar，免确认直接入队，实际取数窗口与原实验完全相同（runner 对缺省取 `spec.get("window") or sample 默认`）
- 修复：比较前把两侧 spec 的**平台缺省值展开**（window→sample 窗口；universe→liquidity_default 归一）再做 exact/similar 判定

### R1-P2-4 recompute campaign 用子串匹配选目标实验——模块名互为子串时误伤无关实验并写脏复核 verdict
- 位置：`src/research/recompute.py:38-53`（LIKE + `module_ref in text`）
- 事实：`stop@1` 的 campaign 会命中所有引用 `hard_stop@1`/`ma_stop@1` 的实验；`_rewrite_module_ref` 只精确替换 → 被误伤实验按原 spec 重跑并写入带错误 `recompute_with` 标签的 supersedes 复核 verdict，污染台账
- 修复：解析 spec 后递归收集**精确相等**的模块引用串判定是否真引用；LIKE 仅作初筛

### R1-P2-5 head_to_head 摘要带假 0 换手（DS-P1-3 同类残留）且缺长窗口三注记
- 位置：`src/research/evaluations/head_to_head.py:128-131,147-149`
- 事实：`compute_summary(nav, trades=[], turnover_total=0.0)` 恒 0 假证据（backtest.py 同类问题已修过）；warnings 缺 `long_window_annotations`（§6.6.4 要求对全部评估模块生效）
- 修复：换手按不可用记 None（或 load_fills 实算）；补三注记

### R1-P2-6 决策 C3"非实验路径触碰 holdout 同样留痕"未显式落地
- 位置：`portfolio/service.py` / `backtester.py`（engine_runs 无任何 holdout 触碰标记）
- 事实：实验路径 check_window 已闭环；非实验路径（建设期手动跑、实盘运行器）的触碰只能从 run_params_json.window 间接推断，无显式标记
- 修复：`run_backtest` 落库时在 run_params_json 补 `holdout_touched` 布尔（触碰必留痕的显式化；不拦截）

### R1-P2-7 append-only 守卫缺口：`is_reproduction` 不在 `trg_research_experiments_guard_update` 白名单
- 位置：`src/data/storage/db.py:371-384`（WHEN 子句漏列）vs `db.py:296`（列存在，仅 INSERT 写入）
- 事实：SQL 直改 `is_reproduction` 可增减研究线尝试计数（DSR 输入）而触发器不拦；且触发器是 `CREATE TRIGGER IF NOT EXISTS`——**改触发器体不会传播到存量库**，须 DROP+CREATE
- 修复：WHEN 补 `is_reproduction` 比较；该触发器改为 DROP IF EXISTS + CREATE（幂等重建，存量库同步生效）

### R1-P2-8 冻结门位于 `is_trading_day` 判断之前——节假日白等 30 分钟 + 噪声留痕
- 位置：`src/core/jobs.py:188`（冻结门）先于 `jobs.py:216`（非交易日守卫）
- 修复：`not force` 时先判非交易日再进冻结门（与 R1-P1-4 同函数一并修）

## P3（本轮择修，其余记录在案）

| # | 位置 | 问题 | 处置 |
|---|---|---|---|
| R1-P3-1 | portfolio_risk.py:70-72 | HeatCapGate 死代码（`unstopped = ...` 赋值未用），"unstopped_symbols 落日志"实际未做 | 修：删死代码，gate_log 记入 unstopped 清单 |
| R1-P3-2 | execution.py:86,96-111 | buffered_rotation 的 max_swaps 参数声明未用（恒至多换 1 只） | 修：实现 max_swaps 上限 |
| R1-P3-3 | execution.py:117 + YAML | rebalance_band docstring"先卖后买同日完成再平衡"与 §5.4.2 同标的禁回补矛盾 | 修：随 R1-P1-1 改注记 |
| R1-P3-4 | gateway/live_overlay.py:29 | 拉全量历史只为取最后一根 volume（每日 14:00 白耗 IO） | 修：改最近窗口查询 |
| R1-P3-5 | backtester.py:57-74 | ATR 面板在停牌 NaN 行上：复牌日 TR 退化为 high−low，跨停牌缺口波幅被低估 | 修：_precompute_atr 对 close 先 ffill 再算 TR（不触 core/indicators，连续序列结果不变） |
| R1-P3-6 | reports.py:97 | max_underwater_days 按交易日、max_dd_recovery_days 按日历日——同族指标单位不一致 | 修：统一为交易日（索引位置差） |
| R1-P3-7 | strategy.py 载入路径 | 元模块成员参数只在工厂实例化时校验，"载入即拒绝"弱化一步 | 修：parse/resolve 阶段对 members 做同构 schema 预校验 |
| R1-P3-8 | event.py:132-133 | `spec.get("window", [d])[0]` 对 `window: null` TypeError；primary_horizon 不校验 ∈ horizons（写错静默永远 inconclusive） | 修：统一 `or` 写法 + intake 校验 |
| R1-P3-9 | distribution.py/bucket.py | universe 非法值 / bucket.expect 非法值不做 intake 校验（工程失败污染研究线+DSR 计数） | 修：入口校验 |
| R1-P3-10 | recompute.py | 复核 run 不写 research_runs（血缘断一层）；CLI rerun 无 --run 选项 | 修：补 INSERT + CLI 选项 |
| R1-P3-11 | lifecycle.py:125-139 | list_stale_evaluating 以 started_at（running 起点）为烂尾基准，长 run 误报 | 修：以 engine_run 收口时间 COALESCE |
| R1-P3-12 | stats/paired.py:41-44 | 尾部截断对齐是 API 陷阱（长度不等时按位置错配；当前唯一调用点已按日期 join） | 修：长度不等即 raise |
| R1-P3-13 | stats/fdr_pbo.py | pbo_cscv 的 seed 参数无用（误导可复现性）；奇数 n_blocks 时 IS/OOS 不对称 | 修：删 seed；奇数 raise |
| R1-P3-14 | module_gate.py:113-159 | 前缀稳定性探针 6 位舍入+单切点，与"位级一致"宣称有差距 | 修：容差收紧至 1e-12，docstring 如实标注单切点 |
| R1-P3-15 | conclusion.py:94 | effect==0 在 expect=negative 时被计为方向命中（0 无方向） | 修：0 不计入一致率分子 |
| R1-P3-16 | fdr_pbo.py bh_fdr | adjusted p 封顶 1.0 依赖初值巧合 | 修：显式 min(1.0, ...) |
| R1-P3-17 | bucket.py:194-195 | 特征并列时等频分桶出现空桶 → spread NaN 静默 inconclusive | 修：空桶警告 |
| R1-P3-18 | worker.py:165-166 | 停机竞态下 dispatcher 死于裸 assert | 修：改 if-break |
| R1-P3-19 | live.py:289-301 | 卖出清单缺 T+1（sellable=0）可执行标记；SlotLimitGate 对已持仓 intent 静默丢弃不落 gate_log | 修：清单补 sellable 字段；丢弃记日志 |
| R1-P3-20 | main.py:609-622 | `except ImportError` 裹住整个挂载链——trend_mcp 内部 ImportError（含新增 research_tools 导入）被误报为"mcp 包未安装"，/mcp 静默消失 | 修：先单独探测可选依赖 `mcp`，其余 import 移出 try（内部错误 fail-fast） |
| R1-P3-21 | verdict_rules.py:24 vs backtest.py:356 | docstring 称判定门 ΔSharpe 指"差序列年化 Sharpe"，实现 deltas.delta_sharpe 是序列级差值——口径文档失真（门=序列级>0 ∧ 配对t ∧ DSR_diff 联合） | 修：docstring 重写对齐实现 |
| R1-P3-22 | context.py:90-92 | PanelView.date_at 可越界索引全面板未来日期（仅日期无价格） | 修：clamp 到 upto |

## 待决策点（不在本轮修复，最终报告统一提交用户）

1. **R1-D-1 DSR golden 缺论文 worked example 对拍**：现有 golden 是"同公式独立转写+代数性质"，能抓笔误、抓不出对 Bailey 2014 的共同误读；设计验收判据写明"Bailey 论文 worked example 对拍"。需用户提供论文原例数值（或确认以代数性质+独立复算为验收口径即可）。位置：tests/unit/test_research_stats.py:62-77。
2. **R1-D-2 holdout"每策略线限额"**：架构稿 §2.6 机制 4 的"限额"在后续TODO B5 明列为运行期再评估项（触发：holdout 消耗 ≥3 次后）；自动计数部分当前可由台账（verdicts.holdout_touched / tokens 消耗记录）统计。确认"限额留待 B5"口径，或要求现在实现配置化配额。
3. **R1-D-3 ETF 涨跌幅名称启发式**：`159781 双创50ETF` 实测得 ±10%（名称不含"创业板/科创"）；跨板块双创类 ETF 需权威对照表（数据线/阶段 7 事项）。`.BJ`（北交所 ±30%）当前不在数据池，如未来入库需先补规则。
4. **R1-D-4 test_auth_wall 2 项失败为环境缺 `mcp` 包**：非回归（pre-change 对照复现）。建议开发环境 `pip install mcp`，或接受在无包环境跳过（本轮未加 skip 标记，避免掩盖真实回归信号）。
5. **R1-D-5 rule_backtest/metrics.compute_summary 口径**：Sortino 下行差用负收益子集 std（业界通行全样本 min(0,r) 口径）+ summary 的 win_rate/profit_factor 在 trades=[] 时恒 0。属**存量共享件**，改动会波及旧分支数字——按"存量口径保护"原则不动，仅注记；如需改口径应走存量变更评估。
6. **R1-D-6 mark_interrupted 启动收割与 CLI 在途 run 的多进程边界**：单实例约定有开发日志背书；CLI `--run` 与 app 并存时启动收割可能误判在途 run。维持单实例约定 + 文档注记，或未来下 DB 层（多实例不支持已声明）。
7. **R1-D-7 round trips R 倍数分母硬编码 1.5×ATR**：与持仓实际止损模块无关（首个课题=止损选型时各臂 R 不可比）。现 docstring 已声明"通用分母"；改为实际止损距离属口径变更，留作运行期决定。

## 已验证无问题的方面（抽查与全查结论）

1. **L2 引擎力学**：费用（佣金最低5元/印花税方向/ETF免）、撮合（整手递减、涨跌停卡控、跳空止损按开盘、T+1、止损阻塞次日重试语义、触发先于阻塞判定）、账户（T+1 次日开盘滚动、空仓 1% 计息 golden 252000→10 元、heat None 语义）、不加仓 fail-loud。
2. **日循环八条语义**全部落实且有真牙测试（止损优先测试用可区分价格钉死执行序）。
3. **PIT**：as_of 必填、物理截断、PanelView 限窗、受限句柄逐日重绑、越权拒绝落 audit。
4. **tradability**：除权基准 `/f_t` 三方一致（详设注记+因子语义+实现）、板块幅度、ETF 标的板块跟随、IPO 豁免按交易日历、HALF_UP 到分。
5. **统计件公式**：PSR/DSR/MinTRL 与 Bailey 2012/2014 一致（含非超额峰度、N=1 退化）；配对 t 与 Cornish–Fisher 小样本临界；区块 bootstrap；BH-FDR（单调化+封顶）；CSCV PBO 的 λ 与 <0.5 判据。
6. **append-only 主链**：9 表触发器 + 全部 UPDATE 路径逐一核对仅改白名单列（除 R1-P2-7）；已落定 final_verdict 库层不可改写；runs 表 insert-only。
7. **骨架五条/attempt_index/supersedes 单向指针/课题量化摘要/可降不可升**：与 §6.4/§6.6 一致。
8. **DSL/模块门**：ref 负位移编译期拒绝、AST 白名单、三门全插槽、装载失败隔离。
9. **分层铁律**：L4→L3→L2 执行面全链合规；L2/L3/L4 数据面全走 gateway；`rule_backtest.metrics` 复用有 §5.8 明文豁免（与 C 组代理"建议打捞"的判断相比，以设计文档豁免为准，仅存退役耦合注记）。
10. **存量无回归**：18 新表/触发器不触存量表；connect() 纯别名；迁移对 fresh/老库均安全；冻结门不永久卡死（30min 顺延+2h 补跑哨兵+次日 cron/启动补偿三重兜底）；/research-ledger 在登录墙内；base.html 纯增量导航。
11. **测试不是假测试**：合成数据回归锚钉具体数值、真前视模块被门拒、触发器用真 SQL 试改、对抗性修复有历史注释为证。

## 修复与验收计划

1. 按 P1→P2→择修 P3 顺序修复，每项配最小钉子测试；
2. 修复后全量回归（含新钉子）须全绿（存量 4 个既有失败除外）；
3. 重跑 `scripts/run_base_v1_sample.py` 刷新 60/40 对比产物（R1-P1-1 行为修正后的诚实数字），数字变化记入开发日志附录；
4. 派 ≥2 个独立验收子代理逐项复核本报告问题清单的修复有效性，全部通过后本报告状态改 CLOSED 并提交推送。
