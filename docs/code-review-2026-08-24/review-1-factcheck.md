# 评审意见 1：主报告事实核查（fact-check）

- 评审人：事实核查型评审（独立复核）
- 日期：2026-08-24
- 核查对象：`docs/code-review-2026-08-24/code-review-report.md`（v1）中所有带 file:line 证据的结论
- 核查基线：**当前工作区**（注意：工作区相对 HEAD bba50b3 有 8 个已修改文件 + 1 个未跟踪脚本，行号以工作区为准；主报告未声明审查基线，实测其行号与工作区吻合）
- 核查方法：逐条打开所引文件对应行号亲核；对因果类论断做实际复现（pytest 收集实测、git 全历史 `log -S` 搜索、全仓 grep 调用方分析）

---

## 一、P0 条目核查

### P0-1 `/__dev_set_session` 调试端点 —— **无法复现（不属实）**

- 报告称端点位于 `src/app/routers/auth.py:68-81`，豁免名单位于 `main.py:312-313`。
- 亲核事实：
  - `src/app/routers/auth.py` 当前**全文仅 66 行**，无任何 `__dev_set_session` 端点；登录墙豁免名单 `main.py:312-313` 为 `_EXEMPT_PATHS = {"/login", "/api/auth/login", "/api/auth/logout", "/favicon.ico"}` 与 `_EXEMPT_PREFIXES = ("/static", "/mcp")`，**不含**该路径。
  - 全仓 grep（含 .py/.html/.md）：`__dev_set_session` 仅出现在本报告自身中。
  - `git log --all -S "__dev_set_session" -- src/` **全历史零匹配**——该端点在当前 git 历史中从未存在过。
- 结论：报告头条 P0 在当前代码库与全部 git 历史中均无法复现。若报告基于某个瞬态未提交的工作区状态，则该状态已消失且未留痕；无论如何，「仍在线」的论断及行动清单第 1 条不成立。报告自称「所有关键结论由主审查人亲核代码验证」，此条显然未亲核。
- 附带说明：该漏洞模式本身（GET 写 session cookie + 豁免登录墙）分析正确，建议保留为安全审查 checklist 条目，但不应作为当前代码的事实结论。

### P0-2 MCP 5 个读工具无鉴权 —— **属实**

- `main.py:313`：`_EXEMPT_PREFIXES = ("/static", "/mcp")` —— `/mcp` 前缀整体豁免登录墙，精确吻合。
- `src/trend_mcp/server.py`：`trend_dashboard`(99)、`intraday_dashboard`(124)、`symbol_detail`(195)、`calc_stop_loss`(306)、`list_instruments`(348) 五个工具函数体内确无任何鉴权调用；仅 `add_trade`(420) / `open_positions`(462) 调 `tr.authenticate(username, password)`（444、483 行）。
- `server.py:50`：`transport_security={"enable_dns_rebinding_protection": False}` —— 逐字精确。
- 因果核查：工具经 FastMCP `@mcp.tool()` 注册即暴露；`main.py:381-384` 注释自述「The app sits directly behind the frp relay」，公网暴露前提成立。分级 P0 合理。

### P0-3 MCP 以工具参数传密码 —— **属实**

- `server.py:420-454`（`add_trade`，420 def、454 return）与 `462-557`（`open_positions`，462 def、557 return）行号区间精确吻合；`username`/`password` 确为 MCP 工具 JSON schema 参数（421-422、462 行签名）。
- 因果（凭据进入 MCP 客户端日志/LLM 上下文）为 MCP 协议的固有属性，成立。分级 P0 可接受（亦有观点定 P1，但叠加 P0-2 的公网暴露，P0 不算夸大）。

### P0-4 迁移/回填脚本 `shutil.copy2` 备份 WAL 活库 —— **属实**

- `scripts/migrate_category_simplify.py:128`：`shutil.copy2(DB_PATH, backup)`（报告称 126-129，区间吻合，126 行起为 backup 路径构造）。
- `scripts/backfill_batch_excess_metrics.py:179`：`shutil.copy2(DB_PATH, target)`（报告称 175-179，精确）。
- `src/data/storage/db.py:71`：`conn.execute("PRAGMA journal_mode=WAL")` 逐字精确；`db.py:78-97` `backup_to()` 用 `VACUUM INTO`（91 行）——正确实现确实存在且未被这两个脚本使用。
- 因果成立（WAL 未 checkpoint 部分不在主文件内）。建议（统一改 `backup_to()`）可行。

