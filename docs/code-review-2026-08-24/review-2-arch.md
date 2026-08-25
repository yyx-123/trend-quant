# 评审意见（review-2：架构师/技术负责人视角）

- 评审日期：2026-08-24
- 评审对象：`docs/code-review-2026-08-24/code-review-report.md` v1
- 评审方式：通读主报告后独立扫描 `src/`（68 模块）、`web/`、`scripts/`、`tests/`、`pyproject.toml`、`.gitignore`、`git remote/log`，并对关键条目打开代码亲核。本评审不做逐条事实核查（另有评审负责），聚焦覆盖面、分级、方案质量、行动清单、报告结构五个维度。

---

## 一、覆盖面完整性

总体覆盖面好：安全、重复实现、性能、测试、运维、前端、文档七大块均有涉及，且前端 26 接口对照、MCP 工具清单这类"易漏区"都查到了。但以下整块或重点点位缺失：

### 1.1 并发与多线程安全：无专项，应补一节（哪怕是"已检查、结论良好"的正面记录）

报告通篇没有并发审查结论。我亲核后认为本项目并发纪律**总体良好**，值得记录的设计：`RevisionCache` 双检锁单飞（`src/services/dashboard.py:39-48`）、`_update_job_lock` 非阻塞防重入（`src/app/main.py:108-118`）、per-symbol 锁（`src/data/service.py:21-34`）、每调用新建 SQLite 连接规避 `check_same_thread`（`src/data/storage/db.py:66-76`）。但有两个残留风险报告未提：

- **`Database._market_symbols_cache` 跨进程不失效**：`db.py:57-62` 注释自知"writes from OTHER processes do not invalidate this cache"。项目存在大量脚本直写库的文化（P3-1 的 13 个脚本），脚本写库后 web 进程的符号缓存陈旧，与 P3-1 联动，至少应在脚本使用约定中写明"写库后重启服务"或提供失效入口。
- **APScheduler 默认线程池下三类任务可并发**：`core/scheduler.py:41` 的 `BackgroundScheduler` 默认 max_workers=10，`daily_update`、月度 `stock_industry_sync`（scheduler.py:59-66）、盘中快照可同时命中同一 WAL 库。叠加 P2-16 指出的无显式 `busy_timeout`，月初 1 号与交易日重合时有写冲突重试风险。P2-16 只提了批量回测场景，漏了调度器内部并发这个更常发的来源。

### 1.2 数据正确性/口径：P1-13 漏了最关键的一处本地时区使用

- **`core/jobs.py:63` `today = date.today()`**：每日 16:30 补库主任务的**交易日门控**用宿主机本地日期，而调度器按 `settings.app.timezone` 触发（scheduler.py:41）。在 UTC-9 等时区主机上，北京 16:30 触发时本地日期仍是前一天，`is_trading_day` 门控判错一天。同理 `src/app/main.py` `_run_daily_update` 内两处 `_date.today()`（传给 `run_post_update_pipeline` 与 `record_job_run_safely` 的 run_date）。项目既然专门造了 `market_now()`（`core/calendar.py:37-57`），"生产咽喉"模块反而漏改——P1-13 只列了 intraday_service 的 11 处，恰好漏掉报告自己定级 H1 零测试的模块。应并入 P1-13 的证据清单，这也反向印证了 H1 补测的优先级。
- **chinese_calendar 年度数据边界的运维仪式无着落**：`core/calendar.py:60-85` 对超出库数据范围的年份退化为 weekday-only 判断，且每年只 `logger.warning` 一次。今天是 2026-08-24，库数据到 2026 年；2027-01-01 起法定假日会被当成交易日——更糟的是 `_daily_update_catchup`（main.py 启动补偿）的 expected 计算也走同一日历，会把假日判为"应更未更"，**每次重启都触发一次无效 force 补跑**，直到人工升级库。这个"每年 12 月 pip install --upgrade chinese_calendar"的仪式只写在 calendar.py docstring（1-10 行），README/CLAUDE.md/部署文档均无（已 grep 确认），叠加 P2-14 无告警通道，warning 无人能看见。建议新增 P2 条目：把年度升级写进部署文档 + 在导航栏盘后更新条复用渠道暴露"日历数据过期"状态。
- **`is_trading_time` 不校验交易日**：`core/calendar.py:88-99` 只看时间窗不看是否交易日，与同文件 `is_realtime_available`/`is_past_market_open` 语义不一致；当前唯一调用方（calendar.py:177）自己补了交易日判断所以没炸，属潜伏 API 陷阱。建议函数内补 `is_trading_day` 或改名 `is_continuous_auction_hours` 明示语义。P3。

