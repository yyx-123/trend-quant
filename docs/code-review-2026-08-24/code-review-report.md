# 代码全量审查报告（最终版 v2）

- 审查日期：2026-08-24（v1）→ 2026-08-25（v2 修订）
- **审查基线**：2026-08-24 的工作区（含 8 个未提交修改文件 + 1 个未跟踪脚本 `scripts/backfill_batch_excess_metrics.py`；相对 HEAD bba50b3）。注意：审查期间工作区中 `src/app/routers/auth.py`、`src/app/main.py` 的未提交改动被回滚，影响一个条目（见 R-0）。
- 审查范围：`src/` 全部 68 个 Python 模块（约 1.7 万行）、`web/` 9 个模板 + style.css（约 1.5 万行）、`scripts/` 13 个脚本、`tests/`（702 个可收集测试）、`config/`、README/CLAUDE/Makefile/pyproject/.gitignore
- 审查方法：主审查人逐文件精读后端全部核心模块；3 个并行扫描代理覆盖前端/测试/脚本运维；随后 1 个**独立盲审代理**（不接触本报告）同范围重审、2 个**评审代理**（事实核查 + 架构评审）核查本报告；全部分歧已裁决，三方意见与修订记录见同目录 review-1-factcheck.md / review-2-arch.md / blind-audit-comparison.md / CHANGELOG.md
- 版本：v2.2（终版）。v1→v2→v2.1→v2.2 的全部修改见 CHANGELOG.md

## 总体评价

代码质量整体**高于一般个人项目**：核心计算（趋势值、指标、回测引擎）有单一实现来源和 golden 测试锁定；新鲜度/口径约定（DB 优先、盘中不落库、data_versions 内容版本）执行自觉；密码 pbkdf2、参数化 SQL、HttpOnly cookie 等安全基本盘在线。

评审代理复核后补充的**正面结论**（避免高估风险）：
- **并发纪律总体良好**：RevisionCache 双检锁单飞（dashboard.py:39-48）、`_update_job_lock` 非阻塞防重入（main.py:108-118）、per-symbol 锁（data/service.py:21-34）、每调用新建连接规避 `check_same_thread`（db.py:66-76）。残留风险见 P2-16/N。
- **错误恢复路径好于直觉**：`_daily_update_catchup` 三路漏更检测（main.py:204-235）、`scripts/rerun_daily_update.py` 手动恢复、批量回测启动清理（db.py:1920-1930）、过期 session 有清理（services/auth.py:44）。

主要系统性问题集中在五块：
1. **安全暴露面**：MCP 通道整体无鉴权 + 以工具参数传密码（P0-1/P0-2）；登录无防护无审计（P1-1）。
2. **部署可用性断裂**：全新部署无法创建首个用户（P1-3），deploy.sh 与线上现状全面漂移（P1-7）。
3. **数据正确性口径**：Web 日 K 指标的窗口截断与 MCP 已修 bug 同源（P1-4）；EOD/盘中聚合口径不一致（P2-8/G）；时区混用（P1-5）。
4. **运维体系单薄**：唯一不可再生资产（2.94GB 主库）无定时备份（P1-2）；无 CI、无 dev 依赖声明、无失败外发告警。
5. **重复实现已发生真实漂移**：后端 `_category_path` 等 4-6 份、前端 `esc` 7 份、EOD/盘中两套 500 行聚合骨架、止损卡渲染两份且内容已不一致。

---

## 〇、已闭环事项（R-0）

### R-0 `/__dev_set_session` session fixation 端点 —— 审查期间已消失，确认闭环

- 时间线：2026-08-24 审查开始时，该端点存在于**工作区未提交改动**中（`src/app/routers/auth.py` 尾部 + `main.py` 登录墙豁免名单；会话起点 git status 显示这两个文件为 modified）。其模式是教科书级 session fixation：GET 端点把 query 中任意 token 写入 session cookie 且在登录墙豁免名单内。代码注释自标「临时端点·验证完必须删除」。
- 现状：审查期间该未提交改动被回滚。2026-08-25 复核：当前工作区 `auth.py` 仅 65 行无此端点、`main.py` 豁免名单无此路径、全仓 grep 与 `git log --all -S` 均无匹配。**无需行动，已闭环。**
- 教训保留：临时调试端点应有明确的生命周期管理（创建时即登记待删清单/验收手册条目），避免"自标临时但长期在线"。

---

## 一、P0（现役，立即处理）

### P0-1 MCP 通道 7 个工具中 5 个完全无鉴权

- 位置：`src/app/main.py:313`（`/mcp` 前缀整体豁免登录墙）、`src/trend_mcp/server.py:99,124,195,306,348`
- 问题：`trend_dashboard`、`intraday_dashboard`、`symbol_detail`、`calc_stop_loss`、`list_instruments` 五个读工具**没有任何鉴权**（只有 `add_trade`/`open_positions` 调 `tr.authenticate`，server.py:444,483）。`main.py:304-305` 注释宣称「MCP 为机对机通道，工具调用自带 username/password 逐次鉴权」——**与实现矛盾**（7 个里只有 2 个验密码）。服务经 frp 对公网暴露（main.py:381-383 注释自述无 nginx 前置）。
- 影响：任何人可读取全市场行情缓存/标的池/指标数据，登录墙对数据面形同虚设；盲审补充：`intraday_dashboard` 不传 category 时任何人可触发 600+ 标的全市场实时计算（server.py:133 自述需 1 分钟以上），构成未鉴权的 CPU/行情额度 DoS 向量；`calc_stop_loss` 可被当免费 API 滥用烧 tickflow 付费额度。
- 建议：二选一——(a) MCP SSE 仅监听 127.0.0.1/独立通道，不随 frp 暴露；(b) 加鉴权（FastMCP auth 或前置验 token 的 ASGI 中间件），读工具至少一层静态 token。另：`server.py:50` 主动关闭 `enable_dns_rebinding_protection`，与无鉴权叠加，需一并评估恢复。

### P0-2 MCP 以工具参数传密码，凭据进入 LLM 上下文与日志

- 位置：`src/trend_mcp/server.py:420-454`（`add_trade`）、`462-557`（`open_positions`）
- 问题：username/password 作为 MCP 工具参数逐次传递——出现在 MCP 客户端日志、LLM 对话上下文、可能的云端模型请求里。
- 建议：迁移到 token 制（服务端预签发静态 token，配置在 MCP 客户端 env）；过渡期文档明示该账号应为低权限专用账号。

### P0-3 `instruments.py` 潜伏 NameError：类目表为空时分类下拉/新增/更新全部 500

- 位置：`src/app/routers/instruments.py:102` 调用 `_config_items()`，但 20-30 行 import 清单里没有它（定义在 `src/services/instrument_admin.py:62`；同模块 23 行已导入 `_config_name_map`，说明是遗漏而非有意）
- 触发条件：`db.list_instrument_categories()` 返回空（instruments.py:97-99 的降级路径才走到 102 行）；`/api/categories`、`/api/add`、`/api/{symbol}/update` 均经此。当前表非空所以没炸；全新部署/表被清空时 100% NameError。
- 建议：补 import；为该降级路径补空表 API 测试（现有测试全部命中表非空路径所以没抓到）。盲审给出更彻底的选项：删掉这条从 metadata 反推分类树的 fallback（与 instrument_categories 表职责重叠）——二选一，至少修一个。

### P0-4 测试套件收集即失败：2 个 MCP 测试文件缺依赖守卫

- 位置：`tests/unit/test_mcp_stop_mode.py:5`、`tests/unit/test_mcp_symbol_detail.py:24` 顶层 `from trend_mcp import server`
- 实测复现：未装 `mcp` 的环境下 `pytest --collect-only` → **702 collected, 2 errors**（ModuleNotFoundError 中断收集）。后续所有修复都靠测试套件验证，收集即失败等于蒙眼施工。
- 建议：两个文件加 `pytest.importorskip("mcp")`。（同文件发现的 `asyncio_mode="auto"` 警告是死配置——async 测试实际走 `unittest.IsolatedAsyncioTestCase`（stdlib）正常运行，见 tests/test_market_view.py:182；该配置项并入 P1-6 dev 依赖条目一并清理。）

---

## 二、安全专项（P1/P3）

### P1-1 登录接口无暴力破解防护、零审计日志，且 4xx 不可见

