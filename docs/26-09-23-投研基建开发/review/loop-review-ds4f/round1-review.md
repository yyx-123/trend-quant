# Round 1 审查报告（loop-review-ds4f）

> **状态：OPEN（待修复 + 子代理验收）**
>
> 日期：2026-09-25
> 审查对象：commit `c986ad1`（投研基建一期全量实现的当前 HEAD，基线 `f93031e`）
> 审查依据：`docs/26-09-20-投研基建架构/`（架构稿 20 条决策 + 总体方案设计 §1–§10 + 附录 A + 后续TODO 分期边界）、`2026-09-23-开发日志.md`
> 审查方式：主审人逐行读 L2 引擎（matcher/fees/engine/account/models/stops）、L1.5 gateway（tradability/panel 关键段）、评估模块与统计件关键段，并**在真实生产库上独立实证**；另派 4 个独立审查代理分面覆盖（L3 portfolio 回测器与插槽 / research 评估与统计 / gateway 与 parity 与存量影响 / 测试完备性与服务面通道）。所有 P1/P2 断言均经主审人逐条复核源码或自行复现后才列入本报告。
> 测试基线（主审人实跑，`pytest tests/ -q`）：**1541 passed / 1 failed**；唯一失败 `tests/test_instruments_bulk_backfill.py::InstrumentAddJobManagerTest::test_rejects_second_add_job_while_running` 为 Windows 临时文件 `PermissionError`，审查代理在 `f93031e^` 基线上对照复现同一失败 → **存量 flake，无新增回归**。
> 与既有审查的关系：`review/` 下已有 A/B/C、DS、K3+DS、GLM53F（7 轮）等多轮审查。本轮**刻意避开其已闭合项**，专攻未覆盖面；下述问题经核对**均未出现在既有任何一份报告中**。

## 总体结论

**FAIL（4 项 P1 + 12 项 P2 + 一批 P3）**。

前序审查对"骨架/纪律/工程安全"覆盖很密，但对**真实市场数据的口径正确性**与**证据链的诚实性**仍有明显缺口。本轮四项 P1 全部属于"数字系统性错误但测试全绿"的类型——其中两项（除权基准、holdout 窗口）会直接改变**已发布的研究结论可信度**。

---

## P1（修复后才允许闭合本轮）

### R1-P1-1 可交易性除权基准价晚一根 bar 生效——274 只标的自 2015 年起 467 天假涨停 + 465 天假跌停

- 位置：`src/gateway/tradability.py:205-208`（因子按"bar 日期 == 因子存储日期"入表）与 `:221`（`base = prev / f_t`）
- 事实（**主审人在 5.5GB 生产库上独立实证**）：本项目因子语义为 `qfq(t) = raw(t) / Π_{ex_date ≥ t} f`（`core/adjustment.py:15` 明文，`bisect_left` 实现）。该语义下"去断裂"要求 `divisor(t-1) = f·divisor(t)`，即**存储日 E 的下一根 bar（E+1）才是价格真正跳水的除权除息日**；E 本身仍是除权前价。对 `factor ≥ 1.15` 的 400 条记录逐条比对 raw 相邻收益：**107/107 无歧义样本的跌幅全部落在 E+1，落在 E 的为 0**（例：`000333.SZ` f=1.5569 存 2016-05-05、raw 实际跳水在 05-06）。因此正确口径是"对 E+1 的基准价除以 f"，实现却对 E 除以 f，**两个方向同时错**：
  - E 日：`limit_up = raw(E-1)/f×1.1`，当日收盘 ≈ `raw(E-1)`（未跳水）→ `f ≥ 1.1` 即**假涨停**，引擎买不进；
  - E+1 日：`limit_down = raw(E)×0.9`，当日收盘 ≈ `raw(E)/f` → `f ≥ 1/0.9` 即**假跌停**，尾盘卖出与盘中止损全部 `unfilled`。
- 量化影响（enabled 池 874 只，`factor ≥ 1.0` 且 ≥2015 年）：**假 `is_limit_up` 467 天 / 假 `is_limit_down` 465 天 / 涉及 274 只标的**。base-v1 的 universe 是 `category_filter(asset_type: all)` = 全 874 只，故这些日子**实际落在已部署策略与 14:00 实盘清单上**。
- 为何历轮审查均绿：`tests/unit/test_gateway.py:212 test_tradability_ex_dividend_base_price` 手搓"跌幅与因子同一天"的合成夹具（raw 10.0→6.6、因子存同日 1.5）并断言由此推出的 7.33/6.00 — **钉子把错误口径钉死了**。
- 修复：因子改为对"存储日的下一根运行 bar"生效（等价表述：对 t 日用"存储日 == 前一 bar 日"的因子）；`core/adjustment.py` 不动（它与 vendor 口径一致，改动会污染全库 qfq）；重写钉子为真实口径 + 真实调用形态（`Gateway.get_tradability`）。

### R1-P1-2 holdout 窗口判定按字符串比较——改个日期格式即可静默绕过 holdout 卡控（AI 通道可达）

