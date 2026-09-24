# K3+DS 修复验证复审报告

> 验证人：独立复审代理（只读，不改任何代码）
> 日期：2026-09-24
> 对象：开发者对 K3 盲审（2 P1 + 8 P2）与 Deepseek 盲审（6 P1 + 9 P2）两份 FAIL 报告的全量修复
> 方法：逐项读码核对 → 实跑全部指定钉子 → 关键口径自建一次性探针（内存构造/隔离 tmp 库，未落盘、未写生产库）
> 实跑基准：指定套件 `test_review_k3ds.py`(16) + `test_critical_paths.py`(6) + `test_module_behaviors.py`(12) + `test_paired_verdict.py`(7) = **41 项全过**；
> 全量回归 `pytest tests/ --ignore=tests/test_instruments_bulk_backfill.py` = **1465 passed (381s)**；
> 被排除文件单跑 = 2 failed，均为 `PermissionError: [WinError 32]` 临时目录竞态（存量已知 flake，与新栈无关，与声称一致）。

---

## 0. 总结论

**7 项 P1 全部已修复且修复真实有效**（代码在、通道在、钉子真实可失败、关键口径经我独立探针复核）。
**P2 抽验 10 项中 8 项已修复；2 项修复论证不成立**：

1. **冻结门"顺延留痕"分支必然 NameError**（新发现，修复 DS-P2-6 时引入）——`jobs.py` 超时顺延路径
   引用了尚未定义的局部变量 `today`，留痕调用永远执行不到；且配套钉子是重言式假阳性。
2. **parity 预热边界仍错位一根 bar 且归因白名单零接线**——边界从"i≥34"改成"i≥36"，而旧栈 earliest
   cross 在 i=35（我用构造数据实证：i=35 金叉时旧栈买入、新栈不买）；`attribute_diffs` 白名单函数
   存在但全库零调用，§8"超纲即测试失败"的机器判据仍未落地。

按章程字面（P0/P1 未决即 FAIL）P1 已清，但本次验证的问题是"开发者声称已全部修复并配了钉子"——
该声称对两项 P2 不属实（其中一项是会抛异常的新 bug，一项有实证复现的口径错位），且钉子假阳性
正是 DS-P2-8 批评过的同类问题复发。判 **FAIL（轻量级：P1 全清，两项 P2 修复声称不成立，返修面小）**。

---

## 1. P1 逐项验证

### P1-1 涨跌停新股豁免口径（K3-P2-1=DS-P1-1）——已修复

**代码**：`src/gateway/tradability.py:92-103` 新增 `cal_ordinals`——按**交易日历**
（`min(窗口起点, 各标的上市日)` 起全轴 `is_trading_day` 过滤）构造，`:158-161` 的
`elapsed = searchsorted(cal_ordinals, day) − searchsorted(cal_ordinals, listing_day)`
已与传入窗口解耦。上市日早于窗口时右侧项不再恒 0。
**钉子**：`tests/unit/test_review_gaps.py::test_ipo_before_window_does_not_disable_limits`
（老股 2001 上市、窗口 2024-03-04~08 → 每日 `no_limit=False` 且 `limit_up=11.0`）+
`test_live_single_day_form_has_limits`（单日 dates → `limit_up=11.0`、`is_limit_up=True`）。
**实跑**：2 钉 + 存量 `test_ipo_no_limit_day5_vs_day6`（窗口内上市的第 5/6 日边界）3 项全过。
旧实现下前 4 日 `no_limit=True` → 删掉修复钉子必红，钉子具备可失败性。

### P1-2 holdout token 端到端（K3-P1-2=DS-P1-2）——已修复

**代码**：`src/research/pipeline.py:35-36,70-80` 新增 `_pick_unconsumed_token`——调用方未显式
传 token 时自动带出"绑定该实验（或全局用途）的最早未消费 token"；worker/CLI/MCP 因此全部
经 pipeline 自动接通（`worker.py:117` 等无需改签名）。CLI 另有 `--token` 显式透传
（`scripts/research_cli.py:113`）。
**钉子**：`tests/integration/test_review_k3ds.py::test_holdout_grant_to_run_end_to_end`
——无 token 触碰 holdout → failed；发放 → 同窗口跑通 → `consumed_at` 非空 +
`holdout_touched=1` + warnings 含 `holdout_touched`。**实跑通过**。全链（发放→跑通→
消费→留痕）每一环都有断言。

### P1-3 walk_forward 崩溃（DS-P1-3）——已修复

