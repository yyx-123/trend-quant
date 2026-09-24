# GLM53F 盲审报告（投研基建一期，2026-09-24）

> 审查人：GLM-5.3-Flash（独立盲审轮）
> 审查对象：当前工作区全部未提交改动——L1.5 `src/gateway/`、L2 `src/engine/`、
> L3 `src/portfolio/`、L4 `src/research/` 四个新包 + `run_freeze`/`research_ledger`
> 路由/MCP 研究工具/CLI 脚本/19 个新测试文件 + 7 个存量文件改动。
> 方案依据：`docs/26-09-20-投研基建架构/` 三份文档 + `2026-09-23-开发日志.md`。
> 盲审声明：**未读取** `review/` 目录下任何既有报告（A/B/C/DS/K3/Deepseek 及其
> 复审），全部结论独立得出；开发日志仅作"待验证主张"对待，每条关键发现均经
> 审查人本人对源码二次核验（标注【亲证】）或经多路独立代理交叉确认（标注【交叉】）。
> 审查方式：本人亲读三层核心代码与详设关键章节（§3/§4/§5.3–5.7/§6.5–6.7/§7–10/附录A）、
> 全部存量 diff；另派 6 路只读深查（方案一致性/引擎网关/投研包/组合层/存量影响/
> 测试完备性）；实证抽查新增测试 174 项（单测 101 + 集成/接口 73）全部通过。
> 全程只读，未改动任何代码。

---

## 0. 总体结论

**判定：FAIL（有条件）——方案落地质量高于常规水准，但存在 4 项 P1 代码级缺陷
与 1 项 P1 级测试缺口，按本项目自身"P1 清零才收口"的惯例，修复并配钉子复审前
不应进入运行期使用。**

先说做到了什么（这部分经逐项实证，不是客套）：

- **分层铁律成立**：L4→L3→L2 执行链、数据面唯一经 L1.5（`BoundGateway` 的
  as_of 绑定/越权拒绝/审计留痕【亲证】）、新栈对 rule_backtest/services 零改动
  （git diff 逐 hunk 核验【亲证】）。
- **账务与撮合语义正确**：费用四件（万 0.854/最低 5 元/印花税分品种/滑点方向）、
  整手、T+1（begin_day 滚动 + 卖出校验 sellable）、止损三态（跳空按开盘/盘中按
  stop 价/不叠滑点/触发先于阻塞）、不加仓 fail-loud、现金链逐笔一致——matcher/
  account/fees 亲读无账务错误【亲证】。
- **统计件公式层无硬伤**：PSR/DSR/MinTRL 与 Bailey 2012/2014 逐项一致（含
  Euler–Mascheroni 项、非超额峰度换算）、区块 bootstrap 环形无越界且种子可复现、
  BH-FDR 单调化正确、CSCV 构造合理【亲证 + golden 15 项含 DSR 绝对锚实证】。
- **纪律机制主路径扎实**：骨架五条入口卡控（含创建型七槽全填/全 none 拒绝/
  params 域前置校验/from 一致性）、append-only 触发器列白名单（18 个触发器全部
  只挂新表，IFNULL 处理正确）、holdout token 原子消费、verdict 可降不可升 +
  库层 final 落定守卫、重复检测两档、判定阈值平台持有（全通道无写入入口）。
- **base_v1.yaml 与附录 A 逐参数一致**；benchmark 阶梯 7+1 与 §5.5 一一对应；
  §5.14 预置模块清单无缺无多（universe 3 + signal 8 + rank 5 + sizing 5 +
  portfolio_risk 6 + position_risk 9 + execution 4 + 元模块）。
- **测试实证**：新增 174 项测试本地全绿；费用/撮合/统计判定器有硬编码数值钉
  （DSR 绝对锚 0.574009265、利息 252000→10 元等），隔离干净（tmp_path + autouse
  重定向，无网络/无生产库触碰）。

但以下 4 项 P1 都属于**"不崩溃、安静地给出错误结果"**的类型——恰恰是这套基建
宣称要防住的失效模式，故判 FAIL 而非 PASS。

---

## 1. P1 发现（4 项代码 + 1 项测试缺口）

### P1-1【亲证】ETF 被套用股票板块涨跌停幅度——假涨停卡买