- 位置：`src/research/holdout.py:45-50`（`return str(end)[:10] >= holdout_start`）对比 `src/research/evaluations/_common.py:66`（`pd.Timestamp(end)`）与 `src/gateway/panel.py`（同样的 pandas 解析）
- 事实：判定侧把 `end` 当**字符串**比字典序，取数侧把同一个字符串当**时间戳**解析 → 两者对同一输入给出不同语义：
  - `window: ["2024-01-01", "01/01/2026"]` → `"01/01/2026" < "2025-01-01"`（`'0' < '2'`）判为**未触碰**，而 panel 实际取到 2026-01-01；实验跑到 `evaluating`、`n=500`（合法样本 262）、`window_kind='sample'`、`holdout_touched=0` —— **留痕同时说谎**；
  - 同为绕过：`" 2026-01-01"`（前导空格，`' ' < '2'`）、`"\t2026-01-01"`。
- 可达路径：MCP `research_propose_experiment` + 面板型三模块（`event_study` / `bucket_analysis` / `distribution`；三者的 `_spec_errors` 都不校验 `window`）。`portfolio_backtest`/`head_to_head` 走 `date.fromisoformat` 会响亮失败，故不在此列。
- 影响：holdout 是整套研究纪律的核心闸门（决策 C3 + §6.6.4），却可被**一个格式字符串**绕过且不留痕 → 一切"未见过的数据"结论都失去保证。
- 修复：①`_spec_errors` 对 `window` 显式校验（两元素、`YYYY-MM-DD` 正则、start < end）；②`window_touches_holdout` 改为解析后按 `date` 比较，解析失败按 **fail-closed** 抛 `HoldoutError`；③补边界钉子（`end == holdout_start` 必须算触碰）。

### R1-P1-3 bucket_analysis 对读 `ctx.account.positions` 的信号模块直接崩溃 → 实验 failed 且污染 DSR 试验计数

- 位置：`src/research/evaluations/bucket.py:178`（`self.account = None`）对比 `src/research/evaluations/event.py:200/430`（`_EmptyAccount` 桩）
- 事实：`bucket` 的 `_ScanCtx` 把 `account` 置 `None`，而 `event` 提供 `_EmptyAccount`（`positions → {}`）。内置信号模块 `abs_momentum@1`（`src/portfolio/slots/signal.py:276` 一带读持仓）在 bucket 扫描中抛 `AttributeError: 'NoneType' object has no attribute 'positions'`；**同一实验在 event_study 下正常产出 273 个事件**（审查代理实证）。
- 影响：异常在 `pipeline.run_experiment` 落为 `status=failed`；而按 `experiments.py:271-278`，只有 `rejected_intake` 不计入 `attempt_index`，于是**每次崩溃都抬高 DSR 的试验次数 N**，多重检验折扣被虚增——与 DS-P2-4 已修的那类"工程失败污染研究线"同源。
- 修复：bucket 的 `_ScanCtx` 复用 `event._EmptyAccount`（并加一条"读持仓的信号模块在 bucket 下可跑"的钉子）。

### R1-P1-4 组合报告 `turnover` 被除以两次平均权益——恒为真值的 1/avg_equity（≈1e-6），即"假 0 换手"的第三次复发

- 位置：`src/portfolio/reports.py:188`（把 `_turnover(fills, nav_rows)` 作为 `turnover_total=` 传入）与 `:209`（同一函数直接作 `turnover_total` 输出）；被调方 `src/rule_backtest/metrics.py:136`（`turnover = turnover_total / avg_equity`）
- 事实：`reports._turnover` 自身已经 `traded / avg_equity`（返回**比率**），`compute_summary` 约定入参是**成交额**（全仓其他调用点 `research/evaluations/backtest.py:189`、`head_to_head.py:138` 均传货币总额）→ 报告 `summary.turnover` 被再除一次平均权益。审查代理实证：合成 NAV（equity 1e6、成交 21000）→ `report['turnover_total']=0.021` 而 `summary['turnover']=2.1e-08`（比值恰为 1e-6 = 1/avg_equity）；真实 run 上 `turnover_total=10.299` vs `summary.turnover=1.034e-05`。
- 影响：`summary` 是报告与评估链直接消费的面（`research/evaluations/backtest.py` 的 `_nav_summary` 同族），恒零换手是**假证据**——正是 DS-P1-3 / R1-P2-5 已修两次的同一类问题的第三个幸存点。
- 修复：`_turnover` 返回货币成交总额；比率另起键名（`turnover_ratio`）；补"非平凡换手"钉子（断言数值而非仅键存在）。

---

## P2

### R1-P2-1 信号模块的 rolling 指标被停牌缺口污染——缺口后第 n 根 bar 产生幻影交叉信号

