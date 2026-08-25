# 主审查报告事实核查（review-a-factcheck）

- **核查日期**：2026-08-24
- **核查对象**：`docs/code-review-2026-08-24/review-report.md`（下称"报告"）
- **核查方式**：只读代码逐条比对（文件:行号 + 代码内容），并用 `.venv/Scripts/ruff.exe check src scripts --select F401,F841,F811` 对报告 §5 的死代码清单做了全量复核。未运行 pytest。
- **核查范围**：全部 6 条 P0 + S/Q/C 系列全部条目 + §4/§5 全部条目 + §7/§8/§9/§10 抽查，合计约 50 条论断。
- **合规说明**：核查中曾误用 `uv run` 触发依赖同步，在项目根目录生成了 `uv.lock`（git untracked），已删除恢复原状；`.venv`/`.uv-cache` 为环境缓存，不涉及项目源文件。除此之外未对项目做任何写操作。

---

## 1. P0 级条目（全量核查）

### P0-1 instruments.py 兜底分支 NameError —— 属实

- `src/app/routers/instruments.py:102` 确为 `for item in [*db.list_instrument_metadata(), *_config_items()]:`。
- 该文件 import 列表（20–30 行）只从 `services.instrument_admin` 导入了 `_category_path_from_parts`、`_config_name_map` 等 9 个名字，**不含 `_config_items`**；模块内（含 `__future__` 注解延迟求值，不影响运行时 NameError）无定义。全项目 grep 确认 `_config_items` 仅定义于 `instrument_admin.py:62`。
- 触发路径属实：`_category_options()`（95 行起）在 `list_instrument_categories()` 返回空时进入兜底分支。
- 补充佐证报告建议的合理性：`instrument_admin.py:62-63` 的 `_config_items()` 本身就是 `list_instrument_metadata()` 的薄包装，兜底分支 `[*db.list_instrument_metadata(), *_config_items()]` 即便修好 import 也会把同一份元数据装载两遍——报告"该兜底本身在重复 `_config_items` 的逻辑"的批评准确。
- 定级【高】合理：全新部署/分类表被清空即 500，且正常路径永不触发、测试无覆盖。

### P0-2 MCP 五个工具零鉴权 —— 核心事实属实，"frp 暴露"引用不成立（部分属实）

- `src/trend_mcp/server.py:48-51` 确为 `FastMCP("trend-quant", transport_security={"enable_dns_rebinding_protection": False})`。
- `src/app/main.py:312-313`：`_EXEMPT_PREFIXES = ("/static", "/mcp")`，且 409 行 `app.mount("/mcp", _mcp_app.sse_app())`——`/mcp` 整体在登录墙之外，属实。
- 工具鉴权分布属实：`trend_dashboard`/`intraday_dashboard`/`symbol_detail`/`calc_stop_loss`/`list_instruments` 五个工具入参无凭据（server.py:98-412）；`add_trade`（420 行）/`open_positions`（462 行）逐次调 `tr.authenticate(username, password)`。
- **问题**：报告称"当前部署经 frp 暴露公网（README/部署文档）"。全项目（README.md、docs/、config/、scripts/、Makefile）grep `frp`/`公网`/`反向代理`，**除本次审查目录外无任何 frp 记录**。实际部署脚本是 `scripts/deploy.sh`：云服务器 + nginx 监听 80 + 公网 IP（`proxy_pass http://127.0.0.1:8000`，150-178 行）——**公网可达的结论本身成立（且 /mcp 在同一 server 块下随之暴露），但"frp"及"README/部署文档"的引用来源不存在**。
- 建议修正：把"经 frp 暴露公网（README/部署文档）"改为"按 `scripts/deploy.sh` 的部署方式（nginx + 公网 IP/域名）/mcp 随主站公网可达"；若作者另有未入库的 frp 部署事实，应注明信息来源。
- 其余风险描述（`intraday_dashboard` 消耗 TickFlow 实时报价配额）与代码行为一致（server.py:177-187 每次都新建 DataService 拉实时报价）。定级【高】合理。

### P0-3 SQLite 无 busy_timeout —— 属实

