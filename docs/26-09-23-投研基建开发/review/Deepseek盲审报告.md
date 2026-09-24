# Deepseek 盲审报告（投研基建一期 · 阶段 0–5）

> 审查日期：2026-09-24
> 审查对象：工作区当前改动——新增 `src/engine/`、`src/gateway/`、`src/portfolio/`、
> `src/research/`、`src/core/run_freeze.py`、`src/app/routers/research_ledger.py`、
> `src/trend_mcp/research_tools.py`、`scripts/research_cli.py`、
> `scripts/run_base_v1_sample.py`、两个模板；修改 `src/data/storage/db.py`、
> `src/app/main.py`、`src/core/jobs.py`、`src/core/scheduler.py`、
> `src/trend_mcp/server.py`、`web/templates/base.html`、`.gitignore`。
> 比对基准：`docs/26-09-20-投研基建架构/`（架构稿 20 决策 = 约束、总体方案 = 落地蓝本、
> 后续TODO = 分期边界）+ `docs/26-09-23-投研基建开发/2026-09-23-开发日志.md`（交付声称）。
> 审查身份：量化系统研究员 / 量化系统开发工程师 / 资深架构师 / 资深 QA（盲审轮）。
> **只读声明：本次审查未修改任何代码；副作用自查见附录 B。**

---

## 0. 总判定

**FAIL（阻断级 6 项）。** 工程骨架与"力学层"质量显著高于同级项目（费用/整手/T+1/
止损撮合/as-of 卡控/append-only/统计件 golden 都经我逐条复核，见 §7），
**但存在 6 项必须修复的阻断问题**，其中三类性质严重：

1. **规则层静默算错**：涨跌停卡控因"新股无限价窗口"按*运行窗口*而非*上市日*计算，
   在每次回测的前 4 个交易日失效、在实盘清单里**恒失效**（§1.1，已实证）。
2. **纪律层统计口径错**：`confirmed` 判定用"未配对单序列 PSR"做"配对比较"，
   对真实单槽实验（ρ≈0.95–0.99）**几乎不可能给出 confirmed**，同时对低相关场景
   又过度宽松（ρ=0 时假阳率 12.3%）。它决定*每一个实验的结论*（§1.6，已用蒙特卡洛实证）。
3. **"声称已交付但实为未接线/不可达"**：holdout 放行链路断裂（人工放行也跑不了
   样本外实验，§1.2）、`walk_forward` 模式必崩（§1.3）、`context_filter` 被静默忽略
   却在台账产出无条件结论（§1.4）、策略入库门 `promote_to_library` 抛 `NameError`
   （§1.5）。这四项在开发日志里均记为"已交付/已修"。

需要说明的对比：此前的 A/B/C 三代理评审（含 R2/R3 复审）与上一轮 DS 盲审**全部 PASS**。
本轮之所以仍能找出阻断项，是因为这些缺陷**恰好都落在"现有测试无法失败"的路径上**
（§6.2 列出 9 条"删掉也全绿"的关键路径）——这正是盲审轮的价值所在。

---

## 1. 阻断级问题（P1）

### P1-1 涨跌停卡控在回测窗口前 4 个交易日失效、在实盘清单恒失效

**现象**：新股"上市初期无涨跌幅限制"的窗口被按*本次运行传入的日期数组*计算，
而该数组是运行窗口（回测几百天、实盘 1 天），不是该标的的上市后交易日序。

**证据**
- `src/gateway/tradability.py:91-93`：`trading_ordinals` 由**传入的 `dates`** 构造；
- `src/gateway/tradability.py:146-151`：
  ```python
  elapsed = np.searchsorted(trading_ordinals, day_ordinals, side="right") \
          - np.searchsorted(trading_ordinals, listing_day.toordinal(), side="right")
  no_limit = (day_ordinals >= listing_day.toordinal()) & (elapsed < _IPO_NO_LIMIT_DAYS)
  ```
  上市日早于窗口起点时，右侧 `searchsorted` 恒为 0，`elapsed` 退化为"窗口内第几天"；
- 调用形态：`src/portfolio/backtester.py:152-157` 传 `dates=run_dates`（窗口全部交易日）；
  `src/portfolio/live.py:255-256` 传 `dates=[panel.dates[t_idx]]`（**单日**）。

**实测（我本机跑真实函数，无落盘）**
```
老股 600519.SS（上市 2001-08-27），窗口 2024-03-04 起 10 个交易日：
  2024-03-04 no_limit=True  limit_up=nan
  2024-03-05 no_limit=True  limit_up=nan
  2024-03-06 no_limit=True  limit_up=nan
  2024-03-07 no_limit=True  limit_up=nan
  2024-03-08 no_limit=False limit_up=11.0      ← 第 5 个交易日才恢复正常
live 形态（dates = 单日 2024-03-15）：
  no_limit=True  limit_up=None  is_limit_up=False
```

**影响**：`no_limit=True` → `limit_up/down_price=None` → `is_limit_up/is_limit_down=False`
（`tradability.py:155-160`），于是
① 每次回测的前 4 个交易日"涨停可买、跌停可卖"；
② **实盘清单的 `tradability` 字段对全部标的恒为"无涨跌停限制"**（14:00 清单的展示口径错误）；
③ 组合回测的 regime 分段、walk-forward 折、高原补跑都是**独立 run**，每一段的头 4 日都重复这个洞。
现有 3 个 IPO 测试（`tests/unit/test_gateway.py:244-256`、`tests/unit/test_review_gaps.py:91-107`）
全部把 `dates` 起点设在上市日当天或之前，**唯一能命中的生产调用形态没有被覆盖**。

**建议**：以标的自身的上市交易日序（交易日历）计算 `elapsed`，与传入 `dates` 解耦
（或要求调用方传"上市日至今"的完整轴）；补两条钉子：老股 + 窗口首日（回测形态）、
单日 dates（live 形态）。

