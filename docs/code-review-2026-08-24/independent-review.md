# 独立代码审查报告 · 2026-08-24

审查范围：`src/`（全部模块）、`web/templates`（内联 JS）与 `web/static`、`scripts/`、`tests/`、`config/`、根目录文档与构建配置。
方法：逐文件人工精读 + `ruff check src scripts`（.venv 内 ruff，235 条诊断）辅助。本报告为独立结论，未参考本目录下任何已有审查文档。

严重级别定义：**高** = 正确性/安全/数据完整性风险，可能在生产直接暴露；**中** = 明确缺陷或显著的可维护性/性能/运维问题；**低** = 风格、卫生、轻微冗余。

---

## 一、正确性 Bug

### B1【高】`_category_options` 引用未导入的 `_config_items`，分类表为空时 500
- 类别：正确性 / 潜在异常
- 位置：`src/app/routers/instruments.py:102`
- 证据：`for item in [*db.list_instrument_metadata(), *_config_items()]:`，但文件顶部 import 列表（20-30 行）没有 `_config_items`；ruff 报 `F821 Undefined name`。该分支在 `instrument_categories` 表为空时才会走到（95-99 行 `if rows: return rows`），所以日常被掩盖——一旦生产分类表被清空/新部署空库，`/instruments/api/categories`、新增标的（251 行）、编辑标的（500 行）全部 NameError 500。
- 建议：补导入或直接使用 `get_db().list_instrument_metadata()`（本函数 102 行刚查过同一表，`_config_items()` 是纯重复查询），并补一个「空分类表」的 API 测试。

### B2【高】除权因子同步存在「空结果擦除存量因子」风险
- 类别：正确性 / 数据完整性
- 位置：`src/data/service.py:264-272`（`sync_ex_factors`）、`src/data/provider_tickflow.py:283-297`
- 证据：vendor 对某标的返回空 entries 时，`fetch_ex_factors` 仍写入 `factors_by_symbol[symbol] = []`（provider 287-297 行无条件赋值）；`sync_ex_factors` 中 `factors_equal(stored, [])` 为 False → `db.replace_ex_factors(symbol, [])`（先 DELETE 全量再插空，db.py:1399-1403）→ 随后 `rematerialize_qfq` 用空因子重算 qfq，**历史除权全部丢失，价格口径静默错误**。vendor 临时故障/接口字段变更都会触发。
- 建议：对「存量有因子、本次拉到空」的情况加保护（拒绝覆盖并告警），或对空因子结果要求二次确认/延迟到下一轮。

### B3【高】生产环境没有创建登录用户的任何入口
- 类别：正确性 / 运维能力
- 位置：`src/data/storage/db.py:1428`（`create_user`）
- 证据：全仓 grep `create_user` 的调用方只有 `tests/`；无 CLI 脚本、无管理端点、文档（README/docs）也没有首次建用户指引。2026-08 登录墙上线后，新部署的实例**无法产生第一个用户**，只能手工 sqlite3 写库（且要自己会算 pbkdf2 格式）。
- 建议：提供 `scripts/create_user.py`（交互输入密码，`db.create_user`），并在 README「运行」节补一行。

### B4【中】`build_sw_tree` 第一段循环是整段死代码
- 类别：冗余/死代码（复制粘贴残留）
- 位置：`src/services/stock_industry.py:224-248`
- 证据：224-225 行初始化 `l1_order/l2_order`，226-235 行的 for 循环对每个 code 做校验后 `continue` 或自然结束，**不写任何结果**；236-237 行又把 `l1_order/l2_order` 重新初始化，239-261 行的第二个循环做同样校验并真正填充。第一段循环完全无效，疑似编辑时复制粘贴后忘了删。
- 建议：删除 224-235 行。

### B5【中】盘中快照 DB 懒加载失败后永久返回 None
- 类别：正确性 / 边界条件
- 位置：`src/services/dashboard_snapshot.py:49-60`
- 证据：`latest_snapshot()` 先置 `_snapshot_loaded = True` 再尝试 `load_dashboard_snapshot()`；一旦该次 DB 读取抛异常（51-58 行），`_snapshot` 永远停在 None，之后所有调用直接返回 None 不再重试——直到下一次重算成功或进程重启。表现：服务重启后看板「恰好」在 DB 抖动时丢快照，则全天不回显。
- 建议：加载失败时不要置 `_snapshot_loaded`（或允许下次调用重试）。

### B6【中】SQLite 连接未设置 `busy_timeout`，并发写直接报 "database is locked"
- 类别：正确性 / 并发
- 位置：`src/data/storage/db.py:66-76`（`_connect`）
- 证据：每条连接只设了 `PRAGMA journal_mode=WAL`，没有 `PRAGMA busy_timeout`。系统存在多个并发写者：批量回测线程（每格一次 commit，batch_service.py:533-535 + `_flush_counts`）、手工交易写库、登录/滑动续期写 sessions、16:30 日更、盘中快照落库。WAL 下写-写仍互斥，默认 busy_timeout=0 意味着撞上即 `OperationalError`，例如用户在批量回测跑批时平仓一笔交易。
- 建议：`sqlite3.connect(..., timeout=10)` 或连接后 `PRAGMA busy_timeout=10000`。