### P0-5 `instruments.py:102` 漏导入 `_config_items` —— **属实**

- `src/app/routers/instruments.py:102`：`for item in [*db.list_instrument_metadata(), *_config_items()]:` 逐字精确。
- 20-30 行 import 清单（`from services.instrument_admin import ...`）含 `_config_name_map` 等 9 个名字，**确无 `_config_items`**；该函数定义于 `src/services/instrument_admin.py:62`。
- 因果核查：`_category_options()` 在 97-99 行 `rows = db.list_instrument_categories(); if rows: return rows`——仅当表为空时才走到 102 行的降级路径，故表非空时不炸、表空时 NameError，推断完全成立。降级路径一旦被触发，`/instruments/api/categories` 等调用方全部 500 成立。分级 P0（潜伏但触发即全灭）合理。
- 附带佐证：同模块已导入 `_config_name_map`（23 行），说明 `_config_items` 是遗漏而非有意不导入。

### P0-6 两个 MCP 测试文件收集即失败 —— **属实（已实测复现）**

- `tests/unit/test_mcp_stop_mode.py:5`、`tests/unit/test_mcp_symbol_detail.py:24`：`from trend_mcp import server` 均为顶层导入，行号精确；两文件均无 `importorskip`（grep 计数 0）。
- 实测 `pytest --collect-only`：**702 tests collected, 2 errors**，错误即这两个文件的 `ModuleNotFoundError: No module named 'mcp'`，与报告的「702 个可收集测试」「2 个 ImportError 中断收集」逐字吻合。
- `pyproject.toml:46` `asyncio_mode = "auto"` 存在且本机未装 pytest-asyncio，实测产生 `PytestConfigWarning: Unknown config option: asyncio_mode` 警告，与报告描述吻合。

---

## 二、P1 条目核查

### P1-1 登录无暴力破解防护 —— **属实**

- `auth.py:37-52`：`/api/auth/login` 全函数无任何限流/锁定逻辑；`auth.py` 全文无 `import logging`/logger（亲读全文确认），零审计日志属实。
- `db.py:23`：`_PASSWORD_ITERATIONS = 200_000` 精确。

### P1-2 明文兜底分支 —— **属实**

- `db.py:34-45` `verify_password`：39-44 行 pbkdf2 分支，**45 行 `return hmac.compare_digest(stored, candidate)` 明文比对兜底**逐字精确。
- `db.py:498-509`：`_migrate_schema` 一次性明文→哈希迁移（注释 498-499、UPDATE 505-508）精确吻合。建议（加 warning 观测或排期删除）可行。

### P1-3 cookie 缺 Secure / session token 明文落库 —— **属实**

- `auth.py:45-51`：`set_cookie(..., httponly=True, samesite="lax")` 无 `secure`；`main.py:364-367` 滑动续期 cookie 字符串同样只有 `HttpOnly; Path=/; Max-Age=...; SameSite=lax`。
- `db.py:355-362`：sessions 表 `token TEXT PRIMARY KEY` 存明文 token，精确。

### P1-4 无 CSRF token / logout 为 GET —— **属实**

- `auth.py:55`：`@router.get("/api/auth/logout")` 精确；docstring（1-6 行）自述「退出为 GET 链接（导航栏直接引用）」。
- 补充（报告遗漏，见「遗漏补充」）：logout 还在登录墙豁免名单内（`main.py:312`），CSRF 强制退出连 session 前置校验都没有。

### P1-5 前端 XSS 三处 + 双重转义 —— **属实（全部亲核）**

- `market_view.html:1689`：`chartTitleEl.innerHTML = \`${payload.display_label || ...}\`` 逐字精确，未走 `esc()`；display_label 由服务端 `format_symbol_display(symbol, name)` 合成，name 用户可编辑 → 存储型 XSS 成立。
- `batch_backtest.html:1053`（renderHeatmap）：`var label = cols[p.value[0]] + ' × ' + rows[p.value[1]];` 未转义，拼接进 tooltip HTML；`:1484`（renderYearChart）同构 `strategies[...] + ' × ' + years[...]` 未转义；`:1310`（renderScatter）`return esc(d[2]) + ' ' + esc(d[3] || '')` 确实转了——「知道要转、漏了两处」的描述精确。
- `market_view.html:1604`：`tradeMetaEl.textContent = ... \`${label ? esc(label) + ' | ' : ''}...\`` —— esc() 产物赋 textContent，双重转义逐字精确。