- 位置：`src/app/routers/auth.py:37-52`（无限流/锁定/日志，全文件无 logger）；`src/services/trade_records.py:58-68`（`authenticate` 被登录与 MCP 共用）；`main.py:249-265`（4xx 刻意不记日志）
- 影响：公网暴露下可无限爆破；pbkdf2 20 万次迭代（db.py:23）使每次尝试耗约 0.1s 服务端 CPU，本身也是 DoS 向量；爆破行为在任何日志里都不可见。
- 建议：进程内滑动窗口限流（如每 IP+用户名 5-10 次/分钟 + 指数退避）；登录成败写审计日志（含来源 IP）；对 401/403 计数采样记录。

### P1-2 无定时 DB 备份 —— 唯一不可再生资产处于无保护状态（v2 自 P2 升级）

- 位置：`db.py:78-99`（`backup_to()` 仅 2 个调用方：indicator_builder.py:126 全量重建前、一个迁移脚本）
- 现状：主库 2.94GB（行情 + 交易记录 + 回测结果，单人项目唯一不可再生资产）。日常备份靠偶发事件触发；手工备份 `pre-auth-wall-20260824.db`（1.7GB）不匹配 keep=3 修剪 glob（db.py:94）会永久留存；git bundle 代码备份断更一个月（最新 bundle 停留在 2026-07-18）——**代码与数据两条备份线同时事实失效**。
- 建议：每日更新成功后自动 `backup_to(keep=3)`（约 8.8GB 磁盘，keep=7 的 20GB 预算不现实）+ 每周一份异机/云盘；恢复演练至少做一次；手工备份命名/存放位置写入运维约定。

### P1-3 无用户创建入口 —— 全新部署无法登录（盲审新发现，亲核属实）

- 位置：`db.py:1428` `create_user` 在 src/、scripts/ **全仓零调用方**（仅 tests 使用）；deploy.sh 全流程不建用户、不配 `.env`；README 部署章节（53-64 行）无首个用户引导
- 影响：按 README/deploy.sh 部署的全新实例，登录墙生效后无人能登录，只能手工 sqlite 插明文密码再靠启动迁移哈希——该引导流程无任何文档记载。
- 建议：提供 `scripts/create_user.py`（参数式，哈希落库）并在 README 写明；deploy.sh（若保留）补 `.env` 与首个 admin 引导。

### P1-4 Web 日 K 指标在「截断窗口」上计算 —— 与 MCP 已修复 bug 同源（盲审新发现，亲核属实）

- 位置：`src/app/routers/market_view.py:288-290` 先 `data.tail(limit)`，随后 `build_market_payload` 在该窗口上算 EMA/MACD/RSI/趋势值（:183,307）；而 `trend_mcp/server.py:223-227` 注释明确记载「EMA 族指标无限记忆，先截断再算会让数值依赖请求窗口——旧的窗口截断 bug」，MCP 已改全历史计算
- 影响：`limit` Query 允许 `ge=1`（market_view.py:250），显式传小 limit 时同一标的同一日期的 MACD/EMA/趋势值随窗口变化，Web 与 MCP 口径不一致。默认 20000 掩盖了问题，属潜伏 bug。
- 建议：与 MCP 对齐——指标全历史计算、输出数组再 tail。

### P1-5 时区混用：「今天」的判定分散在两种时钟（v1 P1-13 + P2-6 合并，证据补强）

- 位置：`core/jobs.py:63` `today = date.today()`（**每日 16:30 补库主任务的交易日门控**用宿主机本地日期，而调度器按 `settings.app.timezone` 触发——UTC-9 等时区主机上会判错一天）；`main.py:167,175` 两处 `_date.today()`；`routers/rule_backtest.py:72-73`；`intraday_service.py` 11 处 `datetime.now()`（538,579,668,732,801,807,820,826,867,1010,1016）
- 背景：`core/calendar.py:37-57` 专门造了 `market_now()` 解决非 CN 时区主机偏差；生产咽喉模块反而漏改。
- 建议：凡「交易日/会话语义」一律 `market_now()`，凡「系统事件时间戳」可 `datetime.now()`，约定写进 CLAUDE.md；全局 grep `date\.today\(\)|datetime\.now\(\)` 逐一归类。

### P1-6 dev 依赖未声明 —— 新机器装完跑不起测试，阻塞 CI（v2 新增，评审 2 指出根因）

- 位置：`pyproject.toml` 的运行时 dependencies 之外只有 tushare 一个可选组；pytest/pytest-cov/ruff 等开发工具全部未声明（无 dev 可选组、无 requirements-dev.txt、无 lock 文件）；`mcp>=1.0.0` 等仅下界约束（tickflow 唯一钉版）
- 影响：新机器/新环境靠 `pip install -e .` 装不出可跑测试的环境，CI 落地第一块绊脚石。（精确表述：mcp 在运行时依赖中，P0-4 的收集错误源于本地 venv 未安装它而非缺 dev 组——但 dev 组缺失意味着任何新环境都无法规范地获得测试工具链。）
- 建议：加 `[project.optional-dependencies] dev = [...]`；顺手删 `asyncio_mode` 死配置（P0-4）；pandas/numpy/fastapi 考虑加上界（可选）。

### P1-7 deploy.sh 与线上现状全面漂移，且以 root 运行服务

- 位置：`scripts/deploy.sh:11,68,106-109,169-176`
- 问题：INSTALL_DIR 硬编码 `/opt/trend-quant`（线上实为 `/srv/trend-quant`，git log 04559e4 更正过文档但脚本没跟上）；目录无 `.git` 时无确认 `rm -rf`（:68）；systemd `User=root`；配 nginx 反代而线上实际直连 frp（main.py:381-383）；auth_basic 默认注释（假设无登录墙时代）；代码走 GitHub clone 而 README:68 说分发走 git bundle——**脚本、文档、现实三方互不一致**。
- 建议：按 frp + /srv 现状重写或删除（以 docs/stock-industry-etf-holdings/server-rollout.md 为准）；服务降权专用用户；删除前把仍有效的步骤（systemd/log 目录）抽出来。

### P1-8 前端存储型 XSS 三处 + 一处双重转义（全部亲核）

- `web/templates/market_view.html:1689`：`chartTitleEl.innerHTML = \`${payload.display_label ...}\`` 未走 `esc()`——标的名称来自外部行情接口 + 用户可编辑，存储型入口。
- `web/templates/batch_backtest.html:1053`（renderHeatmap）与 `:1484`（renderYearChart）：ECharts tooltip 拼接用户可命名的策略名/类目名未转义；同文件 renderScatter:1310 却用了 `esc()`——知道要转、漏了两处。
- `web/templates/subject_market.html:1092`：`statusEl.innerHTML` 拼入未转义的 `st.last_error`（服务端异常文本）。
- `web/templates/market_view.html:1604`：`esc()` 产物赋给 `textContent` → 双重转义（策略名含 `&<>` 时显示 `&amp;` 字面量）。
- 建议：三处补 `esc()`/改 textContent；修双重转义；「tooltip formatter 一律 esc」写入前端约定。

### P1-9 会话硬化包（cookie Secure / token 哈希 / 绝对过期 / 明文兜底观测）

- `auth.py:45-51`、`main.py:364-367`：cookie 仅 HttpOnly + SameSite=Lax，无 Secure（frp 出口若为 HTTPS 应加，做配置开关）；`SESSION_TTL=30 天` 滑动续期无绝对上限（services/auth.py:8 自述「活跃用户实际永不过期」），被盗 token 长期有效。
- `db.py:355-362,1467-1473`：session token 明文落库，库文件泄露 = 全量在线会话被劫持；建议存 SHA-256 摘要。
- `db.py:34-45`：verify_password 明文兜底分支（45 行）为迁移遗漏保留；建议确认全库无遗留明文后加 warning 观测或排期删除。
- 可选：绝对过期（如 90 天强制重登）、「登出全部会话」入口、登录计时侧信道缓解（trade_records.py:66 用户不存在时不跑哈希，毫秒级可区分有效用户名——单人系统危害极低，记录备查）。

### P1-10 CSRF 单防线 + GET logout + 登录墙边缘行为