- `src/data/storage/db.py:66-76` `_connect()` 只执行 `PRAGMA journal_mode=WAL`，行号精确。
- 全项目（src/ scripts/ tests/ config/）grep `busy_timeout` 零命中。
- 并发写者列举抽查属实：批量回测 `_flush_counts` 每格一次 UPDATE（`rule_backtest/batch_service.py:535`）+ 每格 `_write_cell`；盘中快照每 5 分钟（`core/scheduler.py:17-24` INTRADAY_SNAPSHOT_CRONS，交易时段 */5）；登录滑动续期写 sessions（`services/auth.py:74` `touch_session`）；日更 16:30（`config/app.yaml:5` `update_time_after_close: "16:30"`）。
- 建议（`PRAGMA busy_timeout = 5000`）与 `_connect()` 结构完全兼容，一行可落地。定级【高】合理。

### P0-4 登录无速率限制 —— 属实

- `src/app/routers/auth.py:37-52` `/api/auth/login` 无任何频限/失败计数，逐行确认。
- pbkdf2 200k 属实（`db.py:22-23`：`_PASSWORD_ALGO = "pbkdf2_sha256"`、`_PASSWORD_ITERATIONS = 200_000`）。
- MCP `add_trade`/`open_positions` 逐次密码鉴权且无频限，属实。
- 定级讨论：pbkdf2(200k) 使每次在线尝试服务器端耗时 ~0.1s 量级，对强密码的在线爆破实际不可行；但弱密码/撞库仍无防护，且报告建议仅为进程内滑动窗口（~40 行），成本极低。【高】可接受，但标注"单用户系统"上下文后定为【中】也说得过去——不构成定级错误。

### P0-5 session token 明文落库 —— 属实

- `src/services/auth.py:45-46`：`token = secrets.token_hex(32)` 后 `db.create_session(..., token, ...)`；`db.py:1467-1473` `create_session` 将 token 原文 INSERT 进 `sessions` 表。
- 报告给出的四个改动点（`create_session`/`get_session_user`/`delete_session`/`touch_session`）均在 db.py 中存在，方案可落地。定级【中】合理。

### P0-6 symbol_detail indicators 未截尾 —— 属实

- `src/trend_mcp/server.py:237` 在全历史 `df` 上调 `compute_market_indicators`；248 行 `df = df.tail(n)` 之后，`dates`（249）、`candles`（263-266）、`volumes`（268-270）均截尾，而 271 行 `"indicators": indicators` 原样放入 payload。
- `services/market_indicators.py:68-122` 确认 `compute_market_indicators` 返回的 ma/atr/boll/macd/bias/volume_ma/rsi/trend 全部为输入 df 全长的序列。
- 报告引用的注释（server.py:223-226 "Output arrays are tailed afterwards to the requested number of days"）逐字存在，确与实际行为不符。
- 配套事实属实：`tests/unit/test_mcp_symbol_detail.py` 只断言 `len(payload["dates"]) == DAYS`（118/144/166 行）等，无任何 indicators 数组与 dates 长度一致性断言。
- 建议（截尾 indicators + 补长度断言）可落地。定级【中】合理。

---

## 2. 安全专项 S-1 ~ S-7（全量核查）

| # | 结论 | 证据 |
|---|------|------|
| S-1 | 属实 | `auth.py:45-51` `set_cookie(..., httponly=True, samesite="lax")` 无 `secure`；`main.py:365-366` 续期 cookie 字符串同样无 `Secure`。补充：`deploy.sh` 默认纯 HTTP（HTTPS 需自行 certbot），报告"若经 HTTPS 暴露再加、本地 HTTP 不加"的条件化建议与此兼容，表述得当 |
| S-2 | 属实 | 全站仅 `SameSite=lax`（上述两处），无 CSRF token 机制 |
| S-3 | 属实 | `auth.py:55-60` `@router.get("/api/auth/logout")`，且模块 docstring（4-5 行）自述"退出为 GET 链接" |
| S-4 | 属实 | `db.py:34-45` `verify_password` 尾部 `return hmac.compare_digest(stored, candidate)` 明文兜底分支逐字存在 |
| S-5 | 属实 | `db.py:91` `conn.execute(f"VACUUM INTO '{dest}'")`；dest 当前由 `backup_to` 内部生成（85-88 行），报告"当前无注入面、建议提前校验"的定性准确 |
| S-6 | 属实 | `src/app/routers/` 与 `main.py` 中 grep `is_admin` **零命中**——管理类接口确实不校验角色；`is_admin` 仅在 `services/auth.py:76` 解析后随 user 返回 |
| S-7 | 属实 | 项目根目录无 `requirements.txt`/`uv.lock`/`poetry.lock`/`Pipfile.lock`（git tracked 文件确认；核查中曾短暂出现 uv.lock 系核查方误操作，已删除） |