### P1-2 holdout 放行链路端到端断裂：人工放行后样本外实验必然失败

**现象**：`holdout_token` 的唯一生产者是 `pipeline.run_experiment(..., holdout_token=)`，
而**所有真实调用方都不传**该参数。于是人工在页面上发的 token 永远无人消费，
`enforced=1`（阶段 5 默认开）下任何触碰 holdout 窗口的实验都会 `HoldoutError` → `failed`。

**证据**
- 消费侧：`src/research/holdout.py:99-102`（无 token 且触碰 holdout → 抛错）；
- 唯一传入点：`src/research/pipeline.py:21,35`（`ctx["holdout_token"]`）；
- 真实调用方全部省略：
  `src/research/worker.py:117`、`scripts/research_cli.py:101`、
  `src/trend_mcp/research_tools.py:51`、`src/research/recompute.py:91`（显式 `None`）；
- 发放侧只有 web：`src/app/routers/research_ledger.py`（POST `/holdout/grant`）→ `holdout.grant_token`；
- 失败被吞成 failed：`src/research/pipeline.py:59-61`。

**影响**：§6.6.4 的"人审放行 + 计数 + holdout_touched 留痕"整条机制**不可用**：
token 只能越积越多（`consumed_at IS NULL`），`holdout_touched` 只能靠 failed 文本体现；
开发日志把"holdout 默认生效"列为阶段 5 交付项，实测只"生效"在*拦截*方向，
**放行**方向不存在。这也是 A-R2/R3 未覆盖的路径（他们验证的是拦截与 token 原子性单元件）。

**建议**：`run_experiment` 从 DB 取"该实验的未消费 token"（或由通道显式传递 token_id），
并在失败路径同样写 `holdout_touched`；补一条"人工发 token → 队列运行 → 实验成功且
holdout_touched=1"的集成钉子。

### P1-3 `walk_forward`（样本外滚动）模式必崩，实验直接 failed

**现象**：拼接的 OOS 净值序列用占位标签 `oos-0/1/2…`，随后被 `pd.to_datetime` 解析。

**证据**
- `src/research/evaluations/backtest.py:311-315`：
  `{"date": f"oos-{i}", "equity": float(v)}`（exp 与 base 两串）；
- `src/research/evaluations/backtest.py:458`：`exp_rets = _daily_returns(exp_result["daily_nav"])`；
- `src/portfolio/reports.py:28`：`df["date"] = pd.to_datetime(df["date"])`。

**实测**：`pd.to_datetime` 对该标签 →
`DateParseError: Unknown datetime string format, unable to parse: oos-0, at position 0`。
异常被 `pipeline.py:59-61` 转 `failed`。**零测试覆盖**（`grep -rn walk_forward tests/` 无命中）；
开发日志阶段 5 记"walk_forward 选项（spec.window_mode）"为交付项——不成立。

**建议**：拼接序列保留真实日期（各折日期去重/顺延），或该路径改用
`_safe_daily_rets`（`backtest.py:658` 已存在、不吃日期）；补一条
`window_mode="walk_forward"` 的端到端钉子。

### P1-4 `event_study` 的 `context_filter`（与 `transition_matrix`）被静默忽略

**现象**：模块 docstring 把 `context_filter` 声明为 spec 字段（"事件扫描（可带 regime
条件过滤）"），`_spec_errors` 不拒绝未知键，**runner 从不读取**——实验照常产出
`confirmed/rejected` 与 `per_horizon` 证据，但回答的是**无条件**问题。

**证据**：`src/research/evaluations/event.py:11`（声明）；
`run_event_study`（`event.py:99-135及后续`）无任何 `context_filter` 读取；
全仓 `grep -rn context_filter src/` → 仅 `event.py:11` 文档、`experiments.py:43,94,121` 注释与字段清单。

**影响**：§6.5.2 明确"`context_filter` 是研究问题的定义（样本选择口径）"，
现在台账里会出现"牛市跌破 MA20 后修复概率偏高"这类**从未被检验**的结论，
且与无条件版本无法区分（重复检测也认不出）。与 A-R2 那次阻断（`trend_score_cross`
未接线）属同一类错误，区别只是这次发生在 spec 字段层。

**建议**：二择一——实现 regime 条件过滤与迁移矩阵；或在 `_spec_errors` 里**显式拒绝**
未实现字段（fail-loud），并在文档里标注"未实现"。前者为正式解。

### P1-5 策略入库门 `promote_to_library` 抛 `NameError`，且无任何通道暴露

**证据**
- `src/research/api.py:243`：`spec = loads(exp.get("spec_json"), {})`；
- 全文件无 `loads` 定义/导入（顶部 import 仅 `pathlib` + `research.*`，见 `api.py:8-21`）
  → 通过两道 gate 后必抛 `NameError`；
- 无通道暴露：`grep -rn promote_to_library src/trend_mcp scripts src/app` → 0 命中。

**影响**：架构稿决策 8"唯一的门在策略库入库（必须实验血缘）"目前**既不可执行、
也无人能调用**；`library.add_version` 只被 benchmark 种子（`portfolio/seed.py`）使用，
晋升路径是空的。`tests/integration/test_review_ds.py:354-392` 只断言两条拒绝分支，
成功路径从未执行过。

**建议**：修 import + 补"确认实验 → 晋升 → 库里出现带 `experiment_id/parent_version_id`
血缘的新版本"的钉子测试；随后把它接进服务面/CLI（§6.7 清单本应包含）。

### P1-6 `confirmed` 判定门的统计口径错误（未配对 PSR 冒充配对比较）

