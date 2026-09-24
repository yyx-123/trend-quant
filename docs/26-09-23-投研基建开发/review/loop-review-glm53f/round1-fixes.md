# Round 1 修复清单（loop-review-glm53f）

> 对应审查报告：`round1-review.md`。本文件逐项记录修复落点与回归钉子，供验收代理复核。
> 状态：**CLOSED**（2026-09-24）——验收子代理 V1（P1 实证）PASS；验收子代理
> V2 首轮 FAIL（R1-P2-2：`_MetaBase` 成员实例未打 `_registered_key`，any_of
> 递归重建全落兜底分支，组合止损仍恒 None；探针对照实证一行修复即可）→ 已修
> （`_MetaBase.__init__` 补 `sub_instance._registered_key = spec.key`）并新增
> 钉子 `test_live_any_of_stop_rebuild_uses_member_keys`（组合止损 = max(单成员
> 重建值) 对拍）→ V2 复验 **V2_REVERDICT: PASS**（成员键/组合止损/any_of_
> unrebuildable 消失三点实证，live ATR ffill 与回测口径 1e-12 逐位一致）。
> 顺修（V2 建议）：live `_atr_through` ffill 同口径；auth mcp 用例
> importorskip（R1-D-4 由待决策转已修）。

## P1 修复

| 项 | 修复落点 | 钉子 |
|---|---|---|
| R1-P1-1 rebalance_band underweight 清仓 | `portfolio/slots/execution.py`：exit 条件 `abs(actual-target)>band` → `actual-target>band`（仅 overweight）；docstring 注明 underweight 不动作的 MVP 语义依据；60/40 与等权 YAML 补口径注记 | `test_module_behaviors.py::test_execution_rotation_behaviors` 重锚（underweight→不动作 / overweight→exit / band 内→不动作三向） |
| R1-P1-2 状态机非原子认领 | `research/lifecycle.py::transition`：UPDATE 加 `AND status = ?`，rowcount=0 → 重读实际状态抛 `LifecycleError("lost the race")` | `test_loop_review_r1.py::test_transition_atomic_claim_double_channel`（monkeypatch 过期读复刻双通道竞态）+ `test_transition_stale_read_rejected` |
| R1-P1-3 worker 启动块无保护 | `app/main.py` lifespan：research worker 启动整块 try/except，失败 `logger.exception` + 降级 `research_worker=None`（存量业务照常启动） | 验收代理代码复核（lifespan 异常注入不便单测） |
| R1-P1-4 补跑哨兵绕过单飞锁 | `core/jobs.py`：新增模块级 `_DAILY_UPDATE_LOCK`，`daily_market_update_job` 自守（non-blocking，占用即返回 `skipped_already_running`）；哨兵单例判断移入 `_catchup_spawn_lock`；main.py post-update 跳过条件补 `skipped_already_running` | `test_loop_review_r1.py::test_daily_update_job_self_guarded`（锁占用时立即返回） |
| R1-P1-5 实盘清单不整手 | `portfolio/live.py::generate_daily_list`：quantity 分支 `int(value // lot)*lot`（lot 读 cn_stock profile）；target_value 分支同步用 profile.lot | `test_live_runner.py::test_live_buy_list_lot_aligned`（全部买单 %100==0） |

## P2 修复

| 项 | 修复落点 | 钉子 |
|---|---|---|
| R1-P2-1 同标的重复开仓覆盖 | `live.py::rebuild_account_from_manual_trades`：按 symbol 聚合（qty 求和、加权成本、entry_date 取最早） | `test_live_runner.py::test_live_account_rebuild_aggregates_same_symbol`（1500 股/加权成本 20.667/最早入场日三断言） |
| R1-P2-2 any_of 止损重建缺失 | `live.py::_rebuild_stop_state` 新增 any_of 分支：逐成员递归重建取 max(stop_price)，重建不出者记入 `module_state.any_of_unrebuildable`；新增 `_unrebuildable_stop_caveats` 把无止损价持仓写进清单 caveats | 验收代理代码复核（any_of 成员重建路径） |
| R1-P2-3 重复检测缺省逃逸 | `research/experiments.py`：新增 `_expand_platform_defaults(spec, evaluation_module)`（模块感知：只展开该模块声明的 window/universe），`find_duplicates` 两侧同口径展开 | `test_loop_review_r1.py::test_duplicate_detection_default_expansion`（省略 window 判 exact 硬拒） |
| R1-P2-4 recompute 子串误伤 | `research/recompute.py`：新增 `_collect_module_refs` 递归收集精确引用串（含 `name@ver(` 内联形）；LIKE 仅作初筛 | `test_loop_review_r1.py::test_recompute_exact_reference_matching`（hard_stop@1 不被 stop@1 命中；内联形可命中） |
| R1-P2-5 head_to_head 假0换手+缺三注记 | `research/evaluations/head_to_head.py`：换手按 fills 实算（`_real_turnover`）；warnings 接入 `long_window_annotations(start)` | 验收代理代码复核 |
| R1-P2-6 C3 非实验路径 holdout 留痕 | `portfolio/backtester.py::run_backtest`：run_params_json 显式写入 `holdout_touched` 布尔（`_holdout_start(db)` 读配置） | `test_portfolio_backtester.py::test_run_params_records_holdout_touch`（sample=False / 跨入 holdout=True 双向） |
| R1-P2-7 is_reproduction 守卫缺口 | `data/storage/db.py`：guard trigger WHEN 补 `OLD.is_reproduction <> NEW.is_reproduction`；改为 `DROP TRIGGER IF EXISTS` + `CREATE TRIGGER`（定义修订可传播到存量库，注释说明机制） | `test_loop_review_r1.py::test_is_reproduction_guarded_by_trigger`（真 SQL 直改被 IntegrityError ABORT） |
| R1-P2-8 冻结门前移浪费 | `core/jobs.py`：非交易日判断移到冻结门之前（`_daily_market_update_job_locked` 内顺序调整） | `test_core_jobs.py` 既有回归 + 验收代理复核 |