XSS 专项：报告"94 处 innerHTML"实测 95 处（`grep -ro innerHTML web/templates/ | wc -l` = 95），误差 1 可忽略；`eval(`/`new Function`/内联 `onclick=` 与 Jinja `| safe` 均为 0 命中，属实。模板转义函数实测为 6 份 `function esc`（batch_backtest/instruments/rule_backtest/position_strategies/manual_trade/market_view）+ 1 份 `const escHtml` 箭头函数（subject_market.html:120）——"7 个模板各一份 esc"计数正确，但其中一份名字是 `escHtml`，措辞可更精确。

---

## 3. 性能专项 Q-1 ~ Q-11（全量核查）

| # | 结论 | 证据与备注 |
|---|------|-----------|
| Q-1 | 属实 | `src/rule_backtest/batch_service.py:512` 每格 `self.engine.run(...)`；`engine.py:49` 每次 run 新建 `ValueResolver` 并 `set_context_bars(all_bars)` 全序列重算。备注：报告只写 `batch_service.py` 未给目录，实际在 `src/rule_backtest/` 下（全项目唯一，不构成误导）；engine.py 行号 49-52 精确 |
| Q-2 | 属实 | `routers/instruments.py:428-431`：`for symbol in sorted(known_symbols)` 循环内 `db.get_market_data_summary(symbol)`（报告写 429-431，偏差 1 行）。`get_market_data_summary` 每次 `_connect()` 新开连接（db.py 内确认） |
| Q-3 | 属实 | `db.py:848-867` `get_market_dashboard_revision`（报告写 854-867，实际函数起始于 848，引用区间覆盖 COUNT 查询行）；`SELECT MAX(time), COUNT(*) FROM market_data_qfq` 逐字存在；`routers/subject_market.py:121` 每请求调用属实。建议与代码现状兼容（revision 第 4 元素已含 data_versions 版本号，865 行） |
| Q-4 | 属实 | `trend_daily` 主键 `PRIMARY KEY (symbol, time, param_set)`（db.py:316-328），无 `(param_set, time)` 索引（全部 CREATE INDEX 已枚举核对）；`load_trend_daily_bulk`（db.py:1778-1790）按 `param_set=? AND time>=?` 过滤，确实无法命中主键前缀 |
| Q-5 | 属实 | `data/service.py:274-305` `rematerialize_qfq` 为全量 DELETE+INSERT 式重写（`replace_history`），行号精确 |
| Q-6 | 属实 | `services/stop_loss.py:195-199` 对 `_load_instrument_metadata(db)` 逐行比对；`_load_instrument_metadata`（49-54 行）装载整张表；`db.get_instrument_metadata(symbol)` 单查方法存在（db.py:794），建议可直接落地 |
| Q-7 | 属实 | `services/manual_trade.py:82` 先调 `compute_stop_loss`（其内部 `stop_loss.py:181` `db.load_market_data(symbol)`），97 行再次 `db.load_market_data(symbol).copy()`——同一标的两次全历史读，属实 |
| Q-8 | 属实 | `main.py:280-296` `AssetVersionMiddleware.__call__` 每个 HTTP 请求 `style_file.stat()` 一次，逐行确认 |
| Q-9 | 属实 | `batch_service.py:535` `self._flush_counts(batch_id, counts)` 在每格循环尾部无条件执行 |
| Q-10 | 属实 | `data/service.py:824-836` payload 含 `"results": results` 全量逐标的明细并经 `record_job_run_safely` 落库；`db.py:1588-1598` `get_latest_job_run` 每次 `json.loads(d["payload"])` 全量解析 |
| Q-11 | 属实 | `routers/instruments.py` 全部路由 `async def`（171/180/186/215/403/482/543…），体内同步 sqlite/pandas 调用；看板重计算确有 `run_in_threadpool` 注释（subject_market.py:129-130 注释佐证）。"单用户影响小"的定级（信息）合理 |

---

## 4. 架构与重复代码（§4 全量核查）