- 位置：`src/portfolio/slots/signal.py:132`（ma_cross）、`:176-177`（channel_breakout）、`:218`（high_52w）
- 事实：面板缺失 bar 处为 NaN；`close.rolling(n, min_periods=n).mean()` 只计非 NaN 观测，故 NaN 行**及其后 n-1 行**的均线全为 NaN → `above` 全 False → 第 n 行窗口恢复时 `~prev_above & above` 判为**新金叉**。审查代理端到端实证：同价序列中仅抽掉某标的 t=30 的一根 bar，干净序列 `trades=[]`、缺口序列 `trades=[('2023-03-13','buy',4100)]` —— **停牌后 20 个交易日凭空多出一笔买入**；真实面板（182 只 × 504 日）实测 `ma_cross` 入场 4815 vs 4847（20 只标的差异）、`channel_breakout` 10566 vs 10688。真实内部缺口确实存在（`000333.SZ` 40 天、`300223.SZ` 21 天）。
- 波及：`bench_sma200_timing`（ma_cross）与一切 ma_cross/channel_breakout/high_52w 实验；base-v1（macd，`ewm` 携带 NaN 且不会产生幻影交叉）不受影响 —— 故已发布的 base-v1 数字不变，但**基准对比数字会变**，须重跑并注记。
- 修复：rolling 类指标先按列 `ffill`（与 R1-P3-5 对 ATR 的既有裁决同口径），NaN 掩码只保留用于预热/有效性；补"缺口面板无幻影信号"钉子。

### R1-P2-2 walk_forward 模式对成交派生的三项证据字段伪造"零"，并输出自相矛盾的"零成交"告警

- 位置：`src/research/evaluations/backtest.py:379`（`exp_result = {"run_id": None, ..., "trades": None, "unfilled": []}`）、`:508`、`:640-641`、`:667`
- 事实：`turnover` 已按 DS-P1-3 改为 `None`，但同族三兄弟没跟上：walk_forward 实验证据里 `fee_total=0`（真实是 6 个 fold 合计 265 笔成交 / ≈9823 元费用，须直接查 `engine_fills` 才发现）、`trades=null`、`unfilled_by_reason={}`；更糟的是 `not exp_result["trades"]` 对 `None` 为真 → **告警写着"实验窗口内零成交"而实际做了 6 段交易**。
- 修复：与 turnover 同约定——`fee_total=None`、`unfilled_by_reason=None`、`trades=None` 时不发 `no_trades` 告警。

### R1-P2-3 模块门从不探针 `estimate_stop`——纯前视的仓位风控模块可自动过门变 `reviewed`

- 位置：`src/research/module_gate.py:144-147`（position_risk 探针只调 `init_stop` / `evaluate`）与 `:26`（协议声明里**含** `estimate_stop`）；消费方 `src/portfolio/backtester.py:497`
- 事实：`estimate_stop` 的返回值直接进 sizing（风险预算→股数），是**真实影响持仓金额**的接口，却不在任何探针里。审查代理构造"`estimate_stop` 返回未来 10 日最低价、`init_stop`/`evaluate` 保持因果"的作弊模块 → **`passed=True`，prefix_stability 全过**。另 5 类作弊模块（未来收益信号、全样本波动 sizing、幸存者 universe、长度依赖信号）均被正确拒绝，说明门本身有效，只是**漏了一个入口**。
- 修复：position_risk 探针加入 `estimate_stop` 调用并纳入返回值元组。

### R1-P2-4 `heat_cap` × 无止损价风控模块：告警说"不卡控（放行）"，实现却在拒绝**全部**候选 → 静默零成交 run

- 位置：`src/portfolio/slots/portfolio_risk.py:76-91`（`heat is None` 走放行分支；否则对 `stop is None` 的候选 `continue`）+ `src/portfolio/backtester.py:194-203`（发出的 run warning）
- 事实：空仓时 `Account.heat()` 返回 `0.0` 而非 `None`（`engine/models.py:150` 的空仓路径），故"heat 不可知"的放行分支**根本不会走**；`breakeven`/`time_stop`/`none` 的 `estimate_stop` 返回 `None` → 每个候选都命中 `continue`。审查代理实证：cap 300k、3 标的、4 个月，`position_risk=breakeven → trades=0` 且 `warnings=['heat_cap×breakeven@1: …heat_cap 本 run 不卡控（告警放行）']`、`gate_log=[{'gate':'heat_cap','reason':'no_stop_estimate'}×3]`；对照组 `hard_stop → trades=9`。而"止损选型"正是首个课题族。
- 修复：行为与已裁决的 DS-R2 口径对齐——候选无止损估计时**告警放行**（cap 明确不生效并落 gate_log），保证 run warning 与事实一致；杜绝"零成交且原因隐蔽"。

### R1-P2-5 walk_forward 的高原探针跑全窗口，与 OOS 拼接基线相减 → 邻域数字系统性失真并误判 `peak`

- 位置：`src/research/evaluations/backtest.py:592-611`（探针运行）与 `:618-628`（`selected` 用 OOS 拼接、邻域用它相减）、`:395`（`run_params` 原样传全窗口）
- 事实：审查代理实证 `atr_mul` 邻域在静态模式得 `(-0.211, +0.131)`，在 walk_forward 模式得 `(+0.4923, +0.8347)` —— 差**恒定 +0.7033**（`neighbor_std` 逐位相同 0.24212786092930694），而两次探针 run 的 fills/费用逐位一致（67/46 笔、2345.38/1192.76 元）=**同一批全窗口 run**；`neighbor_mean=0.6635` vs `selected=-0.1733` → 判 `peak`（阻断 confirmed）。即：wsf 模式下高原/孤峰证据是"错基准相减"的产物。
- 修复：walk_forward 下让探针与 select 同基准（探针也按 `window_mode` 跑），或显式禁用高原探测并告警，二选一（本轮取"探针随 window_mode 同基准"）。