### P1-6 deploy.sh 路径错误 + 裸 rm -rf —— **属实**

- `scripts/deploy.sh:11`：`INSTALL_DIR="/opt/trend-quant"` 精确；git log `04559e4` 确为「docs: server-rollout 部署路径更正为 /srv/trend-quant（线上实际路径）」——文档改了脚本没跟上，属实。
- `:68`：`rm -rf "$INSTALL_DIR"` 位于 else（无 `.git`）分支，无确认，精确。
- `:106-109`：systemd 单元 `WorkingDirectory=/opt/trend-quant` 等硬编码吻合；`:169-176`：nginx `alias /opt/trend-quant/web/static` + `auth_basic` 两行默认注释（175-176）吻合。

### P1-7 `build_sw_tree` 无效第一轮循环 —— **属实（机制描述有小误）**

- `src/services/stock_industry.py:224-235` vs `236-261` 行号精确：224-225 初始化 `l1_order`/`l2_order`，226-235 第一轮循环（校验 + `logger.error`），236-237 **重新初始化为空 dict**，239-261 第二轮循环做真实赋值；234 与 247 行的 error 日志逐字相同 → 含 '-' 的名字确实打两遍。
- 小误：报告称「第一轮循环对 `l1_order`/`l2_order` 的赋值在 236-237 被重新初始化」——实际上第一轮循环体内**没有任何赋值语句**（只有 continue/error），不存在「赋值被覆盖」，是纯粹的死循环体。结论（编辑残留、应删 224-235）不受影响。

### P1-8 `_prepare_bars` KeyError —— **属实**

- `engine.py:283-286`：`if "date" not in df.columns and "time" in df.columns: ... else: pd.to_datetime(df["date"], ...)`——两列俱缺时走 else 直接 KeyError（报告称 284-286，if 实际在 283，1 行偏差）。281 行空 df 已提前 return，故触发条件为「非空且两列俱缺」，因果成立。
- `rule_backtest/service.py:340-343`：`_filter_bars` 同构问题精确吻合（报告称 339-343）。

### P1-9 `compute_manual_trade` 双倍加载 —— **属实**

- `manual_trade.py:82-91` 调 `compute_stop_loss`；`stop_loss.py:181` 内部 `df = db.load_market_data(symbol)`；`manual_trade.py:97` `df = db.load_market_data(symbol).copy()` 再次全量加载同一标的。持仓 N 笔 = 2N 次全量读，因果成立。

### P1-10 全表扫元数据找单标的 —— **属实（附影响面限定）**

- `stop_loss.py:49-54` `_load_instrument_metadata` 全表 SELECT；`:195-199` Python 线性扫描匹配 symbol；`db.py:794` `def get_instrument_metadata(self, symbol)` 主键查询确实存在且未被使用；`_load_instrument_metadata` 全仓仅 stop_loss.py:49 定义 + :195 一处调用，「删掉无其他调用方」成立。
- 报告未提及的限定：该扫描只在 `stop_mode != "tight"` 的 else 分支（189-199）执行，tight 档不触发——实际影响面比报告语气略小，但不改变结论。

### P1-11 批量回测 prepare/run 标的重解析 —— **属实**

- `batch_service.py:387`（prepare）与 `:456`（run）均为 `symbols = resolve_batch_symbols(self.db, categories)` 逐字相同，两处独立解析；策略快照（386 行 `build_strategy_snapshot`）冻结了策略而标的未冻结，对比成立，进度与 total_cells 漂移的因果成立。

### P1-12 报价缓存键不一致 —— **属实**

- `data/service.py:198-202`：缓存命中分支 `result[symbol] = cached` 以**调用方原始 symbol** 为键；`:216-221`：网络分支 `result.update(quotes)` 以 provider 归一化 symbol 为键；函数入口（191-198）确无统一归一化。
- 调用方核查：`fetch_latest_quotes` 仅两处上游（`intraday_service.py:548`、`stop_loss.py:113`），symbols 均来自库内已归一化键——报告「当前调用链安全、属潜伏问题」的自评准确，P1 分级不夸大。