### B7【中】时区口径混用：naive 本地时间 vs 市场时区
- 类别：正确性 / 时区一致性
- 位置：`src/data/storage/db.py:48-50`（`_dt_str` 依赖服务器 localtime）、`src/app/main.py:880`（dashboard_snapshot `datetime.now()`）、`src/app/routers/rule_backtest.py:72-74`（`_cap_end_date` 用 `date.today()`）、`src/data/intraday_service.py:668, 538`（`datetime.now()`）
- 证据：`market_now()`（core/calendar.py:37-57）明确用配置的 Asia/Shanghai，且 docstring 说明「非 CN 主机上两者不同」；但 sessions 过期、快照 computed_at、回测 end_date 截断、看板 `max_bar < today` 比较（subject_market.py:122-124）都用裸 `datetime.now()`/`date.today()`/SQLite `localtime`。一旦部署到 UTC 主机（常见云默认），会话过期判断、快照新鲜度、回测日期截断全部偏移 8 小时。
- 建议：统一经 `market_now()` 取时间；SQLite 默认值改由应用层写入市场时区时间。

### B8【中】除权因子日期口径与日 K 不一致（UTC vs 上海）
- 类别：正确性 / 时区口径
- 位置：`src/data/provider_tickflow.py:294`（ex_factors 用 `tz=timezone.utc` 取日期）vs `122-123`（日 K 用 `tz_convert("Asia/Shanghai")`）
- 证据：同一 vendor 毫秒时间戳，日 K 落上海日期、因子落 UTC 日期。若 vendor 时间戳是上海当日 00:00（=UTC 前一日 16:00），因子日期会比 K 线日期**早一天**，`compute_qfq`（core/adjustment.py:93-96，`bisect_left(factor_dates, day)`，`ex_date >= t` 参与除权）会把除权提前一天应用，除权前一日的 qfq 价格被多除一次。代码注释声称「已验证」，但两套口径并存本身就是隐患。
- 建议：统一用上海时区换算日期；加一个「除权日前后 qfq 连续性」的回归测试锁定。

### B9【中】批量回测窗口与标的集合存在时间与口径漂移
- 类别：正确性 / 边界条件
- 位置：`src/rule_backtest/batch_service.py:386-388`（prepare 时 `resolve_batch_symbols`）与 `456`（run 时再次 `resolve_batch_symbols`）
- 证据：策略被快照冻结，但**标的集合没有**——POST /run 到线程真正执行之间若有人编辑分类/启停标的，实际跑的宇宙与 ETA/总数不符；`total_cells` 按旧集合算，进度可能超过 100%。另外批次锚定了 `data_anchor_date`，但逐标的 `load_history` 在运行期实时读库，跑批期间撞上 16:30 日更，前后标的的数据截止日不一致（data_version 只记录不强制）。
- 建议：prepare 时把标的清单一并冻结进 `config_json`；run_batch 开始时校验 data_version 未变，否则标记/中止。

### B10【中】月度收益与热力图丢失首月
- 类别：正确性 / 口径
- 位置：`src/rule_backtest/metrics.py:226-228`（`monthly_returns`）、`250-251`（`compute_monthly_heatmap`）
- 证据：`monthly.pct_change().dropna()` 使第一个月恒为 NaN 被丢弃——回测起点所在月的收益在月度收益表和热力图里都看不到（该月往往是信号最集中的区间）。年度收益（197-214）以首日净值为基准包含了首年，两个口径不一致。
- 建议：首月相对首日净值计算（`monthly.iloc[0] / first_equity - 1`），与年度口径对齐。

### B11【中】同一标的的「历史起点」三条路径三种口径
- 类别：正确性 / 口径不一致
- 位置：`src/services/instrument_jobs.py:432, 628`（新增/导入回补硬编码 `date(2020, 1, 1)`）、`src/data/service.py:571`（批量回补缺省 `date(2020, 1, 1)`）、`src/core/jobs.py:84` + `core/strategy_config.py:38`（日更起点取 `backtest_start_primary`，默认 `2025-01-01`）
- 证据：手动新增/ETF 导入的标的历史从 2020 回填；而「启用但从未回补」的标的由每日 16:30 任务从 2025-01-01 开始拉。用回测参数（backtest_start_primary）当日更起点本身也是概念误用。结果：标的池内历史深度参差，看板/回测横截面不可比。
- 建议：定义独立的 `history_start_default` 配置，三条路径统一引用。

### B12【低】`users.username` 唯一约束大小写敏感但写入不规整
- 类别：正确性 / 边界
- 位置：`src/data/storage/db.py:1428-1436`（`create_user` 只 strip 不 lower）、`1451-1456`（`get_user_by_username` 精确匹配）
- 证据：SQLite `UNIQUE` 默认区分大小写，可创建 `Admin`/`admin` 两个用户；登录也区分大小写。
- 建议：用户名统一 lower 存储与查询。

### B13【低】`verify_password` 保留明文比对回退
- 类别：安全 / 正确性
- 位置：`src/data/storage/db.py:34-45`
- 证据：非 pbkdf2 格式时直接 `hmac.compare_digest(stored, candidate)` 明文比对。`_migrate_schema`（498-509）已做一次性迁移，此回退成为永久明文登录通道——若今后任何路径（如手工 SQL）写入明文，即可明文登录且不留痕迹。
- 建议：迁移确认完成后删除回退分支，或回退命中时立即重哈希并告警。

### B14【低】`intraday_service` 变量误命名 + 未用变量
- 类别：可读性 / 死代码
- 位置：`src/data/intraday_service.py:663`（`intraday_ts = result["trend_score"]`——名为时间戳实为趋势值，665 行 `extended_scores + [intraday_ts]` 依赖这个误导性命名）、`283`（`prev_close` 赋值未使用，ruff F841）、`35`（quoted 注解 `"date | None"` 引用函数内局部 import 的 `date`，ruff F821，类型检查器会报错）
- 建议：`intraday_ts` → `intraday_score`；删 283 行；把 `date` 的 import 提到模块级。