**现象**：`verdict_rules` 要求 `PSR ≥ 0.95 且 DSR > 0 且 ΔSharpe > 0 且非孤峰且 regime 无塌陷`
才给 `confirmed`。其中 PSR 的实现是**单序列**检验：把基准策略的*已实现* Sharpe 当作
已知的 `SR*`，用**实验自身的**方差作分母——完全忽略"同一窗口、同一池、只差一个插槽"
带来的配对相关性，也忽略了基准 Sharpe 自身的估计误差。

**证据**
- `src/research/evaluations/backtest.py:476`：`"psr": _psr(sr_hat, sr_base, len(exp_rets), skew, kurt)`；
- `src/research/verdict_rules.py:15-24,69-91`：`min_psr=0.95` 为硬阈值；
- 项目内已有配对工具 `sharpe_block_bootstrap`（`research/stats/bootstrap.py`），此处未用于 ΔSharpe 的推断。

**实测（我本机用仓库自身的 `psr`/`dsr` 跑蒙特卡洛，σ=1%/日、n=2500 日≈10 年、
基准年化 Sharpe 0.8、`attempt_index=4`）**

| 真实改进 ΔSharpe（年化） | ρ=0（独立两臂） | ρ=0.5 | ρ=0.9 | ρ=0.98（单槽实验的现实相关） |
|---|---|---|---|---|
| 0.1 | 17% | 10% | 0% | 0% |
| 0.2 | 23% | 15% | 2% | **0%** |
| 0.3 | 32% | 22% | 6% | 0% |
| 0.5 | 48% | 45% | 45% | 34% |

（另一侧：真实 ΔSharpe = 0 时，ρ=0 的假阳率 **12.3%**、ρ=0.5 为 5.5%——名义阈值 5% 被突破。）

**影响**：改进型实验（平台强制"单槽 diff"，`experiments.py` 的 compound 校验）天然是
高相关配对，于是**真实的 0.2–0.3 个年化 Sharpe 改进也几乎拿不到 `confirmed`**，
全部落入 `inconclusive`；同时低相关场景（如 `head_to_head` 之外的自建对照）又过度宽松。
这直接决定：第一个 AI 实验闭环的结论分布、课题分级（§6.4.2 的 `supported/refuted`
实际会大面积退化为 `insufficient-evidence`）、以及"策略是否值得晋升"。
它不会被任何现有测试发现——`tests/integration/test_research_stage5.py:87-94` 的辅助函数
**跟随平台建议**（`final_verdict = final if suggested=="confirmed" else suggested`），
所以判定器返回什么都绿。

**建议**：改用配对口径（ΔSharpe 的配对标准误 / 配对区块 bootstrap / 直接对日收益差做检验），
保留 PSR 仅作参考展示；阈值入配置（§6.10.3 本就如此要求）；补一条
"合成数据上真实 ΔSharpe=0.2 能到 confirmed、ΔSharpe=0 不越 5%"的钉子测试。

---

## 2. 重要缺陷（P2）

### P2-1 引擎允许对同一标的重复买入，静默丢弃前一持仓（资金少记、净值失真）

- `src/engine/account.py:33`：`account.positions[fill.symbol] = position`（无条件覆盖）；
- `src/engine/engine.py:75-86`：`buy` 路径无"已有持仓"守卫；对照 `sell` 有守卫（`engine.py:108-109`）。
- **实测**：同日同标的两次各买 1000 股 @10.00 → 现金 100000→89995→79990，
  持仓仍为 1000 股（应为 2000），权益静默少记 ≈1 万元，无异常、无 `engine_unfilled` 行。
- 可达性：`static_list` 配置里写重符号（`universe.py:44` 不去重）即可；
  §5.4.2"不加仓（每个持仓一笔买入）"目前只靠上游自觉。
- **建议**：`apply_buy_fill` 对已有持仓 fail-loud（或按 §5.4.2 记 `unfilled` + 新枚举），
  并补钉子；加仓是二期项，现在必须是"报错"而不是"覆盖"。

### P2-2 停牌日的尾盘离场（信号/时间止损）让整个 run 崩溃，而不是记 `unfilled(suspended)`

- `src/engine/engine.py:120-122`：`if bar_close is None: raise ValueError("tail intent requires bar_close")`
  **先于** `matcher.match_tail_sell` 执行，使 `matcher.py:151-152` 的停牌分支**不可达**；
- 调用方：`src/portfolio/backtester.py:260` 传 `bar_close=_field_at(...)`，
  非有限值返回 `None`（`backtester.py:346-351`），停牌日正是这种情形；
  发射方可以是 `time_stop`（不需当日 bar）与 `ma_stop`/信号 exit。
- **实测**：`suspended=True, bar_close=None` 的 tail 卖出 → `ValueError: tail intent requires bar_close`；
  该异常走 `backtester.py:295-300` → `finish_run(failed)` → 实验失败。
- 与 §4.2"③ 触发日停牌/无 bar → unfilled(suspended)"直接冲突。
- **建议**：把 `bar_close is None` 下移到 matcher 内（或按 `card.suspended` 先记 unfilled）；
  补"持仓停牌日触发时间止损"的钉子。

### P2-3 recompute campaign：未接任何通道，且一旦接线会抹掉已确认结论

- 未接线：`grep -rn recompute_campaign src/ scripts/` → 仅 `src/research/recompute.py` 与测试；
  §6.6.7"平台可发起重跑运动"无入口（服务面/MCP/CLI/Web 均无）。
- 潜在破坏：`src/research/recompute.py:96-107` 对**已是 `verdicted`** 的实验追加一条
  `final_verdict` 为 NULL 的新 verdict；`verdict.latest_verdict` 取 `ORDER BY id DESC`
  （`verdict.py:88-90`）→ 台账 `final_verdict` 变 None、REPORT.md 变空；
  而 `confirm_verdict` 要求 `status=="evaluating"`（`verdict.py:122-125`），
  `lifecycle._TRANSITIONS` 无 `verdicted → evaluating`，该新 verdict **永远无法确认**。
