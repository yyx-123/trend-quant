# Round 1 修复与回归（loop-review-ds4f）

> 日期：2026-09-25
> 对应审查：`round1-review.md`（4 项 P1 + 12 项 P2 + 26 项 P3 + 8 类钉子缺口）
> 代码提交：本轮修复代码（见文末 commit）

## 修复总览

| 类别 | 已修 | 记录在案（未修，有理由） | 待用户决策 |
|---|---|---|---|
| P1 | 4 / 4 | 0 | 0 |
| P2 | 12 / 12 | 0 | 0 |
| P3 | 21 / 26 | 4 | 1（R1-P3-17） |
| 钉子缺口 | 8 / 8 类 | 0 | 0 |

---

## P1

### R1-P1-1 可交易性除权基准晚一根 bar 生效
- **修复**：`src/gateway/tradability.py` —— 因子改为对"存储日 E 之后的**第一根该标的 bar**（除权除息日）"生效；用 `searchsorted(bar_ord, E, side='right')` 在"有 bar 的轴日"上定位，O(log n)/因子。`core/adjustment.py` **未改**（它与 vendor forward 口径一致，改它会污染全库 qfq）。模块 docstring 重写为修正后的口径与实证依据。
- **实证**：真实库上 `factor ≥ 1.08` 且 ≥2015 的 enabled 池——
  - 旧口径：假 `is_limit_up` 608 天、假 `is_limit_down` 604 天、330 只标的；
  - 新口径：除权日 `limit_up` 命中 28 天、次日 `limit_down` 命中 7 天、34 只标的，且逐个核对样本（000333.SZ / 000661.SZ / 300496.SZ）收盘价均落在推导区间内。
- **钉子**：`test_tradability_ex_dividend_base_price` 重写为真实口径（登记日不修正、除权日 ÷f，含 `is_limit_down` 必须为 False 的反向断言）；新增 `test_tradability_ex_dividend_real_call_form`（走 `Gateway.get_tradability` + 真库 `save_market_data`/`replace_ex_factors` 的三条真实分支）。

### R1-P1-2 holdout 窗口字符串比较可绕过
- **修复**：①`holdout.parse_window_bound()` 解析 + 规范化，解析失败 **fail-closed** 抛 `HoldoutError`；`window_touches_holdout` 改为按 `date` 比较。②`experiments.validate_window_spec()` 在**平台入口**统一卡 `window` 形状（两元素、严格 `YYYY-MM-DD` 字面量、start < end），覆盖全部评估模块（此前面板型三模块的 `_spec_errors` 都不看 window）。
- **钉子**：`test_holdout_window_format_bypass_is_closed`（7 种畸形/绕过形态全部必须在 entry 被拒）、`test_holdout_window_end_equal_to_holdout_start_is_touched`（`>=`→`>` 变异即失败）、`test_holdout_window_unparseable_is_fail_closed`。

### R1-P1-3 bucket 扫描上下文缺账户桩
- **修复**：`_EmptyAccount` 收敛为 `_common.EmptyAccount`（单一实现），`event.py` 与 `bucket.py` 共用；bucket 不再置 `None`。
- **钉子**：`test_bucket_scan_ctx_provides_account_stub`（含 `unstopped_symbols`，内置件真实调用形态）。

### R1-P1-4 报告换手率被除以两次平均权益
- **修复**：`reports._turnover` → `_traded_amount`（只返回货币成交额）；新增 `turnover_ratio` 键；`summary.turnover` 由 `compute_summary` 自己算一次。
- **钉子**：`test_report_turnover_total_is_currency_not_ratio`（断言 21000 与 0.0208，而非"键存在"）。

---

## P2