### B15【低】`engine._prepare_bars` 对缺列输入抛裸 KeyError
- 类别：正确性 / 潜在异常
- 位置：`src/rule_backtest/engine.py:283-286`
- 证据：`date`/`time` 两列都不存在时走 else 分支 `pd.to_datetime(df["date"])` 直接 KeyError，错误信息不含标的上下文。
- 建议：显式校验并抛带说明的 ValueError。

### B16【低】`PositionState.last_exit_bar_idx` 坐标系耦合脆弱
- 类别：正确性 / 脆弱设计
- 位置：`src/rule_backtest/engine.py:129`、`models.py:63-66`
- 证据：冷却期记账依赖「`len(day_bars)-1` 恰好等于该日在 all_bars 的位置」这一隐含约定（`_prepare_bars` 重置索引 + 过滤保留标签 = 位置才成立）。任何一处对 bars 重新 `reset_index`（例如未来加前视 warmup 裁剪）都会静默错位且 golden 测试未必能抓到。
- 建议：直接用 `idx`（iterrows 的标签）显式记账，去掉对坐标系重合的依赖。

---

## 二、安全隐患

### S1【高】MCP 端点整体豁免登录墙，7 个工具中 5 个完全无鉴权
- 类别：安全 / 鉴权
- 位置：`src/app/main.py:312-313`（`_EXEMPT_PREFIXES = ("/static", "/mcp")`）、`src/trend_mcp/server.py:98-412`
- 证据：main.py 注释称「MCP 为机对机通道，工具调用自带 username/password 逐次鉴权」，但实际上只有 `add_trade`/`open_positions`（server.py:420, 462）带凭据；`trend_dashboard`、`intraday_dashboard`、`symbol_detail`、`calc_stop_loss`、`list_instruments` **没有任何鉴权**。该系统经 frp 直接对公网暴露（main.py:381-383 注释），任何人可未授权读取全量行情看板、标的明细、逐标的指标。且 `FastMCP(transport_security={"enable_dns_rebinding_protection": False})`（server.py:50）关闭了 DNS rebinding 防护。
- 建议：MCP SSE 挂载点加共享密钥/通道鉴权（如 Header token 校验的中间件），或至少对只读工具统一要求凭据；评估重新开启 DNS rebinding 保护。

### S2【高】登录接口无速率限制/锁定，且服务可能以明文 HTTP 暴露
- 类别：安全 / 暴力破解 + 传输
- 位置：`src/app/routers/auth.py:37-52`（无任何限流）、`scripts/deploy.sh`（nginx `listen 80`，HTTPS 仅注释提示）、cookie 无 `Secure`（auth.py:45-51、main.py:364-367）
- 证据：30 天滑动会话（services/auth.py:27）+ 无限试错的登录口 + 明文 HTTP 部署脚本 = 在线暴破与嗅探会话的实际风险。pbkdf2 20 万次只能拖慢离线破解，挡不住在线试错。
- 建议：登录失败计数 + 指数退避/临时锁定；部署强制 HTTPS 后给 cookie 加 `Secure`；会话 TTL 可缩短。

### S3【中】缺安全响应头与 CSRF 缓解盘点
- 类别：安全 / 加固
- 位置：`src/app/main.py`（无 SecurityHeaders 中间件）
- 证据：全站无 `X-Content-Type-Options`/`X-Frame-Options`/`Referrer-Policy`/CSP。退出是 GET 链接（routers/auth.py:55-60）可被跨站 `<img>` 触发强制登出（危害低）。登录墙内写操作靠 SameSite=lax 兜底，无 CSRF token。
- 建议：加一个轻量安全头中间件；退出改 POST。

### S4【低】会话治理薄弱
- 类别：安全 / 会话管理
- 位置：`src/services/auth.py:41-47`、`src/data/storage/db.py:355-362`
- 证据：登录不踢旧会话、无「下线其他会话」能力、会话表无 UA/IP 记录、过期会话只在登录时顺手清理（`issue_session` 调 `delete_expired_sessions`）。被盗 token 无法被定向吊销。
- 建议：记录签发来源，提供管理端「吊销全部会话」；清理挪到定时任务。

### S5【低】`backup_to` 用 f-string 拼 SQL 路径
- 类别：安全 / 注入面（低可控性）
- 位置：`src/data/storage/db.py:91`
- 证据：`conn.execute(f"VACUUM INTO '{dest}'")`；`dest` 来自 `backup_dir` 参数（当前调用方都是内部路径）。若未来该参数暴露给外部输入即注入。
- 建议：对 `dest` 做单引号校验/转义，或注释标注「仅限受信路径」。

### S6【低】tushare 脚本内置「灰产镜像站」通道并打私有属性补丁
- 类别：安全 / 合规与脆弱性
- 位置：`scripts/tushare_common.py:20-37`
- 证据：docstring 明示支持「镜像站账号（如 tuaremax.top 等灰产渠道）」，并通过 `pro._DataApi__http_url` 名字修饰私有属性覆盖请求地址。凭据会发往第三方镜像（token 泄露面），私有补丁随 tushare 升级即碎。
- 建议：至少把镜像能力从默认路径剥离、文档中标注风险；考虑用官方 token。

---

## 三、性能