- 全站变更端点无 CSRF token，仅靠 SameSite=Lax（单防线；logout 为 GET（auth.py:55）且在登录墙豁免名单内（main.py:312），CSRF 强制退出连有效 session 都不要求（危害低）。
- 边缘：`main.py:331` `"/api/" not in path`——`/api`（无尾斜杠）会被当页面请求 303 跳转而非 401 JSON；`_EXEMPT_PREFIXES` 用 startswith，`/mcp` 会匹配 `/mcpanything`（当前无此路由，属脆弱写法）。
- 建议：变更请求要求自定义头（如 `X-Requested-With`）并在 AuthWall 校验，或引入 CSRF token；logout 改 POST 并移出豁免名单；豁免匹配改精确段匹配。

---

## 三、真实 Bug 与潜伏缺陷（其余，P1/P2）

### P1-11 批量回测 prepare 与 run 两次解析标的不一致

- 位置：`batch_service.py:387`（prepare）vs `:456`（run 重新 `resolve_batch_symbols`）
- 问题：策略快照冻结了、标的没冻结——中间新增/禁用/改类目会让实际执行格子数与 total_cells 对不上，进度条失真。
- 建议：标的快照随批次行冻结（新列或 config_json 内），run 直接读快照。

### P1-12 前端 401 处理覆盖不全：登出被伪装成「空数据」（v2 修正子条）

- 无统一 401 跳登录（redirectToLogin 仅 base/market_view/manual_trade/subject_market 四页有）：
  - `position_strategies.html:265-269`：401 JSON 无 `position_strategies` 字段 → 静默显示「暂无仓位策略」（**登出伪装成空数据**）
  - `rule_backtest.html:375-383`：把 401 JSON 原文当错误文案
  - `batch_backtest.html:420`：401 响应上 `meta.categories.forEach` 抛 TypeError → 全局红条「页面脚本出错」
  - `instruments.html:750-756`：有 `resp.ok` 检查会显示错误文案（v1 误报为「已加载 0 个标的」，已修正），但同样不跳登录
- 建议：统一封装带 401 处理的 fetch（见重复实现节），所有页面接入。

### P1-13 `build_intraday_overlay` 自建 DataService 从不 close —— 连接泄漏（盲审新发现，亲核属实）

- 位置：`intraday_service.py:204`（`ds = data_service or DataService()`，全函数无 close）；调用方 Web 标的页（market_view.py:319）与 MCP symbol_detail（server.py:280）都不传 data_service → **每次页面打开/工具调用新建一个 TickFlow/httpx client 泄漏**
- 对照：`stop_loss._fetch_intraday_bar`（stop_loss.py:82-88）有正确的 try/finally close。
- 建议：统一 try/finally close，或配合 P2-4 的 DataService 单例化一并解决。

### P2-1 引擎 `_prepare_bars` / `_filter_bars` 无 date/time 列时 KeyError

- 位置：`engine.py:283-286`、`rule_backtest/service.py:340-343`——`if "date" not in df.columns and "time" in df.columns: ... else: pd.to_datetime(df["date"])`，两列俱缺走 else 直接 KeyError。
- 建议：else 前先判空，报业务错误「行情数据缺少时间列」。

### P2-2 报价缓存与批量报价的键不一致（潜伏，v2 自 P1 降级）

- 位置：`data/service.py:198-202`（缓存命中以调用方原始 symbol 为键）vs `:216-221`（网络返回以归一化 symbol 为键）；函数入口无统一归一化
- 现状：上游（intraday_service.py:548、stop_loss.py:113）均已归一化，当前安全。
- 建议：入口统一归一化，一行修复防未来调用方踩坑。

### P2-3 provider_utils 缺时间列时伪造当前时间 —— 脏数据静默变「今日 K 线」（盲审新发现，亲核属实）

- 位置：`data/provider_utils.py:55`——无 time 列时全部行打 `datetime.now()`
- 建议：缺时间列时报错或丢弃行；该函数是 vendor 数据入口，静默伪造比显式失败危险。

### P2-4 看板 L2 排序疑似错用 `priority_l3`（盲审新发现，降调收录）

- 位置：`dashboard.py:267-275` `_sort_items` 与 `intraday_service.py:960-966` `_sort` 的 key 均含 `item["priority_l3"]`，L2 层级排序（dashboard.py:376-377、intraday_service.py:992-993）同样走它；两份实现一致（疑似复制粘贴）
- 说明：L2 行的 priority_l3 是其子级 min 聚合值，排序确定但字段意图不符（L2 排序语义上应用 priority_l2）。盲审 round-2 补强证据：`instrument_admin.py:101-117` 的 `category_priorities` 为三级各自返回独立 priority——priority_l2 字段存在的全部意义就是给 L2 排序用；当前 L2 排序改吃「子级 priority_l3 的 min 聚合」使 priority_l2 配置对排序**实际失效**（死配置）。误用概率高，但「按最重要子类排」也可能是有意设计，维持待确认定级。
- 建议：确认设计意图；若为误用改 priority_l2 并补排序测试。

### P2-5 看板聚合口径一致性与健壮性（盲审新发现，亲核属实）

- **EOD 成交额加权 vs 盘中简单平均**：EOD 聚合用成交额加权（dashboard.py:83-112 `_aggregate_daily`），盘中对 trend_score/涨跌幅用简单平均（intraday_service.py:842-846）——同一分组在 EOD/盘中两个视图的趋势值/涨跌幅不可比。建议统一为成交额加权（盘中已具备权重原料 `trend_series_amounts`）。
- **盘中聚合全 None 时产出 NaN/非法 JSON 风险**：intraday_service.py:736 `return_1d` 初始化为 None，尾部数据缺失的标的保持 None；`:843` `float(rows_df["return_1d"].mean())` 在整组全 None 时产出 NaN 或抛异常，NaN 经默认 JSON 序列化后浏览器 `JSON.parse` 会抛错。建议 mean 前 dropna，空则置 None。
- `dashboard_snapshot.latest_snapshot` 首次 DB 读取失败后**永久不再重试**（dashboard_snapshot.py:52-60：`_snapshot_loaded=True` 先置位，异常时 None 被永久缓存，进程重启前快照展示失效）。建议失败时不置 loaded 标志。
- `record_industry_sync_job` 无条件 `status="success"`（stock_industry.py:399）——部分行被高优先级挡下/回补 deferred 等异常形态在 job_runs 里全是 success，失去监控意义（P3）。
- `sync_industry_from_tickflow` 缺 API key 时 `os.environ[...]` KeyError 裸抛（stock_industry.py:355），月度调度任务的日志会是晦涩 KeyError 而非可操作提示（P3，建议 `.get` + 明确报错）。

---

## 四、冗余 / 死代码 / 重复实现

### P1-14 后端工具函数多处复制（盲审补全后全量清单）

| 函数 | 位置 | 份数 |
|---|---|---|
| `_category_path` | `db.py:705`、`routers/instruments.py:137`、`routers/market_view.py:47`、`trend_mcp/server.py:76`、`instrument_admin.py:84` | 5 |
| `_number`/`_num` | `dashboard.py:53`、`intraday_service.py:88`、`market_indicators.py:26`、`routers/market_view.py:96` | 4 |
| `safe_float` | `core/trend.py:44`、`data/provider_utils.py:9`、`rule_backtest/indicators.py:9`（签名各异） | 3 |
| SH↔SS 代码转换 | `provider_tickflow.py:46-58`、`stock_industry.py:61-74`、与 `core/symbols.py` 职责重叠 | 3 |
| `symbol_to_code` | `core/symbols.py:31`、`core/display.py:21`（逻辑全同） | 2 |
| `_date_span` | `data/service.py:400`、`instrument_admin.py:39`（逐字相同） | 2 |
| 报价 item→dict 规整 | `provider_tickflow.py:331-343` vs `399-411` | 2 |
| RSI avg_gain/avg_loss 计算 | `indicator_store.py:56-60` vs `116-120` | 2 |
| 元数据兜底加载 | `stop_loss.py:49-54` vs `trend_mcp/server.py:58-65` | 2 |
| `_normalize_symbol` 等透传包装 | `instrument_admin.py:27-36`、`routers/market_view.py:39-44` | 纯转发无价值 |

看板双实现的共用件（EOD vs 盘中逐字复制）：`_ma5`/`_strength`/`_priority`/`_key_tuple`/`_macd_counts`/`_assign_strength`/`_DISPLAY_DAYS=61`（dashboard.py:61-80,177,224-228,25 vs intraday_service.py:459-508,941,971-975,465）。