### R1-P2-6 两种合法 diff 形态拿不到 §6.5.1 高原证据且无（或错因）告警；`verdict="unknown"` 被当通过

- 位置：`src/research/evaluations/backtest.py:726-740`、`src/research/verdict_rules.py:90-102`（collapse/plateau 判定）与 `:150`（`is_plateau`）
- 事实（审查代理实证两条）：
  1. diff 写成 `to: {module, params}` 字典形态（`apply_diff` 会正常应用，resolved yaml 两边同为 `atr_mul: 2.0`）→ `plateau=None`、0 探针，告警文本却归因为"换模块类 diff"（**错因**）；
  2. 参数取合法零值（如 `slippage_tail: 0`）→ `plateau={'verdict':'unknown','reason':'no neighbors','probes':[]}`、零告警，而 `verdict_rules.py:150` 把 `unknown` 当 `is_plateau=True` → **confirmed 门像被检查过一样通过**。至少 15 个合法零值参数（`tail_session.slippage_base/slippage_tail`、`trend_score_cross.threshold`、`liquidity_filter.min_amount20` …）可命中。
- 修复：`_plateau_items` 兼容 dict 形态；参数值为 0 时也要能枚举邻域（用相对步长而非"真值判断"）；`verdict=="unknown"` 一律不算通过并发告警。

### R1-P2-7 parity 归因过于宽松——只要存在 1 个合法尾盘滑点价差，任意数量的数量/净值发散都被吸收

- 位置：`src/engine/parity.py:248-251`（`qty_bound = max(100, |qty|·0.011·max(i,1)+100)`）与 `:273-286`（`downstream_kind` 把超界净值点一律重分类）
- 事实（审查代理直接调用实证）：①权益 1,000,300 vs 500,000（**+100%** 净值错误）叠加 1 个 +0.03% 价差 → `unexplained == []`，全被判 `tail_slippage`；②120 轮序列末笔数量 +100%（200,000 vs 100,000）叠加 +0.1% 价差 → `unexplained == []`、`tail_slippage=120`。算术上 `qty_bound` 在 `i ≥ 91` 时已 ≥ 整仓量。
- 影响：parity 是阶段 1 的机器验收判据（`tests/integration/test_engine_parity.py:113/141/161` 引用 `attribute_diffs`），过宽 = 新旧引擎的等价性保证被稀释。
- 修复：数量界按持仓量封顶（`min(qty, bound)`）；净值级联归因加上限（超过已分类价差可解释的范围即判 unexplained）。

### R1-P2-8 台账 3 个变更 POST 的 CSRF 防线弱于全站 `/api/*` 口径

- 位置：`src/app/routers/research_ledger.py:27-36,158-214`
- 事实：只挡 `Sec-Fetch-Site: cross-site`；`same-site`（同注册域子域/同主机不同端口）**或不带该头**均放行。审查代理实证：带登录 cookie + `Sec-Fetch-Site: same-site` → confirm 303 通过；不带该头 → `POST /holdout/grant` 303 且 `holdout_tokens` 真的多出 `H0001(granted_by=human-default, purpose='probe grant')`；`cross-site` → 403。这三个端点里 confirm 落定**不可逆**、holdout 发放是**治理动作**。
- 修复：与 `/api/*` 统一为 `Origin`/同源头校验（或要求自定义头）；补 conclude / grant 两处的用例（现状删除其中两处 guard 后测试仍全绿）。

### R1-P2-9 库级 append-only 触发器改写不传播到存量库——`research_verdicts` 的定论保护在真实库上缺失

- 位置：`src/data/storage/db.py:424` 一带（`CREATE TRIGGER IF NOT EXISTS trg_research_verdicts_guard_update`）
- 事实：生产库 `data/trend_quant.db` 的 `sqlite_master` 里该触发器仍是**旧体**（663 字符，无 `OLD.final_verdict IS NOT NULL AND OLD.final_verdict <> NEW.final_verdict` 守卫），而源码 `db.py:438` 已含该子句；实测用旧体可 `UPDATE research_verdicts SET final_verdict='rejected'` 成功改写已定论行。同批加固里 `trg_research_engine_runs_*`/`trg_research_experiments_*` 已改 `DROP IF EXISTS + CREATE`，**唯此一个漏了**；且测试全部建新库，永远看不到定义过期。
- 修复：改为 `DROP TRIGGER IF EXISTS` + `CREATE`；补一条"逐个受保护触发器对比 `sqlite_master.sql` 与源码 DDL"的启动/测试探针，让这类遗漏不能再复发。

### R1-P2-10 实盘清单的当日可交易性标注恒为"停牌"