- 测试只断言 `supersedes` 被写入（`tests/integration/test_research_stage5.py:336-360`）。
- **建议**：复核对已终态实验应写"复核 verdict"并保持原 verdict 为 latest（或引入
  `superseded` 视图语义，§6.6.7 原文即"新记录带指针、旧行不改，取代关系反向查询"），
  同时给台账/看板一个"复核后结论"的展示位；接线时补一条"原 confirmed 结论不被抹掉"的钉子。

### P2-4 入口（§6.6.1）未校验"参数域内"与 `diff.from`，"非法实验"先入账再失败

- `src/research/experiments.py:194-215`：只做标题/假设/模块注册/spec 结构 + `from` 是否**相等**的校验；
  参数 schema 校验只发生在运行时 `resolve_experiment_config`（`src/portfolio/strategy.py:111`）。
- 因此 `params={"atr_mul": -5}` 或 `params={"zzz":1}` 能**创建成功**（占一个 `attempt_index`），
  跑起来才 `failed`——污染台账失败记录与 DSR 的试验计数（§6.6.5 的 N）。
- `diff.from` 不参与计算（`strategy.py:176` 注释："`from` 字段是信息性的"），
  写错来源也能创建，台账里的"单变量归因"可能是假的。
- **建议**：入口调用 `registry.validate_params`（或 `parse_strategy_config` 的校验段）；
  `from` 与 base 实际模块不符 → 拒绝（或至少在 spec 中保留 from 并做一致性检查）。

### P2-5 §6.6.4 的"长窗口三个配套注记"没有进入实验路径

- 架构稿/详设要求"**必须带**"：①覆盖率警告 ②费用时代错配 ③幸存者偏差加权（按段递增）。
- 全仓 `grep -rn "覆盖率|费用时代" src/research/` → 0 命中；实验 verdict 的 warnings 只有通用
  `survivorship_bias`（`backtest.py:553` 等）。
- 三条注记目前只出现在 **一次性脚本产物** `data/research/base_v1_sample/comparison.md`
  （且 `data/research/` 被 `.gitignore` 忽略，不入版本库）。
- **建议**：把三条注记做成 `collect_warnings` 的窗口长度参数化产物（2015 起算自动带），
  进 verdict 的 `warnings_json`。

### P2-6 运行期三处风险：冻结门被启动补偿绕过、日更静默顺延、进程被杀留永久 `running`

1. **冻结门（决策 A3）被 `force=True` 绕过**：`src/core/jobs.py:143` `if not force and run_freeze.is_frozen()`；
   启动补偿线程走 `src/app/main.py:278` `update_job(force=True)`（函数体 `:238`），
   而 research worker 在 `main.py:308-309` 才创建——**开机瞬间的补数写入与新栈取数（`gateway` 读
   `trend_daily`/`indicator_daily`/`market_data_qfq`）可能并发**，正是 A3 要消灭的"一次 run 读到
   两版数据"。进程内计数器也意味着独立 CLI 跑批不会被 app 的调度器感知（开发日志已承认边界，
   但 `scripts/research_cli.py` 与 `run_base_v1_sample.py` 默认连**生产库**）。
2. **日更被顺延后静默跳过**：`jobs.py:147-158` 超时 30 分钟即 `return {"status":"deferred_backtest_running"}`，
   且在 `record_job_run_safely` 之前返回——当天日更/指标重建无 `job_runs` 记录、无哨兵、无进程内重试；
   `main.py:213-216` 对该状态直接 `return`。高原补跑 + walk-forward + PBO 变体使一个实验可含
   多次引擎 run，超 30 分钟并非不可能。
3. **进程被杀留下永久非终态**：`research_experiments.status='running'` / `engine_runs.status='running'`
   没有启动补偿（对照存量：`main.py:104` `mark_interrupted_batch_runs()`、
   `main.py:110-112` instrument jobs 的 `mark_interrupted_at_startup`）；worker 启动只补 `queued`
   （`worker.py:54-59`）。后果：`lifecycle.list_stale_evaluating` 只看 `evaluating`（`lifecycle.py:130-139`）→
   该实验不进烂尾清单；`topics.conclude_topic` 因"存在非终态实验"（`topics.py:124-126`）
   **永远无法关题**（只能手改 SQL）。

**建议**：`force=True` 仅在无活跃 run 时放行（或补偿路径也过冻结门）；顺延时写 `job_runs`
与哨兵并在超时后安排进程内重试；启动作业加 `mark_interrupted_research_runs()`（比照存量约定）。

### P2-7 测试无法失败的关键路径（9 条）

以下路径**删掉实现也全绿**（我已用 grep/pytest 复核；与开发日志"逐条有测试钉住"的表述不符）：

| # | 路径 | 证据 |
|---|---|---|
| 1 | `run_freeze` 与 A3 冻结门 | `grep -rn "run_freeze\|deferred_backtest_running" tests/` → 0 命中 |
| 2 | `trend_score_cross`（A-R2 的阻断项本体） | `grep -rn trend_score_cross tests/` → 0 命中 |
| 3 | plateau 补跑 + `verdict_rules`（判定规则全集） | `grep -rn "plateau\|suggest_backtest" tests/` → 0 命中 |
| 4 | PBO 接线（`except Exception: pbo_info=None` 吞错，`backtest.py:543`） | 无断言 |
| 5 | `walk_forward`（P1-3） | 无测试 |
| 6 | MCP 11 个研究工具（含 AI 越权面） | `grep -rn research_tools tests/` → 0 命中 |
| 7 | CLI 通道（`research_cli.py`） | 0 命中 |
| 8 | 阶段 2 验收"v1 vs benchmark 同图自动产出" | 无 slow 测试；唯一证据是 gitignored 产物 |
| 9 | live 账户重建的 T+1（`live.py:75-77`） | 现有测试用"昨日买入"，删掉该行不失败 |