### 1.3 依赖管理：报告只抓到症状，没抓到根因

- **无 dev 依赖声明**：`pyproject.toml` 只有运行时 dependencies，`pytest`/`pytest-asyncio`/`pytest-cov`/`ruff` 全部未声明（无 `[project.optional-dependencies]` dev 组，也无 requirements-dev.txt）。P0-6 抓到"pytest-asyncio 未装"只是这个根因的症状之一——新机器 `pip install -e .` 后**测试根本跑不起来**，且 P2-18 的 CI 建议落地时第一个绊脚石就是它。应升级为独立条目（P1，阻塞 CI 与任何新环境验证）。
- **版本约束策略不一致**：`tickflow[all]==0.1.24` 精确钉版，`mcp>=1.0.0`、`pandas>=2.3.0` 等全部仅下界。pandas 3.0、numpy 3.x 这类上游大版本有静默破坏风险（本项目 pandas 用法深：iterrows/rolling/映射）。个人项目不必上 lock 文件，但至少应对 pandas/numpy/fastapi 加上界，或在 README 写明"重装环境即验证"的纪律。P3。
- `.env.example` 建议与 `.gitignore` 冲突，见 3.5。

### 1.4 单机扩展性天花板：有性能条目但缺"天花板"视角的收口

P2-7/P2-8/P2-9 分散给了三个点状优化，但没有一个判断：**日更链路的总时长随标的数线性增长**（600+ 标的 × 全量 qfq 重写，data/service.py:274-305,382-384），这是单机 SQLite + 单线程顺序补库架构的固有天花板。报告应显式回答"标的池扩到 2000 时会发生什么"：日更窗口是否还能在 16:30-18:00 内跑完、misfire_grace_time=7200（scheduler.py:51）是否还够。不需要行动，但这类"设计容量"结论是架构评审的基本交付物。另外 2.8GB 主库的备份磁盘预算与 P2-15 建议冲突，见 3.6。

### 1.5 错误恢复路径：覆盖尚可，补一个正面发现

报告未专门评估恢复路径，我核查后认为现状**好于报告给人的印象**：`_daily_update_catchup` 三路漏更检测（main.py:204-235）、`scripts/rerun_daily_update.py` 手动恢复入口、批量回测批次启动清理（db.py:1920-1930）、sessions 过期行有清理（services/auth.py:44 调 `delete_expired_sessions`）。建议在报告"总体评价"里补一句正面结论，避免读者高估运维风险。

### 1.6 其他小遗漏

- 登录接口存在**计时侧信道用户枚举**：`services/trade_records.py:66` `user is None or not verify_password(...)` 短路——用户不存在时毫秒级返回，存在时付 20 万次 PBKDF2。单人系统危害极低，列入"可选"即可（登录限流落地后进一步缓解）。
- API 契约稳定性：报告 P2-21 做了前后端对照，可接受。FastAPI 全无 `response_model`，契约靠 golden 测试与联调兜底——个人项目不必行动，但报告可以一句话点明这是知情取舍。

---

## 二、分级合理性

### 应降级