| 项 | 修复 | 钉子 |
|---|---|---|
| R1-P2-1 停牌缺口污染 rolling 指标 | `signal.py` 新增 `_filled_df`（列内 ffill），ma_cross / channel_breakout / high_52w / abs_momentum 的指标输入改用它；MACD（ewm）不动——NaN 不产生幻影交叉，且 ffill 会改 EMA 递归权重 | `test_signal_prepare_ffills_suspension_gap` |
| R1-P2-2 walk_forward 伪造零证据 | `exp_result` 的 `trades/unfilled` 改 `None`（非空列表/空 dict）；`fee_total` 无 run 时记 `None`；`unfilled_by_reason` 为 `None`；新增 `trade_details_unavailable` 告警替代自相矛盾的 `no_trades` | `test_walk_forward_evidence_does_not_fabricate_zeros` |
| R1-P2-3 模块门漏探针 `estimate_stop` | `module_gate._probe` 的 position_risk 分支加入 `estimate_stop(ctx, symbol)` 并纳入返回值元组 | `test_module_gate_probes_estimate_stop`、`test_module_gate_runs_every_slot_probe`（七槽全跑，补 Q8 覆盖缺口）、`test_module_gate_rejects_wrong_signature_per_slot` |
| R1-P2-4 heat_cap 与告警相反（静默零成交） | `HeatCapGate.admit` 对"无止损估计的候选"改为**放行 + 落 gate_log**（与 DS-R2 对"组合热不可知"的裁决同口径）；`backtester` 的运行级告警措辞改为事实（cap 对该 run 不生效、持仓照常建立、不是零成交） | `test_heat_cap_passes_candidates_without_stop_estimate` |
| R1-P2-5 walk_forward 高原探针基准错配 | 抽出 `_wf_fold_windows`（折边界单点实现）与 `_wf_stitch`（OOS 拼接单点实现）；探针在 wf 模式下走 `_run_walk_forward_exp_leg` 跑同一套折 + 同一拼接，与 selected 同基准；PBO 变体矩阵改用探针自带 NAV（wf 下 `run_id=None` 曾静默丢掉整块 PBO） | `test_plateau_items_accepts_dict_form_and_zero_values` 等；行为由 `test_review_k3ds.py::test_walk_forward_end_to_end` 端到端覆盖 |
| R1-P2-6 plateau 证据缺口（dict 形态/零值/unknown 当通过） | `_plateau_items` 兼容 `to: {module, params}` 字典形态并把 `to.params` 合并进参数集；`_with_param` 两种形态都改写；`verdict=="unknown"` 显式发 `plateau_evidence_absent(邻域点未能构造…)` 告警 | `test_plateau_unknown_verdict_is_surfaced_as_absent` |
| R1-P2-7 parity 归因过宽 | 数量界改为"实测累积滑点拖累对应的股数 + 一手"，并以该笔数量的 1/2 为硬上限；NAV 级联归因加上限（`max(日计息×2, 实测拖累×3)`） | `test_parity_attribution_rejects_large_qty_and_nav_errors`（复现审查代理的两个反例） |
| R1-P2-8 台账 CSRF 弱于 /api | `Sec-Fetch-Site` 只放行 `same-origin`/`none`（`same-site` 也拒），并新增 `Origin` 与 `Host` 同源校验 | `test_ledger_mutating_posts_reject_weak_csrf_vectors` |
| R1-P2-9 守卫触发器定义不传播到存量库 | `db.py` 中**全部 8 个 `_guard_update` + 2 个 `_no_update` 触发器**改为 `DROP TRIGGER IF EXISTS` + `CREATE`（此前只有 2 个），存量库下一启动即自愈 | `test_guard_triggers_use_drop_create`；另用临时库注入旧定义实证"重开 Database() 后触发器体已含 final_verdict 子句" |
| R1-P2-10 实盘清单当日恒"停牌" | `compute_tradability` 新增 `live_bars`/`live_bar_day`；`Gateway.get_tradability` **强制** `live_bar_day = as_of 当日`（调用方无法借此注入历史/未来行情）；`live.py` 把面板 provisional 行的收盘传给 tradability | 由 `live_bars` 的强制口径 + `test_gateway.py` 的可交易性用例覆盖 |
| R1-P2-11 bucket 告警被重绑定丢弃 + 伪造 p=0.0 | `warnings = collect_warnings(...)` → `warnings.extend(...)`；`p_value` 仅当 `spread` 有限时计算 | `test_bucket_warnings_keep_module_level_entries_and_p_value_guard` |
| R1-P2-12 bucket 缺 §6.6.3 三注记 | 抽出 `_common.overlap_and_cluster_stats`（event/bucket 共用），bucket 传入 `overlap_ratio`/`top_day_share`/`regimes` | 同上 + `test_bucket_module_level_warnings_survive` |
| R1-P2-13 冻结补跑不跑 post-update pipeline | `daily_market_update_job(..., after_update=None)` 透传到 `_spawn_same_day_catchup`（含 `_daily_market_update_job_locked` 包装层）；`main.py` 抽出 `_finish_daily_update(payload)`，正常路径直接调、顺延路径由哨兵回调（两条路径互斥） | `test_main_lifespan` / `test_main_coverage95` / `test_throat_coverage2` 的 fake 已按真实调用形态加 `**kwargs`；`test_critical_paths.py` 全绿 |

---

## P3（已修 21 项）

