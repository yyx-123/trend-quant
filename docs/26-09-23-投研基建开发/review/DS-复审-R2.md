# DS 复审 R2：投研基建一期「DS 盲审 FAIL → 修复」的独立验证

> 复审人：DS（盲审报告作者本人，复读自己的 5 项 P1 + 11 项 P2 逐条验证）
> 日期：2026-09-24
> 复审对象：`review/DS盲审报告.md`（FAIL）的修复轮，以及自组织验证轮
> `review/DS-验证复审.md`（PASS + 2 残留 → 残留再修 → DS_FINAL_VERDICT PASS）
> 方法：逐条代码走读 + **自建 7 组探针独立实证**（一次性、已删除、未入库）+
> 全量回归复跑（我自己的命令，不复用他人数字）+ 生产库只读快照（`mode=ro&immutable=1`）
> 约束遵守声明：未改动任何源文件/设计文档/数据库；探针文件用后即删；
> 生产库以 immutable 只读打开（无 `-shm/-wal` 产生，实测目录仅剩 `trend_quant.db`）。

---

## 0. 总体结论

**判定：PASS（有条件）——5 项 P1 全部关闭并经我独立实证；但 1 项被"验证通过"的
P2 实测仍未修复，另 1 项 P1 在次要路径上残留，另有 2 项修复副作用需在首个正式
实验前处置（均为 P2 级，按章程不阻断）。**

我复核了自组织验证轮的全部关键结论，其中 **1303 项全量回归可复现**
（我自己跑：unit+api 1127 passed / integration 176 passed = 1303，0 失败），
P1 修复的代码位置与行为我逐条实测通过。**但 `DS-验证复审.md` 的 P2-7
"已修复"结论与事实不符**：该探针形态与内置件的真实调用形态不一致，导致
一个仍会误杀合法模块的 `TypeError` 被放过（§2.2，实测可复现）。这条提醒：
**钉子必须复刻被替代物（内置模块）的真实调用形态，否则"修好了"只是探针的错觉。**

### 结论速览

| 项 | 状态 | 我的证据 |
|---|---|---|
| P1-1 报告完整性 | ✅ 已修 | `full_run_report` 接线 + gate_log 透传 + `/report.json` 端点 + 模板下载链接；钉子实跑通过 |
| P1-2 复现入口 | ✅ 已修 | rerun 三通道 + `is_reproduction` 幂等迁移 + attempt 计数排除；两个 error 分支有 `LifecycleError` 钉子 |
| P1-3 Δ换手 | ⚠️ 主路径已修，**walk-forward 路径残留假 0（P2）** | 探针 A1=0.009955（真）；A2=0.0（拼接路径假）vs A3=None |
| P1-4 热自旋 | ✅ 已修 | 探针 F1：5.0s 内 9 次迭代 = **1.8/s**（修复前 576/s） |
| P1-5 重复检测 | ✅ 已修（双向） | 探针 C1–C3：同 spec/仅顺序不同 → 命中；换条件 → 放行。**但新签名过窄（P2，见 §3.1）** |
| P2-1..P2-6, P2-8..P2-11 | ✅ 已修（P2-8 伴生 1 项新 P2） | 逐条代码 + 探针/钉子（§2） |
| **P2-7 模块门元数据桩** | ❌ **声称已修，实测未修** | 探针 E1：`_StubBoundGateway().metadata.instruments()` → `TypeError: 'NoneType' object is not iterable`；而内置件正是这种无参调用 |

---

## 1. P1 逐项验证（全部关闭）

### P1-1 §6.5.0 完整报告 —— 已修复

- `research/evaluations/backtest.py:585-607`：`_assemble_result` 内构造 `full_run_report`
  = `portfolio.reports.build_report(...)`，传 `gate_log=exp_result.get("gate_log", [])`
  （P2-4 一并接线）、`fills`、`unfilled`、`benchmarks`、`positions_snapshots`、
  `slot_limit`（从 `config.gates` 里取 `slot_limit@N` 参数）；`report["full_run_report"]`
  随 `verdict_insert` 落库 → 课题文件夹 `report.json` 同步物化。
- `/report.json` 端点：`app/routers/research_ledger.py:112-139`（200 + 404 分支齐全）；
  模板 `web/templates/research_experiment.html:28` 加了下拉链接（可发现性解决）。