- 证据：`src/gateway/tradability.py:31-45`——`board_limit_pct` 只识别
  `.SS 68xxxx`（科创）与 `.SZ 30xxxx`（创业板），其余一律 ±10%；函数无
  asset_type 维度，ETF 无任何分支。
- 场景：创业板 ETF（如 **159915**——开发日志 §2 自己举例用的就是它）真实
  涨跌幅 ±20%。某日该 ETF 收盘 +12%（真实板内、未封板）→ 被判
  `is_limit_up=True` → `matcher.match_buy` 返回 `unfilled(limit_up)` →
  系统性假"错过买入"，回测净值路径失真；科创板 ETF（588xxx，±20%）同理。
  池内 201 只 ETF 中所有跟踪创业板/科创板的标的全部受影响。
- 定性：详设 §3.2 的板块幅度表本身只列了**股票**代码前缀（规格缺口），
  实现照抄规格——规格与实现共同缺陷，但后果是确定性的错误成交判定。
  ST ±5% 缺失属同类已文档化限制（unknown 按主板，§3.2 明文），不另立项。
- 建议：`board_limit_pct` 增加 ETF 前缀规则（159/588 等按上市板 ±20%），
  或经 asset_type 分派；配钉子：159915 +12% 日不应判 limit_up。

### P1-2【亲证】元模块 any_of/all_of 不转发 `prepare_with_gateway`——组合信号腿静默零信号

- 证据：`src/portfolio/slots/execution.py:171-226`（AnyOfSignal/AllOfSignal 只有
  `prepare`/`scan`）；`src/portfolio/backtester.py:205-206` 与
  `src/portfolio/live.py:197-199` 均以顶层模块 `hasattr(signal_mod,
  "prepare_with_gateway")` 决定是否接线生产指标。
- 场景：`signal: {module: "signal:any_of@1", members: [macd_cross@1,
  trend_score_cross@1]}`——实例化的是 AnyOfSignal，内部 TrendScoreCross 的
  `_series` 恒为空 → scan 中 `series is None → continue`（signal.py:297-299）→
  该腿永远零信号且**无任何告警**。backtester.py:203 注释宣称的 A-P1-4 接线
  对元模块不成立。研究者会得出"组合后无信号"的假阴性结论入台账——而元模块
  组合恰是 §5.14 的一等公民特性。
- 建议：元模块实现 `prepare_with_gateway` 逐子转发（或 backtester 递归下探）；
  配钉子：any_of 包 trend_score_cross 在合成面板 + 桩 gateway 下产出事件。

### P1-3【亲证】live 止损状态重建违反"T-1 日内路径口径"——实盘清单与回测不同判

- 证据：`src/portfolio/live.py:87-137` `_rebuild_stop_state`——
  `highest = nanmax(high[idx : upto+1])`，`upto = len(panel.dates)-1`，live
  模式面板含**当日 14:00 provisional bar** → 当日盘中冲高回落时，highest
  含当日虚高点；吊灯/ratchet 的 ATR 还冻结在**入场日**（`full = idx`），
  而回测侧 `position_risk.py:130-134` 用 `_atr(-1)` + T-1 highest。
- 场景：10:30 冲高 11.0、14:00 回落 10.4，ATR 0.2、mul 2.5 → live 重建
  stop=10.5，当日 low 10.45 ≤ 10.5 → 清单错误列入"卖出"；同日回测不会触发。
  ratchet 的棘轮位还被当日虚高点永久抬高一档。§5.7"同一策略对象、同一条
  决策代码路径"的承诺在 live 清单路径被打破（hard_stop 不受影响）。
- 建议：重建时 highest/ATR 均取至 T-1（排除最后一根 provisional bar 的
  high 参与 stop 计算；信号判定用当日数据是 14:00 语义允许的，止损价不行）；
  配钉子：同参数下 live 重建价 == 回测第 T 日 evaluate 价。

### P1-4【亲证】复核 verdict 劫持课题量化摘要与分级——未人审的复核稿可翻转课题结论