**最大的漂移温床**：EOD/盘中两套 500 行聚合骨架（dashboard.py:278-395 vs intraday_service.py:511-1019）。采纳评审 2 的两阶段方案：**阶段一**只抽 5-7 个纯函数（零风险，半天）；**阶段二**再动聚合骨架，以现有「cached vs 全量盘中双实现一致性测试」（test_intraday_trend_consistency.py）为守门员；评估 ROI 不足允许停在阶段一。

### P1-15 前端重复实现（已发生真实漂移）

- `esc` HTML 转义 7 份、至少三种写法：6 份 `function esc(`（rule_backtest.html:366、market_view.html:320、manual_trade.html:197、batch_backtest.html:388、instruments.html:271、position_strategies.html:196）+ 1 份箭头函数 `escHtml`（subject_market.html:120）。
- `postJson` 两份且**已漂移**：manual_trade.html:396 有 401 跳转（402 行），subject_market.html:1180 没有。
- 止损卡渲染族两份且**已漂移**：manual_trade.html:309 起 vs subject_market.html:1146 起——字符串拼接 vs 模板字面量、esc vs escHtml、fmtPrice vs fmtPrice3，硬止损触发文案不一致（两页同一数据展示口径不同）。
- 排序三件套两套：manual_trade.html:625-658 vs subject_market.html:807-835；侧栏目录四件套近乎逐行重复：instruments.html:575-669 vs subject_market.html:708-795；`fetchDayCandle` 2 份（manual_trade.html:374 vs subject_market.html:1191）；本地日期函数 4 份；金额缩写 4 份（阈值精度各异）；`redirectToLogin` 4 份。
- 建议：抽 `web/static/app-common.js`。**两个前置**（评审 2）：① `asset_version` 必须扩展为跟踪所有静态资源（现只看 style.css mtime，main.py:277——app-common.js 上线后旧缓存会让「消除漂移」变「引入第三种漂移」）；② 抽取前先裁决止损卡/postJson 以哪份为准（这是产品决策不是机械合并）。

### P2-6 后端死代码

- `routers/instruments.py:41`：模块级 `market_store = MarketStore()` 零使用（grep 确认仅 import + 定义两行）；`:16` 从 core.symbols 导入的三个公开名未使用（死 import）。
- `db.py:1325,1723,1793` 函数内重复 `import pandas as pd`、`:2117` 函数内重复 `import logging`（模块顶部均已导入）。
- `core/benchmarks.py:6-84`：`normalize_benchmark_mode`/`benchmark_symbol_for_mode`/`benchmark_label_for_mode`/`BENCHMARK_OPTIONS`/`COMPARISON_BENCHMARKS`/`CUSTOM_SYMBOL_BENCHMARK` 全仓零调用（grep 确认），仅 `benchmark_market_symbols`/`benchmark_instruments` 存活。
- 分钟 K 死链路：`data/service.py:169` `fetch_minute_history` → `provider_tickflow.py:305-313`（恒 raise）→ `provider_base.py:13` → `provider_utils.py:76` `parse_minute_period`，全链无调用方；`provider_tickflow.py:433` `fetch_trading_calendar` 恒返回 `[]` 无调用方。
- `indicator_builder.py:32` `DIVIDEND_CHECK_BARS`（144 行 `del end_date, lookback` 后仅剩常量）。
- `rule_backtest/loader.py:40-61` YAML fallback：`config/rule_strategies/` 目录在仓库中不存在，双存储模式残留。
- `config/app.yaml:19` `plan: starter` 是唯一被接受的值（provider_tickflow.py:39-40 其他值直接 raise）——无配置意义。
- `data/service.py` 的 `path` 字段三处口径不一（`sqlite/{symbol}` vs `sqlite/raw/{symbol}` vs `sqlite/{mode}/{symbol}`）——假数据字段，建议统一或删除。
- `dashboard.py:231-240` `except AttributeError` 兜底是测试替身遗留（生产 db 必有该方法），可清理。
- `services/stop_loss.py:49-54` `_load_instrument_metadata` 全表扫描找单标的：换成现成主键查询 `db.get_instrument_metadata(symbol)`（db.py:794），一行修复后该函数可删（v2 自 P1 降 P3——表仅数百行，代价微秒级，且只在非 tight 分支触发）。
- 磁盘残留（未跟踪）：`src/notify/`、`src/backtest/`、`src/portfolio/`、`src/strategy/`、`src/engine/` 五目录只剩 `__pycache__`；`src/app/routers/__pycache__/` 有 8 个已删路由的 pyc；`scripts/__pycache__/` 有 7 个已删脚本的 pyc；`src/data/__pycache__/` 留有 provider_akshare/efinance/yahoo.pyc。建议一次性清理。
- `tests/integration/test_intraday_service.py:433-439` `_all_same_score` 死辅助函数。
- **P3：`build_sw_tree` 死循环体**（stock_industry.py:224-235）：第一轮循环无任何赋值，唯一副作用是把含 '-' 行业名的 error 日志打两遍（234 与 247 逐字相同）。删除即可。

### P2-7 前端死代码（v2 修正清单）

- `style.css` 约 60+ 个类无模板引用，大头来自 8 个已删页面（backtest/config/index_market/logs/overview/parameter_optimization/strategy_history/trades）：`log-*` 族、`ov-param-table`、`benchmark-*`、`metric-*`、`calc-chain-*`、`fav-*` 等，部分占用 @media 块（style.css:3268,3304,3319,3958）。（注：`index-board-*` 词根在 subject_market 有活引用，清理时需逐一核对。）
- `instruments.html` 单行 backfill 死功能残渣：`setRowMessage`（761-766）、`refreshRowAfterBackfill`（962-970）、`runBackfill`（972-1017）、点击委托 backfill 分支（1483-1485）、`syncTaskControls`/另一处 `querySelectorAll('button[data-role="backfill"]')`（331、770）——约 60 行空转（rowHtml 已不再渲染该按钮）。注意 `nextStartDateForRow`（958-960）**是活的**（被 runBackfillAll:1302 调用），v1 误判已修正。
- `rule_backtest.html:421` `conditionLines()` 全项目无调用，其 CSS 类也不存在（双重死代码）。
- `instruments.html:671-675` `setupSectionObserver()` 空壳（observer 永远 null）；`manual_trade.html:143,150` 两个未使用变量。
- 建议：一次性删除（git 历史可回溯）；删后把「模板引用 vs 类定义」对照检查脚本化纳入 CI。

---

## 五、架构问题（P2）

### P2-8 `Database` 上帝对象（2119 行、105 个方法、20+ 表）—— 克制处理

- 位置：`db.py`（`_init_tables` 单方法约 350 行 DDL）
- 评审 2 裁决（采纳）：单人项目不存在协作冲突面，全拆 105 个方法为 5+ Store 是 2-3 天纯风险工时、收益主要是审美。**替代方案**：(a) `_init_tables` DDL 按表拆段（半天）；(b) 仅 users/sessions 域随 P1-9 的 token 哈希改造顺手迁出（边际成本最低）。其余冻结。

### P2-9 路由层持有易失任务状态 + JobManager 中断无标记

- 位置：`routers/rule_backtest.py:23-25`（`_rule_jobs`）、`routers/batch_backtest.py:37-38`、`services/instrument_jobs.py` 三个 Manager 单例
- 问题：任务状态全在进程内存，重启即丢。批量回测有 startup 清理兜底（db.py:1920-1930 标记 interrupted）；**三个 JobManager 没有**——进程重启后中断任务既无 job_runs 记录也无 interrupted 标记。前端轮询遇 404 也无「任务已失效」提示。
- 建议：三个 Manager 比照批量回测加启动清理/终态落库；前端轮询遇 404 显式提示「服务已重启，任务丢失」。
- 附带：`instrument_jobs.py:432,628` 硬编码回填起点 `date(2020,1,1)`，与 `core/jobs.py:84` 用 `backtest_start_primary` 配置的口径不一致；`instrument_metadata.start_date` 字段已建但未被这两处使用。

### P2-10 RevisionCache 双实例 + 误导性注释

- 位置：`routers/subject_market.py:20` 与 `trend_mcp/server.py:91` 各 new 一个；`server.py:88` 注释写「shared RevisionCache from services.dashboard」——**注释与实现矛盾**，会误导后续维护者
- 影响：同一份秒级 CPU 全市场计算被缓存两份，冷启动/数据更新后双倍开销。
- 建议：缓存实例下沉到 `services/dashboard.py` 模块级单例；顺手修注释。