- 实跑钉子：`test_backtest_report_contains_gate_rejections_and_round_trips` 通过
  （断言 `summary/round_trips/gate_rejections/unfilled_by_reason/cost/return_distribution/drawdown_durations` 键存在）。
- 残留（P3）：端点无持久化测试（`tests/api` 未覆盖，仅一次性冒烟）；详情页预览仍截断 4000 字符（有全量出口，可接受）。

### P1-2 复现入口 —— 已修复

- `experiments.rerun_experiment`（复制 topic/spec/hypothesis、`parent_experiment_id=原实验`、
  `attempt_index=原值`、`is_reproduction=1`）；`api.rerun_experiment`（auto_queue）；
  CLI `rerun` 子命令；MCP `research_rerun_experiment(run=True)`；`topic_files` 的
  manifest note 现在指向真实存在的命令。
- 计数语义：`experiments.py:130-137` 的 COUNT 加 `AND is_reproduction = 0` ✓；
  钉子 `test_rerun_keeps_attempt_index`（r1.attempt == e1.attempt、后续 e2=2）我实跑通过。
- 迁移：`db.py:1071` 把 `is_reproduction` 放进 `_migrate_schema` 的列迁移表
  （PRAGMA table_info + ALTER ADD COLUMN，幂等）——生产库实测已有该列 ✓。
- 前轮残留（`LifecycleError` 未导入）已修：`experiments.py:15` 导入齐全，
  `test_rerun_error_branches`（两个分支都断言具体类型 + match）我实跑通过。
- 残留（P3）：`promote_to_library` 无 CLI/MCP 通道（服务方法已构成"唯一的门"）；
  `is_reproduction` 不在 append-only 触发器白名单（可被 UPDATE 改，无实测风险，但口径上属"内容字段"）。

### P1-3 Δ换手 —— 主路径已修复，次要路径残留（降为 P2）

- 修复形态正确：`_trades_turnover_total(trades)` 读 `t["price"]×t["qty"]`，
  **与回测器 `_record_result` 的 trades 键一致**（我特意核对过键名，没有踩"键不匹配→静默 0"的坑）；
  正常路径调用点 4 处全接线。
- 探针（自建）：`_nav_summary(nav, [1 笔成交]) → turnover = 0.009955` ✓ 真实。
- **残留（P2）**：walk-forward 拼接路径 `backtest.py:320` 把 `exp_result["trades"]` 设为
  **空列表**，`_assemble_result:440` 用 `exp_result.get("trades")` 重算 → 空列表走
  `trades is not None` 分支 → `turnover = 0.0`（探针 A2）。于是同一个 walk-forward run 里
  `deltas_vs_base.delta_turnover = None`（诚实，显式分支）而
  `experiment_summary.turnover = 0.0`（假 0）——**同字段两个口径，代码注释只对前者成立**。
  一行修复：WF 分支 `exp_result["trades"] = None`。

### P1-4 worker 热自旋 —— 已修复（我用同一探针复测）

- 代码：cap 命中在锁内 requeue、**锁外** `self._stop.wait(0.5)` 再 continue；
  `_dispatch_iterations` 观测点。
- 我自建探针（3 个同会话实验 + max_workers=1/cap=1，5 秒窗口）：
  **9 次迭代 = 1.8/s**（修复前同探针 2881 次 = 576/s），队列正常消化
  （最终 `['evaluating','running','evaluating']`）✓ 热自旋消除。
- 钉子 `test_dispatcher_backs_off_when_session_capped`（<30 次/2.5s）阈值偏松但方向正确（热自旋必然 >100/s）。

### P1-5 重复检测 —— 双向已修复

- `_canonical_spec` 覆盖全 spec（浮点 4 位、列表按 repr 排序、dict 键序）。
- 我的探针（不复用仓库测试）：同 spec → `duplicate_of` ✓；**仅 horizons 顺序不同
  （[10,20] vs [20,10]）→ 也命中**（容差生效，符合"参数容差内"的原意）✓；
  换 `context_filter`+`horizons` → 放行 ✓（详设 §6.5.2 的"另一个实验"不再被误杀）。
- 新问题见 §3.1（签名过窄）。

---

## 2. P2 逐项验证

