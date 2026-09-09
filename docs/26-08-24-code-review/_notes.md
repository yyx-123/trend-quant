# 工作笔记（原始发现流水账，最终报告在 review-report.md）

> 本文件是审查过程中的原始记录，供上下文丢失时恢复。条目格式：[类别] 文件:行 — 发现。

## main.py / 中间件 / 日志
- [性能] src/app/main.py:280-296 AssetVersionMiddleware 每个 HTTP 请求都 stat 一次 style.css（文件系统调用/请求）。低频变更，可缓存+定时刷新或启动时计算。
- [安全] src/app/main.py:364-367 session cookie 无 Secure 属性（SameSite=lax、HttpOnly 有）。若经 frp 以 https 暴露应加 Secure（需视部署是否 https）。
- [安全] 全站 cookie session 鉴权，除 SameSite=lax 外无 CSRF token；POST 类 API（手工交易、导入等）依赖 lax 默认阻止跨站 POST 表单，但老浏览器/子域场景仍有风险。可接受但值得记录。
- [运维] 无任何 metrics/监控埋点（请求耗时、任务成功率只有日志）；无健康检查端点（/healthz 未见）。
- main.py:64 Path("data").mkdir(exist_ok=True) 相对路径，依赖启动 cwd；systemd/不同 cwd 启动会静默建错目录。config 路径同理（config/app.yaml 相对路径 settings.py:45）。
- main.py:405-412 /mcp 挂载豁免登录墙，依赖 MCP 逐次密码鉴权 —— 需核 trend_mcp/server.py 实现。

## auth / session
- [安全] src/services/auth.py:45 session token 明文存 sessions 表（secrets.token_hex(32) 直接落库）。DB 泄露即全部会话被盗；应存 hash（如 sha256(token)）再比对。
- [安全] src/app/routers/auth.py:37-52 登录接口无速率限制/失败计数/锁定，可在线爆破密码（tr.authenticate 需确认是否 bcrypt 等慢哈希）。
- [安全] auth.py:55 logout 用 GET，可被 <img> 预加载等触发强制登出（轻 CSRF）；建议 POST。
- [观察] services/auth.py:64-67 expires_at 解析失败按已过期处理并 delete_session —— 好。但 datetime.now() 本地时区与 DB 存储格式约定依赖一致（需核 db.py 写入格式）。
- [一致性] issue_session 每次登录 delete_expired_sessions 全表扫（sessions 表小，问题不大）。

## jobs / scheduler
- core/jobs.py:63 date.today() 用的是服务器本地时区而非 settings.app.timezone（Asia/Shanghai）。若服务器时区非 CST，交易日判断/落库日期错乱。main.py 里 _daily_update_catchup 用了 market_now() 但 daily_market_update_job 用 date.today()，口径不一致。
- core/jobs.py:80 strategy_cfg 读取在 try 内但未用（除了 adjust/backtest_start）——无问题。
- core/scheduler.py INTRADAY_SNAPSHOT_CRONS 11:0-30/5 会覆盖 11:30（午休开始），15:00 单独一条；午间 11:35-12:55 无任务——注释说正确。OK。
- BackgroundScheduler 内存 jobstore：重启丢 misfire，已有启动补偿兜底（main.py）。设计合理，但启动补偿只在启动时跑一次——若补偿时行情商未发布当日数据，当天再无重试直到次日（16:30 定时任务当天已过点）。边缘场景可记录。
- scheduler.py:90 shutdown(wait=False)：正在执行的更新线程被遗弃，进程退出即中断（daemon 线程），批次标记 interrupted 已有处理（main.py:69）。OK。

