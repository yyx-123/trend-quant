# DS 验证复审：投研基建一期 DS 盲审 FAIL 项修复的独立验证

> 复审人：独立验证代理（非 DS、非开发者）
> 日期：2026-09-24
> 复审对象：开发者对 `docs/26-09-23-投研基建开发/review/DS盲审报告.md`（FAIL：5 项 P1 + 11 项 P2）
> 所声称的"已全部修复"
> 方法：逐项代码走读 + 实跑指定回归 + **9 组自建独立探针实证**（一次性、已删除、未入库）+
> 全量测试回归 + 生产库只读快照（`mode=ro&immutable=1`）
> **约束遵守声明**：本次复审未改动任何源码/设计文档/数据库；一次性探针文件用后已删
> （复核 `git status` 与复审前一致）；生产库以 immutable 只读打开，未产生 `-shm/-wal`。

---

## 0. 总体结论

**判定：PASS（有条件）——5 项 P1 全部修复有效；P2 抽验 9 项有效、1 项（P2-2）修复无效；
另发现 P1-2 修复引入 1 个残留缺陷（NameError）。**

按项目审查章程（P0/P1 未决即 FAIL；P2 不阻断但须在首个正式实验前收口），
DS 原 FAIL 的全部依据（5 项 P1）已消除，故解除 FAIL。**两个残留项必须在首个正式
实验前收口**（见 §4）：

1. **P2-2 修复无效（未修复）**：`_build_manifest` 的 audit 补齐分支在真实路径上永不触发，
   event/bucket/distribution 实验的 `manifest.data_version` 仍恒为 `null`（端到端探针实证）。
2. **P1-2 残留缺陷（新引入，P3 级）**：`experiments.rerun_experiment` 错误分支引用未导入的
   `LifecycleError` → rerun 不存在/非终态实验时抛 `NameError`（探针实证）；happy path
   与计数语义均正确且有测试钉住。

全量回归：**tests/unit + tests/integration + tests/api = 1301 passed，0 失败**（317.9s）。

---

## 1. P1 逐项验证

### P1-1 §6.5.0 完整实验报告 / build_report 死代码 —— **已修复**

| 检查点 | 证据 |
|---|---|
| build_report 有调用方 | `src/research/evaluations/backtest.py:591-601`，`_assemble_result` 内调用 `_reports.build_report(...)`，传入 `gate_log=exp_result.get("gate_log", [])`（P2-4 一并接线）、fills、unfilled、positions_snapshots、slot_limit |
| 报告含 gate_rejections/round_trips/全指标表 | `src/portfolio/reports.py:193-210`：`summary`（`compute_summary` 含 win_rate/profit_factor/avg_holding_days，见 `src/rule_backtest/metrics.py:181-185`）、`benchmark_relative`、`rolling_sharpe_6m/12m`、`drawdown_durations`、`return_distribution`、`cost`、`turnover_total`、`heat_series`、`exposure_series`、`slot_utilization`、`round_trips`、`unfilled_by_reason`、`gate_rejections`、`trade_count` |
| 台账页面全量出口 | `src/app/routers/research_ledger.py:112-139` 新增 `GET /research-ledger/experiments/{id}/report.json` 全量下载端点（详情页仍截断预览，但全量有出口，符合 DS 修复方向） |
| 指定回归 | `tests/integration/test_review_ds.py::test_backtest_report_contains_gate_rejections_and_round_trips` **通过** |
| 独立实证 | 一次性 TestClient 冒烟探针（用后已删）：`/report.json` 返回 200 且 body 含 `report.full_run_report.gate_rejections/round_trips` 原样透出；未知实验 404 |

### P1-2 复现入口 —— **已修复（附 1 个残留缺陷）**