### P2-11 DataService 随处 new：实例级限流器被分身稀释

- 位置：`routers/instruments.py:195,559`、`stop_loss.py:83,111`、`dashboard_snapshot.py:125`、`trend_mcp/server.py:177` 等各自 `DataService()`；限流状态（provider_tickflow.py:43-44 `_next_request_at`）是实例级
- 影响：多入口并发时 vendor 限流预算被分身稀释；与 P0-1 叠加时（公网匿名调用）放大。报价缓存（service.py:46）是模块级所以幸免。
- 建议：DataService 进程级单例，或至少限流状态提升为模块级。P0-1 修复后紧迫性下降。

### P2-12 模块级单例固化 `get_db` —— 代码库自警过的模式仍在犯（盲审新发现）

- 位置：`routers/rule_backtest.py:20` 模块级 `service = RuleBacktestService()`；`MarketStore._get_db()`（market_store.py:11-16）首次调用即永久固化当时的 `get_db()` 返回值
- 问题：`main.py:59-63` 与 `batch_service.py:360-363` 注释明确警告过「测试补丁被永久捕获」模式，路由层却在犯。若首个调用落在测试补丁窗口内，生产路径长期持有测试替身。
- 建议：每次 `db or get_db()` 现取（batch_service 的 lazy 写法就是范本），不要缓存。

### P2-13 配置读取三套并行、env 散落、cwd 相对路径

- env 直连 5 处：`provider_tickflow.py:41,82`、`data/service.py:44`、`main.py:47`、`audit/app_logger.py:30`；`stock_industry.py:355` 用 `os.environ[...]`（KeyError 硬失败）而 provider 用 `os.getenv(..., "")` 优雅降级——同一个 TICKFLOW_API_KEY 两种容错级别。
- `load_dotenv()` 只在 main.py 与个别脚本调用；`sync_stock_industry.py`、`fetch_etf_holdings.py`、`import_all_etf_constituents.py` 直接跑时 `.env` 不加载（名称字段静默为空）。
- 无 `.env.example`，且 `.gitignore:16` 的 `.env.*` 会把它一并忽略（`git check-ignore` 实测）——行动必须包含 `!.env.example` 例外，否则提交不进去。
- **cwd 依赖**（盲审新发现）：`main.py:14`（load_dotenv）、`main.py:64`（`Path("data")`）、`settings.py:45`（`config/app.yaml`）、`app_logger.py:8`（`logs/app`）全部相对当前工作目录——非项目根启动（IDE/其他 systemd 配置）会静默读写错位置的 .env/DB/日志。建议以 `__file__` 锚定项目根或提供 `TREND_QUANT_HOME`。

### P2-14 设计容量结论（评审 2 要求补的收口判断）

日更链路总时长随标的数线性增长：600+ 标的 × 每日全量 qfq 重写 + raw 全量 INSERT OR REPLACE（见 P2-17）。单机 SQLite + 单线程顺序补库是固有天花板。**结论**：当前规模（600+ 标的、日更分钟级）下 16:30-18:00 窗口与 `misfire_grace_time=7200`（scheduler.py:51）充裕；标的池扩到约 2000 时若不先做 P2-17 的增量物化，日更窗口将开始吃紧。无需立即行动，作为扩容触发条件记录。

---

## 六、性能（P2/P3）

### P2-15 回测引擎热路径 iterrows + 逐日 iloc 切片

- 位置：`engine.py:64`（主循环 iterrows）、`:69`（每日 `all_bars.iloc[:idx+1]`）、`:594-595`（基准 iterrows）、`:605-613`（kline payload 两次 iterrows）；盲审补充 `metrics.py:16`、`intraday_service.py:477,834`
- 建议：热循环改 `itertuples()`/numpy 列缓存；预期 2-5 倍提速。无用户可感问题，可延后。

### P2-16 并发残留风险两项（评审 2 新发现，正面结论见总体评价）

- **`_market_symbols_cache` 跨进程不失效**（db.py:57-62 注释自知）：项目存在脚本直写库文化，脚本写库后 web 进程符号缓存陈旧。建议脚本使用约定写明「写库后重启服务」或提供失效入口。
- **调度器三类任务可并发 × 无显式 busy_timeout**：BackgroundScheduler 默认 max_workers=10（scheduler.py:41），daily_update / 月度行业同步 / 盘中快照可同时命中同一 WAL 库；sqlite3 默认 5s 超时在长事务下可能不够。建议 `_connect` 显式 `timeout=30`（即 busy_timeout）。

### P2-17 日更全量重写（raw + qfq 双层）

- 位置：`data/service.py:348-352`（existing+fetched 合并后整段 save_history，INSERT OR REPLACE 全部历史行）+ `274-305,382-384`（raw_updated 即触发 qfq 全量物化 DELETE+INSERT）；`_effective_fetch_start`/`_save_backfill_result` 为取 max(time) 整表加载
- 影响：600 标的 × 平均千行 = 每个交易日近百万行重写，随标的数线性恶化。
- 建议（评审 2 验证数学成立，严格正确非近似）：增量物化——`raw_updated and not factors_changed and not qfq_behind and 新bar日期 > max(ex_date)` 时新 bar 直接以既有除数 append；**边界条件必须保留**：回补历史（新 bar 落在历史除权日之前）divisor ≠ 1，必须仍走全量重写。成本约半天，`tests/unit/test_adjustment.py` 已有单测兜底。max(time) 查询改 `get_market_data_summary`。

### P2-18 读路径热点

- `db.py:848-867` `get_market_dashboard_revision` 每次调用对百万行表 COUNT(*)（5 个调用方：每次看板请求、快照任务、MCP、启动补偿）——建议 row_count 由写入侧维护或接受「MAX(time)+version」两元素 token。
- `market_view /api/daily`：`market_view.py:263` 全量读入内存再 Python 侧 tail 裁剪（:289-290）——建议 SQL 下推 WHERE/LIMIT；盘中模式 `compute_market_indicators` 跑两遍（:307,:352）——只算 combined 一遍。
- `compute_manual_trade` 每笔交易 2 次全量行情读（manual_trade.py:82-97 内 compute_stop_loss 已 load 一次，97 行又 load 一次）；持仓列表 N 笔 = 2N 次；`symbol_annotations` 对每笔持仓 tight/loose 各算一次（trade_records.py:324-335）再放大一倍。有 115 秒事故前科（app.yaml 注释）。建议 compute_stop_loss 支持传入预加载 df。
- `routers/instruments.py:429-431` 列表接口对 600+ 标的逐只 `get_market_data_summary`（每次调用新建连接，600+ 连接）——建议单条 GROUP BY 聚合查询。
- `get_strategy_config` 无缓存逐次查库（strategy_config.py:45-65，被止损/看板/日更按次甚至按标的调用）——进程内短 TTL 缓存 + 写时失效。
- P3：批量回测每格双事务（batch_service.py:533 insert + 535 flush counts，3000 格 = 6000+ 次 WAL 刷盘）——counts 改每标的/每 N 格 flush。P3：`AssetVersionMiddleware` 每请求 stat() 磁盘（main.py:291-296）——缓存 mtime（1s TTL）。

### P2-19 `_rule_jobs` 的 `result_full` 常驻内存无读取方（盲审新发现，亲核属实）

- 位置：`rule_backtest.py:176-177`——完整结果（daily_nav/charts/condition_trace，单策略数 MB）内存保留 30 分钟，且**没有任何端点读取 result_full**（rule_backtest.py:174-175 注释自述「future on-demand detail endpoints」）；前端注释自承大载荷会卡 frp 链路。
- 建议：不存 full（slim 即可），或落临时表按需取。

### P2-20 前端渲染性能

- `subject_market.html` renderBoard（888-905）整板一次 innerHTML、每行 3 个手写 SVG；排序点击全量重建；mousemove 无节流（970-996）。`market_view.html` 单标的加载最多 3 次全图重渲染（1917,1924,1929）全部 `notMerge=true`；resize 无防抖逐个 resize 7 张图（2148-2151）。`instruments.html` renderTable 全量重建无分页（706-733）。
- 正面案例：batch_backtest 明细表 200/页增量渲染（912-941）——全站唯一。
- 轮询：`subject_market.html:1056-1095` 2s 无限轮询无退避无 visibilitychange 暂停（fetch 失败静默 continue）；`base.html:149-150` 每页常驻 30s 轮询同样无暂停；`batch_backtest.html:524-548` 轮询失败 `p=null` 静默 clearInterval——**进度条永远冻结，用户无提示**（这条建议至少加错误提示条，低成本）。
- 建议：看板行 keyed 增量更新或排序只重排 DOM；resize 150ms 防抖；轮询统一加可见性暂停 + 失败退避。