| # | 项 | 状态 | 证据 |
|---|---|---|---|
| P2-1 | 晋升入库 | ✅ 服务层已修 | `api.promote_to_library`：非 verdicted 拒 / 非 confirmed 拒 / 只支持 portfolio_backtest / 版本带 `parent_version_id`+`experiment_id`；钉子双向断言通过。CLI/MCP 无通道（P3） |
| P2-2 | manifest data_version | ✅ 已修 | `topic_files.py:114` 条件改 `if not any(r.get("engine_run_id") ...)` + 从 `gateway_audit(run_id=exp_id, data_version>0)` 取最新；钉子 `test_manifest_data_version_from_audit` 先钉"全部 engine_run_id 为 None"的缺陷形态再钉非空 → 我实跑通过 |
| P2-3 | FDR 覆盖 event/bucket | ✅ 已修 | `event.py:305-309`（`_bootstrap_means` 与 `bootstrap_band` **同种子同抽样口径**，p 值下界 `0.5/n_boot`）、`bucket.py:228`（|随机利差|≥|实际| 的比例）、`conclusion.py:78-85` 回落 `evidence.p_value` |
| P2-4 | gate_log 进报告 | ✅ 已修 | `build_report(gate_log=...)` → `gate_rejections` 计数；随 `full_run_report` 落库 |
| P2-5 | live 新鲜度闸门 | ✅ 已修 | `live.py:176-190`：面板末日<决策日 且 交易日 且 as_of<15:00 → `RuntimeError`；盘后 → caveat 入清单（`caveats=[*freshness_caveats, *_live_caveats]`）。14:05 定时任务路径若拿不到盘中 bar 会 failed 入 `job_runs`（不再静默出旧清单） |
| P2-6 | live T+1 | ✅ 已修 | `live.py:75-77` `sellable = 0 if buy_date >= panel.dates[-1] else qty`。口径注记（P3）：应以"决策日"而非"面板末日"比较，否则盘后跑时昨日买入会被误判为不可卖（当前无害，因清单不用 sellable 判定卖量） |
| **P2-7** | **模块门元数据桩** | ❌ **实测未修复** | 见 §2.2 |
| P2-8 | heat 无止损语义 | ✅ 已修（伴生新 P2） | `engine/models.py:137-158`：`unstopped_symbols()` + 任一缺止损价 → None + 空仓 → 0.0；`portfolio_risk.py:65-68` heat_cap 对 None 拒绝新开仓并落 gate_log。伴生问题见 §3.2 |
| P2-9 | time_stop 持有天数 | ✅ 已修 | `position_risk.py:341-348`：`held = upto - dates.index(entry_date) + 1`（入场日=第 1 日），面板缺该日时回落旧计数器；钉子 days[2]→days[6] 我实跑通过 |
| P2-10 | 环境卫生 | ✅ 已修 | 生产库只读快照：`engine_runs/fills/unfilled/daily_nav/positions` 与 `gateway_audit` **全为 0**，`research_*` 0，`portfolio_strategies=9`/`versions=11`（seed 库保留，设计内）；`.gitignore` 补 `data/*.db-shm`/`-wal`；`run_base_v1_sample.py --db` 可指独立库。目录无 `-shm/-wal` 残留 |
| P2-11 | PIT 逃逸面 | ✅ 已修（口径级） | `PanelView.__slots__=("__panel","_upto")`、`BoundGateway.__slots__` 含 `__gateway`（名称改写）+ 公开访问器 `symbol_col` 等 + 代码内写明信任模型；**前缀探针仍能抓住窥探**（我实测：用改写名直达全量面板的模块 → `prefix_stability=False` 被拒 ✓） |

### 2.2 P2-7 详述：声称已修，实测未修复（本轮最重要发现）

- **缺陷**：`research/module_gate.py:165-184` 的 `_StubBoundGateway.metadata.instruments()`
  在**无参**调用时走了错误分支：
  ```python
  if symbols is None:
      return {s: {...} for s in symbols}   # ← symbols 是 None → TypeError
  ```
- **实测**（探针 E1，一行复现）：
  `_StubBoundGateway().metadata.instruments()` → `TypeError: 'NoneType' object is not iterable`；
  带参调用 `instruments(["A.SS"])` 正常返回 ✓（说明分支写反了/漏了默认清单）。