- **§4.1.1（公式双写）属实**：`intraday_service.py:245` `compute_intraday_trend_cached` 体内 tanh（327-328 行 `np.tanh(bias_mix/2.0)*100` 等）、vol_ratio/3.0 截断、ER `np.clip`、`w_er` 指数、最终 `np.clip(price_direction*confidence, -100, 100)` 全部内联；一致性测试 `tests/unit/test_intraday_trend_consistency.py` 存在。
- **§4.1.2（聚合层平行）属实**：`_number`/`_ma5`/`_strength`/`_priority`/`_key_tuple`/`_macd_counts`/`_sort` 在 `dashboard.py`（53/61/66/72/79/224/267）与 `intraday_service.py`（88/459/494/500/507/971/960）各一份。
- **§4.2（JobManager 三份）属实**：`instrument_jobs.py` 三个类位于 53/309/505 行（文件共 717 行，报告写"53-718"偏差 1 行）。"状态全在内存、重启后显示空闲"与代码一致（status dict + daemon thread 结构抽查确认）。
- **§4.3（工具函数拷贝）**：
  - `safe_float` 3 份属实（`core/trend.py:44` 默认 0.0；`provider_utils.py:9`、`rule_backtest/indicators.py:10` 默认 None——默认值语义不同属实）；
  - `_category_path` 4 份属实（`db.py:705`、`instruments.py:137`、`market_view.py:47`、`server.py:76`，行号全部精确）；
  - 后缀互转 4 处属实（`provider_tickflow.py:47/54` + `stock_industry.py:61/69`）；
  - `esc()` 7 份：计数正确但其中一份名为 `escHtml`（见 §2 备注）；
  - 薄包装属实（`instrument_admin.py:27-36`、`market_view.py:39-44` 逐行确认纯转发）。
- **§4.4**：`db.py` 实测 2119 行（精确命中）；`intraday_service.py:25` `from core.trend import _detect_trend_phase` 私有名跨模块导入属实；`_ordered_providers`（`data/service.py:115-120`，报告写 115-119）与 `params_hash`（`indicator_builder.py:45-49`）grep 全项目零调用方，属实；`_retry_wait_seconds` 解析中文文案「请 X ms 后重试」（`service.py:69-75`）逐字属实。

## 5. 死代码清单（§5 全量核查，ruff 复核）

以与报告相同的 `ruff check src scripts --select F401,F841,F811` 全量复核：

1. `stock_industry.py:224-238` —— **属实，措辞轻微夸大**。第一段 for 循环（224-234）只做「名字含 `-` 则 error 日志」，随后 236-238 行重新声明 `l1_order`/`l2_order` 并重跑同一校验+日志（246-248）——构建逻辑上第一段完全无效，但它并非零副作用：命中剔除分支时会产生**重复的 error 日志**。"整块死代码"结论成立。
2. `_ordered_providers` 无调用方 —— 属实（行号偏差 1：实际 115-120）。
3. `params_hash` 无调用方 —— 属实。
4. `engine.py:9` `latest_field` 未用 —— ruff 确认（F401, engine.py:9:38）。
5. `value_resolver.py:121` 冗余三元 —— 属实（两分支同为 `indicators.field_series(bars, ...)`，仅实参名差异，语义完全相同）。
6. `intraday_service.py:283` `prev_close` 未用 —— ruff 确认（F841）。
7. `instruments.py` 六个未用导入 —— ruff 确认（4:8 threading、6:20 Callable、8:18 pandas、16:26/44/59 三个 core.symbols 名字）。
8. `market_view.py` 四个未用导入 —— ruff 确认（3/6/12/26 行）。
9. `dashboard.py:23` 两个未用导入 —— ruff 确认。
10. 其余四文件各一处 —— ruff 确认（instrument_admin.py:13、market_indicators.py:9、intraday_service.py:15,41、migrate_category_sw2021.py:34）。
11. `db.py:1325,1723,1793` 函数内 `import pandas` —— 抽查 1325、1793 属实（模块顶部 14 行已有 `import pandas as pd`）。
12. tickflow 存根 —— **部分属实（行号张冠李戴）**。`fetch_trading_calendar` 返回 `[]` 确在 433-434；但 `fetch_minute_history` 的 raise 在 **305-313 行**，不在 433-434。事实本身成立。
13. `indicator_builder.py:137-144` `del end_date, lookback` —— 属实（137 行签名、144 行 `del`）。
14. `instruments.py:88-93` 连续 6 个空行 —— 属实（逐行确认）。

补充：ruff 另发现报告未列的 `rule_backtest/service.py:6` `pathlib.Path` 等个别未用导入——报告 §5 非全量枚举，不构成错误。

---

## 6. 正确性与口径 C-1 ~ C-8（全量核查）