---

## 七、运维体系：日志 / 监控 / 备份 / 告警

### P2-21 日志覆盖与风格

- `get_logger` 仅 6 个模块使用，其余约 20 个模块裸 `logging.getLogger`（效果等同，风格不统一）；约 41 个 src 模块完全无日志，值得补的：`routers/auth.py`（登录审计，P1-1）、`data/indicator_store.py`（缓存失效→live 重算回退静默）、`services/dashboard.py`、`services/manual_trade.py`、`routers/market_view.py`。
- `scripts/` 全部 `print` 不落日志文件；季度窗口任务的执行历史只能靠 job_runs 和终端回显。
- 正面：异常处理整体质量好，`except Exception` 基本都有 `logger.exception`；真正静默吞只有 `core/calendar.py:50-51`（有注释说明意图）。
- `record_job_run_safely`（db.py:2107-2119）本身 best-effort——job 记录写失败只 warning，告警链路最后一环可能丢。
- 孤儿日志：`logs/service.log`（4.9MB 仍在增长）与 `logs/calc/calc.jsonl` 在 src/scripts 找不到写入方——疑似历史遗留进程输出，无轮转无归属，建议查明来源后清理。

### P2-22 无性能计时 / 无失败外发告警

- 全 src 仅 2 处耗时日志（rule_backtest.py:179、batch_service.py:547）；无 HTTP 请求计时——2026-08-10 的 115 秒事故只能靠 access.log 事后翻。**建议：纯 ASGI 慢请求中间件（>2s 记 warning，约 30 行）——全报告投入产出比最高的一条**（评审 2 语）。
- 任务失败只记日志 + job_runs；notify 模块已退役无替代；日更失败唯一用户可见面是导航栏通知条（依赖用户打开页面）。建议选定一个方案落地：**失败哨兵文件 + 导航栏盘后更新条复用**（与现有唯一可见面闭环）。
- APScheduler 未注册 error listener，misfire/执行异常默认只进 app.log。

### P2-23 SQLite 加固清单（拆条）

- **busy_timeout**：`_connect`（db.py:66-76）未显式设置（sqlite3 模块默认 5s；批量回测长事务 + 调度器并发下可能不够）——显式 timeout=30。
- **foreign_keys 未开（数据正确性问题，单列）**：`PRAGMA foreign_keys=ON` 从未设置——`manual_trades.user_id`、`sessions.user_id` 外键（db.py:341,357）实际不生效，删用户会留孤儿行。
- **backup_to 路径拼接**：`VACUUM INTO '{dest}'` f-string（db.py:91），路径含单引号即 SQL 语法错误——内部调用安全，建议加引号校验/转义。
- WAL checkpoint 管理靠关连接自动触发，当前无残留，可接受但值得显式化。

### P2-24 备份与恢复（与 P1-2 配套）

- 迁移/回填脚本 `shutil.copy2` 备份 WAL 活库（migrate_category_simplify.py:128、backfill_batch_excess_metrics.py:179）——WAL 未 checkpoint 部分不在主文件内，备份可能缺最近写入，回滚承诺可能失效。正确实现 `db.backup_to()` 现成（v2 自 P0 降 P2：一次性脚本已执行完毕，风险只在重跑时兑现；修复仍要做，半小时）。
- `migrate_category_sw2021.py:19-20` 要求「停服执行」靠自觉——脚本无服务运行检测。
- 恢复演练从未验证过（备份可用性 = 未验证即不存在）。

---

## 八、测试体系

整体：48 个测试文件、702 个可收集测试（unit 34 / api 13 / integration 4 + 根目录 9 个无 marker）。引擎 golden、双实现一致性（memoized vs legacy、cached vs 全量盘中）质量高。实跑证据（2026-08-24 本地，排除 2 个收集错误文件）：**700 passed / 2 failed / 127s**；两个失败均为 `test_instruments_bulk_backfill.py` 在 tearDown 清理 TemporaryDirectory 时 `OSError [WinError 145]`（Windows 上 SQLite/WAL 句柄释放延迟 flake，与文件内 `time.sleep(0.3)` workaround 同源）——已知遗留非回归，但会掩盖真回归。

### P1-16 零测试的生产咽喉模块（补测场景全部具体到「函数+输入+预期」，见附录 A）

- **H1 `core/jobs.py`**（每日 16:30 补库主任务）：非交易日跳过、失败落 job_runs 并 re-raise、`_pool_symbols` 去重与兜底，全无测试。
- **H2 `core/scheduler.py`**：3 类 cron 注册、misfire 参数、幂等 start、shutdown、INTRADAY_SNAPSHOT_CRONS 时段正确性，零测试。
- **H3 `app/main.py` lifespan 编排**：`_daily_update_catchup` 三路漏更检测、`_warm_dashboard`、`_rebuild_check` 无直接测试。
- **H4 `trend_mcp/server.py`**：7 个工具只测 2.5 个；trend_dashboard/intraday_dashboard/list_instruments/add_trade/open_positions 的错误契约（`{ok: False, error}`）完全没锁定。
- **H5 `data/service.py`**：`update_pool_daily` 汇总入口、`ensure_daily_history` 短路、`sync_ex_factors`/`rematerialize_qfq`（除权链路，2026-07 事故相关）只被间接覆盖。
- **H6 `data/provider_tickflow.py`**：`TICKFLOW_BASE_URL` 镜像覆盖、`fetch_ex_factors` 分批/UTC 口径、`_compact_klines_to_dataframe` 时区转换、`fetch_latest_quotes` 分块与补 error，均无测试。
- **盲审补充**：登录墙测试未覆盖 `/mcp` 豁免行为（test_auth_wall.py 全部用例无一条断言 /mcp）——这正是 P0-1 的暴露面，补一条断言成本极低、价值直接。

### P2-25 测试卫生

- **弱断言/恒真断言**：`tests/integration/test_db.py:64` 条件表达式恒真；`tests/test_rule_backtest_engine.py:830-839` 空列表时断言体整体跳过。
- **全局状态泄漏**：`test_instruments_bulk_backfill.py:134,237`、`test_stock_industry.py:306,460` 调 `init_db()` 永久改写进程级单例不还原；`tests/api/test_observability_logging.py:126,148` 动态注册路由不卸载。
- **真实时间/配置依赖**：多个文件模块导入期固化 TODAY（test_market_view.py:267、test_mcp_symbol_detail.py:61 等），跨午夜 flake；`test_provider_tickflow.py:15-23` 无参 `load_settings()` 读仓库真实 app.yaml。
- **重复测试**：`test_indicators.py` vs `test_core_indicators.py`（ATR/ER 几乎一一对应）、`test_smoke.py` 与 test_core_trend 重叠；`test_market_view.py:282-407` 与 `test_mcp_symbol_detail.py` 夹具近乎复制。
- **标记体系缺口**：根目录 9 个文件无 marker（Makefile 三类目标跑不到它们）；全仓零 `slow` 标记；`test_batch_golden.py:79,109` 重复打 marker。
- **已知失败未隔离**（盲审）：CLAUDE.md 自述 `tests/integration/test_intraday_service.py` 有 2 个 pre-existing 失败用例却未打 `xfail`——套件长期红，新回归被噪声淹没。建议打 xfail(strict=False) 并注明原因。
- **脆弱导入**：`tests/test_subject_market.py:8` 从 `app.routers.subject_market` 导入 `build_subject_dashboard_payload`——依赖路由层的偶然再导出（函数早已迁至 services/dashboard.py）。

### P2-26 无 CI、无覆盖率门槛

- 无 `.github/` 或任何 CI 配置；pyproject 有 coverage 配置但无 `fail_under`；`.coverage` 数据文件被 git 跟踪。
- 建议（评审 2 约束）：单矩阵即可（一个 Python 版本 × 一个 OS，部署目标只有一个）；前置依赖 P1-6（dev 依赖声明）；不想维护 Actions 的话，Makefile test 目标 + 本地 pre-push hook 亦可，任选其一。

---

## 九、前端专项（UX/可维护性，P2/P3）

### P2-27 巨型内联脚本与样式组织