- 证据：`src/research/conclusion.py:50-57`——课题摘要对 verdicts 按
  `(experiment_id, id)` 排序后 `latest[exp] = dict(v)`（**后 id 覆盖**，不区分
  supersedes）；`src/research/recompute.py:98-118`——复核 verdict id 更大且
  `final_verdict = suggested_verdict` 自动落定（confirmed_by='platform'）。
- 场景：对某已 verdicted 实验跑一次 recompute_campaign 后关题 → 课题的
  verdict 计数/效应量/p 值全部换成**未经人确认的复核稿**，suggested_grade
  （supported/refuted/mixed）可被翻转。这与 `verdict.py:85-96` 的定论语义
  （"supersedes IS NULL 优先，复核稿不劫持台账定论展示位"）直接矛盾，也违背
  recompute.py 自己第 97 行的注释。同一病灶扩散到展示面：`api.py:109-122`、
  `research_ledger.py:75/103/123`、`topic_files.py:63-64` 都取 `verdicts[-1]`。
- 建议：摘要与展示统一走 `latest_verdict`（原生优先）语义；复核稿只经
  supersedes 反查可见；配钉子：recompute 后关题，课题分级仍取原生 verdict。

### P1-5（测试缺口）【亲证】concentration_cap 全仓零有效测试

- 证据：`tests/unit/test_module_behaviors.py:321-334`——四组合约束之一的
  concentration_cap 唯一断言是 `isinstance(out, list)`，注释自认"生效路径在
  集成层（见 backtester 测试）"；全仓 grep 证实**集成层并无此钉**。删掉该
  gate 的拦截逻辑全部测试照绿。
- 建议：补"已有 2 只同类 l2 持仓时第三只被拦"的真断言（单测构造可注入
  类目的 ctx 即可），并复刻真实无参 `instruments()` 调用形态（项目自己的
  教训：钉子必须复刻真实调用形态）。

---

## 2. P2 发现（口径偏差 / 特定场景错误 / 运维风险）

### 2.1 纪律与判定口径

1. **【亲证】confirmed 门与文档三处不符**（`verdict_rules.py`）：
   ① 模块 docstring 说 confirmed 须"差序列区块 bootstrap 带不含 0"，实现只查
   `t_stat ≥ 1.645`，`paired["band"]` 算了没用；② 1.645 是 z 值，n<~60 时 t
   临界值更高，小样本反保守；③ `plateau is None` 直接放行孤峰检查——换模块类
   diff（无数值参数可扰动）拿不到高原证据也能 confirmed；④ 详设 §6.5.1 的
   "换手增幅成本可解释"腿未进规则（Δturnover/fee_total 已在 evidence 里，未
   参与判定）。verdict_rules 的 docstring 引用 §6.5.1 却静默缩水了条件集。
2. **【亲证】课题方向一致率口径与 §6.4.2 相反**（`conclusion.py:89-92`）：实现
   是"与**多数方向**一致的占比"（max(pos, n−pos)/n），详设明文是"与**假设**
   同向的占比"；`spec.expect` 机器可判腿已存在但摘要未消费。场景：4 实验 3 负
   1 正（假设全 positive）→ 实现给 0.75 通过 0.7 阈值，详设口径应给 0.25——
   可能误升 suggested_grade，与决策 20"可降不可升"精神相悖。
3. **【亲证】regime 基准不可用时静默回退 base 策略 NAV**（`evaluations/backtest.py:455-479`）：
   bench300 无数据时 `bench_nav_for_regime or base_nav` 用实验基准策略自身的
   净值算 SMA200 分段——改进型实验的 regime ΔSharpe 变成"按基准策略净值趋势
   分段"的假证据，直接喂给 regime 塌陷检查。虽有 warning，判定仍照跑。
4. **重复检测的类型逃逸**【交叉】（`experiments.py:59-83`）：历史 spec 数值 2.0、
   重提写 "2.0"（或 true↔1）→ 非 exact 且 `_values_similar` 类型不同判不相似 →
   两档检测双双绕过。依赖各模块 params_schema 强类型兜底。
5. **`_pick_unconsumed_token` 会自动消费无绑定的全局 token**【亲证】
   （`pipeline.py:70-80`）：人为某用途发放的全局 token 会被下一个触碰 holdout
   的实验静默吃掉，与发放意图可能不符；token 消费后实验失败不退还。