### P1【高】手工交易链路对同一标的全历史重复加载 3 次、逐标的线性扫元数据
- 类别：性能 / N+1 + 重复计算
- 位置：`src/services/trade_records.py:222-233`（预取时 `db.load_market_data` 全量历史，只用到最后一天的 volume/日期）、`src/services/manual_trade.py:97`（`compute_manual_trade` 再全量加载一次）、`src/services/stop_loss.py:181`（`compute_stop_loss` 内部第三次全量加载）、`stop_loss.py:195-199`（每次调用 `_load_instrument_metadata` 全表加载 + 线性扫描找本标的）
- 证据：N 笔未平仓持仓的列表接口 = 3×N 次全历史 SELECT + N 次元数据全表扫；`symbol_annotations`（trade_records.py:323-335）还对每笔持仓把 `compute_manual_trade` 按 tight/loose **各算一遍**（两遍唯一的差别是两个 ATR 倍数，却各自重算 ATR 序列、净值序列、回撤等）。
- 建议：`compute_stop_loss` 接受预加载的 df/metadata map；止损双档改为一次计算按两组乘数出两份价格；`_load_instrument_metadata` 换成 `get_instrument_metadata(symbol)` 点查。

### P2【中】看板 revision 每次请求对 100 万行表做 COUNT(*)
- 类别：性能 / 全表扫描
- 位置：`src/data/storage/db.py:854-867`（`get_market_dashboard_revision` 内 `COUNT(*) FROM market_data_qfq`）、调用方 `src/app/routers/subject_market.py:121`、`src/trend_mcp/server.py:115`、`src/services/dashboard_snapshot.py:96`
- 证据：每次看板请求（含前端 30s 轮询链路触发的刷新检查）都跑一次 COUNT(*)+MAX(time)+MAX(updated_at)；~1M 行表即便走索引也是百毫秒级开销，且它只是缓存失效令牌。`data_versions` 表本来就是为此设计的轻量计数器，却没有被用作唯一令牌。
- 建议：revision 改为 `MAX(time)` + `data_versions` 版本号（O(1) 主键查询），去掉 COUNT(*)。

### P3【中】标的列表接口逐标的 `COUNT/MIN/MAX` 查询（N+1）
- 类别：性能 / N+1
- 位置：`src/app/routers/instruments.py:429-431`（循环内 `db.get_market_data_summary(symbol)`）、`src/trend_mcp/server.py:392`（MCP `list_instruments` 同模式）
- 证据：每个标的一次独立 SELECT COUNT/MIN/MAX；600+ 标的 = 600+ 次查询/请求。
- 建议：一条 `GROUP BY symbol` 批量查回（已有现成的 `count_bars_by_symbol` 模式可扩展到 min/max），dict 查找。

### P4【中】指标缓存读路径前置 4 次查询才决定命中
- 类别：性能 / 重复计算
- 位置：`src/data/indicator_store.py:169-190`（`_cache_fresh`：`indicator_cache_info` 2 条聚合 SQL + `get_market_data_summary` + `get_data_version`）
- 证据：每次 `get_series` 先花 4 个 round-trip 判新鲜度，命中后再读缓存表；止损/看板回退路径按标的×指标放大。
- 建议：把新鲜度判定合并为单条 SQL（JOIN/子查询），或在构建侧维护单行的 per-symbol 新鲜度台账表。

### P5【中】行情写入逐行 iterrows 构建记录
- 类别：性能
- 位置：`src/data/storage/db.py:1249-1284`（`_market_records`）
- 证据：`df.iterrows()` + 逐值 `str(raw_value) == "nan"` 判断，是全量物化 qfq（每标的数千行）与批量回填的热路径；600 标的全量重建时是纯 Python 循环百万次。
- 建议：向量化（`df.where(df.notna())` + `itertuples`/`to_numpy`），非正价格过滤用布尔掩码。

### P6【中】`_save_backfill_result` 写完立即整表重读只为计数
- 类别：性能 / 重复 IO
- 位置：`src/data/service.py:504-507`
- 证据：`store.save_history(...)` 后 `saved = store.load_history(symbol)` 全量重读该标的历史，仅为得到 rows_after/起止日期——这些信息可以从 `to_save` 与写入前的 `existing` 直接算出。
- 建议：去掉重读，用本地 DataFrame 推导。

### P7【低】单标的日 K 接口先全量加载再截断
- 类别：性能
- 位置：`src/app/routers/market_view.py:263-290`（`load_market_data` 全历史 → 内存切片 → `tail(limit)`）
- 证据：所有读取都扫该标的全部行；start/end/limit 都不下推 SQL。intraday 分支还对「历史+合成K线」再全量重算一次指标（352 行），与 `build_market_payload` 里的全量计算（183 行）重复一遍。
- 建议：常用窗口场景下推 WHERE；intraday 重算复用首次结果增量追加。

### P8【低】`RevisionCache` 双实例 + 每个 HTTP 请求 stat 一次文件
- 类别：性能（轻微）
- 位置：`src/app/routers/subject_market.py:20` 与 `src/trend_mcp/server.py:91`（两份独立的看板缓存，同一 revision 会各自全量算一遍）；`src/app/main.py:291-296`（`AssetVersionMiddleware` 每个 HTTP 请求 `style_file.stat()`）
- 建议：RevisionCache 提升为共享单例；asset_version 改为定时刷新或启动+写时刷新。