- 9 个模板承载全部约 7700 行业务 JS，无一独立 .js：market_view 2055 行单 IIFE、batch_backtest 1600、instruments 1370、subject_market 1233（且**非 IIFE 包裹**，顶层 const/function 进全局，试算弹窗被迫再套 IIFE 隔离）。
- `rule_backtest.html:3-280` 277 行内联 `<style>`，`position_strategies.html:3-91` 重复其中部分规则——应并入 style.css。
- BOM 不一致：base.html、settings.py、provider_utils.py 文件头带 BOM，其余文件无。
- 缓存串三处三种写法：base.html:7/login.html:7 带硬编码一次性后缀 `-20260824-auth`（临时绕过残留）；batch_backtest.html:247 缺守卫；`asset_version` 只跟踪 style.css mtime（main.py:277）——静态资源增改不会失效缓存，**这是会导致线上 stale 的 bug 级问题，且是 P1-15 公共 JS 抽取的前置**。

### P2-28 UX 一致性

- alert/confirm 与自定义弹窗混用：batch_backtest 9 处原生 alert/confirm + market_view.html:2202，其余页面是行内消息。
- 弹窗可访问性参差：rule_backtest 有 role=dialog/Esc/焦点管理（最佳实践），manual_trade/instruments/batch 弹窗全无。
- market_view 首屏无骨架/loading（六张图区域数据到达前完全空白）。
- 移动端仅标的看板 + 手工交易两页有 768px 适配（既定范围）；instruments 弹窗、batch 宽表手机实际不可用——知情欠账，建议页面标注。
- base.html:95 写死「16:30」而后端更新时间可配置；止损倍数文案两处硬编码（market_view.html:60 vs manual_trade.html:51，后端改倍数需手改两处且措辞已不一致）。

### P2-29 前后端接口对照结论

- 前端调用的 26 个接口全部存在（无幽灵调用）。
- 后端有而前端未接：`GET /api/auth/me`（auth.py:63）、`GET /subject-market/api/trading-status`（subject_market.py:140）——若非为外部调用设计则是僵尸端点，建议确认用途或删除。
- FastAPI 全无 `response_model`，契约靠 golden 测试与联调兜底——个人项目知情取舍，记录备查。

---

## 十、文档与仓库卫生（P2/P3）

### P2-30 过时/不一致文档

- `README.md:11` 与 `CLAUDE.md:30`：写 MCP 5 个工具，实际 **7 个**（add_trade/open_positions 未列入）。
- `main.py:304-305` 注释「MCP 工具调用自带 username/password 逐次鉴权」与实现矛盾（见 P0-1）。
- `docs/architecture-review-2026-08-01.md`：称 MCP 6 工具、db.py 1522 行（现 2119 行）；其 P0 结论是否闭环无跟踪。
- `scripts/fetch_etf_holdings.py:5` docstring 引用已删除的旧方案文档且未注明。
- `docs/batch-backtest/`（4 份）与 `docs/stock-industry-etf-holdings/`（5 份）plan/review 多轮堆积（历史上有两次批量清理先例）；verification-and-rollout 与 server-rollout 内容重叠。
- `TODO.md`（160 字节，3 月后未动）疑似过期。
- **chinese_calendar 年度边界的运维仪式无着落**（评审 2 新发现）：calendar.py:60-85 对超库年份退化为 weekday-only 且每年只 warning 一次；2027-01-01 起法定假日会被当成交易日——更糟的是 `_daily_update_catchup` 的 expected 计算走同一日历，会把假日判为「应更未更」，**每次重启触发一次无效 force 补跑**，直到人工升级库。「每年 12 月 pip install --upgrade chinese_calendar」只写在 calendar.py docstring，README/CLAUDE.md/部署文档均无（grep 确认）。建议写入部署文档 + 导航栏复用渠道暴露「日历数据过期」状态。

### P2-31 仓库里不该有的文件与工作方式

- 被 git 跟踪：`trend-quant.zip`（455KB，7 月 5 日）、`.agents/skills/trend-score-calculator.zip`、`.coverage`。
- `trend-quant-master-20260717_230711.bundle`（14.7MB，未跟踪）：README 称 git bundle 是既定备份机制，最新停留在 7 月 18 日——断更一个月，要么恢复月备要么从 README 删除约定（与 P1-2 一并决策）。
- 孤儿日志文件（见 P2-21）。
- 工作方式：多个大功能直接在 master 上以未提交修改态堆积（当前工作区 8 个修改文件 + 1 个未跟踪脚本）——建议功能分支 + 提交纪律（P3）。
- `is_trading_time` 不校验交易日（calendar.py:88-99 只看时间窗），与同文件 is_realtime_available/is_past_market_open 语义不一致；当前唯一调用方自己补了交易日判断所以没炸，属潜伏 API 陷阱（P3，建议函数内补或改名 `is_continuous_auction_hours` 明示）。
- `tushare_common.py` 镜像站走 tushare 私有属性 hack（`pro._DataApi__http_url`）——对 tushare 升级脆弱，token 与全部请求经第三方镜像属知情风险，建议文档明示（P3）。
- scripts/ 平铺 13 个脚本（一次性迁移/季度运维/部署混杂），`DB_PATH` 硬编码重复 7 处，TickFlow 客户端构造 3 套（P3；建议 `scripts/oneoff/` 与 `scripts/ops/` 分目录 + 公共 `_common.py`）。

---

## 十一、优先级行动清单（v2 重排：测试兜底在所有重构之前，数据备份提前）

| # | 级别 | 动作 | 预估成本 |
|---|---|---|---|
| 1 | P0 | MCP 鉴权或摘公网暴露 + DNS rebinding 评估（P0-1） | 半天 |
| 2 | P0 | MCP 改 token 制或低权限专用账号约定（P0-2） | 半天 |
| 3 | P0 | 修 instruments.py:102 `_config_items` 漏导入 + 空表降级路径测试（P0-3） | 半小时 |
| 4 | P0 | 两个 MCP 测试文件加 `importorskip("mcp")`（P0-4） | 10 分钟 |
| 5 | P1 | **每日定时 DB 备份 keep=3 + 修剪 glob 兼容 + 异机周备决策 + 恢复演练**（P1-2/P2-24） | 半天 |
| 6 | P1 | **dev 依赖组声明（pytest/pytest-cov/ruff/mcp）+ 删 asyncio_mode 死配置**（P1-6） | 10 分钟 |
| 7 | P1 | 首个用户引导脚本 `scripts/create_user.py` + README 部署章节（P1-3） | 半天 |
| 8 | P1 | 登录限流 + 登录审计日志 + 401/403 采样记录（P1-1） | 半天 |
| 9 | P1 | 修 3 处 XSS + 1 处双重转义 + tooltip 一律 esc 约定（P1-8） | 半天 |
| 10 | P1 | **补 H1-H3（jobs/scheduler/lifespan）+ /mcp 豁免断言——生产咽喉零覆盖，先补测试再重构**（P1-16） | 1 天 |
| 11 | P1 | Web 日 K 指标改全历史计算，与 MCP 对齐（P1-4） | 半天 |
| 12 | P1 | 时区整治：jobs.py:63 + main.py 两处 + rule_backtest.py + intraday_service 11 处全改 market_now()（P1-5） | 半天 |
| 13 | P1 | deploy.sh 重写或删除（以 server-rollout.md 为准）+ 服务降权（P1-7） | 半天 |
| 14 | P1 | 会话硬化包：Secure 开关 / token 哈希 / 明文兜底观测（P1-9）；logout 改 POST 移出豁免（P1-10） | 1 天 |
| 15 | P1 | build_intraday_overlay 的 DataService 泄漏修复（P1-13） | 1 小时 |
| 16 | P1 | 前端公共 JS 抽取（**前置：asset_version 扩展 + 止损卡/postJson 口径裁决**）（P1-15/P2-27） | 1-2 天 |
| 17 | P1 | 批量回测标的快照冻结（P1-11） | 半天 |
| 18 | P1 | 统一 401 fetch 封装并接入全部页面（P1-12，依赖 #16） | 半天 |
| 19 | P2 | qfq/raw 增量物化（含「新 bar 日期 > max(ex_date) 才 append」边界）（P2-17） | 半天 |
| 20 | P2 | 慢请求计时中间件 + 失败哨兵 + 导航栏闭环（P2-22） | 半天 |
| 21 | P2 | `.env.example`（**含 `!.env.example` gitignore 例外**）+ env 收口 + cwd 锚定（P2-13） | 半天 |
| 22 | P2 | 死代码清理（build_sw_tree 死循环体/instruments 死 JS/CSS 死类/benchmarks 死 API/分钟 K 死链路等，P2-6/P2-7） | 1 天 |
| 23 | P2 | 迁移脚本备份改 backup_to() + 服务运行检测（P2-24） | 半小时 |
| 24 | P2 | CI 单矩阵 + 移除 .coverage/zip 跟踪 + xfail 已知失败（P2-26/P2-25） | 半天 |
| 25 | P2 | 看板口径治理：L2 排序字段确认（P2-4）+ EOD/盘中聚合加权统一 + NaN 防护（P2-5） | 半天 |
| 26 | P2 | SQLite 加固：busy_timeout=30 + foreign_keys 评估开启（P2-23） | 半天 |
| 27 | P2 | MCP 工具测试补全 + provider/service 缺口（P1-16 附录 A） | 1-2 天 |
| 28 | P2 | 引擎热路径去 iterrows（P2-15，可延后）；result_full 去留决策（P2-19） | 1 天 |
| 29 | P2 | RevisionCache 单例（P2-10）+ DataService 单例/限流模块级（P2-11）+ 模块级单例去固化（P2-12） | 半天 |
| 30 | P2 | chinese_calendar 年度升级写入部署文档 + 日历过期 UI 提示（P2-30） | 1 小时 |
| 31 | P2 | JobManager 中断标记比照批量回测（P2-9）；回填起点口径统一 | 半天 |
| 32 | P3 | Database DDL 分段 + auth/session 域随 #14 顺手迁移；scripts 整理；文档更新（README/CLAUDE 7 工具等）；功能分支纪律 | 持续 |