6. **strategy_line 的 attempt_count 未排除 is_reproduction**（`api.py:220`），
   与 `experiments.py:217-224` 口径不一致——rerun 多次后 AI 读到虚高尝试数，
   而该接口明文是"显著性折扣上下文"。
7. **dict 形态 `to` 过 intake 但 resolve 不支持**（`evaluations/backtest.py:64-100`
   校验端接受 `to: {module, params}`，`strategy.py:204-207` 不支持）→ 白烧
   attempt_index 并落脏 failed 记录【交叉】。

### 2.2 并发、多进程与运维边界（本轮最集中的系统性弱点）

8. **启动清扫无进程归属判定**（`lifecycle.py:164-178`）【交叉】：无条件把所有
   running/evaluating 实验与 `engine_runs`（含 kind='live'）标 failed。双开
   app/reload 会杀死第一个进程的在途 run；重启撞上 14:00 live run 会误杀。
9. **worker dispatcher 循环体无异常守卫**（`worker.py:86-116`）【交叉】：取队首
   时一次瞬时 SQLite busy 异常 → 调度线程带异常死亡且无告警 → 队列永久卡死。
10. **状态机 read-then-write TOCTOU + 非 queued 一律判崩溃**（`worker.py:118-129`
    + `pipeline.py:26-28`）：跨进程双 submit 时，B 会把 A 正在跑的实验标
    failed（`_run_one` 见非终态即 transition(failed)），A 后续转移被拒、留下
    failed+有 runs 无 verdict 的脏账。
11. **【亲证】CLI --run 与 MCP 无 worker 同步路径绕过冻结**（`research_cli.py:110-113`、
    `research_tools.py:49-51`）：`run_experiment` 未包 `frozen_writes()`；且
    冻结本就是进程内计数，开发日志 §1.4"研究 run 统一经 research worker 发起"
    的前提已被后期新增的 CLI/MCP 通道打破，A3 要防的"一次 run 读到两版 qfq"
    在这两条路径不设防。
12. **【亲证】lifespan 启动块无错误隔离**（`main.py:299-317`）：
    `load_reviewed_modules` 逐条 exec reviewed 草稿且无 try/except
    （modules.py:229-257）——一个 reviewed 草稿在依赖漂移后 exec 抛错 =
    **全站起不来**（含看板/手工交易/MCP）。与"新模块过自动测试门即可执行"
    的决策 16 组合后，应用启动可用性被 AI 产物绑定。
13. **【亲证】冻结门超时顺延 = 日更饿一整天**（`jobs.py:146-166` + `scheduler.py`）：
    run 跨 16:30–17:00 → 顺延后"下一次定时触发"是次日 16:30，当日 EOD/除权
    检测/指标重建/看板预热全停一天；启动补偿 force 也要过冻结门，若重启后
    `submit_all_queued` 立即续跑队列，补偿可再次被顺延。顺延应触发当日一次性
    补跑而非等次日 cron。
14. **worker 线程非 daemon，关停阻塞**（`worker.py:69-83`）：`shutdown(wait=False,
    cancel_futures=True)` 停不掉在跑 run，解释器退出 join 非守护线程 → 重启
    要等当前回测自然结束（存量批量回测 worker 是 daemon 随进程死，口径倒退）。
15. **server.py 模块级 import 耦合**（`trend_mcp/server.py:336-338`）：research_tools
    传递依赖新四包；部分回滚缺包时**存量 MCP 整体静默下线**，且 main.py 的
    except ImportError 文案"MCP package not installed"误导排障。

### 2.3 数据与撮合口径

16. **【亲证】tradability 30 日历日垫片**（`tradability.py:78-80`）：窗口起点前
    已停牌 >30 天的标的，复牌日 `prev=NaN` → 涨跌停价 None → 复牌暴涨日照常
    可买（fail-open）。长期停牌复牌恰是 A 股跳空最极端的场景。
17. **主板新股豁免 5 日统一套用**（`tradability.py:35,156-161`）：主板新股上市
    第 2~5 日现实有 ±10% 限制，实现按无限制 → 封板日假成交（详设 §3.2 本身
    未分板块，实现取了最宽近似）。