| # | 结论 | 证据 |
|---|------|------|
| C-1 | 属实 | `data/service.py:343-346`（报告写 344-346）：`except DataProviderError: fetched = pd.DataFrame()  # 区间无新 bar...视为无增量`，逐字确认；`update_pool_daily` 失败计数只认显式失败（820-836 行），空响应确实走 success 路径 |
| C-2 | 属实 | `core/jobs.py:63` `today = date.today()`；`core/calendar.py:146,158` 两处 `day or date.today()`；`routers/rule_backtest.py:72-73` `date.today()`；`market_now()` 体系并存（subject_market.py:121 在用） |
| C-3 | 属实 | `db.py:1324` `load_market_data(..., price_mode="qfq")` 默认 qfq；`trade_records.py:74-92` `create_trade` 将用户成交价原文存库、区间校验复用 `compute_stop_loss`（其 `stop_loss.py:181` 读 qfq）。口径错配链条成立 |
| C-4 | 属实 | `rule_backtest/metrics.py:97` `annual_return = (1.0 + total_return) ** (252.0 / n_days) - 1.0`（报告写 96-97，偏差 1 行） |
| C-5 | 属实 | `provider_utils.py:55` 缺 time 列时 `pd.Series([datetime.now()] * len(data))` 逐行填充，逐字确认 |
| C-6 | 属实 | `db.py:1399-1403` `replace_ex_factors`：`with self._connect()` 内 DELETE 后出块（事务随 commit 结束），再调 `self.save_ex_factors(...)` 第二个连接第二个事务 |
| C-7 | 属实 | `intraday_service.py:786-789` `prev_close = safe_float(closes.iloc[-1], ...)` 取 tail 末根算 `return_1d`；`has_persisted_today_bar`（158 行定义）用于 201/709 行但**未**用于 returns 计算块，报告描述准确 |
| C-8 | 属实 | `dashboard.py:319-321` 缓存命中分支 `trend_lookup.get((symbol, str(t)[:10]), np.nan)` 静默 NaN，无日志 |

---

## 7. §7 ~ §10 抽查

- §7.2 无健康检查端点 —— 属实（`src/app/`  grep `healthz|/health|/api/health` 零命中）。
- §7.4 调度器 misfire 只兜启动 —— 属实。`core/scheduler.py:44-50` 注释自述"进程完全离线造成的错过由 app.main 的启动补偿兜底"；misfire_grace_time=2h 只兜"触发错过"，任务执行失败后确无重试逻辑。
- §7.5 `backup_to(keep=3)` —— 属实（db.py:78 默认 `keep: int = 3`）。
- §7.6 `_market_symbols_cache` 跨进程不失效 —— 属实（db.py:57-61 注释自述 "writes from OTHER processes do not invalidate this cache"）。
- §8.1/8.2/8.3（测试缺口对应 P0-1/P0-3/P0-6）—— 属实（见各 P0 条）。
- §8.4 `_aggregate_daily`（dashboard.py:83）/`_assign_envelope`（188）存在，缺直接单测的说法与 tests 目录结构一致。
- §8.6 `_daily_update_catchup` 存在（main.py:182），tests 中无对应命名单测文件，属实。
- §8.8 `test_tushare_scripts.py` 存在、其余脚本无对应测试文件，属实。
- §9.1 README 过时 —— 属实：README:11 仍写"MCP 服务（/mcp/sse）：5 个工具"，server.py 实际 7 个；README 架构树的 services 列举（22-23 行）确实缺 stock_industry/manual_trade/stop_loss/trade_records/auth/batch_service 等。
- §9.2 `docs/architecture-review-2026-08-01.md` 存在，属实。
- §9.3 `config/app.yaml:13` 注释"【会员状态】当前为付费年会员"与 19 行 `plan: starter` 并存，provider `plan != "starter"` 直接 raise（`provider_tickflow.py:39-40`），属实。
- §9.4 `core/indicators.py:23`、`core/trend.py:27` 两处"future P1"注释逐字存在；`data/indicator_store.py` 已落地（193 行 `get_series`），属实。
- §9.5 git 状态 4 份文档 D、多份 M —— 与核查时 `git status --short` 输出完全一致，属实。
- §10.1 `DELETE /api/runs/{batch_id}`（batch_backtest.py:302）→ `db.delete_batch_run`（db.py:2037-2049）物理级联删除 run+cells+features，无回收站，属实。
- §10.2 `_rule_jobs`（rule_backtest.py:23）TTL=1800s（25 行，即 30 分钟），404 文案"回测任务不存在或已过期"（212/232 行），属实。
- §10.6 `GET /api/dashboard/refresh-status` 存在（subject_market.py:146），属实。

---

## 8. 发现的报告自身问题（汇总）

