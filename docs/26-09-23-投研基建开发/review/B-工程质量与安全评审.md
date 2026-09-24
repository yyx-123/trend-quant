# B 评审：工程质量与安全评审（投研基建一期）

评审人：评审子代理 B（只读审查，未改任何源文件）
日期：2026-09-23
范围：git status 全量改动面（7 个存量文件 + src/engine、src/gateway、src/portfolio、src/research 等新增栈 + 新测试）

---

## 总体结论

**有条件通过**（修复 P1 项后可转为通过）。

- 存量影响：**零破坏**。新 DDL 为纯增量（CREATE TABLE/TRIGGER IF NOT EXISTS），在 2026-09-15 生产备份库副本与当前生产库副本上实测：49 个新对象（18 表 + 18 触发器 + 13 索引）、0 删除、0 修改既有对象、关键表行数前后一致、二次构造幂等。
- 全量测试：新增 135 个测试全过；存量 unit+api 1120 个全过、integration 等 213 个全过，无回归。
- 但有 **4 项 P1** 未决：DSL 负位移算术逃逸（前视可进 reviewed 模块）、回测失败留 `engine_runs.status='running'` 孤儿行、实盘清单取数标的集漏当前持仓（文档自述与实现不符）、python 模块 `exec` 完全无沙箱且提案时即执行。

按评审规则（有 P0/P1 未决即 FAIL），文末裁决为 FAIL（条件性）。

---

## 存量影响评估（逐文件）

### src/data/storage/db.py
- 新增 `_RESEARCH_STACK_DDL` 全部 `IF NOT EXISTS`，对既有库零风险（实测见上）。
- `_init_tables` 末尾追加 `conn.executescript(_RESEARCH_STACK_DDL)`（第 1012 行附近）：在既有 `_connect` 事务语义内执行，未触碰任何既有 CREATE 语句。
- `connect()` 公开别名（522-529 行）就是 `_connect()` 的透传包装，提交/关闭语义不变，无破坏。
- `load_market_data_many` 重构为 `load_market_data_window_many(symbols, None, None, ...)` 委托：窗口子句 `start/end` 为 None 时不拼 `clauses`，SQL 与原实现逐字一致；`str(start)[:10]` / `end + " 23:59:59"` 为参数化绑定，无注入面。存量调用方（calc_stop_loss_batch 等）行为不变。
- 其余为类型注解去引号（`"pd.DataFrame"` → `pd.DataFrame`，文件已有 `from __future__ import annotations`），无行为差异。

### src/app/main.py
- research worker 启动正确受 `TREND_QUANT_DISABLE_SCHEDULER` 门控（297 行）：`core/env.py::scheduler_disabled()` 运行时动态读环境变量，测试 conftest 的 monkeypatch 在 lifespan 前生效。实测：禁用开关下 `app.state.research_service=None`，`/research-ledger` 页面经 `_service_or_testbed` 回退直连测试库，匿名 303/POST 401/登录 200。
- 新增路由 `research_ledger.router` 被既有 AuthWall 中间件覆盖（实测匿名 GET→303、POST→401）。
- 瑕疵：lifespan 的 `finally` 只 `scheduler_manager.shutdown()`，**research worker 不随 shutdown 停止**（见 P2-5）。另有 316 行后多余空行（样式）。

### src/core/jobs.py
- `intraday_period_refresh_job` 返回值改写（`**{"ts": ...}` → `"ts": ...`）为纯等价改写。
- `daily_market_update_job` 冻结门：`force=True`（启动补偿）不受冻结影响（设计上补跑优先级最高，可接受）；非 force 时最长等 30 分钟、超时返回 `deferred_backtest_running` 顺延下一触发点。对现有任务的影响：日更最多延迟到次日 16:30 或重启补偿（启动补偿会兜底，因 `deferred` 不算 completed/partial）。盘中任务（5 分钟快照、周月 K 刷新）不写日 K 表，不在冻结面内也不受冻结影响——冻结覆盖面对回测读取的 1d 表是充分的。
- `live_daily_list_job` 为新增 cron（周一~周五 14:05，`misfire_grace_time=1800, coalesce=True`），未部署策略时读配置即跳过，对存量任务无干扰；异常路径记 `job_runs(failed)` 不炸调度器。

### src/core/scheduler.py
- `live_list_job` 参数可选（默认 None），既有调用方签名兼容； BackgroundScheduler 默认线程池（10）下一个冻结中的日更任务最长占 1 线程 30 分钟，可承受。