---

## 附录 A：测试缺口补测场景清单（函数 → 输入 → 预期）

**core/jobs.py**
- `daily_market_update_job(settings, force=False)` + mock 非交易日 → `status=="skipped_non_trading_day"` 且 job_runs 有 `daily_update_skip` 记录
- `update_pool_daily` 抛异常 → re-raise 且 job_runs 有 `status="failed"`、payload 含错误信息
- `_pool_symbols()` 含 disabled 标的 + benchmark 重复 → 剔除+去重+大写；`get_db()` 抛错 → 仅返回 benchmark

**core/scheduler.py**
- `start()` 后 `jobs_snapshot()` 断言任务 id 集合与 daily_update 的 misfire_grace_time==7200；二次 start 幂等；shutdown 后为空
- `INTRADAY_SNAPSHOT_CRONS` 展开后时刻全部落在 9:35-11:30 / 13:00-15:00

**app/main.py**
- `_daily_update_catchup`：mock 最后成功 run_date < 应有交易日且已过 16:30 → 触发一次 force 补跑；已是今日 → 不触发
- `AssetVersionMiddleware`：请求后 asset_version 更新；非 http scope 直通

**trend_mcp/server.py**
- `trend_dashboard` 连续两次调用 → 第二次命中 RevisionCache 不重算
- `intraday_dashboard` 非交易日 → `ok=False`；category 过滤生效；post_close 标记正确
- `symbol_detail(days=5)` 100 根数据 → dates 恰为最后 5 天；空 symbol/无数据 → `ok=False`
- `calc_stop_loss` 抛 StopLossError → `{ok: False, error}`
- `add_trade` 密码错误/价格超区间 → `ok=False`；`open_positions` 含 error 持仓时 summary 只加正常笔
- **登录墙**：未带 cookie 请求 `/mcp/sse` → 按当前设计应豁免（断言此行为以锁定暴露面；P0-1 修复后改为断言 401）

**data/service.py**
- `update_pool_daily`：1 成功/1 no_data/1 异常 → 计数与 job_runs 正确
- `_retry_wait_seconds`：含「请 N ms 后重试」→ 解析出对应秒数；`_non_retryable_provider_error`：权限错误 → 命中
- `ensure_daily_history`：本地已覆盖 → 不调 provider，返回 up_to_date
- `sync_ex_factors`：新因子 → 落库且 changed 含该标的
- 报价缓存：TTL 内第二次 `fetch_latest_quote` 不打 provider

**data/provider_tickflow.py**
- `TICKFLOW_BASE_URL` env → 镜像 base_url 生效
- `fetch_ex_factors` 120 标的 → 3 批；缺 timestamp 的 entry 跳过；UTC 毫秒 → UTC 日期口径
- `fetch_latest_quotes` 51 标的 → 2 chunk；单 chunk 异常 → 该 chunk 全部 error 且不中断；未返回标的 → "no quote returned"

**db.py**
- `save_ex_factors`/`load_ex_factors` 往返 + 重复写幂等
- `load_market_dashboard_history(days=30)` 真实 SQLite：只含近 30 天且带 category/amount 字段
- `get_market_dashboard_revision` 写入后 version 递增

**其他**
- `RevisionCache`：同 revision 只算一次；revision 变化重算；并发 single-flight
- `condition_engine`：`days_since_last_exit` 为 None 时 `>=` 特判通过、`<=` 不通过
- engine 边界：空 bars/单根 bars/NaN 价格/`start_date > end_date`/无 date/time 列（P2-1）的行为契约锁定
- `core/display.py` vs `app/instrument_display.py` 双实现一致性参数化测试（防漂移）
- 看板排序：L2 排序字段（P2-4 确认意图后）排序测试
- `migrate_raw_qfq.py`：dry-run 不写库 + 全量迁移幂等（**可选**：一次性脚本已执行，补测价值低于 jobs/scheduler）

---

## 附录 B：round-2 复审补充发现（盲审代理第二轮定向深挖，全部 P3 级，不影响主清单分级）

1. **N1 冗余索引与读侧缺索引**：`idx_market_data_raw_symbol_time`（db.py:143-144）、`idx_market_data_qfq_symbol_time`（db.py:159-160）、`idx_ex_factors_symbol_time`（db.py:170-171）与各自 `PRIMARY KEY (symbol,time)` 完全同列——rowid 表上 PK 已自动建索引，这三个是重复的，白白放大 1M 行表的每次写入（与 P2-17 叠加）。建议删除（零风险）。读侧：`load_trend_daily_bulk`（db.py:1778-1790，`WHERE param_set=? AND time>=?` 无法利用 PK 前缀，trend_daily 全扫，被盘中快照每 5 分钟调用）、`load_indicator_latest`（db.py:1763-1776，全 PK 覆盖扫描）、`load_market_tail`（db.py:808-822，`WHERE time>=?` 无索引全扫）。现状单次数十至数百毫秒、5 分钟级频率，可接受不紧急；视容量再加 `trend_daily(param_set,time)`、`market_data_qfq(time)`。
2. **N2 冷路径全表重查询（备查）**：`load_market_dashboard_history`（db.py:824-846）ROW_NUMBER 窗口作用于 qfq×metadata 全量 join 后才过滤；`routers/batch_backtest.py:97` meta 接口每次页面加载跑 `count_bars_by_symbol()` 全表 GROUP BY。低频路径，记录备查。
3. **N3 标的管理路径重复全表加载**：`instrument_admin.py:66-81` `_known_managed_symbols` 一次调用内 `list_instrument_metadata()` 两次 + 一次 DISTINCT 全表；`:120-136` `_next_sort_order` 同样两次全表。毫秒级，与 P2-18 的列表 N+1 同源，合并处理。
4. **N4 API 测试每用例两次 pbkdf2 拖慢套件**：`tests/api/conftest.py:56-58` 每个 API 测试 create_user（哈希一次）+ 登录（校验一次）≈ 固定 +0.2s/用例。建议夹具内注入预计算哈希或测试环境调低迭代数（与 CI 提速目标一致）。
5. **N5 误导性变量名**：`intraday_service.py:663` `intraday_ts = result["trend_score"]`——名为时间戳实为趋势值，与 668 行真正的日期冲突，建议改名 `intraday_score`。

---

*本报告为 v2.2 终版。三方意见与修订依据：review-1-factcheck.md（事实核查）、review-2-arch.md（架构评审）、blind-audit-comparison.md（盲审比对）、blind-audit-round2.md（盲审 round-2 确认）、review-1-factcheck-round2.md / review-2-arch-round2.md（评审 round-2 确认）、CHANGELOG.md（修订记录）。三方均已确认达成一致。*