### P1-13 `datetime.now()` 混用 —— **属实（11 处逐字吻合）**

- `intraday_service.py` 实测 `datetime.now()` 11 处：538、579、668、732、801、807、820、826、867、1010、1016——报告称「538、579、668、732、801、807、820、826、867、1010 等」共 11 处，精确吻合；同文件 `market_now()` 使用 9 行，门控函数走市场时区属实；`core/calendar.py:37-57` `market_now()` 定义与「解决非中国时区主机偏差」的设计意图逐字吻合。

### P1-14 前端 401 覆盖不全 —— **部分属实（instruments.html 子条不属实）**

- `position_strategies.html:265-269`：`fetch` 后无 `resp.ok` 检查，`data.position_strategies || []` 在 401 JSON 上得 `[]` → 静默空数据，属实。
- `rule_backtest.html:375-383`：`if (!resp.ok) throw new Error(await resp.text())`（377 行）——401 时把 `{"detail":"..."}` JSON 原文当错误文案，属实。
- `batch_backtest.html:420`：`r.json()` 后直接 `renderChips(meta)`（422 行），401 JSON 无 categories 字段 → TypeError，属实。
- **`instruments.html:750-753` 子条不属实**：752 行实际有 `if (!resp.ok) throw new Error(data.detail || '列表加载失败')`，401 时走 catch 显示「加载失败：未登录或登录已过期」（756 行），**不会**显示「已加载 0 个标的」。报告此子条与代码不符（张冠李戴）。条目主旨「仅四页有 401 跳登录、其余无跳转」仍然成立。

### P1-15 批量回测轮询静默冻结 —— **属实**

- `batch_backtest.html:524-548`：`pollProgress` 524 起，528 行 `.then(function (r) { return r.ok ? r.json() : null; })`，530 行 `if (!p) { clearInterval(state.pollTimer); state.pollTimer = null; return; }`——非 200 静默停止、无任何用户提示，逐字精确。

### P1-16 subject_market 无限轮询 —— **属实**

- `subject_market.html:1066` `for (;;)` + `:1067` 2s sleep + `:1074-1075` fetch 异常 `continue` 静默；无退避、无 visibilitychange（1072 行有 401 跳登录，属实但不妨碍条目结论）。
- `:1092`：`statusEl.innerHTML = ... ${st.last_error} ...` 未转义插入，精确吻合。
- `base.html:149-150`：`poll(); setInterval(poll, 30000);` 精确，无页面隐藏暂停。

### P1-17 后端工具函数多份复制 —— **属实（1 处行号偏差）**

- `_category_path` 5 份：`db.py:705`、`routers/instruments.py:137`、`routers/market_view.py:47`、`trend_mcp/server.py:76`、`instrument_admin.py:84`（`_category_path_from_parts`）——全部精确。
- `_number`/`_num` 4 份：`dashboard.py:53`、`intraday_service.py:88`、`market_indicators.py:26`、`market_view.py:96`——全部精确。
- `_date_span`：`data/service.py` 实际在 **400**（报告称 399，1 行偏差）、`instrument_admin.py:39` 精确。
- `symbol_to_code`：`core/symbols.py:31`、`core/display.py:21` 精确。
- `routers/instruments.py:16` 死 import 属实（grep 确认三个公开名仅出现在 import 行）；`dashboard.py:231-240` `except AttributeError: return {}` 兜底精确吻合。

### P1-18 后端死代码 —— **属实**

- `instruments.py:41` `market_store = MarketStore()`：grep 确认全文件仅 19（import）、41（定义）两行，零使用，属实。
- `db.py:1325/1723/1793` 函数内重复 `import pandas as pd`、`:2117` 函数内重复 `import logging`——四行逐一 sed 验证精确。
- `tests/integration/test_intraday_service.py:433-439` `_all_same_score` 定义后全文件无调用（grep 仅 433 一处），属实。
- `src/notify`、`src/backtest`、`src/portfolio`、`src/strategy` 四目录实测只剩 `__pycache__`，属实。

### P1-19 前端重复实现（已漂移） —— **属实（细节两处偏差）**