### P2-8 "钉子"假阳性（回归锚本身是重言式）

- `tests/integration/test_review_ds.py:114`：`assert abs(deltas["delta_turnover"]) >= 0`
  —— `abs(x) >= 0` 恒真，**包括它声称已修的"恒 0 假证据"**；
- `tests/integration/test_review_ds.py:143`：`assert isinstance(fr["gate_rejections"], dict)`
  —— `{}` 也通过，注释却称"是真数据"；
- `tests/integration/test_research_stage5.py:473`：`assert v["suggested_verdict"] in ("confirmed","rejected","inconclusive")`
  —— 与 `insert_platform_verdict` 的入参枚举（`verdict.py:39-40`）同义，属重言式；
- `tests/integration/test_research_evaluations.py:169`：裸 `with pytest.raises(Exception):`
  （开发日志称"4 处裸 raises 全部改型"——未完成）。

### P2-9 parity（阶段 1 验收）的归因判据未实现，且存在 1 根 bar 的边界错位

- `src/engine/parity.py:136-137` 声明"归因类别即阶段 1 验收白名单：
  limit_card / t_plus / tail_slippage / cash_interest"，但代码只会产出
  `count_mismatch` / `trade_mismatch`（`parity.py:156,164`）
  → §8"差异只允许来自三类、超纲即测试失败"**不可机器验证**（现有 parity 测试只断言
  零差异 + 尾部滑点比值，`tests/integration/test_engine_parity.py:101-146`）；
- 对齐是**位置对齐**（`parity.py:151-164`），多/少一笔即连锁误报；
- `nav_divergence` 用 `zip`（`parity.py:169`），序列长度不一致会被静默截断；
- 预热边界：新栈 `golden = i >= 1 and i >= warmup_bars-1`（`warmup_bars=35`）→ 从 **i=34** 放行；
  旧栈 `idx+1 < min_rows`（`rule_backtest/value_resolver.py:185-189`）且 cross 的昨日值在
  `bars.iloc[:-1]` 上解析（`rule_backtest/condition_engine.py:57-63`）→ 最早 **i=35**。
  现有测试数据首个交叉落在 i=37，靠运气通过（`test_engine_parity.py:64-79`）。

---

## 3. 次要问题（P3）

1. **verdict 的判定字段可被 SQL 改写**：`trg_research_verdicts_guard_update` 的白名单
   未含 `final_verdict`/`reasoning`（`db.py:395-408`），一次 `UPDATE` 即可改写历史判定，
   与 §2.6"历史永不改写"有张力（代码层有 `final_verdict is not None → 拒绝`，但库层无守卫）。
   建议加 `WHEN ... OR OLD.final_verdict IS NOT NULL AND OLD.final_verdict <> NEW.final_verdict`。
2. **判定阈值未入配置**：`src/research/verdict_rules.py:15-24` 硬编码；§6.10.3 要求"阈值入配置"。
3. **课题量化摘要口径**（§6.4.2）：`conclusion.py:85-88` 的 `direction_consistency` 是
   "多数占比"（构造上恒 ≥0.5），不是"与假设同向的占比"（方向在 `spec.expect` 里，且 backtest 无 expect）；
   `_effect_size`（`conclusion.py:25-36`）把 ΔSharpe、Δ均值(%)、q_spread 混在一个中位数里 → 该数字不可解释。
4. **分层越层**：`src/research/evaluations/backtest.py:585` `from engine.store import EngineStore`（L4→L2，
   注释误标"L3 包内合法"）；`src/research/module_gate.py:69,127,137` L4→L2 类型引用。
   （`rule_backtest.metrics` 的复用有 §5.8"复用清单"明文授权，但与 §1"新代码不得从研究栈反向依赖"冲突，
   建议裁决后统一：或把 `compute_summary` 上移 `core/`。）
5. **AI 通道权限边界**：`src/trend_mcp/research_tools.py:148-195` 的 `confirm_verdict`/`conclude_topic`
   以共享 AI 会话执行；§6.7"写按 owner_session 隔离"未实现（任意会话可操作任意实验）。
   表面上不影响降级约束，但与"AI 沙箱边界=服务面"的叙述不一致，建议在文档中裁决或实现归属校验。
6. **报告完整性**：`src/research/topic_files.py:102` 把 evidence JSON 截断到 6000 字符且**无截断标记**，
   与 §6.5.0"一个都不许摘要化"冲突（`report.json` 完整，`REPORT.md` 不完整）。
7. **plateau 对创建型实验整个跳过**：`backtest.py:497` `if plateau_items and not is_creation`，
   且 `verdict_rules.py:86` 把 `plateau is None` 当作通过 → 创建型实验的过拟合探针缺失
   （§6.1.4 豁免的是 compound 降档，不是 §6.5.1）。
8. **`plateau_neighbors` 的 ±2 分支**可把选定值本身算作邻居（`verdict_rules.py:32-35`，如 `per_l2=1` →
   `{1,3}`），污染"邻域同向/1σ"统计。
9. **regime 拆分静默丢样本**：`research/evaluations/_common.py:57` 只垫 120 自然日（≈82 交易日），
   而 `regime_labels` 需 SMA200（`_common.py:152` `min_periods=200`）→ 每个窗口前 ≈118 个交易日
   标为 `unknown` 被排除（无警告、无计数）；且基准硬编码 `510500.SS`（`_common.py:146`），
   池内无该标的时全部 unknown、`single_regime` 警告永不触发。
10. **`heat_cap` 在 heat 不可知时"告警放行"**（`portfolio_risk.py:64-69` 返回原清单）：与 run 警告文案
    一致（有意的取舍，DS-P2 的修法），但意味着 `time_stop`/`breakeven`/`none` 配置下 heat_cap 静默失效；
    建议在 verdict 汇总里显式转为"未卡控"标记（目前只进 gate_log/warnings 文本）。