## db.py（数据层）
- [可靠性-高] db.py:66-76 `_connect` 未设 `PRAGMA busy_timeout`。WAL 下写写互斥，批量回测 worker（每格 commit）+ 看板快照写 + session touch + 每日更新并发时，sqlite3 默认 busy_timeout=0 会立刻抛 OperationalError("database is locked")。全库未见设置（需 grep 确认）。建议 connect 后 busy_timeout=5000。
- [性能-低] 每次 `_connect` 都执行 `PRAGMA journal_mode=WAL`——journal_mode 是库文件级持久设置，每查询一次白费一次 round-trip。
- [性能] db.py:854-867 `get_market_dashboard_revision` 每次 COUNT(*) 扫 market_data_qfq（~百万行），若每请求调用则开销可观；可改存计数或用 data_versions 替代。
- [性能] trend_daily 主键 (symbol,time,param_set)，而 `load_trend_daily_bulk` 按 (param_set, time>=?) 全表过滤——无法用主键前缀，需索引 (param_set, time)。dashboard 热路径。
- [安全-低] verify_password 明文比对兜底（db.py:45）：若迁移遗漏任一行明文，该接口永久兼容明文；建议迁移完成后移除明文分支或加告警日志。
- [安全-低] backup_to 的 `VACUUM INTO '{dest}'` 用 f-string 拼 SQL（db.py:91）：dest 内部生成暂无注入面，但若 backup_dir 未来来自外部输入则有注入/路径穿越风险；建议改为参数校验。
- [一致性] save_etf_constituents 先 UPDATE 全表置 0 再 upsert（同事务）——好；但 `replace_ex_factors`（1399-1403）DELETE 与 save 分两个连接/事务，中间崩溃会留空因子表。
- [架构] db.py 2119 行单文件上帝类：strategies/metadata/market/sessions/trades/jobs/batch 全在一起，可拆 repository。
- [小] db.py:1325/1723/1793 函数内 import pandas（模块顶部已 import pandas as pd）——冗余。
- [小] clear_market_data 删全表但 `_market_symbols_cache.pop` 在 with 块内 conn 上 pop（1373 行位置在 with 内但无妨）。

## data/service.py
- [性能] update_pool_daily 串行逐标的 ensure_daily_history → 每个有增量的标的 rematerialize_qfq 全量加载 raw + 全量 DELETE/INSERT 重写 qfq（service.py:274-305, 382-386）。池子数百标的 × 全历史重写，日更耗时随历史变长线性增长；可做增量物化（仅重算最近因子变化点之后）。
- [正确性-中] ensure_daily_history:344-346 把 DataProviderError（空响应）当「无增量」。若行情商故障/限流返回空，所有标的静默 up_to_date，日更仍记 completed——故障被吞。建议区分「真空区间」与「疑似故障」（如全池为空时告警/记 failed）。
- [运维] update_pool_daily 把含全部逐标的 results 的巨大 payload 写进 job_runs（service.py:824-836），行臃肿；get_latest_job_run 每次 json.loads 全量。建议落库摘要、明细另行可查。
- [小] `_symbol_locks`/`_quote_cache` 字典无上限（标的有界，风险低）。
- [疑似死代码] `_ordered_providers`（115-119）provider_priority 恒为 ["tickflow"]，需 grep 是否还有调用方。
- [供应商耦合] _retry_wait_seconds 解析中文错误文案「请 X ms 后重试」决定退避——文案变更即失效；建议同时看 HTTP 状态/Retry-After。

## core / provider 层
- [重复实现] safe_float 两处：core/trend.py:44 与 data/provider_utils.py:9（签名默认值不同，语义略异）——应统一。
- [隐患-低] provider_utils.standardize_ohlcv:55 缺 time 列时用 datetime.now() 逐行填充——静默伪造时间戳，坏数据入库难查；建议 dropna 或抛错。
- [正确性-低] compute_qfq 对 amount 不调整（与 vendor 一致，已注明）；注意下游若用 amount 计算换手率需注意。
- [一致性] core/jobs.py:63 与 calendar.previous_trading_day:146 用 date.today()（服务器本地时区），而 market_now() 用配置时区——同一项目两套「今天」。服务器若 UTC，16:30 后 date.today() 与 CST 日期可能差一天。

## intraday_service.py
- [架构-中] compute_intraday_trend_cached(245-379) 是 core/trend 公式的第二份实现（权重/tanh/clip 全部内联复制），虽有一致性测试兜底，公式演进时双写漂移风险高；建议抽出共享的纯函数核心。
- [重复] build_intraday_dashboard 内 620-624/644-645 行内联清洗 hist 与 _clean_daily_hist(141-155) 重复，未复用。
- [封装] intraday_service 导入 core.trend 私有函数 _detect_trend_phase（trend.py:292）——跨模块用私有 API。
- [性能-低] build_intraday_dashboard 对全部 symbols 算完才按三级分类过滤（811-815），未分类标的白算一轮。
- [正确性-低] return_1d 的昨收取 tail 最后一根（787）：若当日K线已落库（盘中快照时段外触发?）则 prev_close=今日，return_1d=0；快照只在交易时段跑，风险低但值得注意 has_persisted_today_bar 未用于 returns 计算。