18. **【亲证】live 对账 price_diff 分母含模型滑点**（`live.py:269/286/379-396`）：
    bench 用 `est_price`（收盘×(1±滑点)）→ 度量的是"实际 vs 模型预估"的残差，
    而架构稿 §2.1/详设 §5.7 定义的对账标定对象是"实际成交价 vs **收盘基准价**"
    （即 slippage_tail 标定样本）。ref_price 字段已在清单里却未用于 diff。
19. **元模块成员参数绕过 schema 校验**【亲证】（`execution.py:160-168`）：
    `spec.factory(sub_params)` 直调，`atr_mul: -5` 顶层被拒、包进 members 静默
    通过 → 反向止损、heat 全错。违反 strategy.py"载入即拒绝"承诺。
20. **market_gate benchmark 不在面板时静默空转**（`portfolio_risk.py:155-169`）：
    面板 symbols 只来自 universe，指数基准取空序列 → 闸门全程不触发、无日志
    →"闸门有没有用"的实验得到假阴性【交叉】。
21. **momentum benchmark 名不副实**（`signal.py:272-276` + `backtester.py:451`）：
    `allows_action` 只 gate 买入，abs_momentum 对跌出 top 的持仓每日发 exit →
    实际是"日退出 + 月进入"混合体，YAML 注释与 §5.5 声称的"月度检查"不符【交叉】。
22. **every_n 是日历日序数取模**（`execution.py:27-31`），docstring/文档说"每 N
    个交易日"——节奏差约 30% 且遇假日漂移（开发日志记了"改日期序数取模"，
    但文档语义未同步）。

---

## 3. P3 择要（边缘 / 文档 / 健壮性，全部记录在案）

- **engine**：`post_day_update` 的 `day_low` 死参（未来模块传了也不生效）；
  `engine.py:145` 缩进残留；`finish_run` 单事务失败 → run 留 running 靠启动
  收割兜底（engine_orders UNIQUE 冲突时整个 run 子记录全丢）；audit flush 失败
  丢留痕且失败请求不留 audit；`ATTRIBUTION_WHITELIST` 常量定义后无消费方
  （parity 测试直接断言三类差异，验收判据部分由测试承担）；panel 窗口首日
  `pre_close=NaN` 不回查窗口外；live_overlay 每 symbol 全历史加载的性能隐患；
  `release()` 的 max(0,·) 钳制会掩盖多余 release。
- **account**：持仓标的无收盘价时 positions_value/heat 静默跳过（与 heat 文档
  "缺失必须显式可见"不一致；L3 已用 ffill 契约兜底）；计息口径"空仓资金"实现
  为全部剩余现金逐日复利（语义两读皆可，建议文档写明）。
- **research**：holdout.py 文件头 docstring"默认关"与代码默认开矛盾；
  `window_touches_holdout` 不防 start>end；psr_sortino 用负收益子集 std 而非
  标准半偏差（已自注近似）；课题 FDR 家族异质（backtest 的 1−psr 展示件 p 与
  event/bucket 经验 p 混族）；bucket 是全事件池化分位而非 docstring 的"信号日
  截面分桶"；CSCV `array_split` 块不等长、全平手时 λ=0 虚增 PBO、bh_fdr 对
  NaN 不设防；walk-forward 恒带 `no_trades` 假警告；`_with_param` 会改同槽全部
  item；SMA200 窗口内预热 → holdout 短窗口大半无 regime；`primary_horizon`
  不校验 ∈ horizons（错写=永远 inconclusive 无提示）；event 用边沿日、bucket
  用 scan 日（valid_days>1 时研究对象不同日）；paired 对齐按尾部截断而非日期
  join（head_to_head 反而是对的——单侧缺日时差序列错位）；recompute 不写
  research_runs（复核取证无 runs 行）；confirm 两段事务第二段失败留 evaluating；
  attempt_index COUNT-then-INSERT 并发可撞号；conclude_topic 的 summary 形参
  可注入自带分级（加固项）；module_gate 只用 `params={}` 跑三门。