- `postJson` 漂移：`manual_trade.html:396` 版 402 行有 `if (resp.status === 401) { redirectToLogin(); ... }`；`subject_market.html:1180` 版无 401 处理——**漂移逐字证实**，行号精确。
- `esc` 7 份：实测 6 份 `function esc(`（rule_backtest:366、market_view:320、manual_trade:197、batch_backtest:388、instruments:271、position_strategies:196 全部精确）；第 7 份在 `subject_market.html:120` 但**名字叫 `escHtml`**（箭头函数），报告以「esc 7 份、两种写法」概括，行号对、名字不完全对。
- 止损卡渲染族两份且漂移：`renderStopStats` 实测在 `manual_trade.html:309`（报告称 304）与 `subject_market.html:1146`（报告称 1128-1159）——行号有 5-18 行偏差；两版实现确实不同（字符串拼接 vs 模板字面量、`esc` vs `escHtml`、`fmtPrice` vs `fmtPrice3`、subject_market 版硬止损含「于 {date} 被击穿」文案），「两份且已漂移、口径不一致」结论成立。

### P1-20 前端死代码 —— **部分属实（nextStartDateForRow 误判）**

- `conditionLines`（`rule_backtest.html:421`）：全仓 grep 仅定义处一处，死代码属实。
- `setupSectionObserver`（`instruments.html:671-675`）：`sectionObserver` 全文件仅 261（声明 null）、672-673（重置 null）三处，从不赋非 null——空壳属实。
- 死 CSS 抽查：style.css 中 `ov-param-table`（4 处定义）、`calc-chain`（14 处）、`log-*`/`metric-*`/`fav-*` 族（61 处）在全部模板中零引用，属实（注意：词根 `index-board` 在 `subject_market.html` 有活引用，报告写的 `index-board-*` 一族是否全死未逐一验证）。
- **误判**：报告把 `nextStartDateForRow`（958-960）列入「约 80 行永远空转」——但该函数被**活功能** `runBackfillAll` 在 `instruments.html:1302` 调用（`start_date: nextStartDateForRow(row)`），且 `runBackfillAll` 在 1527 行有事件绑定。真正死的只有 `setRowMessage`/`refreshRowAfterBackfill`/`runBackfill`/点击委托 backfill 分支（rowHtml 677-702 确认不再渲染 `data-role="backfill"` 按钮，此点报告判断正确）。

### P1-21 零测试生产咽喉 —— **属实（抽查 H1/H2/H4）**

- tests/ 全目录 grep `core.jobs`/`core.scheduler`/`daily_market_update_job`/`update_pool_daily`：仅 `tests/api/conftest.py:29-30` 的 `_disable_scheduler`（防 scheduler 在测试中启动的 fixture）——**反而佐证** jobs/scheduler 无直接测试。H1/H2 属实。
- H4（MCP 7 工具只测 2.5 个）：两 MCP 测试文件实测仅覆盖 calc_stop_loss stop_mode 透传与 symbol_detail，trend_dashboard/intraday_dashboard/list_instruments/add_trade/open_positions 无测试，属实。

---

## 三、P2 条目抽查（14 条）