**代码**：`src/research/evaluations/backtest.py:362-365` 拼接序列改用各折真实日期
（`fold_dates = [str(r["date"]) for r in exp_r["daily_nav"][1:]]`，与 `_safe_daily_rets`
的 len−1 收益对齐），不再是 `oos-i` 占位；`reports.py` 的 `pd.to_datetime` 不再被喂
非法标签。另：`trades=None`（非空列表）使拼接路径 `turnover=None` 显式不可用
（`:376-380`），避免假 0。
**钉子**：`test_walk_forward_end_to_end`——2 折跑通、`evidence.walk_forward.folds` 为 2、
拼接路径无单 run 报告、turnover 为 None。**实跑通过**。

### P1-4 context_filter 静默忽略 + exit 事件不可达（DS-P1-4 + K3-P2-3）——已修复

**代码**（`src/research/evaluations/event.py`）：
- `context_filter` 真实实现：benchmark 的 close vs SMA200 regime 掩码（`:196-213`），
  作用于事件日（`:236`）；`_spec_errors` 校验 `{benchmark, rule}` 形状与白名单规则
  （`:97-102`）；benchmark 不在面板 → `ValueError`（fail-loud）。
- `event_side: entry|exit|both`（`:95-96` 校验、`:224-227` 过滤）——exit 侧事件
  （死叉/"跌破 MA20"类研究）可达。
- `transition_matrix` → `raise NotImplementedError`（`:245-249`，fail-loud）。
- 附带收益：K3-P2-2 的事件多重性也一并修掉——`seen_events` 按 (symbol, 事件日)
  去重（`:216,238-241`）且前瞻收益从**事件日**起算（`:231-235`），不再按候选期每天
  重复计事件、不再从 scan 日错位起算。

**钉子**：`test_event_context_filter_actually_filters`（牛市子集事件数 < 全集）、
`test_event_side_exit_events_reachable`（exit 侧产出事件）、
`test_event_transition_matrix_fails_loud`（failed 且 error 含 "transition_matrix"）。
**实跑 3 项全过**。

### P1-5 promote NameError + 无通道（DS-P1-5）——已修复

**代码**：`src/research/api.py:23` `from research.ledger import loads`（`ledger.py:39`
有定义）；`promote_to_library`（`api.py:224-259`）两道门（verdicted + final=confirmed）
后经 `resolve_experiment_config` 解析 diff、以 `experiment_id + parent_version_id`
血缘入库。**通道**：CLI `promote` 子命令（`scripts/research_cli.py:71,148-149`）+
MCP `research_promote_to_library`（`src/trend_mcp/research_tools.py:180-187`）。
**钉子**：`test_promote_success_path`——confirmed 实验晋升，断言新版本的
`experiment_id`/`parent_version_id` 血缘且库中可查。**实跑通过**；
`test_critical_paths.py::test_mcp_research_tools_registered_and_callable` 与
`test_cli_subcommands_exist` 钉住通道面（12 个 MCP 工具注册、CLI 含 promote/recompute）。

### P1-6 判定器 PSR 口径（DS-P1-6）——已修复（统计论证成立，钉子文案有一处夸大）

**代码**：
- `src/research/stats/paired.py` 新增配对检验：日收益**差序列**的 Sharpe + 配对 t +
  区块 bootstrap 带 + 差序列 DSR（按 attempt 次数校正）；样本 <30 返回 None。
- `src/research/verdict_rules.py:119-125` 门 = `ΔSharpe>0 + t≥1.645 + DSR(diff)>0 +
  非孤峰 + regime 无塌陷`；单序列 PSR 降为展示件（`BACKTEST_RULES` 兼容引用）。
- 阈值入配置：`load_rules(db)`（`:30-43`）读 `app_config` 的 `research.rules.*`，
  `backtest.py:648` 实以 `load_rules(db)` 调用；`backtest.py:510,528,540` 把 `paired`
  接进 `evidence.stats.paired`。

**我对统计论证的独立复核（蒙特卡洛，200 次/档，n=2500，ρ=0.98）**：

| 差序列年化 Sharpe | 实测 mean t | 理论 t=Δ·√(n/252) | confirmed 率 |
|---|---|---|---|
| 0.0 | 0.04 | 0.00 | 6.5% |
| 0.2 | 0.67 | 0.63 | 16.5% |
| 0.5 | 1.62 | 1.57 | 48.0% |
| 0.8 | 2.56 | 2.52 | 80.0% |