- 位置：`src/portfolio/live.py:335-337`（只传 `dates=[panel.dates[t_idx]]`）+ `src/gateway/tradability.py:201`（`suspended = is_trading_day & ~has_bar`）
- 事实：14:05 生成清单时当日 EOD bar 尚不存在（16:30 才写），面板当日的合成 bar 并未喂给 tradability → 全部标的 `suspended=True`，涨跌停标记永不置位。审查代理在真实库实证：09-24（无 raw bar）5 只样本全 `suspended=True`，09-23（有 bar）全 `False`。
- 影响：清单是人/电话执行的直接依据，"全部停牌"是明显的误导标注（目前仅标注、不自动卡控，故未升级为 P1）。
- 修复：把面板当日合成 bar 传给 tradability，或至少把当日 `suspended` 显式标为"未知/推断中"而非 True。

### R1-P2-11 bucket 的 `empty_buckets` 告警被丢弃 + 退化场景伪造 `p_value = 0.0`（全族最显著）

- 位置：`src/research/evaluations/bucket.py:250`（append）vs `:293`（`warnings = collect_warnings(...)` **重绑定**丢弃前文）；`:282-284`（`if spread is not None` 但 `spread` 可为 NaN）
- 事实：并列特征导致空桶时，`warnings` 只剩 `survivorship_bias`，`empty_buckets` 文本消失（审查代理实证）；同时 `spread=NaN` 使 `np.abs(random_spreads) >= nan` 全 False → `p_value = 0.0`，而 `conclusion.py:107-110` 把它送进课题内 BH-FDR → 抬高 `still_significant`。
- 修复：`warnings.extend(...)` 而非重绑定；`p_value` 仅在 `spread` 有限时计算，否则 `None`。

### R1-P2-12 bucket 结构性缺失 §6.6.3 要求的重叠/截面聚集/单 regime 三类注记

- 位置：`src/research/evaluations/bucket.py:293`（`collect_warnings(event_count=…)` 未传 `overlap_ratio` / `top_day_share` / `regimes`）
- 事实：§6.6.3 要求五类注记对**全部**评估模块生效；event_study 有（12 个同日事件会告警 `cross_sectional_clustered(top1%日集中 100%)`），bucket 结构性拿不到其中三类（审查代理 A/B 对照实证：bucket 只出 `sample_size_small` + `survivorship_bias`）。
- 修复：复用 event.py 的重叠/集中度/regime 计算后一并传入。

### R1-P2-13 冻结顺延的"当日补跑"哨兵只补 EOD 数据，不跑 post-update pipeline → 该日指标缓存缺失

- 位置：`src/core/jobs.py:151-163`（哨兵直接调 `daily_market_update_job`）对比 `src/app/main.py:228`（`run_post_update_pipeline` 只在 app 的调度入口里）
- 事实：除权检测 + `indicator_daily`/`trend_daily` 重建位于 `main._run_daily_update` 内，哨兵路径**不可达**；其后 `_daily_update_catchup`（`main.py:262-268`）见 `last_ok == today` 也不再补 → 一旦发生 >30min 冻结顺延，当日指标缓存缺失直至次日 16:30（看板 `get_production_indicator` 读到旧值）。
- 修复：把编排下沉为可共享的函数（或让哨兵调同一入口），保证"补跑 = 数据 + pipeline"。

---

## P3（本轮择修，其余记录在案）