| 条目 | 现级 | 建议 | 理由与依据 |
|---|---|---|---|
| P0-4 迁移脚本 copy2 备份 | P0 | **P2** | 两个脚本均为一次性且已执行完毕（backfill_batch_excess_metrics.py docstring 自称"2026-08 旧批次回填，幂等可重跑"；P3-1 也承认"已执行完毕 3 个"）。风险只在重跑时兑现，与同栏"在线可被任何人利用的 session fixation"（P0-1）完全不是一个时效级别。修复仍要做（半小时），但放在 P0 会稀释 P0 的语义。 |
| P1-7 build_sw_tree 死循环体 | P1 | **P3** | 纯编辑残留，唯一副作用是特定脏数据下日志打两遍（stock_industry.py:224-235 vs 236-237 亲核属实）。无正确性影响，是死代码而非"真实 Bug"——且它本来就不该在"真实 Bug 与潜伏缺陷"一节（见 5.2）。 |
| P1-10 stop_loss 全表元数据扫描 | P1 | **P3** | `instrument_metadata` 表量级是标的池大小（数百行），全表 SELECT + Python 线性扫描的代价是微秒级，且每次止损计算只发生一次（stop_loss.py:49-54,194-199）。换成主键查询（db.py:794 现成）是对的一行修改，但性能收益约等于零，与 P1-9（持仓列表 2N 次全量行情读，有 115 秒事故前科）放同一档严重失真。 |
| P1-12 报价缓存键不一致 | P1 | **P2** | 报告自己承认"当前调用链上游都先 normalize_symbol，所以是潜伏问题"（data/service.py:191-222）。潜伏 + 一行入口归一化可修，P2 合适。 |
| P1-16 subject_market 2 秒无限轮询 | P1 | **P2** | 单用户系统，无并发代价，影响只是笔记本合盖后的电量与一个 innerHTML 未转义（后者应并入 P1-5 的 XSS 条目）。退避+visibilitychange 是对的方向，但不是 P1。 |

### 应升级

| 条目 | 现级 | 建议 | 理由与依据 |
|---|---|---|---|
| P2-15 无定时 DB 备份 | P2 | **P1** | 这是单人单机项目**唯一不可再生的资产**（2.8GB 行情 + 交易记录 + 回测结果，已亲核 data/trend_quant.db=2.8G）。当前备份依赖偶发事件触发（db.py:78-99 仅两个调用方），最近一次手工备份停留在登录墙改造前，且不匹配 keep=3 修剪规则会永久留存；叠加 P2-23 指出的 git bundle 备份断更一个月——**代码与数据两条备份线同时处于事实失效状态**。对单人项目，数据丢失的期望损失远高于 XSS（登录墙后单用户），应排在所有前端条目之前。 |
| （新增）dev 依赖未声明 | 无 | **P1** | 见 1.3。阻塞 P2-18 的 CI 落地与任何新环境验证。 |
| （新增）jobs.py 本地时区门控 | 无 | 并入 **P1-13** | 见 1.2。P1-13 现有证据全是 intraday_service，补上 jobs.py:63 与 main.py 两处 `_date.today()` 后该条目的说服力反而完整了。 |

### 维持但需补充说明

- **P0-6 测试收集失败**：P0 合理——后续所有修复都靠测试套件验证，收集即失败等于蒙眼施工。但条目内捆绑的"pytest-asyncio Unknown config option 警告"是洁癖级，应拆出（并入新的 dev 依赖条目），否则"完成"无法判定。
- **P2-4 DataService 随处 new**：维持 P2，但应注明与 P0-2 的**叠加关系**：无鉴权 MCP + 实例级限流器（provider_tickflow.py:43-44）意味着公网匿名调用 `calc_stop_loss` 时每个入口各自持有限流预算， vendor 额度被分身稀释的问题会被放大。P0-2 修复后此条紧迫性自然下降。
- **P1-9 compute_manual_trade 重复加载**： severity 维持 P1（有事故前科），但它不是"真实 Bug"，是性能条目，应移入第六节（见 5.2）。

---

## 三、方案质量（站在个人项目/单开发者/单机部署的约束下）

### 3.1 Database 拆分（P2-1）：方向对，但**现在不做全拆**