- **为什么严重**：内置件就是无参调用——`portfolio/slots/universe.py:64`（category_filter）、
  `universe.py:106`（liquidity_filter）、`portfolio_risk.py:100`（concentration_cap）全部是
  `ctx.gateway.metadata.instruments()`。因此"与内置同形"的 AI 模块仍然过不了门：
  我的探针 D1（universe 模块调 `instruments()`）→ `passed: False`，失败点在 determinism 段
  的 AttributeError/TypeError。**P2-7 的原始现象（合法模块被误杀）完整保留。**
- **为什么之前"验证通过"**：新钉子与验证探针都用的是不带参数的**对照形态**——
  stage5 的 `test_python_gate_accepts_clean_module` 是 **signal** 模块（从不碰 `ctx.gateway`），
  验证代理的探针想必走了带参路径。**钉子未复刻内置件的调用形态**，于是缺陷逃逸。
- **修复建议（一行 + 一钉）**：`symbols is None` 时返回面板同构的默认元数据
  （例如对 `GATE00..GATE05` 返回类目/asset_type）；钉子用 **无参** `instruments()` 的
  universe 与 portfolio_risk 各一例（复刻内置形态），并断言 `passed is True`。

---

## 3. 修复引入/暴露的新问题（P2）

### 3.1 重复检测签名过窄：换 window / primary_horizon / 加无关字段即可逃逸

- **事实**（我的探针 C4–C6，同一 event_study 线）：
  - 仅把 `window` 起点从 2015-01-01 改成 2015-01-02 → `queued`（不再命中）；
  - 仅改 `primary_horizon` → `queued`；
  - 仅加一个平台不消费的字段（`notes`）→ `queued`。
- **不符合的条款**：详设 §6.6.6 要求"同 evaluation_module + 同 subject_key + **spec 相似
  （参数容差内）** → 列出历史实验（含失败）要求确认"；实现现在只识别**完全一致**，
  且"相似"的边界变成了"任意字段都不能差"，等于把容差从"实验自由度"挪到了"文本一致性"——
  一个字段就能绕过（典型场景：把窗口挪一天"重掷骰子"）。
- **建议**：分两档——完全相同 → 硬拒（现状）；相似（忽略运行级字段 `window`/
  `initial_capital`/`n_folds` 等，其余数值按容差）→ 命中并要求 `allow_duplicate=True` 确认
  （即把"逃逸成本"从"加字段"提高到"显式确认"）。

### 3.2 heat_cap 与"无止损价"止损模块的相互作用：组合被冻结

- **事实**（我的探针 G1）：当持仓中存在 `stop_price = None` 的仓位时，
  `HeatCapGate.admit` 返回 `[]` 并记 `heat_cap/*/heat unknown (unstopped positions)`。
  而以下模块**按设计**长期/阶段性无止损价：
  - `time_stop`：`init_time_stop` 恒 `stop_price=None`（它是时间离场，不是价格止损）→ **永久冻结新开仓**；
  - `breakeven`：激活前 `stop_price=None`（浮盈达标才上移到成本价）→ 激活前冻结；
  - `none`：本就是无止损（预期行为）。
- **影响面**：首个研究课题就是"止损选型"（详设附录 A 备注 + 后续TODO §5），
  其串行实验链会逐个试 `time_stop`/`breakeven`；若实验配置里同时挂了 `heat_cap`
  （工具箱标配），结果会是"零成交 → inconclusive"，而原因只在 `gate_log` 里一行字。
  这不是数据错误（是可解释的保守拒绝），但**会在首个课题里制造一批假 inconclusive**。
- **建议（择一）**：(a) 策略载入时对 `heat_cap + (time_stop|breakeven|none)` 组合告警/拒绝
  （与 live 的 `_live_caveats` 同风格）；(b) 让 position_risk 模块声明 `heat_reference_price`
  （如 time_stop 用 `entry_price` 或"全仓位市值"作为最坏损失代理）；(c) 至少在
  `evidence.warnings` 里把"heat 不可知导致的零开仓"显式写成警告（现在只在 gate_log）。

### 3.3 其他 P3（记录在案，不阻断）