| 条目 | 结论 | 亲核证据 |
|---|---|---|
| P2-1 Database 上帝对象 | 属实 | `db.py` 实测 2119 行（逐字吻合）；`grep -c "    def "` = **105**（逐字吻合）；`_init_tables` 在 99 行 |
| P2-3 RevisionCache 双实例 | 属实 | `routers/subject_market.py:20` 与 `trend_mcp/server.py:91` 各自 `_dashboard_cache = RevisionCache()`，均精确 |
| P2-4 DataService 随处 new | 属实 | `instruments.py:195,559`（带 `provider_priority` 参数）、`stop_loss.py:83,111`、`dashboard_snapshot.py:125`、`server.py:177` 全部精确 |
| P2-5 env 散落 5 处 | 属实 | `provider_tickflow.py:41,82`、`data/service.py:44`、`main.py:47`、`app_logger.py:30` 全部精确；`stock_industry.py:355` `os.environ["TICKFLOW_API_KEY"]`（KeyError 硬失败）vs provider `os.getenv(..., "")`，两种容错级别逐字证实 |
| P2-7 引擎 iterrows | 属实 | `engine.py:64` `bars.iterrows()`、`:69` `all_bars.iloc[: idx + 1]`、`:594-595` 基准 iterrows、`:605` kline payload iterrows，均精确 |
| P2-8 qfq 全量重写 | 属实 | `service.py:274` `rematerialize_qfq`（docstring 自述「全量重写」）、`:382-384` `if raw_updated or factors_changed or qfq_behind:` 触发条件精确 |
| P2-9 revision 全表 COUNT | 属实 | `db.py:848-867` 精确（855-856 行 `SELECT MAX(time)..., COUNT(*)...FROM market_data_qfq`）；五个调用方 `subject_market.py:102,121`、`main.py:209`、`dashboard_snapshot.py:96`、`server.py:115` 全部精确 |
| P2-10 /api/daily 全量加载 | 属实 | `market_view.py:263` `db.load_market_data(...)` 全量读、`:289-290` Python 侧 `tail(limit)` 裁剪、`:307`/`:352` 盘中两遍指标计算，方向与行号吻合 |
| P2-13 仅 2 处耗时日志 | 属实 | `rule_backtest.py:179`、`batch_service.py:547` 两处 `elapsed=` 日志均精确 |
| P2-15 backup_to 仅 2 调用方 | 属实 | grep 全仓：`indicator_builder.py:126` + `scripts/migrate_category_sw2021.py:162`，恰两个 |
| P2-16 SQLite 加固缺口 | 属实 | `db.py:66-76` `_connect` 无 `busy_timeout`、无 `PRAGMA foreign_keys`；`db.py:341,357` 两处 `REFERENCES users(id)` 外键声明精确；`:91` f-string `VACUUM INTO '{dest}'` 精确 |
| P2-18/P2-23 入库文件 | 属实 | `git ls-files` 实测：`.coverage`、`trend-quant.zip`、`.agents/skills/trend-score-calculator.zip` 三者均被跟踪，逐字吻合 |
| P2-22 README MCP 工具数 | 属实 | `README.md:11`：「MCP 服务（/mcp/sse）：**5 个工具**」精确；server.py docstring 与实际注册均为 7 个 |
| P2-12 日志盲区（部分） | 基本属实 | `get_logger` grep 到 7 个文件（含定义处 `audit/app_logger.py`，使用方恰 6 个，吻合）；`calendar.py:50-51` 静默 `except Exception: pass` 带注释（报告称 51-52，1 行偏差）；「41 个模块完全无日志」的大数未逐一复核 |

## 四、附录 A 抽查（7 组，全部属实）

- `core/jobs.py`：`_pool_symbols`(23)、`daily_market_update_job`(50) 存在。
- `core/scheduler.py`：`INTRADAY_SNAPSHOT_CRONS`(18)、`jobs_snapshot`(94) 存在。
- `data/service.py`：`_retry_wait_seconds`(69)、`_non_retryable_provider_error`(78)、`sync_ex_factors`(256)、`ensure_daily_history`(312)、`update_pool_daily`(767) 全部存在。
- `data/provider_tickflow.py`：`_compact_klines_to_dataframe`(114)、`fetch_ex_factors`(251)、`fetch_latest_quotes`(348) 全部存在。
- `db.py`：`load_market_dashboard_history`(824)、`save_ex_factors`(1379)、`load_ex_factors`(1405)、`record_job_run_safely`(2107，与 P2-14 所引 2107-2119 吻合) 全部存在。
- `condition_engine.py:80-86`：`days_since_last_exit` 为 None 的特判注释与逻辑存在，附录补测场景设计对口。
- `scripts/migrate_raw_qfq.py` 文件存在，「破坏性迁移脚本零测试」的补测建议成立。

---

## 五、报告错误清单（按严重度排序）