### src/trend_mcp/server.py
- 末尾追加 `register_research_tools(mcp)`：导入期只做函数注册（research_tools 顶层只有 logger 导入，无 DB 访问），`app.main` 在 `_service()` 内惰性导入，无循环导入；Bearer token 中间件对全部工具（含新 9 个研究工具）统一生效。向后兼容（存量 MCP 测试全过）。

### web/templates/base.html
- 导航新增一行链接，纯增量。

### .gitignore
- 新增 `data/research/`（运行产物排除），无影响。

---

## 发现的问题清单

### P0 阻断
无。（python-exec 一项若按"AI 通道不可信"的威胁模型定级则为 P0；按本项目决策 16「不设人审必经门」+ Bearer token/登录墙双通道鉴权的既定信任模型，定 P1，见 P1-4。）

### P1 应改

**P1-1 DSL 负位移算术逃逸：前视表达式可免测入库（研究完整性漏洞，已实证）**
- 文件：`src/research/dsl.py:86-91`；免测放行点 `src/research/modules.py:63-67`
- `compile_expression` 对 `ref(x, k)` 的负位移检查只覆盖「字面负常量」与「一元负号」两种 AST 形态，`ref(close, 0-1)`（BinOp）直接绕过。实测：该表达式编译通过，第 0 行输出 = 次日收盘（2.0）——真前视。
- 而 DSL 模块在 `propose_module` 里是「构造保证安全 → 免测试直接 reviewed」（`gate: dsl_exempt`），前缀稳定性测试只测 python 模块。即一条 AI 提的 `ref(close, 0-1)` DSL 信号会带着前视直接进入回测运行路径，且产出的是「看起来显著」的垃圾结论。
- 修复：对 ref/shift 类参数在编译期做常量求值（`ast.literal_eval` 白名单求值，非负整数才放行，求不出常量的一律拒绝）；或对 DSL 模块也跑一遍 module_gate 的前缀稳定性探针作为兜底。

**P1-2 回测中途失败永久遗留 `engine_runs.status='running'` 孤儿行（已实证）**
- 文件：`src/portfolio/backtester.py:159-173, 275-276`（`begin_run` 已提交、日循环无 try/finally、`finish_run(status="finished")` 只在成功路径调用）；`src/research/pipeline.py:59-62` 只把实验转 failed，不回填 engine run。
- 实证：向 signal.prepare 注入异常后，`engine_runs` 第二行永远 `status='running', finished_at=NULL`；全代码库无任何 `finish_run(status="failed")` 调用点。
- 影响：运行血缘表失真（烂 running 行累积），且无法区分「还在跑」与「早死了」；长事务/缓冲子记录按设计丢弃可以接受，但状态行必须收口。
- 修复：`run_backtest` 的 `with run_freeze.frozen_writes():` 块加 try/except，异常路径调 `store_obj.finish_run(status="failed", error=str(exc))` 后 re-raise。

**P1-3 实盘清单取数标的集漏当前持仓，与函数自述直接矛盾**
- 文件：`src/portfolio/live.py:281-294`（`_live_universe_symbols` 只取 universe 池，docstring 写「universe 池 + 当前持仓（持仓必须在面板里）」）
- 后果链：持仓标的若掉出 enabled 池/universe（标的管理页禁用、类目调整）→ 不在面板 → `_rebuild_stop_state` 返回 None → 当日无止损评估、无信号退出扫描依据（`extra_held` 的 scan 读空序列）、sell 清单 `ref_price/est_price=None`。真实金钱场景下持仓「静默失管」，而 14:05 定时任务照常产清单，从外观看不出缺口。
- 修复：`symbols = sorted(set(universe_symbols) | {open positions from manual_trades})`（`rebuild_account_from_manual_trades` 已在同一函数内取 open trades，可复用其清单或先查后取）。

**P1-4 python 模块 `exec` 完全无沙箱，且在「测试门」阶段即执行（含被拒模块）**
- 文件：`src/research/modules.py:105-123`（`_load_python_module`：`exec(compile(source, ...), namespace)`，namespace 未设 `__builtins__` → Python 自动注入全量 builtins）；`src/research/module_gate.py:104+`
- 实证：模块源码里 `import os`、 `().__class__.__bases__[0].__subclasses__()` 均正常执行。门控流程是「先 exec 拿到 Module 类 → 再跑契约/确定性/前缀稳定性」——**代码是否通过测试门与它是否已执行无关**，恶意/失控代码在提案瞬间就已在 app 进程内跑完。
- 触达面：MCP AI 通道（`research_propose_module`，Bearer token）与 Web 登录墙后的服务层。决策 16 明确「AI 提代码 + 自动门、不设人审」是既定设计，本项不判设计对错，但两点必须正视：(a) 文档自述的「自动测试门」没有任何隔离语义，建议至少在开发日志/架构稿显式写明「python 模块 = 进程内任意代码执行，信任边界 = token 持有者」；(b) 低成本加固：exec 前跑 AST 静态筛查（禁 `import`/`__import__`/dunder 属性访问/`open`/`eval`/`exec`，命名空间显式给受限 `__builtins__`），把「诚实错误」与「脚本小子」两档都挡掉，对抗性逃逸留给前缀探针与人工抽检。
- 附带：`BoundGateway._gateway` 单下划线可被 python 模块直接访问（`ctx.gateway._gateway.get_panel(as_of=未来)`）绕过 as-of 绑定——在无沙箱的前提下这只是既有大洞的一个注脚，不单独列级。