## P3 择修（19 项）

R1-P3-1（HeatCapGate 死代码→unstopped 逐标的落 gate_log + backtester 告警下钻 any_of 成员）、
R1-P3-2（buffered_rotation max_swaps 生效）、R1-P3-3（rebalance_band docstring 对齐）、
R1-P3-4（live_overlay 改 10 日窗口查询取前收量）、R1-P3-5（ATR 面板 close 先 ffill 再算 TR，
无 bar 日保持 NaN，连续序列结果不变）、R1-P3-6（回撤修复天数统一交易日）、
R1-P3-7（元模块成员参数载入期 schema 预校验）、R1-P3-8（event.py window 用 `or` 形 +
primary_horizon ∈ horizons 校验）、R1-P3-9（bucket/distribution 的 universe/expect 入口校验）、
R1-P3-10（recompute 补写 research_runs + CLI rerun 加 `--run/--token`）、
R1-P3-11（list_stale_evaluating 基准改引擎 run 收口时间 COALESCE）、
R1-P3-12（paired 长度不等 raise，不再尾部截断错配）、R1-P3-13（pbo_cscv 删无用 seed、
奇数 n_blocks raise）、R1-P3-14（module_gate 探针容差 1e-6→1e-9 + docstring 如实声明
单切点）、R1-P3-15（conclusion effect==0 不计入方向一致率）、R1-P3-16（bh_fdr 显式封顶 1.0）、
R1-P3-17（bucket 空桶警告）、R1-P3-18（worker dispatcher 裸 assert 改优雅退出）、
R1-P3-19（卖出清单补 sellable/executable 字段 + slot_limit 已持仓丢弃落 gate_log）、
R1-P3-20（main.py ImportError 收窄：仅可选依赖 mcp 缺席静默，内部错误 fail-fast）、
R1-P3-21（verdict_rules docstring ΔSharpe 双口径重写）、R1-P3-22（PanelView.date_at 钳制）。

对应钉子散布：`tests/unit/test_loop_review_r1.py`（14 项）、`tests/unit/test_module_behaviors.py`
（重锚 + max_swaps 两向）、`tests/integration/test_live_runner.py`（2 项）、
`tests/integration/test_portfolio_backtester.py`（1 项）。

## 待决策点（未修复，最终报告统一提交）

R1-D-1（DSR 论文 worked example 数值待用户提供）、R1-D-2（holdout 每策略线限额=后续TODO
B5 运行期项，确认口径）、R1-D-3（ETF 涨跌幅权威对照表=阶段 7）、R1-D-4（环境缺 mcp 包，
建议 `pip install mcp` 或确认跳过）、R1-D-5（rule_backtest/metrics Sortino 口径=存量保护
不动）、R1-D-6（启动收割 vs CLI 在途 run=单实例约定维持）、R1-D-7（R 倍数 1.5×ATR 通用
分母=口径变更留运行期）。

## 回归结果（2026-09-24 主审实测）

- **全量套件**（`pytest tests/ -q`，deselect 已知 flake 文件
  test_instruments_bulk_backfill.py）：**1470 passed / 2 failed / 4 skipped**。
  2 个失败 = `tests/api/test_auth_wall.py` 的 mcp 用例——改动前提交
  （f93031e~1）同环境同样失败（本机未装 `mcp` 包），非本轮回归（见
  round1-review.md 待决策 R1-D-4）。
- **sample 验收产物重跑**（scripts/run_base_v1_sample.py，sample 窗口
  2015-01-01~2024-12-31，全池 874 只）：

  | 策略 | 修复前（开发日志） | 修复后 | 变化归因 |
  |---|---|---|---|
  | base-v1 | 0.70% / -71.6% / 0.18 | **7.47% / -69.11% / 0.38** | R1-P3-5（ATR 复牌跳空被系统性低估→止损过密过窄→换手成本侵蚀；修复后 ATR ≥ 旧值，探针实证 0.2125→0.2489） |
  | bench-buy-hold-csi300 | 2.52% / -45.6% / 0.22 | 2.52% / -45.61% / 0.22 | **逐位不变**（对照组：不经 ATR/再平衡路径 ✓） |
  | bench-60-40 | 3.32% / -31.0% / 0.31 | **2.50% / -30.99% / 0.25** | R1-P1-1（暴跌年不再割底空仓；MDD 不变、年化回落至与买入持有可比——符合"傻瓜分散"基准的真实语义） |
  | bench-random-entry | 7.02% / -69.3% / 0.37 | 6.50% / -70.30% / 0.35 | R1-P3-5（hard_stop + ATR 同 base-v1 归因） |

  产物落 `data/research/base_v1_sample/comparison.{json,md}`（gitignore 内，
  不入库）。数字变化即"修复后诚实结果"，已随本清单存档；开发日志的阶段 2
  记录为历史快照不改写，本轮差异以本表为准。