### P9【低】`result_full` 常驻内存与 `condition_trace` 无界增长
- 类别：性能 / 内存
- 位置：`src/app/routers/rule_backtest.py:176`（每个 job 同时保留完整结果与 slim 结果两份）、`src/rule_backtest/engine.py:60,100,142`（`condition_trace` 每日每条件一条，5000 根 K 线 × 多条件 = 数十万 dict）
- 证据：job TTL 30 分钟且只在创建新 job 时惰性清理（128-135 行）；并发多次长区间回测时内存堆积明显。批量回测的 `extract_cell` 丢弃大字段是对的，单标的链路没有同等处理。
- 建议：job 只留 slim 结果（debug_log 按需单独存）；`condition_trace` 仅在 debug 模式收集。

---

## 四、冗余 / 死代码 / 重复实现

### D1【中】看板聚合助手在两个文件整体复制
- 类别：冗余 / 跨文件复制粘贴
- 位置：`src/services/dashboard.py:53-80, 61-63, 224-228`（`_number/_ma5/_strength/_priority/_key_tuple/_macd_counts`）与 `src/data/intraday_service.py:88-104, 459-461, 494-504, 971-975` 逐函数同实现；`dashboard.py:25` `DISPLAY_DAYS = 61` vs `intraday_service.py:465` `_DISPLAY_DAYS = 61`（注释自述「保持一致」）；`_aggregate_daily`（dashboard.py:83-112）与 `_weighted_daily_trend_series`（intraday_service.py:468-491）是同一加权口径的两套实现（向量化 vs 字典循环），极易漂移。
- 建议：提取 `services/dashboard_common.py` 共享；加权聚合统一为一套实现。

### D2【中】`_category_path` 同逻辑 5 份实现
- 类别：冗余
- 位置：`src/data/storage/db.py:704-711`、`src/services/instrument_admin.py:84-85`（`_category_path_from_parts`）、`src/app/routers/instruments.py:137-145`、`src/app/routers/market_view.py:47-55`、`src/trend_mcp/server.py:76-84`
- 建议：收敛到 `core`（如 `core/categories.py`）一份，各处引用。

### D3【中】`safe_float` 三份、`symbol_to_code` 两份、`_date_span` 两份
- 类别：冗余（违反项目自定的「一个概念一份实现」硬约束）
- 位置：`src/core/trend.py:44-50`、`src/rule_backtest/indicators.py:10-18`、`src/data/provider_utils.py:9-19`（三份语义略异的 safe_float：默认值 None vs 0.0、是否去逗号）；`src/core/symbols.py:31-36` vs `src/core/display.py:21-25`（后者就是前者的复制）；`src/data/service.py:399-406` vs `src/services/instrument_admin.py:39-45`
- 建议：统一进 `core` 并明确各自语义归属；`instrument_admin.py:27-36` 的 `_symbol_to_code/_symbol_suffix/_normalize_symbol` 纯转发包装函数一并删除，直接 import 原函数。

### D4【中】前端工具函数与整块 UI 逻辑跨模板复制
- 类别：冗余 / 前端
- 位置：`esc()` 在 `web/templates/batch_backtest.html:388`、`instruments.html:271`、`manual_trade.html:197`、`market_view.html:320`、`rule_backtest.html:366`、`subject_market.html:120`（escHtml）六份；止损卡片/悬停文案整段（stopPill/hardStopTip/chandelierStopTip/ratchetStopTip/stopDistanceTip/withTip）在 `manual_trade.html:246-708` 与 `subject_market.html:1114-1175` 两份（后者是箭头函数改写版）；买入日价格区间提示逻辑 `manual_trade.html:386-469` vs `subject_market.html:1204-1262` 两份。
- 建议：抽 `web/static/js/` 共享模块（目前 static 只有 echarts 和 css），模板只留页面专属逻辑。止损卡片已出现一次漂移风险（两处文案分别维护）。

### D5【中】三个 JobManager 结构性复制
- 类别：架构 / 重复
- 位置：`src/services/instrument_jobs.py:53-282`（BulkBackfillJobManager）、`309-476`（InstrumentAddJobManager）、`505-715`（EtfConstituentImportJobManager）
- 证据：lock + status dict + `_copy_status` + daemon thread + `record_job_run_safely` 收尾的骨架完全同构，约 600 行可压缩到一半；且三者都从 `instrument_admin` import 下划线私有函数（21-26 行），`routers/instruments.py:20-30` 同样 import 了 9 个私有函数——私有契约被跨模块大量消费，等于没有封装。
- 建议：抽 `JobManager` 基类（状态机 + 线程生命周期）；`instrument_admin` 的共用函数改为公开命名。

### D6【低】引擎 K 线图 MA 不用统一指标库
- 类别：冗余 / 违反硬约束 1
- 位置：`src/rule_backtest/engine.py:617-619`（`close.rolling(period).mean()` 手写）vs `core/indicators.py:27-31`（`sma`）
- 建议：改用 `core_ind.sma`。

### D7【低】provider 单只/批量报价的字段映射复制两份
- 类别：冗余
- 位置：`src/data/provider_tickflow.py:330-343` vs `398-411`（name/price/open/high/low/volume/amount/ts 映射逐行相同）
- 建议：抽 `_quote_item_to_dict(item, symbol)`。

### D8【低】未使用导入与死变量（ruff 实测）
- 类别：死代码
- 位置：F401 ×19，例如 `src/app/routers/instruments.py:4,6,8,16`（threading/Callable/pandas/normalize_symbol 等）、`src/services/dashboard.py:23`（compute_trend_indicator、trend_config 导入未用）、`src/rule_backtest/engine.py:9`（latest_field 未用）、`src/services/instrument_admin.py:13`；RUF100 未用 noqa ×28；`src/data/intraday_service.py:41` 函数内 import 的 `date` 未用。
- 建议：`ruff check --fix` 一轮（106 条可自动修），并纳入 CI。