报告的克制是对的（"优先级不高"），我要更进一步：单人项目不存在"冲突面最大"问题（不会和自己 merge conflict），2119 行的真实痛点只是导航与 `_init_tables` 350 行 DDL。全拆 105 个方法为 5+ 个 Store 类是 2-3 天的纯风险工时，收益主要是审美。**替代方案**：(a) `_init_tables` DDL 按表拆段（报告已提，半天）；(b) 仅把 users/sessions 两个域（约 150 行，db.py:341-362,1444-1530 一带）随 P1-3 的 token 哈希改造顺手迁到 `services/auth.py` 邻近模块——安全改造本来就要动这块代码，边际成本最低。其余冻结。

### 3.2 EOD/盘中聚合合并（P1-17）：方向正确，但必须两阶段，报告缺测试兜底路径

500 行平行实现（dashboard.py:278-395 vs intraday_service.py:511-1019）确为最大漂移温床，判断准确。但一次性合并是高危重构，报告只给了目标没给路径。建议明确两阶段：**阶段一**只抽 5 个纯函数（`_ma5`/`_strength`/`_priority`/`_key_tuple`/`_macd_counts`，dashboard.py:61-80,224-228 vs intraday_service.py:459-508,971-975，逐字相同，零风险，半天）；**阶段二**再动聚合骨架，且必须以报告自己提到的"cached vs 全量盘中双实现一致性测试"为合并守门员——先确认该测试覆盖两条路径再动手。阶段二若评估后 ROI 不足，停在阶段一也是可接受的终态（漂移温床缩小 80%）。

### 3.3 前端公共 JS 抽取（P1-19/行动#7）：赞成，两个落地陷阱报告没提

web/static/ 已存在且已被使用（echarts.min.js、style.css），无构建工具需求，1-2 天估价现实。但：
- **缓存失效**：`asset_version` 只跟踪 style.css 的 mtime（main.py:277，报告 P2-20 已抓到但**没有与行动#7 联动**）。app-common.js 上线后若版本串不含它，浏览器缓存的旧文件会让"消除漂移"变成"引入第三种漂移"。行动#7 必须显式包含"asset_version 扩展为跟踪所有静态资源"。
- **先裁决口径再抽取**：止损卡渲染族两处副本已漂移（manual_trade.html:304-369 有硬止损附带当日最低/收盘，subject_market.html:1128-1159 没有），`postJson` 两处 401 处理不一。抽取前必须先做一次产品决策"以哪份为准"——这不是机械合并，报告把它写成纯技术动作了。

### 3.4 增量 qfq 物化（P2-8/行动#12）：数学成立，成本估高了，且有一个边界条件没写

我验证了 `core/adjustment.py:88-96` 的数学：divisor 是"≥bar 日期的因子后缀积"，对最新除权日之后的 bar 恒为 1。因此"无新因子时新 bar 直接 append raw 值"**严格正确**（不是近似）。实现只需改 `data/service.py:382-384` 的触发条件：`raw_updated and not factors_changed and not qfq_behind and 新bar日期 > max(ex_date)` → 走 append 路径。成本约**半天**（报告与 iterrows 捆绑估 1-2 天），且 `tests/unit/test_adjustment.py` 已有完善单测兜底。报告漏写的边界：**回补历史数据场景**（新 bar 落在历史除权日之前）divisor ≠ 1，必须仍走全量重写——触发条件里务必保留对"新 bar 日期 vs 最大 ex_date"的判断，否则补历史时会写入未除权的脏 qfq。

### 3.5 配置收口（P2-5/行动#13）：建议本身有一个无法落地的 bug

报告建议"新增 `.env.example` 全量列出"，但 `.gitignore` 第 16 行是 `.env.*`——我用 `git check-ignore -v .env.example` 验证：**该文件会被忽略，提交不进去**。行动#13 必须包含"`.gitignore` 增加 `!.env.example` 例外"，否则这条行动以当前形态执行必然失败。

### 3.6 定时备份（P2-15/行动#11）：磁盘预算没算