| # | 位置 | 问题 | 处置 |
|---|---|---|---|
| R1-P3-1 | `src/portfolio/reports.py:141-167` | `pair_round_trips` docstring 声称内存路径可用，实际要求 `quantity/fill_price/fill_date`，而回测器内存交易是 `qty/price/date` → `KeyError` | 修：兼容两套键名（或改 docstring） |
| R1-P3-2 | `src/portfolio/reports.py:230-239` | `_slot_utilization` 由持仓快照派生，空仓日缺行而非记 0 → 报告利用率序列有缺齿（实测 61 天 vs NAV 110 天） | 修：以 nav 日期轴为准，无快照记 0 |
| R1-P3-3 | `src/portfolio/backtester.py:569` | `r_multiple` 分母硬编码 1.5×ATR，与全项目"按配置止损距离"口径（`rule_backtest/models.py:129`）不一致，跨策略不可比（实测 atr_mul=2.0 时 −0.795 vs 正确 −0.597） | 修：字段改名自描述 + 注记（口径变更留待决策） |
| R1-P3-4 | `src/portfolio/backtester.py:456` | `for symbol in held - member_symbols` 迭代 `set` → 退出/成交记录顺序随 `PYTHONHASHSEED` 变化（实测 seed 0/1/12345 两两不同），数值不受影响但台账不可复现 | 修：`sorted(...)` |
| R1-P3-5 | `src/portfolio/slots/execution.py:38-39` | `_is_first_day_of_period` 把 `panel.upto == 0` 当动作日，使月末/月初门控与窗口起点耦合（窗口首日非月初时月度门在月中触发） | 修：显式注释语义（窗口无预热时的兜底），或改为按周期键判定（留待决策） |
| R1-P3-6 | `src/portfolio/slots/universe.py:93-95` | `liquidity_filter` docstring 承诺 `levels` 参数，schema 里没有 → 按文档写配置会被拒 | 修：docstring 去掉 `levels` |
| R1-P3-7 | `src/portfolio/slots/rank.py:17-21` | `_event_day` docstring 写"交易日距离"，实际算自然日（排序结果等价，仅注释失真） | 修：注释改写 |
| R1-P3-8 | `src/portfolio/backtester.py:12-13` | 模块 docstring 把"同日先卖后买允许"与"边卖边买不允许"并列（同标的时二者是同一件事，实际语义是"跨标的可用释放的资金/槽位、同标的当日禁回补"） | 修：docstring 重写 |
| R1-P3-9 | `src/portfolio/context.py:58-67,79-81` | `series/matrix` 返回可写视图（模块可静默改写面板）；`lookback(0)` 返回整条序列（`[-0:]`） | 修：返回只读副本 + `n==0` 守卫 |
| R1-P3-10 | `src/portfolio/backtester.py:299-314` | 同一 (标的,日) 止损被阻塞 + 信号退出阻塞时落**两条**不可区分的 unfilled → `unfilled_by_reason` 重复计数 | 修：同 (标的,日) 只留首条，或在内存 unfilled 带 `intent.reason` |
| R1-P3-11 | `src/portfolio/backtester.py:371-382` + `src/gateway/panel.py:146` | 全 none 的 `blank-base@1` 直接跑 L3 会以 `PanelRequestError("symbols must be non-empty")` 顶层报错（非领域错误） | 修：抛 `BacktestError` 明确语义 |
| R1-P3-12 | `src/research/dsl.py:6` vs `:58-64` | docstring 自带示例 `(close > sma(close,20)) & (volume > ref(volume,1))` 被自己的门拒绝（`BitAnd` 不在白名单；`and/or` 可用） | 修：白名单加 `BitAnd/BitOr`（与 `and/or` 语义一致） |
| R1-P3-13 | `src/research/modules.py:226-238` | `retire_module` 只改库行，进程内 REGISTRY 仍可被新实验引用，与"下架只禁新引用"宣称不符（须重启才生效） | 修：下架时同步从 REGISTRY 摘除 |
| R1-P3-14 | `src/research/evaluations/event.py:320-333` | regime 分段的 `delta_mean` 减的是**全局**无条件均值而非 regime 匹配基线 → regime 自身漂移被记成事件效应（实测 0.00852 vs 正确 0.00747，符号可翻转） | 修：报告 regime 条件基线（新增字段，不改既有键） |
| R1-P3-15 | `src/research/verdict_rules.py:145-149` | regime 分段短至 5 天也可经 collapse 门否决 confirmed（实测 `below:{n_days:9, delta_sharpe:-2.1855}`） | 修：分段最小样本下限 + 告警 |
| R1-P3-16 | `src/research/evaluations/backtest.py:194` | `_segment_metrics` 死代码 | 修：删 |
| R1-P3-17 | `src/research/evaluations/distribution.py:89-101` | §6.5.4 要求"按类型×周期分组"，实现只有 `by_year` | 修：补类型维度（或改 §6.5.4 措辞——留待决策） |
| R1-P3-18 | `src/research/recompute.py:127` | 复核 campaign 固定 `holdout_token: None` 且 CLI/MCP 无处传 token → 任何触碰 holdout 的历史实验必然 `failed` 且"静默丢目标" | 修：结果显式标 `skipped: holdout_touched` |
| R1-P3-19 | `src/research/modules.py:80,83` | 模块装载/门失败把 `str(exc)[:500]` 原样回给 MCP 客户端（可能泄内部路径/异常细节），与 `_error_payload` 口径不一致 | 修：对齐口径（细节只进日志） |
| R1-P3-20 | `src/gateway/live_overlay.py:36-44` | "前一根 bar"查询用 10 自然日窗口，长假（实测 2023-10-09 前为 11 日缺口）后取不到 → 合成 bar `volume=0`；且 `end=None` 会把当日自己的 bar 当"前一根" | 修：窗口放宽 + `end=as_of` |
| R1-P3-21 | `src/gateway/panel.py:183-203` | live overlay 行只校验 `start_day`、绕过 `end` 边界（`end=2024-03-13, as_of=2024-03-15` 时仍插入 03-15 行） | 修：补 `day > end_day` 跳过 |
| R1-P3-22 | `src/gateway/audit.py:54-67` | flush 失败静默丢弃最多 `capacity` 条审计行并把异常抛进取数热路径（与 docstring "异步不阻塞"矛盾） | 修：异常只记日志并保留待重试 |
| R1-P3-23 | `src/core/jobs.py:333-337` | `live_daily_list_job` 的配置解析在 `try` 外，配置畸形时无 `job_runs` 留痕 | 修：解析移入 try |
| R1-P3-24 | `src/engine/store.py:8-9,126-144` | 持仓快照缓冲是 `持仓数 × 交易日数` 行全内存（注释"每 run ≈2 万行"低估；满仓 800 标的×1250 日 ≈100 万行） | 修：按日/按块增量 flush |
| R1-P3-25 | `src/gateway/tradability.py` 注记 | `.BJ`（北交所 ±30%）当前不在池内；未入库前无影响 | 记录在案（数据线二期） |
| R1-P3-26 | `src/portfolio/live.py:335` | `estimate_stop` 到热卡口的链路在 live 侧未复用（回测侧已接） | 记录在案（运行期再评估） |