- **恒等式成立**：配对 t ≈ ΔSharpe×√(n/252)（差序列口径），实测与理论逐档吻合。
  "诚实检出下限≈0.5"（功效在该点≈50%）的说法成立。
- **DS 原始关切已治愈**：两臂各 1% 日波、ρ=0.98、**臂 Sharpe 差 0.2** 的场景对应差序列
  Sharpe≈1.0（差序列波动 √(1−ρ²) 倍收窄），实测 **confirmed 率 93%**——修复前该场景
  ≈0%。这正是 DS-P1-6 蒙特卡洛表的核心行。
- **假阳率与名义相容**：Δ=0 时 6.5%（200 次 MC，二项标准误≈1.75%，与名义 5% 单尾相容，
  无过度宽松证据；对比修复前 ρ=0 时 12.3%）。
- **钉子文案的一处夸大（注记，不推翻修复）**：`test_paired_verdict.py` docstring 称
  "ΔSharpe=0.2 在 10 年日频下不可达 confirmed"——严格说是**期望 t=0.63、功效≈16%**，
  并非确定性不可达；且 `test_gate_inconclusive_below_detection_floor` 用
  `if paired["t_stat"] < 1.645:` 条件断言（条件不成立时断言被跳过），属弱钉。
  `test_gate_zero_edge_never_confirmed`（20 固定种子 ≤5%）与
  `test_gate_deterministic_blocks`（五门槛逐项可失败）是实钉。**实跑 7 项全过**。

### P1-7 停牌日 tail 卖出崩溃（K3-P1-1=DS-P2-2）——已修复

**代码**：`src/engine/engine.py:131-143`——`fill_mode=="tail"` 且 `bar_close is None`
时直接构造 `MatchResult(status="unfilled", reason="suspended")` 返回，不再
`raise ValueError`；`unfilled` 正常落账（`:167-168`）。原 `ValueError` 语义保留给
真正的未知 fill_mode。
**钉子**：`test_tail_sell_on_suspended_day_records_unfilled`（引擎级：
`unfilled(suspended)` + 持仓不动）+ `test_suspended_tail_sell_inside_run_continues`
（集成级：time_stop 到期日落在停牌区，run 完成且 unfilled 含 suspended）。
**实跑通过**。原 `matcher.match_tail_sell` 的 suspended 分支不再是唯一（不可达）路径。

---

## 2. P2 抽验逐项