报告建议"每日更新成功后 `backup_to(keep=7)`"。主库 2.8GB，keep=7 ≈ **20GB 磁盘**，且 `VACUUM INTO` 是全量拷贝，每日一次对同盘 IO 与备份窗口都不便宜（db.py:89-93）。建议修正为：keep=3 日备（~8.4GB）+ 每周一份异机/云盘（与 P2-23 的 git bundle 机制一并决策：要么恢复月备要么从 README 删除约定）。同时修剪 glob（db.py:94）兼容存量手工备份的问题报告已提，保留。

### 3.7 CI 引入（P2-18/行动#14）：可行，但要防止做成多矩阵过度设计

已验证 `git remote origin = github.com/yyx-123/trend-quant.git`，GitHub Actions 可行，半天估价合理。两个约束：(a) **单矩阵即可**（一个 Python 版本 × 一个 OS——部署目标只有一个，矩阵 CI 对个人项目是纯浪费）；(b) 前置依赖是先解决 dev 依赖声明（1.3），否则 CI 环境连 pytest-asyncio 都装不出。若不想维护 Actions，更省的替代是 Makefile 已有 test 目标 + 本地 pre-push hook——两者任选其一即可，不必都做。

### 3.8 慢请求计时中间件（P2-13/行动#11）：赞成，这是全报告投入产出比最高的一条

纯 ASGI 中间件约 30 行，直接回答"115 秒事故"类问题的第一现场。建议从行动#11 的捆绑中单独提前。

---

## 四、行动清单（第十一节）评估与重排

### 4.1 现有清单的三个结构性问题

1. **清单覆盖不全**：以下有编号的条目在行动清单里**没有对应行**——P1-3（cookie Secure/session token 哈希）、P1-4（CSRF/logout 改 POST）、P1-6（deploy.sh 路径+裸 rm -rf）、P1-12（报价键归一）、P1-16（轮询退避）、P2-3（RevisionCache 单例）、P2-4（DataService 单例）、P2-12（日志补盲）、P2-16（SQLite 加固）、P2-17（测试卫生）。其中 P1-3/P1-4/P1-6 是 P1 级却无行动，优先级清单失去了"完备索引"的作用。
2. **捆绑粒度不一**：行动#8 把"删一个死循环体（5 分钟）"和"style.css 67 个死类对照清理（需脚本化验证，数小时）"捆成 1 天；行动#12 把两个风险画像完全不同的优化（iterrows 是纯重构、qfq 增量碰数据正确性）捆成 1-2 天。应拆开，否则无法追踪与验收。
3. **排序对单人项目的真实风险权重不足**：数据备份（#11，P2 位）排在前端重构（#7）之后——对这个项目，丢数据的期望损失远大于 XSS 与代码漂移（见二、P2-15 升级理由）；测试兜底（#9）排在大规模重构（#7、#8）之后，顺序反了——**应该先补测试再重构**，否则#7/#8 没有安全网。

### 4.2 建议重排（含成本重估）