## 待决策点（不在本轮修复，最终报告统一提交用户）

1. **R1-D-1 python 模块门的信任模型（原 Q2）**：门与装载是**进程内 `exec`**，审查代理用 `pd.io.common.os.system(...)` 实证拿到任意命令执行且 `passed=True` 自动入注册表 —— 与总体方案 §6.7"AI 无任意代码执行路径"的硬承诺直接冲突，也使"grant_holdout 不上 MCP"的边界形同虚设（拿到代码执行即可自造 token/读 `.env`）。既有审查（B-P1-4）已按"加深 AST 预筛 + 信任模型写入文档"处理过一轮。**两条路**：(a) 真隔离（门/装载进子进程 + 受限环境，python 草稿在人工抽检前不自动入注册表）；(b) 承认 MCP 为完全信任通道，修订 §6.7 措辞并收紧 token 发放范围。本轮先做**低成本缓解**（属性链黑名单：`os/sys/subprocess/ctypes/pickle/shutil/importlib` 等），残余风险请决策。
2. **R1-D-2 除权基准修复后的数字重基线**：R1-P1-1 的修复会改变 enabled 池上 274 只标的 932 个交易日的卡控判定 → 已落库的组合 run / 已发布基准数字需重跑。请确认"重跑 sample 产物并在开发日志注记数字变化"的处置方式。
3. **R1-D-3 R 倍数分母（R1-P3-3）**：维持"1.5×ATR 通用分母"（则字段改名自描述）还是改为"按配置止损距离"（口径变更，需重跑）。
4. **R1-D-4 `_is_first_day_of_period` 的 `upto == 0` 兜底（R1-P3-5）**：窗口无预热数据时的"首个动作日"是刻意的逃生口，还是窗口起点泄漏进动作日集合？
5. **R1-D-5 课内 FDR 的 p 值族构成**：backtest 贡献 `1−PSR`（未配对口径，正是 DS-P1-6 判定为"高相关配对下错误判据"的那个统计量），event 贡献单侧自举 p、bucket 贡献双侧置换 p —— 同一 BH 族混着不同零假设。是刻意（§6.6.5 确写 PSR 应 gate confirmed）还是应统一为可比 p 值？
6. **R1-D-6 bucket 分桶基准**：§6.5.3 说"信号日**截面**按特征分 M 组"，实现用全样本合并分位（单调整变换不污染收益，但时变特征可产出"零截面排序力却单调"的结论，随机打乱对照消不掉这一效应）。是否为刻意简化？
7. **R1-D-7 创建型实验无高原证据**：`backtest.py:581 not is_creation` 使 blank-base 创建型实验跳过高原探测且无注记（Deepseek 盲审曾提出，其中一条腿已修）。是有意豁免还是遗漏？
8. **R1-D-8 `phase_combo_entry@1` / `composite@1` 文档-注册表不一致**：§5.2.2/§5.2.3 正文把它们列为内置件，§5.14（权威清单）与注册表都没有 → 照文档写配置会报"模块未注册"。

## 高价值钉子缺口（本轮一并补，均属"删掉实现 CI 仍绿"）

审查代理用变异测试（复制 `src/` 到临时目录、改副本、用 `-o pythonpath=` 跑同一套测试）实证以下**已修项其实未被钉住**：

| 缺口 | 变异 | 结果 |
|---|---|---|
| holdout 门边界/格式 | `>=` 改 `>`（`end == holdout_start` 被放行） | 44 个用例全绿（= R1-P1-2 的直接成因） |
| verdict 强度序 | `VERDICT_RANK[final] <= VERDICT_RANK[suggested]` 改 `return True` | 5 文件 66 用例全绿（现有"不可升级"用例实际被方向翻转分支挡住） |
| worker 会话并发上限 | `per_session_cap` 判断改 `if False` | 相关钉子仍 PASS（断言数的是调度 tick） |
| conclude 行级守卫 | 删 `AND status='open'` | 40 用例全绿（只验了先到的 `require_open_topic`） |
| module_gate 属性检查 | 删 AST 属性检查 | 27 用例仍绿；且 `_probe` 只跑过 universe/signal 两槽，**rank/sizing/portfolio_risk/position_risk/execution 五槽探针全仓从未执行**（docstring 却称"全插槽三门全跑"） |
| `_slug` 清洗 | 删 `re.sub(r"[\\/:*?\"<>|]")` | 37 用例绿；探针显示清洗器缺席时恶意课题标题可把目录写到 topics root **之外** |
| 台账 holdout 发放 / `report.json` | — | 两个端点**零用例** |
| 未覆盖的关键新路径 | — | `src/gateway/live_overlay.py` **0%**（而 14:00 实盘清单作业正是走它）、`src/trend_mcp/research_tools.py` **39%**（11 个工具体全未执行）、`src/research/dsl.py` **55%**（AI 写 DSL 信号模块这条路径端到端无测）、`_common.py` 默认池 `liquidity_default` 过滤**全仓 0 命中** |