## indicator_builder / dashboard_snapshot
- [疑似死代码] indicator_builder.params_hash(45-49) 需 grep 调用方。
- [API 冗余] detect_adjustment_breaks 保留 end_date/lookback 参数却 del（137-144）——兼容旧调用的包袱，宜清理签名。
- [运维-低] rebuild_if_needed 全量重建前 db.backup_to()（126）——好；但 backup keep=3 固定，重建+手工多次备份可能挤掉更早的好备份。
- dashboard_snapshot._run: DataService() 每轮 new 一个（含 TickFlow client），5 分钟一轮，可复用但影响小。

## dashboard.py vs intraday_service.py
- [重复-明显] services/dashboard.py 与 data/intraday_service.py 大段重复：_number/_ma5/_strength/_priority/_key_tuple/_macd_counts/_sort（逐行复制的私有 helpers），以及「L2/L3/标的 聚合→strength→嵌套 children→groups」的整体编排两套平行实现。EOD 与盘中口径需一致，但应抽共享模块（如 services/dashboard_common.py），否则口径漂移只是时间问题。
- [性能-低] dashboard.py:319-327 每标的按日期逐个查 trend_lookup/trend_series.get——已比旧版 61x 好；可进一步 merge。
- market_indicators._num 与 dashboard._number/intraday._number 三处近似（语义略异：round(6)）。

## stop_loss / manual_trade
- [性能] stop_loss.compute_stop_loss:195 为查单个标的 stop_atr_mul 加载整张 instrument_metadata 表并逐行比对（_load_instrument_metadata），列表接口按持仓逐笔调用 → 每笔一次全表查询。应 db.get_instrument_metadata(symbol)。
- [性能] manual_trade.compute_manual_trade:97 在 compute_stop_loss 已 load_market_data 后再次全量加载同一标的行情——每笔持仓两次全历史读取。
- [正确性-风险] 手工交易买入价是用户实际成交价（未复权），而止损/净值基于 qfq 序列：买入后若发生除权，buy_price 与 qfq K线口径错配（买入价区间校验、硬止损基准、盈亏百分比全部失真）。ETF 分红少所以影响小，但应记录或做 buy_price 复权换算。
- [良好] UNSET_INTRADAY_BAR 哨兵区分「未预取」与「确认无盘中」，注释清晰。

## instrument_jobs
- [重复] 三个 JobManager（BulkBackfill/InstrumentAdd/EtfConstituentImport）脚手架高度雷同：lock+status dict+daemon thread+snapshot+record_job_run+close。可抽公共基类/装饰器。
- [口径不一致] 历史回补起点三处三个值：instrument_add / ETF 导入硬编码 2020-01-01（instrument_jobs.py:432,628），core/jobs.py:84 默认 2015-01-01，strategy_config 默认 2025-01-01。新标的回补深度与日更窗口口径不一，应统一从 strategy_config 取。
- [运维] 三个 JobManager 状态全在内存，重启后页面显示 idle 但 job_runs 有历史——用户无法分辨「从未跑过」与「跑完丢了状态」。可从 job_runs 恢复最近状态。
- instrument_jobs 从 instrument_admin 导入下划线私有函数（_append_instrument_config 等）——同包内容忍度尚可，但说明这些应为公开 API。

## stock_industry.py
- [死代码-确凿] build_sw_tree(217-271) 第一段 for 循环（224-235）完全无效：l1_order/l2_order 在 236-238 被重新赋空并重新循环——复制粘贴残留的整块死代码。
- [健壮性] sync_industry_from_tickflow:355 用 os.environ["TICKFLOW_API_KEY"] 直接取（缺失时 KeyError 不友好），且硬编码 base_url，未走 provider 的 TICKFLOW_BASE_URL 环境变量覆盖——两处 base_url 口径不一。
- [一致] tickflow_symbol_to_project / project_symbol_to_tushare 与 provider_tickflow._to/_from_tickflow_symbol 第三、第四处后缀转换实现——应收敛到 core.symbols。