| # | 级别 | 动作 | 成本 | 备注 |
|---|---|---|---|---|
| 1 | P0 | 删 `/__dev_set_session` + 豁免 | 5 分钟 | 不变 |
| 2 | P0 | 两个 MCP 测试文件 importorskip | 10 分钟 | 从原#4 提前：先让测试能跑，后续一切靠它验证 |
| 3 | P0 | 修 instruments.py:102 漏导入 + 降级路径测试 | 半小时 | 不变 |
| 4 | P0 | MCP 鉴权或摘公网暴露 + DNS rebinding 评估 | 半天 | 不变 |
| 5 | P1 | **每日定时 DB 备份（keep=3）+ 修剪 glob 兼容 + 异机周备决策** | 半天 | 原#11 拆分升级；理由见二、3.6 |
| 6 | P1 | **dev 依赖组声明（pytest/pytest-asyncio/pytest-cov/ruff）** | 10 分钟 | 新增，原 P0-6 后半并入 |
| 7 | P1 | 登录限流 + 登录审计日志 + 3 处 XSS + 1 处双重转义 | 半天 | 原#6 不变 |
| 8 | P1 | **补 H1-H3（jobs/scheduler/lifespan）测试** | 1 天 | 原#9 提前到所有重构之前 |
| 9 | P1 | 前端公共 JS 抽取（**含 asset_version 扩展 + 漂移口径裁决**） | 1-2 天 | 原#7，补两个前置，见 3.3 |
| 10 | P1 | 时区整治：intraday_service 11 处 + **jobs.py:63 + main.py 两处** 全改 market_now() | 半天 | 原#13 拆分，证据补强，见 1.2 |
| 11 | P1 | 批量回测标的快照冻结 | 半天 | 原#10 不变 |
| 12 | P2 | qfq 增量物化（含"新 bar 日期 > max(ex_date)"边界） | 半天 | 原#12 拆分，见 3.4 |
| 13 | P2 | 慢请求计时中间件 + 失败告警哨兵 | 半天 | 原#11 拆分 |
| 14 | P2 | `.env.example` + **`!.env.example` gitignore 例外** + env 收口 | 半天 | 原#13 拆分，见 3.5 |
| 15 | P2 | 死代码清理（死循环体/instruments 死 JS/CSS 死类） | 1 天 | 原#8 降 P2（主体是 P1-7 等已降级条目） |
| 16 | P2 | 引擎热路径去 iterrows | 1 天 | 原#12 拆分，可延后，无用户可感问题 |
| 17 | P2 | 迁移脚本备份改 backup_to() + 服务运行检测 | 半小时 | 原#5 降级 |
| 18 | P2 | CI 单矩阵 + 移除 .coverage/zip 跟踪 | 半天 | 原#14，依赖#6 |
| 19 | P2 | P1-3/P1-4/P1-6/P1-12/P2-16 等清单遗漏项打包 | 1 天 | 见 4.1-1 |
| 20 | P2 | MCP 工具测试补全 + provider/service 缺口（附录 A） | 1-2 天 | 原#15 |
| 21 | P2 | chinese_calendar 年度升级写入部署文档 + 日历过期 UI 提示 | 1 小时 | 新增，见 1.2 |
| 22 | P3 | Database DDL 分段 + auth/session 域随 P1-3 顺手迁移；scripts 整理；文档更新 | 持续 | 原#16 修正，见 3.1 |

成本总评：原清单单项估价大体合理（略乐观 10-20%），主要问题是**漏项与排序**而非单价。

---

## 五、报告结构与表达

1. **P1-13 与 P2-6 实质重复**：同一问题（裸 datetime.now）的条目级与项目级两次陈述，且 P2-6 的"~30 处"与 P1-13 的 11 处口径关系未说明。应合并为一条（P1-13 列证据 + 一段约定），行动#13 已经事实上把它们捆在一起了。
2. **P1-7 归类错误**：编辑残留死代码归在"真实 Bug 与潜伏缺陷"，应并入 P1-18（死代码）或 P1-17。同一节里 P1-9/P1-10 是性能条目归在 Bug 节、P1-14/15/16 是前端健壮性归在 Bug 节——第三节实际是"P1 杂物间"，导致第六节（性能，全 P2）与第三节里的性能条目（P1）分级标准肉眼不一致（P1-10 的影响比 P2-7 小几个数量级）。
3. **条目内重复**：P1-17 末尾的 `except AttributeError` 清理（dashboard.py:231-240）在 P1-18 第 5 条原样重复出现。
4. **单条目塞多件事，无法勾选完成**：P0-6（收集失败 + pytest-asyncio 警告，两个级别）；P2-16（busy_timeout / foreign_keys / checkpoint / VACUUM INTO 路径拼接，四个独立加固项——其中 foreign_keys 不生效导致删用户留孤儿行（db.py:341,357）是**数据正确性问题**，比其它三项重，应拆出单列）；P2-20（6 个不相关 UX 子项，且"缓存串三种写法 + asset_version 只跟踪 style.css"是会导致线上 stale 的 **bug 级**问题，不应埋在 UX 条目倒数第二句里——它还恰是行动#7 的前置，见 3.3）。
5. **个别条目描述不足以执行**：P2-14"失败落一个显眼的哨兵文件/每日首次访问弹窗级别提示"给了两个未决选项，执行者仍需自己做设计决策；建议报告直接选定一个（哨兵文件 + 导航栏盘后更新条复用，与 P2-14 自己说的"唯一用户可见面"闭环）。
6. **附录 A 质量高**，但 `migrate_raw_qfq.py` 测试场景（"破坏性最高的迁移脚本零测试"）指向一个已执行完毕的一次性脚本，补测价值低于 jobs/scheduler，建议标注可选。