- **portfolio/live**：strategy.py 静默丢弃漏 module 的 gate 项；元模块放错槽
  运行期才炸；rank freshness 用自然日距离（待与详设口径核对）；ma_cross 在
  停牌 NaN 日可误发 exit/复牌日误发 entry；buffered_rotation 负分位阈值语义
  未文档化、max_swaps 收了不用；round_trips 的 pnl 不含费用（R 倍数与 NAV
  口径有差，未标注）；live 现金重建不含费用且 closed 缺 sell_price 时现金静默
  蒸发；同日重跑清单后 reconcile 对应旧清单；reconcile 不比 qty；新鲜度闸门
  显式传 as_of 可绕过；垫片 500 自然日 ≈340 交易日 < lookback 上限 600/ma 上限
  400（配置合法但指标永不满足/静默退化）；concentration_cap 用当前类目
  （非 PIT，幸存者偏差同源）；水下天数按交易日、修复天数按自然日（同报告
  两口径）。
- **db/运维**：engine_positions 的 UNIQUE 与普通索引同列同序（最大表写放大）；
  research_cli 无 --db 参数（连生产库）；live_daily_list 的 import 在 try 外
  （该次失败无 job_runs 留痕）；新 POST 端点不在 `/api/` 下绕开 X-Requested-With
  CSRF 线（SameSite=Lax 兜底）；禁后台模式下 `_service_or_testbed` 仍写库；
  topics 物化产物未入 .gitignore；开发日志"14 个触发器"实为 18 个；
  experiments 触发器白名单注释未列 topic_id/is_reproduction（行为自洽）。

---

## 4. 测试完备性评估

**总体：体系可信、局部有伪装覆盖。** 19 个新文件 199 个测试函数，marker 分层
齐全，无空体/无 xfail；mock 使用克制（5 处，多数为合法 I/O 边界替换）；关键
机制钉子覆盖表见下。实测抽查：新增单测 101 项 + 集成/接口 73 项本地全绿。

| 机制 | 钉子 | 评级 |
|---|---|---|
| T+1 滚动 | engine_golden + critical_paths | 强 |
| 涨跌停不可成交 | fees_matcher + engine_parity + portfolio_backtester | 强（但见 P1-1：ETF 幅度本身错，钉子注入 card 测不到推导错） |
| 佣金/印花税/整手 | fees_matcher（精确算术） | 强 |
| append-only 触发器 | research_stage0 + portfolio_config | 中强（18 触发器仅 3 表被测，runs/tokens/drafts/topics/sessions 8 个零覆盖） |
| holdout token 原子性 | review_gaps + stage0 | 中（并发竞态未测） |
| verdict 可降不可升 | stage0 + k3ds + stage5 | 强 |
| 重复检测两档 | stage5 + review_ds | 强 |
| DSR/PSR/FDR/PBO 数值 | research_stats（DSR 绝对锚在 review_gaps） | 强 |
| 冻结门 | critical_paths | 中（job 反应强；**计数器本体零直接测试**，钉子 mock 了 is_frozen） |
| worker 并发上限 | stage5 + review_ds | 中（无 ≤max_workers 直接断言；成功标准含 evaluating） |
| 止损优先于信号 | review_gaps（价格可区分版） | 强 |

主要缺口（按危害排序）：P1-5 concentration_cap；run_freeze 计数器本体；
live_runner 买入对账断言包在 `if target["buys"]` 内（数据漂移即静默消失）；
唯一 slow 钉依赖本地产物、默认跑批恒 skip（阶段 2 验收判据实际靠手工跑
`run_base_v1_sample.py`）；一字板合成行情整日快照无用例（详设 §9 golden 清单
明文要求）；`1403 通过`的计数无法静态复核（静态 def test_ 1382，差额应为
parametrize，未验证）。已知 flake（test_instruments_bulk_backfill）经读码确认
为存量时序敏感问题，与本次改动零交集——开发日志该声明成立。

---

## 5. 质量保障流程评估

**这套"AI 自开发 + 三代理评审 + 外部盲审 + 钉子复审"的流程本身是有效且罕见
的**：评审驱动的修复（PIT 双保险、冻结门、DSL ref 收紧、孤儿收割、配对口径
重写）都有对应代码与钉子可查，不是纸面文章。三点流程级建议：