| # | 位置 | 问题 | 性质 | 修正建议 |
|---|------|------|------|----------|
| F-1 | P0-2 | "当前部署经 frp 暴露公网（README/部署文档）"——全项目无 frp 记录，README/部署文档（deploy.sh）只描述 nginx+公网 IP | **部分属实**（暴露结论对、引用来源错） | 改为引用 `scripts/deploy.sh`（nginx listen 80、公网 IP），或注明 frp 为代码库外的部署事实 |
| F-2 | §0 范围、§8 | "70 个测试文件" | **不属实** | 实测 61 个 `test_*.py`（含 conftest/`__init__` 共 68 个 .py）。改为"61 个测试文件（68 个 .py）" |
| F-3 | §0 范围 | "src/ 约 1.2 万行 Python" | **不属实（偏差 ~40%）** | 实测 `find src -name '*.py' | xargs wc -l` = 16,816 行，应写"约 1.7 万行" |
| F-4 | §5 第 12 条 | `fetch_minute_history` 永远 raise 的行号写成 433-434 | 部分属实（事实对、行号错） | 433-434 是 `fetch_trading_calendar`；`fetch_minute_history` 的 raise 在 provider_tickflow.py:305-313 |
| F-5 | §4.3 / §2 | "esc() 7 个模板各一份" | 基本属实、措辞不精确 | 实为 6 份 `function esc` + 1 份 `const escHtml`（subject_market.html:120） |
| F-6 | §5 第 1 条 | "第一段完全无效" | 属实但轻微夸大 | 第一段在命中剔除分支时会产生重复 error 日志，宜表述为"构建逻辑无效、仅剩重复日志副作用" |
| F-7 | §11 路线图 vs §1 编号 | 编号 P0-2、P0-5 的条目在路线图中被排进 **P1** 栏 | **自相矛盾（命名与优先级不一致）** | 要么把 P0-2/P0-5 改编号为 S-x 并在 P1 引用，要么在路线图 P0 栏说明"P0-2/P0-5 因改动量大顺延至 P1"。当前读者会困惑"P0 到底包不包括这两条" |
| F-8 | Q-1/Q-9 | 引用 `batch_service.py` 未给目录 | 轻微 | 全项目唯一（`src/rule_backtest/batch_service.py`），不构成误导，建议补全路径 |
| F-9 | §2 | "94 处 innerHTML" | 可忽略误差 | 实测 95 处 |
| F-10 | §0 范围 | "scripts/（12 个脚本）" | 基本属实 | scripts/ 下为 12 个 .py + deploy.sh + run_dev.ps1 = 14 个文件；若只算 Python 脚本则 12 准确 |

**行号精度总评**：抽查的 ~50 处引用中，除 F-4 外全部命中或偏差 ≤1-2 行（Q-2、Q-3、§4.2、§5.2、C-1、C-4），考虑到工作区有未提交改动，此精度可接受。

## 9. 定级合理性评估

- 无明显夸大或低估的定级。P0-1（高）触发条件苛刻但后果是硬 500 且静默，合理；P0-2（高）在公网可达前提下合理（F-1 修正引用后结论不变）；P0-3（高）多写者属实、修复成本一行，合理；P0-4（高）偏严——pbkdf2(200k) 已大幅抬高在线爆破成本，单用户场景定"中"亦成立，但不算错误；P0-5/P0-6（中）合理。
- C/Q/S 各条定级与其影响面、修复成本的匹配度抽查均合理；S-1 的条件化建议（HTTPS 才加 Secure）与 deploy.sh 默认纯 HTTP 的现状兼容，未见"建议与代码现状冲突"的条目。

## 10. 总体意见

**结论：需修改后交付（改动量小）。**

报告的事实准确率很高：50 余条抽查中仅 2 条不属实（测试文件数、src 行数，均为范围统计而非技术论断）、1 条部分属实（frp 引用）、1 条行号张冠李戴。所有 6 条 P0 的核心技术事实全部属实，建议均可落地。

**必须修改（交付前）**：

1. F-1：P0-2 的 frp 引用改为 deploy.sh/nginx 或注明外部信息来源——这关系到 P0-2 定级的证据链；
2. F-2：测试文件数 70 → 61；
3. F-3：src 行数 1.2 万 → 约 1.7 万；
4. F-7：P0 编号与路线图 P1 归类的矛盾需要一句话澄清。

**建议修改（不阻塞）**：F-4（行号）、F-5（escHtml 措辞）、F-6（"完全无效"措辞）、F-8（补全 batch_service 路径）。