---

## 总评

这是一份**质量明显高于常见 AI 生成审查**的报告：行号亲核、有正面案例、有"潜伏 vs 现役"的区分意识、附录 A 的补测场景可直接执行。主要修正空间在四处：

1. **覆盖面**：并发无专项（结论其实是良好的，但应记录）、数据口径漏了 jobs.py 本地时区门控与 chinese_calendar 年度边界、依赖管理只抓到症状（缺 dev 依赖这个根因）、缺"设计容量"结论。
2. **分级**：P0-4/P1-7/P1-10/P1-12/P1-16 应降，P2-15（备份）应升——对单人单机项目，"数据会不会丢"的权重应高于"代码漂不漂"。
3. **方案**：Database 全拆应冻结（只做 DDL 分段 + auth 域顺手迁移）；聚合合并需两阶段并明确测试守门员；前端公共 JS 有两个未提的前置（asset_version、口径裁决）；增量 qfq 数学成立但成本高估且漏了补历史场景的边界条件；`.env.example` 建议在现状下无法提交。
4. **行动清单**：9 个有编号条目无行动行（含 3 个 P1）、测试应排在重构之前、备份应提前。

### 必须修改（报告修订的阻塞项）

1. 行动清单补全遗漏条目（至少 P1-3/P1-4/P1-6），并重排：备份升 P1 提前、H1-H3 测试移到所有重构之前（按 4.2 表）。
2. P0-4 降 P2、P2-15 升 P1、P1-7/P1-10 降 P3，消除"P1 杂物间"的分级失真。
3. 行动#13 补 `!.env.example` 的 gitignore 例外（否则该行动必然执行失败）；行动#7 补 asset_version 扩展与止损卡口径裁决两个前置。
4. P1-13 证据清单补 `core/jobs.py:63` 与 main.py 两处 `_date.today()`。

### 建议修改

5. 新增三个条目：dev 依赖未声明（P1，阻塞 CI）、chinese_calendar 年度边界运维 ritual（P2）、并发审查结论节（记录正面设计 + `_market_symbols_cache` 跨进程失效 + 调度器并发×busy_timeout 联动）。
6. 聚合合并（P1-17）改写为两阶段方案，阶段二以双实现一致性测试为守门员，允许停在阶段一。
7. qfq 增量物化（行动#12）拆出单行，补"新 bar 日期 > max(ex_date) 才走 append"的边界条件，成本改估半天。
8. 备份方案从 keep=7 改为 keep=3 日备 + 异机周备（2.8GB×7 的磁盘账报告没算）。
9. P2-16 拆条，foreign_keys 孤儿数据子项单列；P0-6 拆出 pytest-asyncio 警告并入 dev 依赖条目。
10. CI 明确单矩阵约束，前置依赖 dev 依赖条目。

### 可选

11. `is_trading_time` 补交易日校验或改名（calendar.py:88-99）。
12. 登录计时侧信道（trade_records.py:66）一句话记录即可。
13. pandas/numpy/fastapi 依赖加上界。
14. 附录 A 中 migrate_raw_qfq.py 补测场景标注"可选"（一次性脚本已执行）。
15. "总体评价"补错误恢复路径的正面结论（catchup 三路检测、rerun_daily_update.py、session 清理）。