| # | 项 | 判定 | 证据 |
|---|---|---|---|
| 1 | 引擎重复买入 fail-loud（DS-P2-1） | **已修复** | `engine.py:69-70` `EngineError("buy intent for already-held symbol ...")`；钉子 `test_engine_duplicate_buy_fails_loud` 实跑通过 |
| 2 | 孤儿收割（K3-P2-4/DS-P2-6c） | **已修复** | `worker.py:65` `start()` 调 `lifecycle.mark_interrupted_research_runs`（`lifecycle.py:157-179`：running/evaluating 实验 + running engine_runs → failed）；钉子 `test_startup_sweep_marks_orphans` 实跑通过（双表各收割 1 行） |
| 3 | recompute 上服务面+CLI（human 门）且定论不劫持（K3-P2-5/6、DS-P2-3） | **已修复** | 服务面 `api.py:277-286`（`sessions.require_human_session` 门）+ CLI `recompute` 子命令；`verdict.latest_verdict`（`verdict.py:85-96`）`ORDER BY (supersedes IS NULL) DESC, id DESC`——复核稿（带 supersedes 指针）不劫持定论展示位；钉子 `test_recompute_does_not_hijack_canonical_verdict`（latest 保持原 verdict + 复核稿可反查）与 `test_recompute_requires_human`（AI 会话 PermissionDenied）实跑通过 |
| 4 | verdict 与课题分级同向约束（K3-P2-7） | **已修复** | `verdict.confirm_verdict` 对 suggested=confirmed → final=rejected 抛 `LifecycleError("downgrade only")`；`conclusion.check_grade_allowed:138-139` `{supported, refuted}` 对翻转直接 False；钉子 `test_verdict_direction_flip_blocked`（confirmed→rejected 拒、confirmed→inconclusive 放行）+ `test_topic_grade_direction_flip_blocked` 实跑通过 |
| 5 | 入口参数域 + from 一致性（DS-P2-4） | **已修复** | 骨架校验经 `module.validate_spec`（`experiments.py:208-212`）→ `backtest.py:88,105` `validate_params`（参数域 min/max）+ `:134` `diff[slot].from ... does not match base config`；钉子 `test_intake_param_domain_and_from_validated`（atr_mul=-5 拒、from=chandelier@1 拒，均 rejected_intake 不占 attempt）实跑通过 |
| 6 | 长窗口三注记进 verdict warnings（DS-P2-5） | **已修复** | `backtest.py:613-617`：`start < "2020-01-01"` 时 `coverage_note`/`fee_era_mismatch`/`survivorship_weighting` 三条进 warnings；钉子 `test_long_window_three_notes_in_warnings` 实跑通过 |
| 7 | 冻结门 force 不绕过 + 顺延留痕（DS-P2-6 前两条） | **修复论证不成立** | 见 §3.1——force 豁免确已删（代码主体成立），但**顺延留痕分支必然 NameError（新 bug）**，留痕实际不可达；钉子是重言式假阳性 |
| 8 | 模块行为矩阵 12 钉（K3-P2-8a） | **已修复** | `tests/unit/test_module_behaviors.py`：1 条全量确定性扫描（每模块两次同输出）+ 11 条具名行为钉（liquidity_filter top_n/阈值、ma_cross exit 死叉、channel_breakout/high_52w、abs_momentum 选最强、trend_score_cross 接线产事件、rank 排序键、sizing 定量、四个组合风控门、ma_stop/pct_trailing/donchian、execution 轮换、any_of/all_of）——抽查断言均落到具体符号/计数/方向，非重言式；**实跑 12 项全过** |
| 9 | 报告 R/MAE/MFE + 集中度时序（K3-P2-8b） | **已修复（一处口径注记）** | `reports.py:207` `concentration_series`（逐日 category_l2 持仓数，经 L1.5 元数据门面）+ `:209` round_trips 优先取 `backtester._round_trips_enriched`（`backtester.py:492-549`：`r_multiple`/`mae_pct`/`mfe_pct`/`holding_days`）。注记：R 分母是**通用 1.5×ATR(入场日)**（`:535`），未关联 `engine_positions` 入场快照的实际 `stop_price`——对非 1.5×ATR 止损模块（chandelier/pct_trailing 等）R 倍数是近似口径 |
| 10 | parity 归因白名单 + 预热边界（DS-P2-9） | **修复论证不成立** | 见 §3.2——`attribute_diffs` 白名单函数存在但**全库零调用**（无测试、无生产接线）；预热边界改成 i≥36，与旧栈 i=35 仍差一根（实证复现），默认测试数据首个有效金叉仍在 i=37，"靠运气"形态未消 |

---

## 3. 两项不成立的详述（含实证）

### 3.1 冻结门"顺延留痕"必然 NameError（新发现）+ 钉子假阳性

**代码实况**（`src/core/jobs.py:143-167`）：

```python
    if run_freeze.is_frozen():            # ← force 豁免确已删除（这部分修复成立）
        ...
        if run_freeze.is_frozen():        # 30 分钟轮询超时
            payload = {..., "status": "deferred_backtest_running", ...}
            record_job_run_safely(
                "daily_update_deferred", payload,
                run_date=today.isoformat(), ...   # ← today 在第 167 行才赋值！
            )
            return payload
    today = market_now().date()           # ← 定义在使用之后
```

**探针实证**（冻结恒真 + sleep 置空，直接调 `daily_market_update_job(settings=None, force=True)`）：

```
daily update deferred: backtest still running after 30min
NameError: cannot access local variable 'today' where it is not associated with a value
```

**后果**：冻结超过 30 分钟时，日更任务不是"干净返回 deferred 载荷并留 job_runs 痕"，
而是 `record_job_run_safely` 的实参求值阶段就抛 `NameError`——留痕永不发生；
`main.py:_run_daily_update` 对该调用无 try/except，异常上抛给 APScheduler（job 记为
error，app 不崩，但 post-pipeline 门与留痕承诺全部落空）。**DS-P2-6 第 2 条
（顺延静默跳过→留痕）的修复声称不成立。**

**钉子假阳性**（`test_critical_paths.py::test_freeze_gate_applies_even_for_force`）：
该测试**从未调用** `daily_market_update_job`；核心断言为
`assert run_freeze.is_frozen() is True or True`（恒真）与 `assert calls["n"] >= 0`
（恒真）；后半段只直接测 `record_job_run_safely` 这个 DB helper 本身能写行。
把 `jobs.py:145` 改回 `if not force and run_freeze.is_frozen():`、或删掉整个顺延分支，
该测试照样全绿——与 DS-P2-8 批评的 `abs(x) >= 0` 同类。**修复须补：**
把 `today = market_now().date()` 移到冻结分支之前（或在分支内现取），并把钉子改为
真实调用 job（冻结恒真 + sleep 置空 → 断言返回 `deferred_backtest_running` 且
`job_runs` 有对应行）。