1. `/report.json` 无持久化测试（`tests/api/test_research_ledger_api.py` 未覆盖新端点）；
2. 钉子 `test_evidence_turnover_is_not_constant:114` 的 `assert abs(delta_turnover) >= 0`
   是恒真断言（真正的强度来自前两行的 `is not None` 与 `> 0`）——正是 C 轮批过的"形状断言"；
3. `test_event_study_same_event_different_regime_not_duplicate:297` 用
   `pytest.raises(Exception, match=...)`（有消息匹配，可接受；宜收紧为 `IntakeRejected`）；
4. `promote_to_library` 的 happy path 无测试（只有两个拒绝分支被钉）；
5. `P2-11` 收口在代码层（改名 + 注释），但架构稿/详设仍写着"上层**物理上不可能**绕过"——
   建议下一轮设计期修订时把措辞改为"门面强制 + 探针兜底 + 信任模型（token 持有者）"，
   或加一行实现注记，让文档与实现同口径；
6. 我在 R1 列出的以下 P3 **本轮未修（未声称修）**，维持观察：元模块仅 3 处注册（§5.2.8"全插槽通用"未达）；
   bucket 单调性定义与 §6.5.3 文字不一致；停牌日对未触发持仓也落 `unfilled(suspended)`；
   benchmark 缺失时 regime 拆分的静默回落；课题摘要把 failed/rejected_intake/在途统一记 `no_verdict`；
   孤儿表 `portfolio_backtest_runs`（0 行）；`src/**/__pycache__` 残留；
7. `src/portfolio/slots/signal.py` 与 `src/research/dsl.py` 的 mtime 在本轮变更，但行数（429/148）
   与关键语义（`ref` 非负整数守卫、scan/prepare 结构）我逐点复核**无实质变化**，
   疑为格式化触碰；如确非有意改动，建议在下次提交前用 `git diff --no-index` 类手段确认无内容漂移。

---

## 4. 回归与卫生（我的独立复跑）

| 动作 | 我的结果 | 与开发/验证方声明对比 |
|---|---|---|
| `pytest tests/unit tests/api -q` | **1127 passed**（200.6s） | 一致 |
| `pytest tests/integration -q` | **176 passed**（122.4s） | 一致（合计 1303 = 验证报告终轮数字） |
| `tests/integration/test_review_ds.py` + backtester + evaluations + ledger API | **30 passed**（41.6s） | 一致（11 项钉子全绿） |
| 生产库只读快照（`mode=ro&immutable=1`） | engine_* / gateway_audit / research_* **全 0**；strategies 9 / versions 11；`is_reproduction` 列存在；目录无 `-shm/-wal` | 与验证报告一致 |
| 自建探针 ×7 组 | 见 §1/§2/§3（含 2 项新发现） | P2-7 结论**不一致**（见 §2.2） |

---

## 5. 结论与收口清单

**5 项 P1 全部关闭**（每条都有我的独立实证或实跑钉子）；P1-3 在 walk-forward 路径留有一处
同类的假 0，性质降为 P2。全量回归 1303 通过可复现，存量卫生处置到位。

**首个正式实验前需收口（4 项 P2）**：

1. **P2-7 模块门元数据桩**（★优先，原始缺陷完整保留、且会让 AI 提的 universe/portfolio_risk
   模块继续过不了门）：修 `instruments()` 无参分支 + 钉子复刻内置调用形态；
2. **P1-3 残留**：WF 分支 `exp_result["trades"] = None`（一行）；
3. **heat_cap × time_stop/breakeven**：组合告警或在 warnings 显式标注"heat 不可知 → 零开仓"；
4. **去重签名过窄**：恢复"相似→确认"这一档，避免换 window/加字段逃逸。

**P3**（§3.3）不阻断，建议随手清理（尤其第 1、2、4 条属"新钉子自身的质量问题"）。

**流程建议（写进审查章程）**：本轮暴露的共性是**"探针形态 ≠ 真实调用形态"**——
P2-7 的修复被"带参调用"的探针误判为通过，P1-3 的残留被"正常路径"的钉子掩盖。
建议固定一条：**每条修复的钉子必须至少包含一个"复刻被替代物真实调用/真实路径"的用例**
（内置模块怎么调、walk_forward 怎么走），并在验证报告里注明探针的调用形态。

**DS_VERDICT_R2: PASS（有条件）**