### D9【低】已退役代码痕迹
- 类别：死代码
- 位置：`src/app/main.py:400-403` 注释「legacy overview page was removed」；`src/data/provider_tickflow.py:305-313` `fetch_minute_history` 必抛异常但仍在 IDataProvider 协议（provider_base.py:14）与 DataService.fetch_minute_history（service.py:169-177）中保留完整转发链；`src/services/indicator_builder.py:137-144` `detect_adjustment_breaks` 保留已作废参数 `end_date/lookback` 并 `del`。
- 建议：删除 minute-history 链路或标注保留理由；清理作废形参。

---

## 五、架构

### A1【中】`Database` 上帝类持续膨胀
- 类别：架构 / 上帝类
- 位置：`src/data/storage/db.py`（2120 行，15+ 个业务域的表全部揉在一个类：策略、行情、因子、行业、ETF 持仓、会话、手工交易、批量回测、快照、配置……）
- 证据：`_migrate_schema`（454-509）把各域的列迁移也揉在一起；任一域改动都要动这个文件。模块化拆分的条件已经成熟。
- 建议：按域拆 store 模块（sessions/users、market、backtest、cache），Database 只做连接与组合。

### A2【中】路由/服务层混用数据层私有实现与跨层重复校验
- 类别：架构 / 分层
- 位置：`src/app/routers/instruments.py:20-30`（消费 services 私有函数）、`src/services/instrument_jobs.py:15`（services 层 import fastapi.HTTPException——服务层不应依赖 HTTP 框架）、`src/rule_backtest/batch_service.py:360-365` 的注释自述为规避测试补丁而做的 lazy lookup
- 建议：job manager 的输入校验错误改为领域异常，由路由映射 400；私有函数公开化（见 D5）。

### A3【低】模块级可变单例散布各层
- 类别：架构 / 可测试性
- 位置：`src/app/routers/rule_backtest.py:20-25`（`service`、`_rule_jobs`）、`src/services/instrument_jobs.py:285,479,718`、`src/services/dashboard_snapshot.py:169`、`src/data/service.py:21-22,46-47`（`_symbol_locks`、`_quote_cache` 进程级全局）
- 证据：全局态是测试污染的主要来源（tests/api/conftest.py 的注释自证了这一点），也使多进程部署（uvicorn workers>1）行为不可预期——该约束没有任何文档说明。
- 建议：文档明确「单进程」部署约束；长期考虑 app.state 承载。

### A4【低】`core` 对 `data` 的反向依赖
- 类别：架构 / 依赖方向
- 位置：`src/core/jobs.py:17-18`（core 导入 data.service / data.storage.db）、`src/core/strategy_config.py:51`（core 运行时导入 data 层）
- 证据：README 自述依赖方向 `services → core / data`，core 本应纯计算；现在 core/jobs 是编排逻辑而非领域核心，放错了层。
- 建议：`core/jobs.py` 上移到 `services/`。

---

## 六、日志 / 监控 / 运维

### O1【中】无健康检查与运行指标端点
- 类别：运维能力
- 位置：`src/app/main.py`（全文无 `/health`、`/metrics`）
- 证据：systemd `Restart=always` 但没有活性探针可挂；调度器状态（`SchedulerManager.jobs_snapshot`，core/scheduler.py:94-100）实现了却**没有任何路由消费它**——死代码 + 运维盲区二合一。
- 建议：加 `/healthz`（DB 可读 + 最近 job_runs 状态）；把 jobs_snapshot 暴露到一个内部状态接口。

### O2【中】15:00 收盘快照后盘中看板冻结至 16:30
- 类别：运维 / 数据新鲜度
- 位置：`src/core/scheduler.py:18-24`（盘中 cron 最后一轮 15:00）、`src/services/dashboard_snapshot.py:93-103`
- 证据：15:00 的报价可能尚未含收盘集合竞价最终值，而 15:00~16:30 之间没有任何再触发；`eod_current` 守卫（今日 K 线已落库则跳过）又保证收盘后不会再算。用户在该窗口看到的是 15:00:00 时刻的估算快照。
- 建议：增加 15:05/15:10 一轮确认快照。

### O3【低】关键后台任务失败只进日志，无主动告警面
- 类别：运维
- 位置：`src/app/main.py:87-94`（`_rebuild_check` 失败仅 logger.exception）、`dashboard_snapshot.py:155-158`
- 证据：指标缓存重建失败时看板静默走实时回退（变慢但无信号）；只有日更有前端通知条（base.html:57-151）。
- 建议：失败计数写入 app_config/job_runs 并在通知条展示。

### O4【低】脚本的数据库路径处理不一致
- 类别：运维 / 一致性
- 位置：`scripts/migrate_raw_qfq.py:107`（`init_db()` 无 --db 参数，只能跑默认路径）、`scripts/rerun_daily_update.py:25`（同）、vs `scripts/fetch_etf_holdings.py` / `sync_stock_industry.py` / `import_all_etf_constituents.py`（有 --db）
- 证据：从非项目根目录执行 migrate 脚本会在 CWD 下**新建空库**并「成功」跑空迁移。
- 建议：统一加 --db 并在缺省时校验文件存在。