| 检查点 | 证据 |
|---|---|
| 服务层 rerun_experiment | `src/research/experiments.py:195-239`：复制 topic/spec/hypothesis/evaluation_module、`parent_experiment_id=原实验`、`attempt_index=原值`、`is_reproduction=1`；`src/research/api.py:143-151` 服务面包装（auto_queue 可选） |
| 不污染尝试计数 | `experiments.py:130-137` attempt_index 计数 SQL 增加 `AND is_reproduction = 0` → 复现不收紧后续 DSR |
| CLI 通道 | `scripts/research_cli.py:62-63`（`rerun` 子命令）+ `:119-127`（调用并打印 attempt_index） |
| MCP 通道 | `src/trend_mcp/research_tools.py:163-177` `research_rerun_experiment` |
| manifest note 对齐 | `src/research/topic_files.py:151-152` 的 `research rerun <experiment_id>` 现在指向真实存在的命令 |
| 指定回归 | `test_rerun_keeps_attempt_index` **通过**：r1.attempt_index == e1.attempt_index、is_reproduction=1、parent 链接正确、后续实验 e2.attempt_index=2（复现未计入） |
| **残留缺陷** | `experiments.py:209/211` 引用 `LifecycleError` 但文件**未导入**（顶部仅导入 `IntakeRejected`）。独立探针实证：`rerun_experiment(db, experiment_id='E9999', ...)` → `NameError: name 'LifecycleError' is not defined`。非终态实验分支同病。happy path 不受影响（测试绿），但错误处理路径全灭 |

### P1-3 Δ换手恒 0 假证据 —— **已修复**

| 检查点 | 证据 |
|---|---|
| _nav_summary 带 trades | `backtest.py:122` `def _nav_summary(nav_rows, trades=None)`；`:135-137` `_trades_turnover_total = Σ\|price×qty\|`；`compute_summary(..., turnover_total=...)` → turnover = 成交总额/平均权益；trades=None 时 turnover 显式记 None（walk-forward 拼接路径，诚实标注） |
| 调用点全接线 | `:360`/`:378`（base）、`:384`/`:439`（实验）均传 `trades` |
| 指定回归 | `test_evidence_turnover_is_not_constant` **通过**：合成有成交 run 上 `experiment_summary.turnover > 0` 且非 None |

### P1-4 worker 热自旋 —— **已修复**

| 检查点 | 证据 |
|---|---|
| cap 分支锁外退避 | `src/research/worker.py:95-110`：cap 命中时锁内 requeue、**锁外** `self._stop.wait(0.5)` 后再 `continue`；`:39` 新增 `_dispatch_iterations` 观测计数器 |
| 指定回归 | `test_dispatcher_backs_off_when_session_capped` **通过**（2.5s 内迭代 < 30） |
| 独立实证 | 自建计数探针（不复用仓库测试）：1 活跃 + 2 同会话排队、max_workers=1/cap=1，**3.0s 内迭代 6 次（2.0/s）**——对照 DS 实测 576/s，热自旋消除 |

### P1-5 重复检测误杀 —— **已修复**

| 检查点 | 证据 |
|---|---|
| _canonical_spec 覆盖全 spec | `experiments.py:40-50`：不再只读 `spec["diff"]`，对**全 spec** 做规范化签名（浮点 4 位、列表按 repr 排序、dict 键序）；`find_duplicates:57-70` 按签名判等 |
| 指定回归 | `test_event_study_same_event_different_regime_not_duplicate` **通过**：同事件换 `context_filter/horizons` → queued（不误杀）；完全同 spec → `duplicate_of`（真重复命中），双向均钉住 |

---

## 2. P2 抽验（任务指定 9 项 + FDR 链路端到端）