## 已验证无问题的方面（抽查与全查结论）

1. **L2 引擎力学**：费用（佣金最低 5 元、印花税方向、ETF 免）、撮合（整手递减解析式与逐手递减等价、涨跌停卡控、跳空止损按开盘、T+1、触发优先于阻塞判定）、账户（T+1 次日开盘滚动、空仓计息 252000→10 元、heat None 语义）、不加仓 fail-loud——全部与详设 §4.2/§4.4 一致且有真牙钉子。
2. **NAV/现金/费用守恒**：审查代理在 110 天、27 笔成交的真实 run 上逐日核对 `cash_t = prev_cash + interest_t + Σ卖出净额 − Σ买入总额`（<1e-6）与 `equity = cash + Σ qty×close`（全对）；费用单次计入、无重复；无 NAV 重建重复计数。
3. **日循环八条语义**（§5.4.1）：`begin_day` → 止损评估（T-1 状态 + T-1 ATR）→ 信号扫描 → 卖出意图（止损/信号/轮换，按序、按 `exited_today` 去重）→ rank/sizing/gates → 买入 → settle；实证"止损与同日信号退出同时触发"、"止损用盘中价成交"、"被阻塞次日重试"、"卖出释放现金当日可跨标的复用、同标的当日禁回补"。
4. **PIT / 前视**：`PanelView` 全部读取限窗（`date_at` 有 clamp）、逐日重绑 `BoundGateway`（拒绝 `as_of`/`data_version` 越权并落 `gateway_audit`）、ATR 面板为因果 rolling、止损用 `upto-1`、`_round_trips_enriched` 切片到 `e_idx+1`——决策路径无未来数据。
5. **统计件公式**（独立复算）：`Φ` 与 `math.erf` 逐值吻合（<1e-15）、`PSR(SR*=SR̂)=0.5` 精确、分母与 Bailey 2012 式(10) 符号一致、`moments()` 的 std=ddof1/峰度=原始、DSR 的 `sr_var` 与 `N·e` 常数符合 Bailey 2014；BH-FDR 与独立单调化实现 6 组输入（含并列/NaN）逐值一致；CSCV PBO 在构造性占优/翻转样本上精确得 0.0 / 0.943；区块 bootstrap 为正确的环形重采、配对 t 逐位吻合手算。
6. **DSL 无绕过**：`Pow/FloorDiv/Subscript/ListComp/Dict/JoinedStr/IfExp/NamedExpr/Attribute/getattr/ref(x,0-1)/ref(x,-1)/ref(x,1*1)` 全部被拒（唯一缺陷是 R1-P3-12 的 docstring 示例）。
7. **模块门有效性**：40 个可实例化内置件全过；6 类蓄意作弊模块中 5 类被正确拒绝（含"知晓面板长度"的变体）——门不是摆设，只漏 `estimate_stop`（R1-P2-3）。
8. **MCP 权限边界**：`trend_mcp/research_tools.py` 只注册 12 个工具，**`grant_holdout` 未暴露**（严格符合设计硬要求）；AI 会话按 token 派生 `ai-mcp-<user>`（有单测）。⚠️ 但可经 R1-D-1 的代码执行路径绕开。
9. **SQL 注入 / XSS / 登录墙**：新栈 SQL 全参数化（逐条抽查，仅 `alloc_id` 拼表名且调用方为内部常量）；两个研究模板无 `|safe`、autoescape 默认开；`GET /research-ledger` 匿名 303、三个变更 POST 匿名 401。
10. **存量无回归**：逐 hunk 复核 `src/core`、`src/data`、`src/services`、`src/app`、`src/rule_backtest`、`src/trend_mcp`、`web`、`config`、`scripts` 的全部改动——`db.py` 仅新增表/触发器（`load_market_data_many` 重构在真实库上 5 标的 × raw/qfq 逐值相同）、`jobs.py` 仅新增单飞锁/冻结门（行为变更即决策 A3 本身）/新 job、`main.py` 仅状态白名单扩展+可选依赖探测收窄+worker 降级启动、其余为纯增量；`data/backups/trend_quant.db`（7 月、1.1GB、投产前）迁移实测：旧表行数逐一不变、二次实例化幂等。**唯一触发器定义过期问题见 R1-P2-9。**
11. **费率交叉核对**：佣金口径与存量 `rule_backtest` 模型一致；印花税股票 0.05%（存量默认 0.1%）差异**有设计文档 §2.4 背书**，非缺陷（仅因 parity 用 ETF 而不可观测）。

## 修复与验收计划

1. 按 P1 → P2 → 择修 P3 顺序修复，每项配最小钉子测试（并遵守项目既有教训：**钉子必须复刻被替代物的真实调用形态**）；
2. 补齐上表 8 类"实现可删而 CI 仍绿"的钉子；
3. 除权基准修复后重跑 sample 产物，数字变化记入开发日志；
4. 派 ≥2 个独立验收子代理逐项复核本报告问题清单的修复有效性（含变异反证），全部通过后本报告状态改 CLOSED 并提交推送。