11. **`research/topics` 默认写 CWD 相对路径**（`api.py:23`），`main.py` 未传 `topics_dir`
    → 运行期在仓库工作树内生成文件（本次审查即在工作区留下了 `research/topics/T001_probe/`，见附录 B），
    且未被 `.gitignore` 覆盖。
12. **实盘清单口径混乱**：`jobs.py:231` 文档与 `LIVE_AS_OF_HOUR` 说 14:00、调度器注册 14:05
    （`scheduler.py:113-123`）；三个 `app_config` 键（`portfolio.live_strategy_version_id` 等）**无任何写入通道**
    → 清单任务实际处于休眠态（安全性上算优点，但"已交付"的表述应修正）。
13. **脚本默认连生产库**：`scripts/research_cli.py:26-31`（无 `--db`）、
    `scripts/run_base_v1_sample.py:73-79`；`run_base_v1_sample.py` 还会写入 9 条策略 + 4 次引擎 run。
    建议加 `--db` 与"生产库需显式确认"。
14. `is_reproduction` 仅存在于迁移路径（`db.py:1068-1071,1097`），不在 `_RESEARCH_STACK_DDL`
    的 `CREATE TABLE` 里——单独用 DDL 建库会让 `rerun_experiment` 报 `no such column`
    （当前 `__init__` 顺序使其成立，属脆弱耦合）。
15. `engine_positions` 无 `UNIQUE(run_id,date,symbol)`（对照 `engine_daily_nav` 有），
    重复 `finish_run` 会静默产生重复快照。

---

## 4. 方案一致性核账（声称 vs 实况）

| 方案要求 | 开发日志声称 | 实况（本报告证据） |
|---|---|---|
| 阶段 5 walk_forward | 已交付 | **必崩**（P1-3） |
| 阶段 5 recompute campaign | 已交付 | 实现存在但**无任何入口**；接线即抹结论（P2-3） |
| §6.6.4 holdout 放行 | "holdout 默认生效" | 放行链路**断裂**（P1-2）；仅拦截侧生效 |
| §6.5.2 `context_filter` | 未提及 | 声明即忽略（P1-4） |
| 决策 8 策略入库门 | 阶段 0/5 相关 | `NameError` + 无通道（P1-5） |
| §6.5.1/§6.6.5 判定规则 | "判定建议规则平台持有" | 规则存在，但**PSR 口径错**（P1-6）；阈值未入配置 |
| §6.6.1 入口卡控五条 | "骨架校验五条全在入口" | 第 4 条"参数域内"缺失；`from` 不校验（P2-4） |
| §6.6.4 长窗口三注记 | "三注记"（脚本产物内有） | 未进实验路径（P2-5） |
| §8 阶段 1 差异报告归因 | "差异只允许三类" | 白名单未实现（P2-9） |
| §6.7 权限：写按 owner_session 隔离 | 未提及 | 未实现（P3-5） |
| §5.12.3-F/G（walk-forward、MC） | MC 已接（`trade_bootstrap_bands`） | MC 已接；walk-forward 不可用 |
| 决策 A3 冻结 | "调度器顺延" | 启动补偿绕过 + 超时静默（P2-6） |
| 一期边界（不做二期项） | 声明遵守 | 复核通过：无因子挖掘/加仓/meta-portfolio/做空等实现 |
| 存量零改动 | "纯新增、对存量表零改动" | 复核通过（见 §5） |

---

## 5. 对存量业务的影响

**结论：无破坏性变更；存量风险集中在运行期行为（P2-6）与新表用词。**

- `src/data/storage/db.py` 的 diff **只有** `CREATE TABLE IF NOT EXISTS`（18 张新表）、
  `CREATE TRIGGER`（18 个，含正文所述 14 个 append-only）、新列的迁移（仅针对新表
  `research_experiments.is_reproduction`）与注释；**没有** 对存量表的 DROP/ALTER/DELETE/UPDATE，
  没有新外键指向存量表（FK 目标全是新表）。
- `load_market_data_many` 改为委托新的窗口批量读（`db.py:2168-2181`），参数表保持不变，
  行为等价（`start/end=None` → 全历史）。
- `src/app/main.py`：只新增 lifespan 内的（a）`live` 清单任务挂载、（b）research worker 启停、
  （c）日更冻结门；存量补偿逻辑（`mark_interrupted_batch_runs`、instrument jobs）未改。
- `src/core/jobs.py`：日更新增冻结门，其余流程（非交易日跳过、`job_runs`、哨兵）不变；
  但冻结门的**静默顺延**是新增的存量风险（当天 K 线/指标不更新且无记录，最多 24h，`P2-6`）。
- `src/core/scheduler.py`：新增一个 `add_job`（14:05 清单），未触碰既有任务；
  `TREND_QUANT_DISABLE_SCHEDULER=1` 时整段不启动（测试隔离依赖它）。
- `src/trend_mcp/server.py`：仅在文件尾追加 11 个 `research_*` 工具注册，未改任何既有工具、
  鉴权路径或 `_token_user`；`grant_holdout` 确认**未**暴露（已枚举）。存量 MCP 面完好。
- `web/templates/base.html`：单行导航新增；模板全程无 `|safe`，Jinja 自动转义生效，
  台账页/详情页的 AI 文本无 XSS 面。
- **生产库现状（只读查询）**：`engine_*`、`gateway_audit`、`research_*`、`module_drafts`、
  `holdout_tokens` 全为 0 行；`portfolio_strategies`=9、`portfolio_strategy_versions`=11（种子）。
  与开发日志"开发期产物清空、种子保留"一致；**尚无任何真实实验记录**。