| # | 项 | 判定 | 证据 |
|---|---|---|---|
| P2-9 | time_stop 第 N 日离场 | **已修复** | `src/portfolio/slots/position_risk.py:341-348`：held 改按面板交易日序号计算（入场日=第 1 天，`upto - dates.index(entry) + 1`）；`test_time_stop_exits_on_nth_day` 通过（entry=days[2] → 首次离场 days[6]=第 5 个交易日） |
| P2-8 | heat 无止损记 None | **已修复** | `src/engine/models.py:143-158`：任一无止损持仓 → heat=None（不再静默少计）、空仓 → 0.0、`unstopped_symbols()` 透出；`src/portfolio/slots/portfolio_risk.py:65-68` heat_cap 门对 None **拒绝新开仓并落 gate_log 告警**（告警随 P2-4 进入报告）；`test_heat_none_when_unstopped_positions` 通过 |
| P2-7 | module_gate 不误杀元数据模块 | **已修复** | `src/research/module_gate.py:90` `_make_ctx` 改用 `_StubBoundGateway()`（`:163-184`，含 metadata.instruments/get_production_indicator）；`:192-193` `_instantiate` 增调 `prepare_with_gateway`；stage5 的 `test_python_gate_accepts_clean_module`/`test_python_gate_catches_lookahead` 全过；**独立探针**：需 `ctx.gateway.metadata` 的 universe 模块 `run_module_gate` → `passed: True`（DS 探针 E2 原判 AttributeError 被拒） |
| P2-6 | live T+1 | **已修复** | `src/portfolio/live.py:75-77`：`sellable = 0 if buy_date >= panel.dates[-1] else qty`；**独立探针**：当日买入 sellable=0、历史买入全可卖 |
| P2-5 | live 新鲜度闸门 | **已修复** | `live.py:176-190`：面板末日 < 决策日且（交易日且 as_of<15:00）→ `RuntimeError` 拒绝；盘后放行并带 caveat；**独立探针双向实证**：14:00 拒绝（"live panel stale..."）、16:00 放行且 caveats 含"面板末日…早于决策日" |
| P2-2 | manifest data_version 从 audit 补齐 | **未修复（修复无效）** | `src/research/topic_files.py:114-125` 的补齐分支是 `if not run_rows:`——但 event/bucket/distribution 评估**会写 research_runs 行**（`engine_run_id=None`，探针实证行数=1），分支永不触发；随后 `if run_rows:` 分支只在 `engine_run_id` 非空时填 data_version。**端到端探针**：event_study 实验跑完后 `gateway_audit` 有 `run_id=E0001, data_version=40`，而 `_build_manifest` 返回 `data_version=None`——与 DS 原缺陷表现一致。现有测试只钉了 backtest 类（`test_research_stage5.py:401-403`，该类有 engine_runs 故能过），未覆盖本缺陷形态 |
| P2-3 | event/bucket 证据 p_value + FDR | **已修复** | `src/research/evaluations/event.py:288/309`（经验 p：`max(p_emp, 0.5/len(boot))`）、`bucket.py:170/228` 证据带 `p_value`；`src/research/conclusion.py:78-82` FDR 收集逻辑：stats.psr 缺省时回落 `evidence.p_value`；**端到端探针**：event_study 落定后 `build_conclusion_summary` 的 `fdr = {'n_tested': 1, ...}`（快筛类课题不再 n_tested=0） |
| P3-1 | 创建型七槽全 none 拒 | **已修复** | `test_creation_all_none_rejected` 通过（`IntakeRejected: ... at least one real module`） |
| P2-1 | promote 需 confirmed | **已修复（服务层）** | `src/research/api.py:222-257` `promote_to_library`：非 verdicted 拒、`final_verdict != confirmed` 拒、版本带 `parent_version_id` + `experiment_id` 血缘；`test_promote_to_library_requires_confirmed` 通过（未跑拒/非 confirmed 拒双向）。注：CLI/MCP/web 尚无 promote 通道包装（grep 实证），服务方法本身已构成"唯一的门"的实现载体 |

---

## 3. 存量卫生验证

| 项 | 判定 | 证据 |
|---|---|---|
| 生产库 engine_* 清空 | **符合** | 只读快照（`mode=ro&immutable=1`，未产生 -shm/-wal）：`engine_runs=0`、`engine_fills=0`、`engine_unfilled=0`、`engine_daily_nav=0`、`engine_positions=0`（DS 审查时为 8/2199/228147/11660/—） |
| 生产库 gateway_audit 清空 | **符合** | `gateway_audit=0`（DS 审查时 26 条） |
| 其余表 | 注记 | `research_*` 五表仍 0（干净）；`portfolio_strategies=9`、`portfolio_strategy_versions=11` 仍在——为 seed 库（blank-base/base-v1/7 benchmark），`seed.py` 幂等属设计内，非跑批产物。主库 5,541,224,448 字节 / 09-24 10:38（清表写入所致，体积较 DS 时 +22.9MB） |
| .gitignore | **符合** | 第 59-60 行：`data/*.db-shm`、`data/*.db-wal`（第 57 行 `data/research/` 仍在） |
| run_base_v1_sample.py --db | **符合** | `scripts/run_base_v1_sample.py:73` `_arg_value("--db")`，`:77` `Database(db_path) if db_path else Database()`（默认仍系统库，可指向独立库） |

---

## 4. 残留项（须在首个正式实验前收口）

1. **P2-2（未修复，建议返工）**：把 `_build_manifest` 的补齐条件从 `if not run_rows:`
   改为「逐 run 行 `engine_run_id` 为空时 / 或全部 engine_run_id 均为空时」从
   `gateway_audit`（run_id=experiment_id）回填 data_version；并补一条 event_study 形态
   的 manifest 断言测试（DS §6.2 建议 10 的钉子至今未钉上——这正是本次修复无效
   且 143→1301 项测试都没发现的原因）。