1. **钉子形态纪律需要硬约束**：DS 轮已总结"钉子必须复刻真实调用形态"，但
   本轮 P1-2（hasattr 探测在顶层实例上做）、P1-5（注释声称集成层有钉实际没有）、
   冻结门 mock 本体，说明该纪律还没有落到"每条修复必有一个复刻真实路径的
   用例"的机械检查。建议把这句写进验收清单并逐条勾选。
2. **规格-实现的偏差要在开发日志留痕**：confirmed 门缩水（换手腿删除、bootstrap
   带未接线）、方向一致率改多数方向、every_n 语义、momentum benchmark 行为、
   heat_cap 告警放行——都是实现期拍板但**未回写方案文档也未在日志声明**的
   口径变化。一期已发生多轮评审仍漏网，说明"偏离必须记录"没有流程卡点。
3. **多进程边界要在设计层面收口**：冻结计数、启动清扫、状态机转移、重复
   检测全部按"单进程"假设建造，而 CLI/MCP/双开都是现实通道。要么文档明令
   单实例运行并加启动锁，要么把这些机制改为 DB 层实现（如冻结表 + 状态机
   条件 UPDATE）。

---

## 6. 修复优先级建议

| 优先级 | 项 | 理由 |
|---|---|---|
| 立即（进运行期前必须） | P1-1 ETF 幅度、P1-2 元模块接线、P1-3 live 重建、P1-4 摘要劫持、P1-5 补钉、P2-11/12（冻结绕过、lifespan 隔离） | 错误结论/错误清单/全站可用性 |
| 随后一批 | P2-1/2/3（判定口径）、P2-8/9/10（并发互杀）、P2-13（日更饿死）、P2-16/17（涨跌停边缘）、P2-18（对账口径）、P2-19（成员参数） | 假阴性实验与运维稳定性 |
| 择期 | 第 3 节 P3 清单 | 逐条已记录，随触碰相应模块时顺手修 |

---

## 7. 已验证一致的关键点（抽查 30 组，符合 27 组）

分层铁律与 BoundGateway（越权拒绝+留痕）；§3.2 除权基准 `/f_t` 方向与
core/adjustment.py 语义自洽；§4.1 六表字段逐字段一致；§4.2 撮合全语义；
§4.3 止损收编口径（entry_price 基准/ATR 含当根/T-1 日内路径）；§4.4 heat 三档；
§5.2/§5.14 模块清单无缺无多；§5.3 配置校验与版本不可变；§5.4.1 日循环四步
（详设伪代码即 rank→sizing→gates，与实现一致）与 §5.4.2 八条语义全部落实；
§5.5 种子与怀疑阶梯一一对应；§5.6 服务面转发（L4→L2 经 L3）；§5.7 live 薄版
（新鲜度闸门/对账/T+1）；§6.1/6.6.1 骨架五条+创建型识别；§6.2.2 三门全插槽+
DSL 免测；§6.3/6.4 状态机+append-only 白名单；§6.6.4 窗口配置与三注记进
verdict warnings；§6.6.5 attempt 排除复现；§6.6.6 两档重复检测；§6.6.7
supersedes 只写新记录；§6.7 沙箱边界与 MCP 不暴露 grant_holdout/recompute；
§8 各阶段验收判据有对应实现/测试；附录 A base_v1 逐参数一致；二期/永不做
清单无越界（无 Kelly/加仓/做空/因子工业化）；存量包零改动（git diff 逐 hunk）。

---

## 8. 审查方法附注

- 本报告所有 P1/P2 关键指控均经本人对源码逐行复核后才收录；子代理报告中的
  两条指控经查证不实或降级：①"ATTRIBUTION_WHITELIST 全仓库零消费"——
  `attribute_diffs` 实际被 parity 测试消费，仅常量本身未被引用（降为 P3）；
  ②"止损基准含费与验收主张冲突"——决策 6"实际买入价"以 fill_price 为准，
  stops.py:9 有明确声明，非缺陷（不予收录）。
- 实证运行：`pytest` 新增单测 7 文件 101 项 + 集成/接口 9 文件 73 项，全部通过
  （17.8s / 117.7s）。未运行全量回归，"1403 通过"以开发日志记载为准并存疑
  （见第 4 节）。
- 本报告为独立盲审产物，供与 review/ 下其他盲审报告交叉比对；未与任何既有
  报告对表。