### O5【低】WAL 模式下用 `shutil.copy2` 做备份
- 类别：运维 / 备份可靠性
- 位置：`scripts/backfill_batch_excess_metrics.py:179`
- 证据：直接复制 `.db` 主文件；WAL 模式下最新数据可能还在 `-wal` 文件中，copy2 得到的备份可丢失最近事务甚至不一致。项目自身已有正确做法（`db.backup_to` 的 VACUUM INTO，db.py:78-97）。
- 建议：改用 `init_db(...).backup_to()`。

### O6【低】部署脚本与文档/现实部署漂移
- 类别：运维 / 文档
- 位置：`scripts/deploy.sh`（`INSTALL_DIR=/opt/trend-quant`、systemd `User=root`、nginx 反代）vs `CLAUDE.md`（"runs uvicorn from `/srv/trend-quant/.venv`"）vs `src/app/main.py:381-383`（"directly behind the frp relay with no nginx in between"）
- 证据：三处对生产拓扑描述互不相同；以 root 跑服务、无 .env 写入步骤（TICKFLOW_API_KEY 如何注入未交代）。
- 建议：对齐一份部署文档；service 降权运行；补 .env 步骤。

---

## 七、测试

### T1【中】已知失败的测试长期留在套件里
- 类别：测试
- 位置：`CLAUDE.md`（"Baseline: 2 pre-existing failures in tests/integration/test_intraday_service.py"）
- 证据：基线红测试会麻痹「全绿」信号，新回归易被淹没。
- 建议：修掉或 `pytest.mark.xfail(strict=True)` 标注原因。

### T2【中】调度/任务层基本无测试
- 类别：测试覆盖
- 位置：无测试文件覆盖 `src/core/scheduler.py`、`src/core/jobs.py`（`daily_market_update_job` 的非交易日跳过/失败记录/catch-up 逻辑）、`src/app/main.py` 的 lifespan 编排（`_daily_update_catchup` 的 expected/behind 判定）、`src/services/instrument_jobs.py` 三个 JobManager（仅 tests/test_instruments_bulk_backfill.py 5 个间接测试）
- 证据：grep 全 tests/ 无 `daily_market_update_job`/`SchedulerManager` 引用。catch-up 判定（main.py:182-222）是数据新鲜度的最后防线，错了就全天无数据且无告警。
- 建议：对 `_daily_update_catchup` 与 `daily_market_update_job` 做表驱动单测（冻结时间/假 DB）。

### T3【中】看板聚合逻辑测试薄弱
- 类别：测试覆盖
- 位置：`src/services/dashboard.py`（395 行核心聚合）仅 `tests/test_subject_market.py`（1 个测试）+ `tests/api/test_subject_market_dashboard_api.py`（5 个）+ `test_subject_holdings_overlay.py`（7 个）覆盖；`_aggregate_daily` 加权口径、`_assign_envelope`、强度百分位无边界用例。
- 建议：对加权聚合（含 amount 缺失等权兜底）补参数化单测。

### T4【低】tests/ 根目录 10 个测试文件无 marker
- 类别：测试 / 一致性
- 位置：`tests/test_market_view.py`、`test_provider_tickflow.py` 等（不属于 unit/api/integration 任一子目录，无 collection 钩子打标）
- 证据：`make test-unit` / `-m unit` 永远选不到它们；与 pyproject markers 约定不一致。
- 建议：归入子目录或根 conftest 统一打标。

### T5【低】API 测试 fixture 对全局态的防御 fragile
- 类别：测试
- 位置：`tests/api/conftest.py:29-45`
- 证据：靠「导入顺序 + monkeypatch 时机」避免污染，注释自承脆弱；任何新模块顶层 `from data.storage.db import get_db` 并在导入期调用都会踩坑。
- 建议：服务层统一依赖注入（db 作为参数/工厂），而非模块级绑定。

---

## 八、文档与注释

### F1【中】README 的 MCP 工具数与实际不符
- 类别：文档过时
- 位置：`README.md`（"MCP 服务（/mcp/sse）：5 个工具"）vs `src/trend_mcp/server.py:1-20`（docstring 列 7 个：trend_dashboard/intraday_dashboard/symbol_detail/calc_stop_loss/list_instruments/add_trade/open_positions）
- 建议：更新 README。

### F2【低】TODO.md 引用不存在的页面
- 类别：文档过时
- 位置：`TODO.md`（"/backtest 页面现有的回测标的勾选逻辑"——实际页面是 /rule-backtest）
- 建议：修正或清理。

### F3【低】架构文档与代码漂移
- 类别：文档
- 位置：`CLAUDE.md`/`README.md` 架构树未列 `audit/`、`services/stop_loss.py`、`services/trade_records.py`、`services/dashboard_snapshot.py`、`services/stock_industry.py`、`rule_backtest/sizing/`、`batch_service.py` 等后增模块；`core/jobs.py` 标为「领域核心（纯计算）」但它做编排（见 A4）。
- 建议：补一轮架构图更新。

### F4【低】硬编码缓存串与 BOM 卫生
- 类别：卫生
- 位置：`web/templates/base.html:7`、`login.html:7`（`style.css?v=...-20260824-auth` 手工后缀，与 asset_version 机制重复且会过期）；UTF-8 BOM 存在于 10 个文件（`src/__init__.py`、`src/core/settings.py`、`src/data/provider_base.py`、`src/data/provider_utils.py`、`web/templates/base.html` 等）
- 建议：去掉手工后缀（asset_version 已够）；批量去 BOM。