- R1-P3-1 `pair_round_trips` 键名（`quantity|qty`、`fill_price|price`、`fill_date|date`）＋新增 `_fill_price/_fill_qty` 助手。
- R1-P3-2 `_slot_utilization` 以 nav 日期轴为准、空仓日记 0。
- R1-P3-3 `r_multiple` 分母自描述：新增 `r_multiple_denominator: "1.5xATR20"`；注释写明"与持仓实际止损倍数无关"。
- R1-P3-4 退出标的顺序 `sorted(held - member_symbols)`（消除 PYTHONHASHSEED 依赖）。
- R1-P3-5 `_is_first_day_of_period` 的 `upto == 0` 逃生口写清语义与副作用。
- R1-P3-6 `liquidity_filter` docstring 去掉不存在的 `levels`。
- R1-P3-7 `rank._event_day` 如实声明"自然日距离"。
- R1-P3-8 `backtester` 模块 docstring 澄清"跨标的可用释放资金/槽位、同标的当日禁回补"。
- R1-P3-9 `PanelView.series/matrix` 返回**只读视图**（`setflags(write=False)`，零拷贝）；`lookback(n<=0)` 返回空数组（原 `[-0:]` = 整条序列）。
- R1-P3-10 同一 (日, 标的, 方向) 的 unfilled 去重（内部键 `_key` 不外泄）。
- R1-P3-11 空 universe 抛 `BacktestError`（原 `PanelRequestError` 顶层报错）。
- R1-P3-12 DSL 白名单加 `BitAnd/BitOr/Invert`（docstring 自带示例此前被自己的门拒绝）。
- R1-P3-13 `retire_module` 同步从进程内 REGISTRY 摘除（`ModuleRegistry.unregister`）。
- R1-P3-14 event regime 分段改用 **regime 匹配基线**，并保留 `delta_mean_vs_global` 供对照。
- R1-P3-15 极短 regime 分段不再行使塌陷否决（`sufficient_sample = n_days >= 30`）。
- R1-P3-16 删死代码 `_segment_metrics`。
- R1-P3-18 recompute campaign 对触碰 holdout 的目标显式记 `skipped`（附原因），不再混成 failed。
- R1-P3-19 模块装载/门失败的对外文案不再回传 `str(exc)`（细节只进日志）。
- R1-P3-20 live overlay 前 bar 查询窗口 10→20 自然日并锚 `end=as_of`。
- R1-P3-21 panel 的 overlay 行补 `end` 边界校验。
- R1-P3-22 审计 flush 失败改为"保留待重试（有上界）+ 记日志 + 不抛"，不再丢行/炸取数/炸 run 收尾。
- R1-P3-23 `live_daily_list_job` 的配置解析移入 `try`（失败落 job_runs）。

### 记录在案（未修，理由）

| 项 | 理由 |
|---|---|
| R1-P3-24 `EngineStore` 持仓快照全内存缓冲 | 属性能/内存优化而非正确性问题；真实影响量级需在满仓长窗口 run 上先量化（当前单 run 最长 10 年 × ≤50 持仓 ≈ 3.75 万行，可接受）。列运行期再评估清单。 |
| R1-P3-25 北交所 `.BJ` ±30% | 当前数据池不含 `.BJ`，无实际影响；补规则需与数据线二期"池扩容"同批做。 |
| R1-P3-26 live 侧未复用 `estimate_stop` → 热卡口链路 | 与回测侧口径统一的改动会改变实盘清单行为，属运行期变更；一期实盘清单不自动卡控，先把口径谈清。 |
| R-Q12 路由 `_service_or_testbed` 吞 503 | 降级时写入的仍是同一生产库（`get_db()` 单例），实际风险很低；"测试专用装配"改注入式属重构，需与测试基建一并决策。 |

---

## 待用户决策（统一在最终报告提交）

1. **R1-D-1 python 模块门的信任模型**（进程内 `exec`，代理实证 `pd.io.common.os.system` 可达任意命令执行且门判 `passed=True`）——与 §6.7"AI 无任意代码执行路径"冲突。
2. **R1-D-2 除权修复后的数字重基线**：enabled 池 274 只标的、932 个交易日的卡控判定改变 → 已落库 run / 已发布基准数字需重跑。
3. **R1-D-3 R 倍数分母口径**（现维持 1.5×ATR 通用分母，已自描述标注）。
4. **R1-D-4 `_is_first_day_of_period` 的 `upto == 0` 逃生口**（已写清，确认是否接受"动作日集合依赖窗口起点"）。
5. **R1-D-5 课题内 FDR 的 p 值族混口径**（backtest 用 `1−PSR`、event 单侧自举、bucket 双侧置换）。
6. **R1-D-6 bucket 分桶基准**（全样本合并分位 vs 信号日截面分位）。
7. **R1-D-7 创建型实验无高原证据且无注记**。
8. **R1-D-8 `phase_combo_entry@1`/`composite@1` 文档-注册表不一致**。
9. **R1-P3-17 distribution 的"按类型×周期分组"**（实现只有 `by_year`）。

---

## 回归结果

- **全量回归（主审人实跑，`pytest tests/ -q`）**：**1572 passed / 1 failed / 0 error**。
  - 唯一失败 `tests/test_instruments_bulk_backfill.py::InstrumentAddJobManagerTest::test_rejects_second_add_job_while_running`
    = Windows 临时文件 `PermissionError`，**审查基线（HEAD）上同一用例同样失败** → 存量 flake，非本轮回归。
  - 基线（Round 1 审查时）：1541 passed / 1 failed → 本轮净增 **+31** 项测试（新增钉子），**新增失败 0**。
- **新增钉子**：`tests/unit/test_loop_review_ds4f.py` 26 项 + `tests/api/test_research_ledger_api.py` 4 项 +
  `tests/unit/test_gateway.py` 2 项（重写 1 + 新增 1）= 32 项；重写的旧钉子 1 项（除权基准）。
- **ruff**：`ruff check src tests scripts` 与基线逐条对比，**新增 0 条**（本轮引入的 7 条已全部修掉；
  仓库自身存量 lint 债务 129 条不在本轮范围——注：CI 的 Ruff 步骤在当前 master 上即为红，
  属存量问题，见最终报告的待决策/记录项）。
- **导入烟测**：`app.main` / research / portfolio / gateway / engine 全栈 import 通过。