- 测试隔离：新增测试全部走 `tests/conftest.py` 的 tmp 库与 `tests/api/conftest.py` 的
  `isolate_api_db`；我实测关键单元 105 项 + 集成/API 51 项，`data/trend_quant.db` 未增行。

---

## 6. 测试与质量保障评估

**规模与结果（我本机实跑）**：新增 15 个测试文件共 **156 项全绿**
（unit 105 项 14.2s；integration/api 51 项 88.0s；`-p no:cacheprovider`）。
存量全量套件（由审查子代理在我未复核的时点运行）报 1402 通过 / 2 失败，
两个失败均为 `tests/test_instruments_bulk_backfill.py` 的 Windows 临时目录清理竞态
（`PermissionError: [WinError 32]`），与本次改动无关（该文件未被触碰）——与开发日志
"1401 通过 / 1 失败"的计数不一致（flaky 计数不稳），属记录精度问题。

**优点（值得保留）**：费用/整手/现金递减/涨跌停/停牌/跳空止损/T+1 有手算金值；
`as_of` 与受限句柄越权有测试；append-only 触发器有 UPDATE/DELETE 拒绝测试；
DSR 有**独立绝对锚** `0.5740092653864478`；DSL 编译期拒绝负位移；前视探针能抓出
全历史 max 模块；测试全部使用隔离库、无仓库写入。

**问题（已在 P1/P2-7/P2-8 展开）**：
1. **关键路径无测试**（9 条，P2-7）——最讽刺的是 `trend_score_cross`：它是上一轮评审的阻断项，
   修完仍无任何测试；同样的模式今天在 `context_filter`（P1-4）上重演。
2. **表面覆盖 ≠ 有效断言**：`abs(x) >= 0`、`isinstance(x, dict)`、枚举重言式、
   跟随平台建议的"自适应"断言（`test_research_stage5.py:87-94`）让判定器任何输出都通过。
3. **统计件的 golden 是"公式复述"**：`tests/unit/test_research_stats.py:23-24` 的"参考实现"
   用 `statistics.NormalDist.cdf`，其内部就是 `math.erf`，与实现同源；只有 DSR 的绝对锚是真独立。
   PSR 没有独立锚——而 P1-6 恰是 PSR 的*口径*错误（不是实现错误），这类错误只有独立口径测试能抓。
4. **验收判据未自动化**：阶段 2 的"v1 vs benchmark 同图"只靠 gitignored 产物，
   无法在 CI 失败；阶段 1 的"差异归因白名单"没有实现。

---

## 7. 我逐项复核、确认无误的部分

为免报告失衡，以下是我**逐行读过并认为正确**的关键件：

- **费用与撮合**：佣金双边 `max(毛额×0.0000854, 5)`（`engine/fees.py:12-13`，与旧引擎一致，
  5 元下限在毛额 58,548 元内生效）；印花税卖出 0.05% 仅股票、ETF 免（`fees.py:59`+`profiles.py:24-29`）；
  买不入印花税；滑点方向正确（买 `×(1+base+tail)`、卖 `×(1−slip)`，`matcher.py:79` vs `fees.py:26`），
  与 §2.4 算例（5.82→5.837）一致；**止损成交不叠滑点**（`matcher.py:217-220`）；
  可负担数量解析式与旧引擎 `_max_buy_qty` 逐行同构（含 `int()` 截断与整手递减）。
- **T+1**：买入 `sellable=0`、卖出校验 `sellable`、`begin_day` 次日滚动（`account.py:20-34,49-57`），
  当日持仓快照如实反映"不可卖"（golden 测试钉住）。
- **止损执行语义**：跳空按开盘、触及按止损价、触发先于阻塞（`matcher.py:204-213`）；
  未触发**不落** unfilled（"没发生的交易不是错过的交易"）——与详设一致。
- **账户与计息**：`heat = Σ max(0,(现价−止损)×量)`；空仓 1%/年按 252 交易日计（252000→10.00/日，有钉子）。
- **append-only 与状态机**：新表所有内容字段的 UPDATE/DELETE 被触发器拒绝（我核对了
  `research_experiments`/`research_verdicts`/`research_runs`/`research_topics`/`holdout_tokens` 的 WHEN 子句）；
  `_TRANSITIONS` 是封闭白名单；`final_verdict` 只能降级（`confirmed` 只能落 confirmed/inconclusive/rejected）；
  `distribution` 固定 `inconclusive`（模块级 `allowed_finals`）。
- **统计件实现（除口径外）**：PSR 与 Bailey & López de Prado (2012) eq.(13) 逐位一致
  （子代理独立复算 + 仓库绝对锚）；DSR 的 `SR0 = √V[(1−γ)Φ⁻¹(1−1/N)+γΦ⁻¹(1−1/(Ne))]`
  与 2014 论文一致；MinTRL 与 PSR 互逆；区块 bootstrap 同种子位级可复现；BH 手算金值正确；
  CSCV/PBO 结构正确（构造性检验 PBO=0）。
- **PIT 与受限句柄**：`get_panel` 强制 `as_of`、`historical` 不返回未来行；日循环**逐日重绑**
  句柄（`backtester.py:239-241`）；`BoundGateway` 拒绝覆写 `as_of/data_version` 并记 `violation:*` 审计；
  取数全经 L1.5，引擎不直连 L1。
- **模块治理与 DSL**：DSL 是构造级白名单（无属性/下标/赋值/lambda，`ref` 只接受非负整数字面量），
  `ref(close, 0-1)` 在编译期被拒；python 模块过契约/确定性/前缀稳定性三门，前视模块能被探针抓住；
  模块不可在通过测试门前被引用（`registry` 是唯一入口）。
- **存量安全**：见 §5（无破坏性 schema 改动、MCP 面完好、模板转义、测试隔离）。

---

## 8. 建议的修复顺序与验收判据