1. **【严重】P0-1 全条无法复现**：`__dev_set_session` 端点在当前工作区与全部 git 历史（`git log --all -S` 零匹配）中均不存在；`auth.py` 现仅 66 行。报告将其列为头条 P0 且自称「行号均已打开文件核对」，此条显然未经亲核，属事实性错误。行动清单第 1 条同步失效。
2. **【中】P1-14 的 instruments.html 子条张冠李戴**：`instruments.html:752` 实有 `resp.ok` 检查，401 会显示「加载失败：未登录或登录已过期」，不会「显示已加载 0 个标的」。条目主旨不受影响，但该子条应删改。
3. **【中】P1-20 误判 `nextStartDateForRow` 为死代码**：它被活功能 `runBackfillAll`（`instruments.html:1302`，1527 行有事件绑定）调用。「约 80 行永远空转」应下修为约 60 行（setRowMessage/refreshRowAfterBackfill/runBackfill/点击委托分支确死）。
4. **【轻】P1-7 机制描述不准**：第一轮循环体内并无对 `l1_order`/`l2_order` 的赋值，「赋值被 236-237 重新初始化」的说法不成立；正确描述是「第一轮循环除重复打 error 日志外无任何副作用」。
5. **【轻】行号系统性小偏差**（不影响结论）：`_date_span` 报告 399 实际 400；`renderStopStats` 报告 304/1128-1159 实际 309/1146；`engine.py` if 报告 284-286 实际 283-286；`calendar.py` 静默吞报告 51-52 实际 50-51。
6. **【轻】esc「7 份」表述**：第 7 份在 subject_market.html:120 但名为 `escHtml`，「两种写法」实为至少三种（function esc ×6 + 箭头函数 escHtml）。
7. **【轻】审查基线未声明**：工作区相对 HEAD 有 8 个已修改文件 + 1 个未跟踪脚本（backfill_batch_excess_metrics.py），报告未说明以工作区还是 HEAD 为基线（实测行号与工作区吻合，推测为工作区）。

## 六、遗漏补充（核查过程中顺带发现）

1. **logout 在登录墙豁免名单内**（`main.py:312` `_EXEMPT_PATHS` 含 `/api/auth/logout`）：叠加 P1-4 的 GET logout，CSRF 强制退出甚至不要求受害者 session 有效即可触发，报告 P1-4 未点名此豁免，应在该条中补强。
2. **`instruments.html:331` 与 `:770`**：`syncTaskControls` 与另一处 `querySelectorAll('button[data-role="backfill"]')` 也是单行 backfill 死功能的组成部分，P1-20 只列了 1483-1485 的点击委托，清理时这两处漏了会留残渣。
3. **`trend_mcp/server.py:88-91` 注释与实现矛盾**：注释写「shared RevisionCache from services.dashboard」，实际是 `RevisionCache()` 新实例（即 P2-3 的双实例问题）——注释本身会误导后续维护者，P2-3 应附带修正注释。
4. **P1-10 影响面限定**：全表扫描仅在 `stop_mode != "tight"` 分支执行，tight 档无此开销——修复优先级可略降，但不改变「换主键查询」的建议。
5. **P0-6 的连锁事实**：`pyproject.toml:46` 的 `asyncio_mode` 警告在每次 pytest 运行时都出现（实测确认），意味着仓库里若有真正的 async 测试，当前并未按 asyncio 语义执行——报告只提了「警告」，未提「async 测试可能根本没被正确运行」这一更深风险（本次未逐一检查是否存在 async 测试函数）。

## 七、总评

- **整体可信度：8 / 10**。
- 30 余条带行号论断中，约 28 条经亲核精确属实或基本属实（P0-2~P0-6、P1-1~P1-13、P1-15~P1-19 及 P2 抽查 14 条、附录 A 抽查 7 组），多条做到行号逐字吻合，P0-6 经实测完整复现（702 collected + 2 ImportError + asyncio 警告），因果推断（P0-2 无鉴权、P0-4 WAL、P0-5 NameError 降级路径、P1-12 键分裂）逐条验证成立，未发现分级夸大（P0-1 除外）。
- 但头条 P0-1 是**无法复现的事实性错误**，且与报告「所有关键结论由主审查人亲核代码验证」的自述直接冲突，严重拉低第一可信度印象；另有 2 处中等级别的子条错误（P1-14 instruments、P1-20 nextStartDateForRow）。
- **交付结论：有条件可交付**——发布前必须：(1) 删除或改写 P0-1 及行动清单第 1 条（改为「确认已不存在」的闭环说明）；(2) 修正 P1-14 instruments.html 子条；(3) 修正 P1-20 的死代码清单（剔除 nextStartDateForRow，补 331/770 行）；(4) 顺手修订第五节列出的行号小偏差。完成上述修订后可作为后续修复工作的可靠基线。