### P2 建议

1. **`src/portfolio/live.py:186-215`**：live 路径两个 `DayContext` 均未传 `history`（默认 []）→ `vol_target`、`drawdown_throttle` 两个依赖 `history_equity()` 的 gate 在实盘清单里静默永不触发，与「回测/实盘同一条决策代码路径」的核心承诺相违。建议 live 重建近 N 日净值序列（manual_trades 记账口径）喂给 history，或在清单 payload 里显式标注「history 类 gate 未生效」。
2. **`src/portfolio/live.py:252`**：`est_stop` 从 `ctx.params` 取，而 `select_entries` 把 `_est_stops` 写在被调方的 `shadow_ctx.params` 上——实盘清单 `est_stop` 恒为 None。应读 `shadow_ctx.params`。
3. **`src/portfolio/backtester.py:103-104`**：`instantiate_modules` 的 gates 循环用了上一循环泄漏的 `slot`（恒为 `"execution"`），`registry.require(binding.module, slot="execution")`。当前靠 require 的全局回退匹配侥幸正确；一旦某 name@version 同时存在于 execution 与 portfolio_risk 两槽就会拿错 spec。改为 `slot="portfolio_risk"`。
4. **`src/research/worker.py:92-95`**：会话达 `per_session_cap` 时实验被立即 requeue，dispatcher 在「队列里全是同会话实验」时无退避热自旋（`Queue.get(timeout=0.5)` 立即返回），一次长回测期间空转烧 CPU。建议 requeue 前 `self._stop.wait(1)` 或计数退避。
5. **`src/app/main.py:319-323`**：lifespan 收尾不停止 research worker（`stop()` 存在但无人调用），进程退出时在跑回测线程靠解释器 atexit 兜底；优雅停机应 `app.state.research_worker.stop()`（注意其 `stop()` 内 `pool.shutdown(wait=False)`，在跑任务仍会被打断——至少把行为显式化）。
6. **`src/research/holdout.py:103-115`**：token 消费是「先查后 UPDATE」非原子（TOCTOU），worker 并发 2 时同一 token 可被两个实验同时使用。UPDATE 加 `AND consumed_at IS NULL` 并校验 rowcount 即可封死。
7. **`src/research/experiments.py:115-121`**：`attempt_index` 的 COUNT 查询与 INSERT 分处两个事务，同 subject_key 并发提案会撞号（无唯一约束，静默重复，影响 DSR 尝试次数口径）。挪进同一事务或容忍并在文档声明近似。
8. **`src/portfolio/library.py:101-107`**：`MAX(version)+1` 与 INSERT 同事务但 SQLite 读已提交快照，并发下靠 `UNIQUE(strategy_id,version)` 兜底抛 IntegrityError——不脏数据但错误面难看；可接受，建议捕获后重试一次。
9. **`src/portfolio/backtester.py:12-13` vs 250-251/417-421**：模块自述「§5.4.2 写死语义：同一标的同日先卖后买允许」，但 `select_entries` 明确排除 `exited_symbols`——同日卖出标的当日不可重入。文档与实现二选一收口。
10. **`src/research/dsl.py:58-64`**：`ast.Constant` + `ast.Mult` 放行 `"x"*200000000` 级别的内存放大（实测编译通过）。AI 草稿来源下属于资源风险面，建议限制常量数值位数/字符串长度，或 eval 外包 resource 限制。
11. **`src/portfolio/slots/signal.py:288-291`**：`TrendScoreCrossSignal.prepare_with_gateway` 全代码库无调用点——backtester/live 只调 `prepare(panel)`，故 `trend_score_cross@1` 注册在册但任何回测里都产零事件。要么在 run_backtest/generate_daily_list 里识别并接线，要么暂摘注册避免误用。
12. **`src/research/evaluations/backtest.py:158-166`**：`_regime_split` 先按 regime 过滤净值再 `np.diff` 当日收益——跨非连续日的跳变被当作单日收益，年化/Sharpe 被系统性扭曲。应按「日收益序列按 regime 过滤」而非「净值序列过滤后差分」。
13. **`src/gateway/tradability.py:35, 146-151`**（近似，文档已声明）：新股 5 日无涨跌幅对全部板块一刀切（主板新股实为首日 44%/次日起 10%），ST 恒按 ±10%（实际 ±5%）。历史回测中这两类失真方向都是「买进了现实买不到的票」，建议 verdict warnings 增加对应提示（当前只有 st_status=unknown 字段透出）。
14. **`src/research/evaluations/bucket.py:127-137`**：`_ScanCtx.account=None`——读取 `ctx.account` 的信号模块（如 `abs_momentum`）在 bucket 实验里直接 AttributeError 转 failed；可对齐 event.py 的 `_EmptyAccount` 桩。
15. **进程边界与竞态（均已记录在设计文档，确认无误）**：run_freeze 仅进程内（CLI 发起的 run 不冻结 app 调度器，已记开发日志）；「日更进行中、新 run 刚好启动」的反向竞态窗口存在但小（面板一次性读入 + data_version 留痕可事后发现），建议后续在日更启动时也置一个写侧标记。
16. **`src/trend_mcp/research_tools.py:148-179`**：`research_confirm_verdict`/`research_conclude_topic` 对 AI 会话开放，超出 `research/api.py` 自述的 AI 沙箱边界（「提实验 + 读台账 + 保存模块草稿」）——「可降不可升」约束仍在，但请确认是否刻意为之。
17. **测试基建**：新测试全部使用 `fresh_registry()` + `test_db`（tmp_path），未发现全局单例/生产目录污染；`tests/integration/test_research_stage5.py` 的 worker 有对应 `stop()`。仓库内 `src/**/__pycache__` 残留已删除模块的 pyc（run_context/signal_engine/execution_rules/position_state/portfolio/stops 等），建议清理以免误判覆盖面。生产库 `data/trend_quant.db` 已被新栈初始化（9 策略 11 版本、8 条 engine_runs），并留有一张开发迭代孤儿表 `portfolio_backtest_runs`（当前代码无任何引用，0 行），建议择机 DROP。