**必修（阻断，建议按此序）**
1. P1-1 涨跌停窗口口径（影响每一个回测/清单的交易日）+ 老股用例钉子；
2. P1-6 判定器改配对口径 + 阈值入配置 + "真实改进能 confirmed / 零边际不越 5%"钉子；
3. P1-2 holdout token 端到端接通 + 集成钉子；
4. P1-3 walk_forward 修复 + 端到端钉子；
5. P1-4 `context_filter` 二择一（实现或拒绝）+ 钉子；
6. P1-5 `loads` 修复 + 晋升成功路径钉子 + 通道暴露。

**次修（本迭代内）**
7. P2-1 重复买入 fail-loud；P2-2 停牌尾盘离场记 unfilled；P2-4 入口参数域/`from` 校验；
8. P2-6 冻结门三处（force 旁路、顺延留痕、启动补偿标记）；
9. P2-3 recompute 语义修正后再接线；P2-5 三注记进 warnings；
10. P2-7 的 9 条关键路径各补 1 条能失败的钉子；P2-8 四处重言式断言改实断言；
11. P2-9 parity 归因白名单或显式声明"归因人工"（并修 1 根 bar 的预热边界）。

**验收判据（按开发日志的既有约定：自动化、能失败）**
- 每条修复必须附"删掉实现即失败"的测试；判定器类修复必须附**独立口径**的 golden
  （如配对显著性用解析/仿真对照，而非复述实现公式）；
- 修完后重跑：新增套件 + 存量全量（排除已知 Windows flake），并重跑一次
  `scripts/run_base_v1_sample.py` 把 v1/benchmark 同图产物纳入版本化产物或 slow 测试，
  使其可回归；
- 修复后的判定器行为应能在合成数据上展示：真实 ΔSharpe=0.2（ρ≈0.98）→ confirmed；
  真实 ΔSharpe=0 → confirmed 比例 ≤5%。

---

## 附录 A：审查方法与取证记录

- **方法**：先通读三份设计文档与开发日志建立验收清单；随后 4 个只读子代理分别深挖
  engine / research / 存量影响 / 测试完备性，产出带 `file:line` 的候选发现；
  **我对每一条要写进本报告的发现独立复核**（读源码 + 必要时跑最小实证），
  并**丢弃了若干经复核不成立的候选**（例：子代理称 `HeatCapGate` 在 heat 未知时
  `return []` 会冻结全部新开仓——当前修订 `portfolio_risk.py:64-69` 实为"告警放行 `return intents`"，
  该条已剔除）。
- **锚点**：审查时工作树最后修改时间为 2026-09-24 11:44（`tests/integration/test_review_ds.py`）；
  我复核的关键件 md5：`src/research/api.py 501b18d0…`、`src/research/verdict.py 38fb8fe2…`、
  `src/research/evaluations/backtest.py bf6515aa…`、`src/gateway/tradability.py 77a58b82…`、
  `src/portfolio/backtester.py aee1cb54…`、`src/portfolio/live.py 549e5952…`。
  本报告只对该修订负责；若其后又有改动，请以 md5 对齐复检。
- **实跑命令（摘要）**：
  - `python -m pytest tests/unit/{research_stats,research_stage0,gateway,engine_fees_matcher,engine_golden,engine_stops_parity,portfolio_config,review_gaps}.py -q` → `105 passed in 14.23s`；
  - `python -m pytest tests/integration/{engine_parity,live_runner,portfolio_backtester,research_evaluations,research_stage5,review_ds}.py tests/api/test_research_ledger_api.py -q` → `51 passed in 88.03s`；
  - 涨跌停实证、`pd.to_datetime("oos-0")`、引擎重复买入/停牌尾盘卖实证、PSR/DSR 蒙特卡洛：
    均为进程内一次性计算，未落盘（脚本片段见正文各条）；
  - 只读 SQLite 查询生产库新表行数（`mode=ro`）。
- **未做**：未在真实全库上重跑 10 年 v1/benchmark（避免长时占用与写库），
  阶段 2 的验收结论沿用其产物文件 + 代码审读；未逐行复核 `web/templates/*.html` 的全部渲染分支
  （已做 XSS/路由/模板变量三面检查）。

## 附录 B：本次审查对仓库的副作用（自查，诚实交代）

1. **`research/topics/T001_probe/`（4 个文件：TOPIC.md、experiments/E0001/{REPORT.md,report.json,manifest.json}）**
   ——由审查子代理探测 `ResearchService.conclude_topic` 时生成（`ResearchService.topics_dir`
   默认是 CWD 相对的 `research/topics`，`src/research/api.py:23`），当前为未跟踪文件。
   我未删除（本轮只读原则），请以 `rm -rf research/topics/T001_probe` 清理；
   该目录同时是 P3-11 的现场证据。
2. 未修改任何源码/模板/测试/配置；未对 `data/trend_quant.db` 写入数据行
   （测试全部使用 tmp 库；复核其新表行数全程为 0）。
   需说明：任何 `Database()` 实例化都会执行 `CREATE TABLE IF NOT EXISTS` DDL，
   因此库文件的 schema 级触碰不能百分百排除（新表此前已存在，属幂等无变化）。
3. 测试运行产生了 `__pycache__` 与 `.pytest_cache`（已被 `.gitignore` 忽略）。

---

**总评**：这是一次**设计与实现密度都很高**的交付——分层、协议化插槽、纪律流水线、
统计判定器、看板与 AI 通道都在，且"力学层"（费用/T+1/止损/as-of/append-only）质量扎实。
问题不在"写得少"，而在**"声明与生效之间的距离"**：四处被记为"已交付"的机制实际不可达或被静默忽略，
一处规则口径在每次运行的头几天算错，一处统计判定器决定所有人结论却不判"配对"。
这六项修完（并各配一条能失败的钉子），一期的"基建整个建完"才站得住。