## routers/instruments.py
- [BUG-潜在 NameError] instruments.py:102 `_category_options` 兜底分支引用 `_config_items()`，但 20-30 行的 import 列表里没有它（只 import 了 _config_name_map 等）。instrument_categories 表为空时触发 NameError 500。需验证：grep _config_items 在路由文件。
- [性能 N+1] /api/list（429-466）对 known_symbols 逐个 db.get_market_data_summary——几百标的 = 几百次连接/查询；应一条 GROUP BY 批量查。
- [重复] update_instrument:505-512 内联重算三级 priority，而 instrument_admin.category_priorities 注释明说「不要各自平行实现」——自相违背。
- [权限] is_admin 字段存在但 instruments 管理接口（add/update/backfill/import）任何登录用户都可调——无角色控制。单用户系统可接受，但 admin 语义名存实亡，需确认设计意图。

## subject_market / manual_trade 路由
- [性能] subject_market.py:121 每个看板请求都调 get_market_dashboard_revision（含 COUNT(*) 百万行表 + MAX(updated_at)）——页面轮询/多用户叠加时是固定开销；revision 可加短 TTL 缓存或改用 data_versions 单值。
- [架构-系统性] 全部路由 async def 内直接同步 sqlite/pandas 调用——轻查询阻塞事件循环；大计算已 run_in_threadpool（看板），但 list_trades、/api/list 等重接口仍在事件循环上同步执行（手工交易列表每笔全历史加载+止损计算）。
- [冗余] market_view.py:39-44 又是 _normalize_symbol/_config_name_map 薄包装；_num/_series 与 market_indicators 重复。

## rule_backtest engine/batch/metrics
- [性能-显著] batch run_batch 每格 engine.run → 每次新建 ValueResolver 并对同一标的全历史重算全部指标序列（batch_service.py:512 + engine.py:49-52）。同一标的 N 个策略重复 N 次相同指标计算；应按标的缓存 resolver/指标序列跨策略复用。
- [性能] _flush_counts 每格一次 UPDATE batch_backtest_runs + insert_batch_cell 一次 INSERT = 每格两次写库 commit；可每 N 格 flush 计数。
- [UX/正确性-低] compute_summary 年化 (1+tr)^(252/n)-1 对极短持仓（几天）会爆炸出天文数字年化；手工交易页与回测详情直接展示，建议短窗口（如 <20 交易日）年化置 None 或标注。
- engine.py:64 主循环 bars.iterrows() 逐日 + update_position_state_for_day/ConditionEngine 传整段 day_bars —— 切片是 view 开销可控，依赖 resolver memoization 才避免 O(n²)；若未来有非 memoized resolve 路径需警惕。

## 路由 rule/batch + MCP
- [安全-中] trend_mcp/server.py:48-51 FastMCP enable_dns_rebinding_protection=False，且 /mcp 在登录墙豁免名单；trend_dashboard/symbol_detail/list_instruments/calc_stop_loss/intraday_dashboard 五个工具零鉴权。若 frp 把 8000 暴露公网，任何人可调——除数据泄露外，intraday_dashboard 每次调用烧 tickflow 实时报价配额（无鉴权的配额消耗攻击面）。建议至少只读工具也加共享密钥或网络层限制。
- [BUG-一致性] MCP symbol_detail（server.py:246-272）：dates/candles/volumes 截尾到 days，但 indicators（compute_market_indicators 全历史长度数组）未截尾——载荷内 dates 与 indicators.* 长度不一致，消费方按位置对齐会错位。
- [重复] _category_path 第四处实现（mcp server.py:76）；instrument_display.py 是 shim（好）。
- [小] rule_backtest.py:72-74 _cap_end_date 又用 date.today()；previous_trading_day(date.today()) 双 today 调用。
- [观察] _rule_jobs 内存任务 30min TTL、重启即丢——前端轮询重启后 404「任务不存在或已过期」，可接受但 UX 上可提示。

## base.html / 前端
- [小] base.html:7 静态资源版本号 = middleware 的 mtime + 手工日期后缀 "-20260824-auth"——双轨 cache-bust，手工后缀容易遗忘；建议只留 mtime。
- [小] 每页 30s 轮询 daily-update/status，后台标签页不暂停（document.visibilityState 未用）；单机影响微小。
- [小] hideBar 再次 fetch status 仅为了取 ts，可直接用已拿到的 status。