---

## 修复建议（优先级序）

1. P1-1：`dsl.py` 对 ref 位移参数做编译期常量求值 + 非负校验（~10 行），并考虑把 DSL 信号也过一遍 `module_gate` 前缀探针。
2. P1-2：`backtester.run_backtest` 加 try/except → `finish_run(status="failed", error=...)`。
3. P1-3：`_live_universe_symbols` 并入 `manual_trades` 当前持仓标的。
4. P1-4：`_load_python_module` 前加 AST 静态筛查（禁 import/dunder/open/eval/exec）+ 显式受限 `__builtins__`；并在文档显式声明信任模型。
5. P2-1/2/3/4/6：live 清单的 history 接线、est_stop 来源、slot 泄漏、dispatcher 退避、token 原子消费，均为小改动高确定性收益。

---

## 附：本次只读验证留痕

| 验证 | 方法 | 结果 |
|---|---|---|
| DDL 幂等 | 临时库 `Database()` 两次构造 + 触发器行为（内容 UPDATE/DELETE 拒绝、白名单 UPDATE 放行、NULL 安全） | 通过 |
| 既有库零影响 | 复制 2026-09-15 生产备份（4.8GB）→ `Database()` → sqlite_master 前后快照对比 | 新增 49 对象、0 删除、0 修改、二次构造一致 |
| 生产库副本 | 复制 `data/trend_quant.db`（5.2GB）同法对比 + 关键表行数 | 0 增删改（库已被新栈初始化）、行数全等 |
| 新测试 | pytest 新增 13 文件 | 98 unit + 37 integration/api 全过 |
| 存量回归 | pytest tests/unit + tests/api（1120）、tests/integration 等（213） | 全过 |
| 测试开关 | 临时库 + TestClient 实测匿名/登录/POST | AuthWall 全覆盖、worker 正确不起、testbed 回退正常 |
| DSL 逃逸 | `ref(close, 0-1)` 编译求值 | 确认前视（输出次日值） |
| exec 面 | `_load_python_module` 注入 `import os`/dunder subclass | 确认全量 builtins 可用 |
| 卡 running | 注入 prepare 异常后查 engine_runs | 确认孤儿 `running` 行 |

B_VERDICT: FAIL