### 3.2 parity：归因白名单零接线 + 预热边界仍错位一根（实证）

**(a) `attribute_diffs` 未接线**：`src/engine/parity.py:176-209` 的白名单归因函数
（limit_card/tail_slippage/cash_interest/unexplained）实现存在，但全库 grep 证实
**零调用点**——无任何测试、无生产路径调用它。§8"差异只允许三类、超纲即测试失败"
的机器判据仍未落地（现有 parity 测试直接断言零差异——对被覆盖场景更强，但
"有差异时的归因判据"仍不可机器验证）。另 `ATTRIBUTION_WHITELIST` 含 `t_plus`，
代码注释自认该类别现实中永不产出（死类目）。

**(b) 预热边界改错方向一根**：`parity.py:75,88` 现为
`warmup_bars = 26 + 9 + 1`（=36）+ `i >= warmup_bars` → **新栈最早 i=36**。
旧栈口径（我复核 `rule_backtest/value_resolver.py:185-189` +
`condition_engine.py:57-63`）：macd 值需 `idx+1 ≥ 35`（idx≥34），cross 的昨日值
在截断序列上解析需 `idx ≥ 35` → **旧栈最早 cross i=35**。注释自称"最早 i=35"，
代码实际 i≥36。

**探针实证**（搜索合成序列使金叉恰落 i=35，双引擎对拍）：

```
seed=0:  bar35=2023-02-20  legacy买入=True   新引擎买入=False
seed=6:  bar35=2023-02-20  legacy买入=True   新引擎买入=False
seed=44: bar35=2023-02-20  legacy买入=True   新引擎买入=False
```

i=35 金叉时旧栈买入、新栈不买 → count_mismatch。默认测试数据（`_bars()`）首个
有效金叉在 **i=37**（i=1 的 warmup 期交叉两侧都忽略），边界差异依旧不可见——
DS 批评的"靠运气通过"形态未消，只是错位方向反了（由偏松一根变偏严一根）。
**修复须补**：边界改 i≥35（`warmup_bars = max(fast,slow)+signal`、`i >= warmup_bars`），
加一条"金叉恰落 i=35 时两侧同买"的边界钉，并把 `attribute_diffs` 接进 parity 测试
（差异非零时断言 `unexplained` 为空）。

---

## 4. 附带观察（不阻断，注记级）

1. `test_paired_verdict.py` 的"0.2 不可达"文案与条件断言弱钉（§1-P1-6 末）——建议改
   表述为"功效≈16%"并把条件断言改成固定种子下的确定性构造。
2. `_round_trips_enriched` 的 R 分母为 1.5×ATR 通用口径（§2-9 注记）——建议在报告
   字段名或文档中注明，避免被解读为"按实际止损距离的 R"。
3. `engine.py:145-151` 的 `else:` 块缩进 20 格（合法但非常规），建议顺手归一。
4. K3-P2-2（事件多重性）已随 P1-4 一并修复（`seen_events` 去重 + 事件日起算），
   但无"同一金叉 5 天只算一次"的专项钉子——现有 `test_event_context_filter_actually_filters`
   间接覆盖计数口径，建议补一条显式去重钉。

---

## 5. 验证动作留痕

| 动作 | 结果 |
|---|---|
| 指定套件 41 项（k3ds 16 + critical 6 + module 12 + paired 7） | 全过（26.4s / 10.9s 两批） |
| IPO 三钉（含存量窗口内边界） | 全过 |
| 全量回归（exclude 已知 flake 文件） | **1465 passed, 0 failed**（381.6s） |
| `tests/test_instruments_bulk_backfill.py` 单跑 | 2 failed = WinError 32 临时目录竞态（存量已知，与声称一致） |
| 探针：配对 t 恒等式 + 功效表（200 MC × 4 档） | 与理论逐档吻合；臂差 0.2@ρ=0.98 功效 93% |
| 探针：假阳率（Δ=0，200 MC） | 6.5%，与名义 5% 二项相容 |
| 探针：冻结超时顺延路径 | **NameError: today 未定义（§3.1）** |
| 探针：parity 边界（i=35 金叉对拍，3 种子） | **旧买/新不买，错位复现（§3.2）** |
| grep：`attribute_diffs` 调用点 | 零（仅定义处） |
| grep：`holdout_token` 传递链 | pipeline 自动带出 + CLI 显式透传，闭环成立 |
| grep：`promote`/`recompute` 通道 | CLI + MCP + 服务面（human 门）均在 |