### F5【低】仓库混入构建/数据工件
- 类别：卫生
- 位置：git 跟踪了 `.coverage`、`trend-quant.zip`、`.agents/skills/trend-score-calculator.zip`；根目录另有 14MB `.bundle`（已被 .gitignore 排除，正确）
- 建议：`.coverage`/`.zip` 加入 .gitignore 并从索引移除。

### F6【低】pyproject 声明了未使用的依赖
- 类别：依赖卫生
- 位置：`pyproject.toml`（`pyarrow>=21.0.0`、`email-validator>=2.2.0`）
- 证据：全 src 无 `import pyarrow` / EmailStr / email_validator 引用。
- 建议：删除或注明用途。

---

## 九、用户体验

### U1【中】标的管理页无法停用/删除标的
- 类别：UX / 功能缺口
- 位置：`web/templates/instruments.html`（全文无 `enabled` 相关 UI）、`src/app/routers/instruments.py`（`InstrumentUpdateRequest` 77-81 行只有三级类目）
- 证据：`enabled` 字段决定日更池（core/jobs.py:30-34）与看板过滤，但只能在建标时写入，之后无法从 UI 改；误加的标的不想管了只能动数据库。
- 建议：更新接口加 enabled 字段 + 列表页加启停开关。

### U2【低】登录页无密码可见性切换、无 Caps Lock 提示等
- 类别：UX
- 位置：`web/templates/login.html:13-22`
- 建议：低优先级增强。

### U3【低】subject_market 快照错误信息直接拼 innerHTML
- 类别：UX / 安全（轻微 XSS 面）
- 位置：`web/templates/subject_market.html:1092`（`${st.last_error}` 未转义插入 innerHTML）、`1021, 1079`（同模式插入服务端字符串）
- 证据：last_error 来自服务端异常字符串（dashboard_snapshot.py:158 `str(exc)`），异常消息可携带标的名等外部数据；当前内容受控，但模式本身不安全（其他模板都配了 esc/escHtml 助手）。
- 建议：统一走 `escHtml` 或 textContent。

---

## 十、其他

### X1【低】冗余二级索引
- 类别：存储
- 位置：`src/data/storage/db.py:143-144, 159-160, 170-171`
- 证据：`market_data_raw/qfq`、`ex_factors` 的 `PRIMARY KEY (symbol, time)` 已自动建索引，`idx_*_symbol_time` 是重复索引，白付写放大与存储。
- 建议：删除冗余索引（存量库需手工 DROP，迁移里不做）。

### X2【低】`save_market_data` 的 INSERT OR REPLACE 与「raw append-only 永不改写」叙事相抵触
- 类别：一致性
- 位置：`src/data/storage/db.py:1294-1299`
- 证据：raw 表被宣称为 append-only 真源，但写入语句允许同主键覆盖；若 vendor 回溯改数（正是该架构要防的事），旧值会被静默覆盖。
- 建议：raw 表用 `INSERT OR IGNORE` + 冲突检测告警（值不同才记 warning），把「不改写」落成机制。

### X3【低】登录态接口幂等性盘点
- 类别：设计
- 位置：`src/app/routers/manual_trade.py:104-114`（close 幂等，好）；`src/services/instrument_admin.py:202-245`（add_constituent_stock 用「检查-再插入」+ 捕获 ValueError 兜底并发，可用但 `start_add_instrument` 路由 215-275 行的 409 预检与 job manager 的状态判定之间存在 TOCTOU 窗口）
- 建议：接受现状即可，注意文档化。

---

## 附：ruff 摘要（`ruff check src scripts`，235 条）

| 规则 | 数量 | 说明 |
|---|---|---|
| DTZ005/DTZ011/DTZ007 | 53/20/7 | naive datetime 调用（与 B7 互证） |
| RUF100 | 28 | 未用 noqa |
| BLE001 | 27 | 盲捕 Exception（多数是有意的「不拖垮」语义，建议逐条确认） |
| RUF046 | 22 | 冗余 int() 强转 |
| I001 | 21 | import 排序 |
| F401 | 19 | 未用导入（见 D8） |
| B008 | 6 | FastAPI `Depends` 默认值（框架惯例，可配置忽略） |
| F821 | 2 | **未定义名（B1、B14）** |
| S110 | 2 | try/except/pass（`src/core/calendar.py:51`、`scripts/migrate_raw_qfq.py:51`） |
| F841 | 1 | 未用变量（B14） |

---

## 最易被其他审查者遗漏的 5 个问题

1. **`_config_items` 未导入的 NameError 只在「分类表为空」时引爆**（instruments.py:102）——正常库永远走不到该分支，所有功能测试都覆盖不到；ruff F821 是唯一的信号灯。
2. **vendor 返回空因子列表会静默擦除存量除权因子并重物化出错误 qfq**（service.py:264-272 + provider_tickflow.py:287-297）——藏在「diff 检测」的正常路径里，不是异常路径。
3. **除权因子日期用 UTC、日 K 日期用上海时区，同一 vendor 时间戳两套口径**（provider_tickflow.py:294 vs 122-123）——代码注释写着「已验证」反而让人不再怀疑；错位一天时 `bisect_left` 语义会把除权提前一天。
4. **`latest_snapshot()` 一次性 DB 失败后永久返回 None**（dashboard_snapshot.py:51-60）——`_snapshot_loaded` 在 try 之前置位，瞬时故障被固化成全天故障。
5. **生产没有任何创建首个登录用户的入口**（db.py:1428 的 `create_user` 调用方全在 tests/）——登录墙是 2026-08 新上的，部署文档/脚本都没跟上，全新部署直接锁死。