2. **P1-2 残留（新引入，一行修复）**：`src/research/experiments.py` 顶部补
   `from research.errors import LifecycleError`（现 rerun 不存在/非终态实验时抛
   NameError 而非语义化错误；MCP/CLI 通道会把 NameError 原文透给调用方）。
3. **P2-1 通道注记（可选）**：promote 仅有服务方法，无 CLI/MCP/web 包装；
   若希望 AI 通道可晋升，需加薄通道（门禁逻辑已在服务层，通道无额外风险）。

---

## 5. 复审过程留痕

| 动作 | 结果 |
|---|---|
| 指定回归 `pytest tests/integration/test_review_ds.py tests/integration/test_research_stage5.py -q` | **22 passed**（55.4s） |
| 全量回归 `pytest tests/unit tests/integration tests/api -q` | **1301 passed, 0 failed**（317.9s） |
| 独立探针 ×9 | P1-4 迭代计数（2.0/s）、P1-2 NameError 实证、P2-7 元数据模块过门、P2-6 当日买入 sellable=0、P2-5 新鲜度双向、P2-2 端到端（audit=40 vs manifest=None）、P2-2 分支实证（research_runs 行数=1）、P2-3 FDR n_tested=1、/report.json 冒烟（200+404） |
| 探针清理 | 一次性测试文件用后已删；`git status` 与复审前一致；生产库 immutable 只读未产生 -shm/-wal |

**DS_VERIFY_VERDICT: PASS**

---

## 6. 残留复核（2026-09-24 终轮）

> 针对本报告 §4 列出的两个残留项，开发者声称已修复。以下为独立终验：
> 代码走读 + 指定钉子实跑 + **不复用仓库测试的自建探针** + 全量回归。

### 6.1 残留 1：rerun 错误分支 NameError —— **已修复**

- **代码**：`src/research/experiments.py:15` 现为
  `from research.errors import IntakeRejected, LifecycleError`，`:209`/`:211` 两处
  `raise LifecycleError(...)` 有了真实导入。
- **钉子**：`tests/integration/test_review_ds.py::test_rerun_error_branches`
  断言不存在（match="not found"）与非终态（match="only terminal"）两分支均抛
  `LifecycleError`——**通过**。
- **独立探针**（自建、一次性）：rerun `E9999` →
  `LifecycleError: experiment not found: E9999`；rerun queued 态实验 →
  `LifecycleError: only terminal experiments can be rerun (status=queued)`。
  NameError 不再出现。

### 6.2 残留 2：manifest data_version 补齐分支永不触发 —— **已修复**

- **代码**：`src/research/topic_files.py:114` 条件由 `if not run_rows:` 改为
  `if not any(r.get("engine_run_id") for r in run_rows):`——正是本报告 §4.1 建议的
  修法；event/bucket/distribution 的 runs 行（`engine_run_id=None`）现在能进入
  audit 回填分支。
- **钉子**：`test_manifest_data_version_from_audit` 先断言
  `rows and all(r["engine_run_id"] is None ...)`（钉住缺陷形态本身），再断言
  `manifest["data_version"] is not None and > 0`——**通过**。
- **独立探针**（复跑本报告 §2 P2-2 原判"修复无效"的同一端到端场景）：
  event_study 实验跑完后 `research_runs` 1 行且 `engine_run_id` 全 None、
  `gateway_audit` 1 行 `data_version=40`；`_build_manifest` 返回
  **`data_version=40`**（修复前同场景为 `None`）；物化产物
  `experiments/E0001/manifest.json` 的 `data_version` 同为 **40**。

### 6.3 终轮回归

- `tests/integration/test_review_ds.py`：**11 passed**（21.6s）；
- 全量 `tests/unit + tests/integration + tests/api`：**1303 passed，0 failed**
  （373.0s；较上轮 1301 净增 2，即上述两条新钉子）。

### 6.4 终轮结论

两个残留项均修复有效且各有实证型钉子；DS 盲审 5 项 P1 + 抽验 P2 的修复至此
**全部验证通过**，无未决 P0/P1/P2（本报告 §4.3 的 promote 通道注记为可选项，
不构成缺陷）。

**DS_FINAL_VERDICT: PASS**