（探针均为一次性内存计算或 pytest tmp 隔离库，未改代码、未写生产库。）

---

K3DS_VERIFY_VERDICT: FAIL

---

## 残留复核（R2，2026-09-24 最终确认）

> 复审人对 §3 两项不成立（含 3 个残留点 + 1 个钉子假阳性）的修复逐项独立复核：
> 读码 → 重跑 R1 的复现探针 → 实跑相关套件 → 全量回归。结论：**4 项全部修复成立**。

### R2-1 冻结顺延路径 NameError——已修复

`src/core/jobs.py:143` `today = market_now().date()` 已提到冻结门（`:146`）之前，
顺延分支 `:165` 的 `run_date=today.isoformat()` 引用安全。
**重跑 R1 探针**（恒冻结 + sleep 置空，直调 `daily_market_update_job(settings=None, force=True)`）：
R1 抛 `NameError`，R2 **干净返回 `status=deferred_backtest_running`**（探针尾部的
`RuntimeError: Database not initialized` 是裸探针无 conftest 初始化全局库所致，
留痕写入由 pytest 环境覆盖，见 R2-2）。

### R2-2 重言式冻结测试——已重写为真调用，有牙

`tests/integration/test_critical_paths.py::test_freeze_gate_applies_even_for_force`
（`:121-136`）：monkeypatch 恒冻结 + sleep 快进后**真实调用**
`jobs.daily_market_update_job(settings, force=True)`，断言
`payload["status"] == "deferred_backtest_running"` 且 `test_db.get_latest_job_run(
"daily_update_deferred")` 有对应行。可失败性分析：R1 的 NameError 代码下该测试
必然报错（函数真被调用）；若回退 force 豁免（`if not force and is_frozen()`），
force=True 会绕过门走入真实日更分支，status 断言必红。**实跑通过**。

### R2-3 parity 预热边界——已对齐旧栈 i≥35，双侧探针零分歧

`src/engine/parity.py:77,90`：`warmup_bars = 26 + 9`（=35）+ `i >= warmup_bars` → i≥35，
与旧栈最早信号日（`value_resolver` idx+1≥35 + cross prev 段满 35 根 → i≥35）一致。
**重跑 R1 边界探针**（构造金叉恰落 i=35 的合成序列双引擎对拍）：R1 为"旧买/新不买"
（3 种子复现），R2 **19 个 i=35 样本 agree=19 / diverge=0**；反向侧 i=34 金叉样本
**15 个 agree=15 / diverge=0**（两侧均不买）——边界两侧都对齐。

### R2-4 attribute_diffs 白名单——已重写为正确机器判据并接线

`src/engine/parity.py:182-208`：判据改为"**卡控日不得有对应方向成交**"
（涨停日无 BUY/跌停日无 SELL → violations），不再用卡控场景下连锁错位的逐笔位置
对齐。接线：`test_engine_parity.py:111`（零卡控 → violations 为空）与 `:152`
（涨停卡控场景 → 卡控日零违规成交）两处实调；全库 grep 确认仅这 2 个调用点、
无旧签名残留。**判据牙齿探针**：构造新引擎涨停日 BUY → 必报
`{"kind": "limit_up_buy"}`；无卡控 → 零误报。

### R2 实跑留痕

| 动作 | 结果 |
|---|---|
| `test_critical_paths.py` + `test_engine_parity.py`（9 项） | 全过（9.1s） |
| R1 三个复现探针重跑（NameError / i=35 边界 / 判据牙齿） | 全部转阴（见上） |
| 全量回归（exclude 已知 flake 文件） | **1465 passed / 0 failed**（494.7s；与修复声称的"1445/1 flake"差异为计数口径噪音——本复审两次独立实跑均为 1465 全绿、零真实失败） |

**R2 结论**：R1 判 FAIL 的全部依据（2 项 P2 修复论证不成立 + 1 个钉子假阳性 +
1 个新引入 NameError）均已消除且各有能失败的钉子；此前通过的 7 项 P1 与 8 项 P2
未受影响（全量回归零回归）。一期修复链至此闭合。

K3DS_FINAL_VERDICT: PASS
