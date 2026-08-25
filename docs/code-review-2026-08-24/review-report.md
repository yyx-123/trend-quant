# Trend Quant 全项目代码审查报告（v2.1 · 最终版）

- **审查日期**：2026-08-24（v2.1 回签轮修订于 08-25 凌晨）
- **版本**：v2.1（v1 → v2 合并三方意见 → v2.1 回签轮修订；逐条采纳/驳回记录见 `reconciliation.md`）
- **版本说明**：本目录另有一套**并行审查**产物（`code-review-report.md` v2.2 及其 review-1/review-2/blind-audit 系列，由另一个并行会话产出）。两套独立审查在主要发现上高度收敛（MCP 零鉴权、NameError、busy_timeout、测试收集失败、无定时备份等），互为盲测印证；v2.1 已吸收其经我方逐条核实的高价值独有发现（见 §13）。
- **范围**：`src/`（实测 16,816 行 Python，全部通读）、`web/templates` + `web/static`（模式扫描 + 抽样精读）、`scripts/`（12 个 Python 脚本 + deploy.sh）、`tests/`（61 个 test_*.py）、`config/`、`pyproject.toml`、`Makefile`、`README.md`、`docs/`、`scripts/deploy.sh`
- **方法**：主审查逐文件精读并记录（原始流水账 `_notes.md`）+ ruff 静态检查 + grep 交叉验证；一名独立子代理从零做同样的全量审查（67 条编号发现）；两名评审子代理分别做事实核查（约 50 条论断逐条比对）与覆盖面/建议质量评审。所有 P0 级论断均经至少两方独立核实。
- **约束**：全程只读；除本目录外未对项目做任何写操作；未运行 pytest 全量（仅做 `--collect-only` 验证，详见 P0-8）。

---

## 0. 总体评价

代码整体质量**高于一般个人项目**：分层清晰（app → services → core/data 单向依赖）、关键口径有刻意的单一实现（指标库、趋势值、止损）、并发与缓存失效的边角案例大多有注释说明设计理由、测试数量多且含金量高（golden 测试、新旧实现等价性测试、盘中/EOD 一致性测试）。

主要问题集中在六个方面：

1. **两个确凿的数据正确性隐患**：`instruments.py` 兜底分支 NameError（P0-1）；vendor 返回空因子列表会静默擦除存量除权因子并重物化出错误 qfq（P0-7）——后者是真正的「悄悄算错钱」类 bug；
2. **安全基线薄弱**：登录无速率限制、session token 明文落库、MCP 五个工具零鉴权暴露在登录墙之外、deploy.sh 默认 HTTP + root 运行；
3. **批量回测的重复计算与 SQLite 并发写加固**：同标的多策略全量重算指标（Q-1，最重任务的 N 倍浪费）；并发写依赖 Python 驱动默认 5s busy_timeout，未显式配置亦无并发写回归测试（P0-3）；
4. **测试套件在当前环境收集即失败**（两个 MCP 测试文件顶层 import 未安装的 `mcp` 包）——所有其他测试建议的地基；
5. **大段平行实现**：EOD 看板与盘中看板的聚合层、盘中趋势公式与 `core/trend` 的公式双写、JobManager 脚手架三份、esc/safe_float/_category_path 等工具函数 N 份拷贝（且前端止损卡两处副本已经漂移）；
6. **数据资产保护薄弱**：2.8GB 不可再生单库无定时备份，两个迁移/回填脚本用 `shutil.copy2` 复制 WAL 活库（备份可能缺最近事务）。

---

## 1. 严重问题（P0 级，建议优先处理）

### P0-1 【Bug · 高】`instruments.py` 兜底分支 NameError —— 分类表为空时 500

`src/app/routers/instruments.py:102`：

```python
for item in [*db.list_instrument_metadata(), *_config_items()]:
```

`_category_options()` 在 `instrument_categories` 表为空时走兜底分支，但 `_config_items` **不在本文件的 import 列表里**（第 20–30 行），模块内也无定义（ruff F821 实证）。一旦分类表为空（全新部署、分类树被清空、迁移中途），`/instruments/api/categories`、新增标的、编辑标的全部 NameError → 500。正常路径（分类表非空）永远不触发，现有测试全部通过——这正是它危险的原因。

另外兜底分支即便修好 import 也是重复劳动：`_config_items()`（instrument_admin.py:62）本身就是 `list_instrument_metadata()` 的薄包装，与列表第一项装载的是同一份数据。

**建议**：兜底分支直接用已查出的元数据（删掉 `_config_items()` 调用），并补一个「分类表为空」的 API 测试。

### P0-2 【安全 · 高】MCP 五个工具零鉴权，且关闭了 DNS 重绑定保护

`src/trend_mcp/server.py:48-51` 创建 FastMCP 时 `enable_dns_rebinding_protection=False`；`src/app/main.py:312-313` 把 `/mcp` 列入登录墙豁免前缀。结果：`trend_dashboard`、`intraday_dashboard`、`symbol_detail`、`calc_stop_loss`、`list_instruments` 五个工具**无需任何凭据**即可调用，只有 `add_trade` / `open_positions` 逐次校验密码。

公网可达性证据：仓库内 `scripts/deploy.sh:150-178` 生成的 nginx 站点监听 80 端口并反代到 8000（/mcp 随之公网可达）；`main.py:381-383` 注释又自述「直接挂在 frp 中继后、无 nginx」——两种部署形态描述互相矛盾（见 §9-6），但**无论哪种，/mcp 都随主站对外可达**。

风险不止数据泄露：`intraday_dashboard` 每次调用都新建 DataService 拉实时报价（server.py:177-187），消耗 TickFlow 付费配额（按次限流）——陌生人可零成本烧光配额、间接造成拒绝服务与直接经济损失。

**建议**：① MCP 通道加共享密钥（环境变量配置，SSE 连接层或工具入参校验）；② 至少对消耗外部配额的工具加进程级调用频限；③ `enable_dns_rebinding_protection=False` 改为 `allowed_hosts` 白名单。

### P0-3 【可靠性 · 中】SQLite busy_timeout 依赖驱动默认 5s，未显式配置、无并发写测试

`src/data/storage/db.py:66-76` 的 `_connect()` 只显式设置了 `journal_mode=WAL`，全项目（含 scripts）没有任何地方显式设置 `busy_timeout`（已 grep 确认）。

**经回签轮实测修正的事实**（v2 此前表述有误）：Python `sqlite3.connect` 的 `timeout` 参数默认 5.0 秒，会落实为 busy_timeout——实测新连接 `PRAGMA busy_timeout` 返回 **5000**。因此本项目每条连接（含带外脚本）本来就带 5 秒等待，并非「默认 0、相撞即抛」。

残留风险（仍然成立但降为中）：① 5s 是**隐式**依赖，任何人改成显式 `connect(timeout=0)` 或换驱动即失效；② `rematerialize_qfq` 全量重写、迁移脚本等长写事务叠加并发写者（批量回测每格写、盘中快照、滑动续期、16:30 日更、带外脚本）时，5s 可能不够，超时即 `OperationalError`；③ **调度器内部并发**——`BackgroundScheduler` 默认 `max_workers=10`（scheduler.py:41 未设限），日更、月度行业同步、盘中快照三类任务可并发命中同一 WAL 库（月初 1 号与交易日重合时现实发生）；④ 全无并发写回归测试。

**建议**：`_connect()` 显式 `PRAGMA busy_timeout = 10000`（一行，消除隐式依赖并给长写事务留余量），补一个多线程并发写测试；带外脚本遵守同一约定（O-6 联动）。

### P0-4 【安全 · 高】登录接口无速率限制 / 无失败锁定 / 失败零审计

`src/app/routers/auth.py:37-52` 的 `/api/auth/login` 无任何频限、失败计数或锁定；`services/trade_records.py:58-66` 的 `authenticate` 失败时**不写任何日志**——当前连「有人在爆破」都无从察觉。MCP 的 `add_trade`/`open_positions` 逐次密码鉴权同理无频限。

缓解因素：pbkdf2(200k)（db.py:22-23）使单次尝试服务端耗时约 0.1s，对强密码的在线爆破实际不可行；单用户系统上下文下此条定「中」也成立——但修复成本极低（进程内滑动窗口约 40 行），且失败审计日志是任何告警的前提。

**建议**：登录与 MCP 逐次鉴权统一加进程级滑动窗口限流（如每 IP/每用户 10 次/分钟 + 连续失败指数退避）；`authenticate` 失败打 warning 日志（不含密码）。

### P0-5 【安全 · 中】session token 明文落库 + 30 天滑动续期无绝对上限

`src/services/auth.py:45-46` 把 `secrets.token_hex(32)` 原文写入 `sessions` 表。SQLite 文件（或其备份）一旦泄露，所有未过期会话直接可用。叠加 `SESSION_TTL = 30 天` 的滑动续期（活跃用户实际永不过期），泄露窗口无限长。

**建议**：① 库存 `sha256(token)`，校验时先 hash 再查（改动点：`create_session`/`get_session_user`/`delete_session`/`touch_session` 四处）；② 滑动续期加绝对上限（如 90 天强制重登）。

### P0-6 【Bug · 中】MCP `symbol_detail` 的 indicators 数组未按 days 截尾，与 dates 长度不一致

`src/trend_mcp/server.py:237-272`：`compute_market_indicators` 在**全历史**上计算（这是对的，EMA 有无限记忆，注释也写明了），随后 `dates`/`candles`/`volumes` 都 `_tail(n)` 截尾，唯独 `indicators` 原样放入 payload——里面每个数组仍是全历史长度。消费方按位置把 `dates[i]` 与 `indicators.ma["5"][i]` 对齐会错位整段历史。注释（223-226 行）声称"Output arrays are tailed afterwards"，与实际行为不符；`tests/unit/test_mcp_symbol_detail.py` 只断言 dates 长度，未断言 indicators 与 dates 一致。

**建议**：对 indicators 内所有序列同样截尾，并补长度一致性断言。

### P0-7 【Bug · 高 · 数据完整性】vendor 返回空因子列表会静默擦除存量除权因子

`src/data/service.py:264-272`（`sync_ex_factors`）+ `src/data/provider_tickflow.py:283-297`：

vendor 对某标的返回空 entries 时，`fetch_ex_factors` 仍无条件写入 `factors_by_symbol[symbol] = []`；`sync_ex_factors` 中 `factors_equal(stored, [])` 为 False → `db.replace_ex_factors(symbol, [])`（`db.py:1399-1403` 先 DELETE 全量再插空）→ 随后 `rematerialize_qfq` 用空因子重算 qfq，**该标的历史除权全部丢失，qfq 价格口径静默错误**。vendor 临时故障、字段变更、标的退市边缘状态都可能触发——它藏在「diff 检测」的正常路径里，不是异常路径，现有的新鲜度校验（日期不变、行数不变）也看不见它。

**建议**：对「存量有因子、本次拉到空」的情况拒绝覆盖并告警（空列表视为「本次无数据」而非「无因子」）；因子表变化触发的重物化前对「因子数从 N→0」做显式拦截。同时把 `replace_ex_factors` 的 DELETE+INSERT 并成一个事务（C-6）。

### P0-8 【测试地基 · 高】当前环境下测试套件收集即失败（mcp 未安装）

`tests/unit/test_mcp_symbol_detail.py:24`、`tests/unit/test_mcp_stop_mode.py:5` 顶层 `from trend_mcp import server`，两文件均无 `importorskip`；而项目 `.venv` **实际未安装 `mcp` 包**（本次审查实测：`pytest tests/unit/test_mcp_stop_mode.py --collect-only` → `ModuleNotFoundError: No module named 'mcp'`，收集中断）。`pyproject.toml` 虽声明了 `mcp>=1.0.0`，但环境未同步。

后果：本报告 §8 的所有补测建议、P0-1/P0-3/P0-6 的回归测试，都建立在一个当前无法完整收集的套件之上。修复约 10 分钟（补装 `mcp`，或给两个文件加 `pytest.importorskip("mcp")`），且与 §7-O8（pyproject 无 dev 依赖声明）是同一根因。

**建议**：补装 `mcp` 并让全量收集转绿；两个测试文件加 `importorskip` 防御；pyproject 增加 dev 依赖声明（见 §7-O8）。

---

## 2. 安全专项（其余）

| # | 级别 | 位置 | 问题 | 建议 |
|---|------|------|------|------|
| S-1 | 高 | `scripts/deploy.sh` | 部署脚本三个危险点：① 生成的 nginx 站点仅 `listen 80`（150-178 行）——登录密码与 session cookie 明文过公网；② systemd `User=root`（104 行）——公网可达应用以 root 常驻；③ 克隆分支 `rm -rf "$INSTALL_DIR"`（73 行）——代码与数据同目录（SQLite 库/logs/backups 都在其下），`.git` 损坏后重跑脚本会**静默删掉生产数据库**；且无 `.env`（TICKFLOW_API_KEY）注入步骤 | 脚本按腐烂处理：重写为「数据目录独立 + 普通用户运行 + 默认要求 HTTPS（无域名则仅监听内网/本机）+ 部署前自动备份」；或直接删除并以运维文档替代 |
| S-2 | 中 | `main.py:364-367`, `routers/auth.py:45-51` | session cookie 无 `Secure` 属性 | HTTPS 化后加 `Secure`（环境变量控制，本地 HTTP 开发不加）；与 S-1 联动 |
| S-3 | 中 | 全站 | 无安全响应头（`X-Content-Type-Options`/`X-Frame-Options`/`Referrer-Policy`） | 零成本一行中间件；CSP 与内联 JS 冲突可暂缓 |
| S-4 | 中 | 全站 | cookie 会话无 CSRF token，仅靠 `SameSite=lax` | lax 已挡常规跨站 POST 表单，单用户场景可接受（可选：写操作加自定义头校验） |
| S-5 | 低 | `routers/auth.py:55-60` | 退出登录用 GET | 可被 `<img>`/预加载触发强制登出；改 POST + 前端小改 |
| S-6 | 低 | `db.py:34-45` | `verify_password` 保留明文比对兜底分支 | 迁移已完成时移除明文分支，或命中时立即重哈希并告警 |
| S-7 | 低 | `db.py:91` | `VACUUM INTO '{dest}'` f-string 拼 SQL | 当前 dest 内部生成无注入面；对路径做单引号校验并注释「仅限受信路径」 |
| S-8 | 信息 | 全站 | `is_admin` 字段存在但管理类接口（标的增改、批量回补、ETF 导入、批次删除）不校验角色（grep `is_admin` 在 routers 零命中） | 单用户场景可接受；要么落实角色控制，要么文档写明「登录即全权」 |
| S-9 | 低 | `services/auth.py` | 会话治理薄弱：登录不踢旧会话、无「吊销全部会话」能力、无 UA/IP 记录、过期会话只在登录时顺手清理 | 记录签发来源；提供吊销端点；清理挪到定时任务 |
| S-10 | 低 | `trade_records.py:58-66` | `user is None or not verify_password(...)` 短路构成用户枚举计时侧信道（用户不存在时毫秒返回） | 单用户系统危害极低，记录即可；可与 P0-4 的失败日志一起做 |
| S-11 | 低 | `scripts/tushare_common.py:20-37` | 内置「镜像站账号」通道并打私有属性补丁（`pro._DataApi__http_url`）：token 发往第三方镜像，补丁随 tushare 升级即碎 | 文档标注风险；镜像能力从默认路径剥离；长期换官方 token |
| S-12 | 信息 | `pyproject.toml` | 无锁定文件（无 requirements.txt/uv.lock），生产重建版本漂移；**根因是无 dev 依赖声明**（pytest/pytest-asyncio/pytest-cov/ruff 均未声明，见 §7-O8）；版本约束策略不一致：`tickflow[all]==0.1.24` 精确钉版而 `pandas>=2.3.0` 等仅下界（pandas 3.0 类上游大版本有静默破坏风险）；`asyncio_mode = "auto"` 在无 pytest-asyncio 的环境下是死配置（async 测试实际走 stdlib `IsolatedAsyncioTestCase`） | 增加 dev optional-dependencies；核心依赖加上界或写明「重装环境即验证」纪律；导出锁定文件 |
| S-13 | 中 | `trend_mcp/server.py:420-454, 462-557` | MCP 以工具参数传密码：`add_trade`/`open_positions` 的 username/password 作为工具参数逐次传递——会进入 MCP 客户端日志、LLM 对话上下文乃至云端模型请求 | 迁移到 token 制（服务端预签发静态 token，配置在 MCP 客户端 env）；过渡期文档明示该账号应为低权限专用账号 |
| S-14 | 低 | `main.py:331, 338`、`routers/auth.py:27-34` | 登录墙边缘行为：① `"/api/" not in path` 使 `/api`（无尾斜杠）被当页面请求 303 跳转而非 401 JSON；② `_EXEMPT_PREFIXES` 用 `startswith`，`/mcpanything` 类路径也会被豁免（当前无此路由，脆弱写法）；③ `/login` 对已登录用户每次 GET 都经 `resolve_session` 可能触发滑动续期写库（豁免路径上的多余 DB 写） | ① 改精确段匹配；② 前缀匹配改 `path == p or path.startswith(p + "/")`；③ 登录页续期可跳过 |

XSS 专项：95 处 `innerHTML` 赋值逐一抽查，未见 `eval`/`new Function`/内联 `onclick`，Jinja 侧无 `| safe` 滥用。**发现四处未转义插入 + 一处双重转义**（前 3 处经两套独立审查交叉核实）：

1. `subject_market.html:1092`：`${st.last_error}`（服务端异常字符串）未转义插入 innerHTML（1021/1079 同模式插入服务端文本）；
2. `market_view.html:1689`：`chartTitleEl.innerHTML` 直接拼接 `payload.display_label`（标的名来自外部行情接口/用户编辑，存储型入口）；
3. `batch_backtest.html:1053` 与 `:1484`：ECharts tooltip 拼接用户可命名的策略名/类目名未转义（同文件 1310 行却用了 `esc()`——知道要转、漏了两处）；
4. `subject_market.html:1021/1079`：快照状态文案拼接服务端字段；
5. `market_view.html:1604`：`esc()` 产物赋给 `textContent` → 双重转义（名称含 `&<>` 时显示 `&amp;` 字面量，非安全问题但显示错误）。

修复建议：1-3 补 `esc()`/改 textContent，4 统一 `escHtml`，5 去掉多余 `esc()`；「tooltip formatter 一律 esc」写入前端约定。另：`esc` 在 6 个模板各抄一份（`esc`）+ 1 份箭头函数（`escHtml`），新增插值忘记转义的风险长期存在（见 §4.3）。

---

## 3. 性能专项

| # | 级别 | 位置 | 问题 | 建议 |
|---|------|------|------|------|
| Q-1 | 高 | `rule_backtest/batch_service.py:510-523` + `engine.py:49-52` | **批量回测同标的多策略重复计算指标**：每格 `engine.run` 新建 `ValueResolver` 并对同一标的同一组 bars 重算全部指标全序列；N 个策略 = N 倍重复 | 按标的复用 resolver/指标序列。**规格注意**：缓存键必须含 `(symbol, 数据版本, 指标参数 fingerprint)`——不同策略的 `indicator_config` 不同，只按标的缓存会让参数不同的策略互相污染序列 |
| Q-2 | 中 | `routers/instruments.py:429-431`、`trend_mcp/server.py:392` | 标的列表对每个标的单独 `get_market_data_summary` → N+1 查询（每查一次新开一个连接，600+ 标的 = 600+ 次查询）；MCP `list_instruments` 同模式 | 加一条 `SELECT symbol, COUNT(*), MIN(time), MAX(time) ... GROUP BY symbol` 批量方法（`count_bars_by_symbol` 模式可扩展） |
| Q-3 | 中 | `db.py:854-867` + `routers/subject_market.py:121` | 每个看板请求都跑 `get_market_dashboard_revision`，其中 `COUNT(*)` 扫百万行的 `market_data_qfq`；revision 第 4 元素已是 `data_versions` 版本号，行数是冗余信息 | 去掉 COUNT(*)，revision 用 `MAX(time)` + `data_versions` 单值（O(1) 主键查询） |
| Q-4 | 中 | `db.py` trend_daily | `load_trend_daily_bulk` 按 `(param_set, time>=?)` 过滤，主键是 `(symbol, time, param_set)` 无法命中前缀 → 全表扫描；看板/盘中快照热路径每轮都调 | 加索引 `(param_set, time)` |
| Q-5 | 中 | `data/service.py:274-305, 382-386` | 日更对有增量的标的 `rematerialize_qfq` 全量重写 qfq（全历史 DELETE+INSERT），随历史变长逐日线性变贵 | 记账观察；后续可做增量物化（只重算最新因子变化点之后） |
| Q-6 | 中 | `services/stop_loss.py:195-199` | 为查单个标的的 `stop_atr_mul`，`_load_instrument_metadata` 加载整张元数据表逐行比对；手工交易列表按持仓逐笔调用 → 每笔一次全表扫 | 改用 `db.get_instrument_metadata(symbol)` 单查 |
| Q-7 | 中 | `trade_records.py:222-233` + `manual_trade.py:97` + `stop_loss.py:181` | **手工交易链路同一标的全历史加载 3 次**：列表预取一次（只用到最后一天 volume）、`compute_manual_trade` 一次、`compute_stop_loss` 内部再一次；N 笔持仓 = 3×N 次全历史 SELECT。`symbol_annotations`（trade_records.py:323-335）还对每笔持仓按 tight/loose 把 `compute_manual_trade` **整算两遍**（唯一差别是两个 ATR 倍数，却各自重算 ATR 序列、净值序列、回撤） | df/metadata 作为参数透传；止损双档改为一次计算按两组乘数出两份价格 |
| Q-8 | 低 | `main.py:280-296` | `AssetVersionMiddleware` 每个 HTTP 请求 `stat()` 一次 style.css；且版本只跟踪 style.css——若按 §4.3 抽出 `web/static/common.js`，新文件无版本跟踪，浏览器旧缓存会造成「第三种漂移」 | 版本号改为「static 目录所有文件的 max(mtime)」进程内缓存 + 定时刷新；这是 common.js 落地的前置 |
| Q-9 | 低 | `rule_backtest/batch_service.py:535` | `_flush_counts` 每格一次 UPDATE（叠加每格一次 INSERT）= 每格两次写库 | 每 N 格 flush 一次计数 |
| Q-10 | 低 | `data/service.py:824-836` | `update_pool_daily` 把含全部逐标的明细的巨型 payload 写入 `job_runs`；`get_latest_job_run` 每次全量 `json.loads` | 落库存摘要（计数+失败列表），明细仅留日志 |
| Q-11 | 低 | `data/indicator_store.py:169-190` | `_cache_fresh` 每次 `get_series` 前置 4 个 round-trip（`indicator_cache_info` 2 条聚合 SQL + `get_market_data_summary` + `get_data_version`）判新鲜度 | 合并为单条 SQL，或构建侧维护单行 per-symbol 新鲜度台账 |
| Q-12 | 低 | `db.py:1249-1284` | `_market_records` 用 `df.iterrows()` + 逐值字符串判断构建 upsert 记录——全量物化与批量回填热路径上的纯 Python 百万次循环 | 向量化（`itertuples`/`to_numpy` + 布尔掩码） |
| Q-13 | 低 | `data/service.py:504-507` | `_save_backfill_result` 写完立即整表重读该标的历史，只为得到 rows_after/起止日期——可从 `to_save` 与写入前的 `existing` 直接算出 | 去掉重读 |
| Q-14 | 低 | `routers/subject_market.py:20` vs `trend_mcp/server.py:91` | `RevisionCache` 两个独立实例，同一 revision Web 与 MCP 各自全量算一遍看板 | 提升为共享单例 |
| Q-15 | 低 | `routers/rule_backtest.py:176` + `engine.py:60,100,142` | 单标的回测 job 同时常驻完整结果与 slim 结果两份；`condition_trace` 每日每条件一条无界增长（5000 根 × 多条件 = 数十万 dict）；job TTL 30 分钟且只惰性清理 | job 只留 slim 结果（debug_log 按需单独存）；`condition_trace` 仅在 debug 模式收集 |
| Q-16 | 低 | `routers/market_view.py:263-290` | 日 K 接口先全量加载再内存切片 `tail(limit)`，start/end/limit 不下推 SQL；intraday 分支对「历史+合成K线」再全量重算一次指标，与首次全量计算重复 | 常用窗口下推 WHERE；intraday 重算复用首次结果增量追加 |
| Q-17 | 信息 | 全部路由 | `async def` 路由内同步 sqlite/pandas 调用阻塞事件循环；大计算已 `run_in_threadpool`（看板），但手工交易列表、instruments 列表等重接口仍在事件循环上同步执行 | 单用户系统影响小；若多人使用，把重接口挪到 threadpool |
| Q-18 | 中 | `provider_tickflow.py:43-44` + 全部 `DataService()` 新建点 | **DataService 随处 new → 实例级限流器被分身稀释**：`_rate_limit_lock`/`_next_request_at` 是 provider 实例属性，每 `DataService()` 一个独立限流状态；`stop_loss._fetch_intraday_bar`（stop_loss.py:83）等热路径每次调用新建实例，进程级限流意图（tickflow 按次限流）被稀释为「每实例限流」。建议 DataService 单例化（app.state 或模块级），与 C-22 一并解决 |
| Q-19 | 低 | `data/service.py:198-221` | `fetch_latest_quotes` 返回字典的键不一致：缓存命中按调用方原始 symbol 入键（:200），新拉取的按归一化 symbol 入键（:221）——调用方传非规范代码时同一响应里键两种写法并存（`_quote_cache_get/_put` 内部已归一化，故仅影响返回字典的键，当前上游调用方均已归一化，无实际影响）。建议入口统一归一化（一行） |

---

## 4. 架构与重复代码

### 4.1 两份必须保持一致的公式/聚合实现（最大架构隐患）

1. **`compute_intraday_trend_cached`（`intraday_service.py:245-379`）是 `core/trend.calculate_trend_score_series` 公式的第二份手写实现**：权重、tanh、clip、vol_ratio、ER 全部内联复制。目前靠 `tests/unit/test_intraday_trend_consistency.py` 锁一致性，但公式演进时双写漂移只是时间问题。
2. **EOD 看板与盘中看板的聚合层整体平行**：`services/dashboard.py` 与 `data/intraday_service.py` 各自实现 `_number`/`_ma5`/`_strength`/`_priority`/`_key_tuple`/`_macd_counts`/`_sort`（逐行相同）、`DISPLAY_DAYS`（61，两处，注释自述「保持一致」）、以及「L2/L3/标的聚合 → strength → 嵌套 children → groups」的完整编排；`_aggregate_daily`（向量化）与 `_weighted_daily_trend_series`（字典循环）是同一加权口径的两套实现。

**建议（两阶段，控制风险）**：阶段一只抽 5+ 个逐字相同的纯函数进 `services/dashboard_common.py`（零风险，半天）；阶段二再动聚合骨架与公式核心，以 `test_intraday_trend_consistency` 等一致性测试为守门员，允许评估后停在阶段一。

### 4.2 JobManager 脚手架三份 + 内存状态不可恢复

`instrument_jobs.py` 的 `BulkBackfillJobManager` / `InstrumentAddJobManager` / `EtfConstituentImportJobManager`：锁 + status dict + daemon thread + snapshot + `_copy_status` + record_job_run + close data_service 全部雷同，约 600 行可压缩一半。三者及 `routers/instruments.py:20-30` 还大量 import `instrument_admin` 的下划线私有函数——私有契约被跨模块消费，等于没有封装。

三个管理器的状态全在内存，重启后页面显示「空闲」而 `job_runs` 有历史——用户无法区分「从未跑过」与「跑完丢了状态」。

**建议**：抽公共基类（状态机 + 线程生命周期）；共用函数改公开命名；状态可从 `job_runs` 恢复最近一次做展示。

### 4.3 工具函数多处拷贝（且前端块级副本已漂移）

| 函数/块 | 份数 | 位置 |
|------|------|------|
| `safe_float` | 3 | `core/trend.py:44`（默认 0.0）、`data/provider_utils.py:9`、`rule_backtest/indicators.py:10`（默认 None 且去逗号——语义还不一样） |
| `_category_path` | 5 | `db.py:705`、`instrument_admin.py:84`、`routers/instruments.py:137`、`routers/market_view.py:47`、`trend_mcp/server.py:76` |
| `.SS`/`.SH` 后缀互转 | 4 | `provider_tickflow._to/_from_tickflow_symbol`、`stock_industry.tickflow_symbol_to_project`/`project_symbol_to_tushare` |
| `esc()` (JS) | 7 | 6 个模板各一份 `function esc` + `subject_market.html:120` 一份 `const escHtml` 箭头函数 |
| `_num`/`_series`/`_number` | 4+ | `market_indicators.py`、`market_view.py`、`dashboard.py`、`intraday_service.py` |
| `_date_span` | 2 | `data/service.py:399`、`instrument_admin.py:39` |
| `_normalize_symbol` 等薄包装 | 多处 | `instrument_admin.py:27-36`、`market_view.py:39-44` 纯转发函数 |
| **止损卡/悬停文案整块** | 2 | `manual_trade.html:246-708` vs `subject_market.html:1114-1175`——**两副本已经漂移**（手工交易页含「硬止损附带当日最低/收盘」，看板页没有）；买入日价格区间提示逻辑同样两份 |
| 报价字段映射 | 2 | `provider_tickflow.py:330-343` vs `398-411` 逐行相同 |
| 引擎 K 线 MA | 2 | `engine.py:617-619` 手写 rolling mean，未用统一指标库 `core_ind.sma` |

建议：Python 侧统一收进 `core/`；前端抽 `web/static/common.js`（esc、fmt、止损卡组件）——**前置是 Q-8 的版本号机制扩展**，且止损卡合并前必须先做产品裁决「以哪份为准」（这不是机械合并）。

### 4.4 其他架构观察

- **A-4【中】`core` 对 `data` 的反向依赖**：`core/jobs.py:17-18`（core 导入 `data.service`/`data.storage.db`）、`core/strategy_config.py:51`（运行时导入 data 层）。README 自述依赖方向 `services → core / data`，core 本应纯计算；`core/jobs.py` 是编排逻辑，应上移到 `services/`。
- **A-5【中】services 层依赖 HTTP 框架**：`instrument_jobs.py:15` import `fastapi.HTTPException`——服务层不应感知 HTTP。job manager 的输入校验错误应改领域异常，由路由映射 400。
- **A-6【低】模块级可变单例散布各层**：`_rule_jobs`（rule_backtest.py:23）、三个 JobManager、`snapshot_runner`、`_symbol_locks`/`_quote_cache`（service.py:21-47）。全局态是测试污染的主要来源（tests/api/conftest.py 注释自证），也使 `uvicorn workers>1` 行为不可预期——**「单进程部署」这一约束没有任何文档说明**。
- **A-7【低】`db.py` 2119 行上帝类**：15+ 个业务域揉在一个 `Database`。但全拆 105 个方法对单人项目是 2-3 天纯风险工时，建议降级处理：`_init_tables` DDL 按表分段 + users/sessions 域随 P0-5 的哈希改造顺手迁出，其余冻结。
- `data/service.py:115-120` `_ordered_providers`、`indicator_builder.py:45-49` `params_hash` 无调用方（死代码）；`_retry_wait_seconds` 解析中文错误文案「请 X ms 后重试」决定退避（service.py:69-75）——vendor 文案一变即失效，建议优先看 HTTP 状态码，文案解析只做兜底。

---

## 5. 死代码 / 冗余清单（ruff + 人工复核）

**A 组：删除有行为收益**

1. `services/stock_industry.py:224-238` —— `build_sw_tree` 第一整段 for 循环是复制粘贴残留：`l1_order`/`l2_order` 在 236-238 行被重新赋空后重跑；构建逻辑完全无效（唯一副作用是命中剔除分支时产生**重复的 error 日志**）；
2. `data/service.py:115-120` —— `_ordered_providers` 无调用方；
3. `services/indicator_builder.py:45-49` —— `params_hash` 无调用方；
4. `services/indicator_builder.py:137-144` —— `detect_adjustment_breaks` 保留已作废参数 `end_date`/`lookback` 并 `del`（旧比价法包袱）；
5. `data/provider_tickflow.py:305-313` + `433-434` + `data/service.py:169-177` —— `fetch_minute_history` 必抛异常却保留完整转发链（provider 协议 + DataService 转发），`fetch_trading_calendar` 永远返回 `[]`：保留接口可以，但应标注「存根」避免误以为可用；
6. `src/backtest`、`src/engine`、`src/notify`、`src/portfolio`、`src/strategy` 五个目录只剩 `__pycache__`（骨架时代残留，未跟踪但污染工作区）；`src/trend_etf_system.egg-info/` 构建残留混在 src 下——删除并加入 .gitignore；
7. git 跟踪了 `.coverage`、`trend-quant.zip`、`.agents/skills/trend-score-calculator.zip`——构建/数据工件入库（含敏感信息残留面），从索引移除并加 .gitignore。

**B 组：纯卫生（ruff F401/F841，可 `--fix` 大半）**

8. `routers/instruments.py:4,6,8,16` —— `threading`/`Callable`/`pandas`/`normalize_symbol`/`symbol_suffix`/`symbol_to_code` 六个未用导入；
9. `routers/market_view.py:3,6,12,26` —— `datetime`/`numpy`/`strip_etf_suffix`/`TREND_MA_PERIODS` 四个未用导入；
10. `services/dashboard.py:23`、`services/instrument_admin.py:13`、`services/market_indicators.py:9`、`data/intraday_service.py:15,41`、`rule_backtest/engine.py:9`、`rule_backtest/service.py:6`、`scripts/migrate_category_sw2021.py:34` —— 各一处未用导入；
11. `data/intraday_service.py:283` —— `prev_close` 赋值未用（F841）；663 行 `intraday_ts = result["trend_score"]` 变量误命名（名为时间戳实为趋势值）；35 行 `quote_trade_date` 的 `"date | None"` 注解引用函数内局部 import 的 `date`（ruff F821，类型检查器会报错）；
12. `rule_backtest/value_resolver.py:121` —— `field_series(bars, field) if field != "volume" else field_series(bars, "volume")` 两分支相同，冗余三元；
13. `db.py:1325,1723,1793` —— 函数内 `import pandas`，模块顶部已有导入；
14. `routers/instruments.py:88-93` —— 连续 6 个空行；`core/indicators.py:23`、`core/trend.py:27` 两处「future P1」注释（P1 已完成）；
15. `routers/instruments.py:464`、`data/service.py:392,491,551` —— `"path": f"sqlite/..."` 响应字段是 parquet 时代遗留的死字段（前端不消费）；
16. `web/templates/base.html:7`、`login.html:7` —— 静态版本串手工后缀 `-20260824-auth`（与 asset_version 机制重复且会过期）；UTF-8 BOM 存在于 10 个文件（`src/__init__.py`、`core/settings.py`、`provider_base.py`、`provider_utils.py`、`base.html` 等）；
17. `pyproject.toml` —— `pyarrow`、`email-validator` 声明但全 src 无引用。

---

## 6. 正确性与口径风险

| # | 级别 | 位置 | 问题 |
|---|------|------|------|
| C-1 | 中 | `data/service.py:343-346` | `ensure_daily_history` 把 `DataProviderError`（空响应）一律当「区间无新 bar」。若行情商故障/限流导致全池返回空，所有标的静默 `up_to_date`，日更仍记 `completed`——故障被吞。建议：全池为空或失败率超阈值时任务记 failed/partial 并告警 |
| C-2 | 中 | `core/jobs.py:63`、`core/calendar.py:146,158`、`routers/rule_backtest.py:72`、`services/auth.py` sessions 过期、`db.py:48-50` `_dt_str` 等 | **两套「今天」并存**：`date.today()`/`datetime.now()`/SQLite `localtime`（宿主机时区）vs `market_now()`（配置时区，docstring 明示「非 CN 主机两者不同」）。最重的一处是 `core/jobs.py:63`——日更主任务的交易日门控用宿主机本地日期；UTC 主机上 16:30 后与北京时间差一天，日更日期、会话过期、快照新鲜度、回测日期截断全部偏移 8 小时。建议全部收敛到 `market_now()`，SQLite 默认值改应用层写入 |
| C-3 | 中 | `services/manual_trade.py` 全链路 | 手工交易的买入价是用户**实际成交价（未复权）**，而止损/净值/区间校验全部基于 qfq 序列：买入后若发生除权，口径错配（区间校验、硬止损基准、盈亏比例全部失真）。ETF 分红少所以现实影响小，但股票类标的已批量入池（ETF 重仓股导入），风险在变大。建议买入时按当日因子把 buy_price 换算成 qfq 口径存储（或记录 raw + 换算因子）——**此条需要产品裁决，见 §14** |
| C-4 | 中 | `data/provider_tickflow.py:294` vs `122-123` | 除权因子日期用 UTC（`tz=timezone.utc` 取日期），日 K 日期用 Asia/Shanghai——同一 vendor 毫秒时间戳两套口径。若 vendor 时间戳是上海当日 00:00（=UTC 前一日 16:00），因子日期比 K 线日期早一天，`compute_qfq` 的 `bisect_left`（`ex_date >= t` 参与除权）会把除权提前一天应用。注释声称「已验证」，但两套口径并存本身是隐患。建议统一时区 + 加「除权日前后 qfq 连续性」回归测试 |
| C-5 | 中 | `services/dashboard_snapshot.py:49-60` | `latest_snapshot()` 先置 `_snapshot_loaded = True` 再尝试 DB 读取——一次性 DB 抖动后 `_snapshot` 永久停在 None，直到下一次重算成功或进程重启。瞬时故障被固化成全天故障。建议失败时不置位，允许下次重试 |
| C-6 | 低 | `db.py:1399-1403` | `replace_ex_factors` 的 DELETE 与 save 分两个连接两个事务，中间崩溃留空因子表 → 下次物化出未复权「qfq」（与 P0-7 同一修复窗口） |
| C-7 | 低 | `rule_backtest/metrics.py:97` | 年化 `(1+tr)^(252/n)-1` 对极短持仓（几天）爆出天文数字；手工交易页与回测详情直接展示。建议短窗口（如 <20 交易日）年化置 None 或前端标注 |
| C-8 | 低 | `rule_backtest/metrics.py:226-228, 250-251` | 月度收益与热力图 `pct_change().dropna()` 丢失首月（该月往往是信号最集中的区间）；年度收益以首日净值为基准包含首年，两个口径不一致。建议首月相对首日净值计算 |
| C-9 | 低 | `rule_backtest/batch_service.py:386-388, 456` | 批量回测策略被快照冻结但**标的集合没有**——POST /run 到执行之间若有人编辑分类/启停标的，实际宇宙与 ETA/总数不符，进度可超 100%；且批次锚定 `data_anchor_date` 但逐标的实时读库，跑批撞上 16:30 日更时前后标的数据截止日不一致。建议标的清单冻结进 `config_json`，run 开始校验 data_version |
| C-10 | 低 | `data/provider_utils.py:55` | `standardize_ohlcv` 缺 time 列时用 `datetime.now()` 逐行填充——静默伪造时间戳入库。建议改为丢弃并告警 |
| C-11 | 低 | `intraday_service.py:786-789` | 盘中看板 `return_1d` 的「昨收」取 tail 末根；若当日 K 已落库，末根就是今日，return_1d 恒 0。快照任务已在今日落库后 skip，风险低，但建议 returns 计算也过 `has_persisted_today_bar` |
| C-12 | 低 | `services/dashboard.py:319-321` | 缓存 trend 命中分支 `(symbol, date)` 查不到的日期静默给 NaN，无日志；建议缺口率异常时记 debug |
| C-13 | 低 | `db.py:1428-1436, 1451-1456` | `users.username` UNIQUE 区分大小写但写入只 strip 不 lower——可创建 `Admin`/`admin` 两个用户。建议统一 lower |
| C-14 | 低 | `rule_backtest/engine.py:283-286` | `_prepare_bars` 对 `date`/`time` 两列都缺的输入抛裸 KeyError，无标的上下文。建议显式校验抛 ValueError |
| C-15 | 低 | `rule_backtest/engine.py:129` + `models.py:63-66` | `last_exit_bar_idx` 冷却期记账依赖「`len(day_bars)-1` 恰好等于该日在 all_bars 的位置」这一隐含约定；任何对 bars 重新 `reset_index` 的改动都会静默错位。建议直接用 iterrows 的标签 `idx` 显式记账 |
| C-16 | 低 | `db.py:1294-1299` | raw 表宣称「append-only 永不改写」，但写入语句是 `INSERT OR REPLACE`——vendor 回溯改数（正是该架构要防的事）会静默覆盖旧值。建议 raw 表用 `INSERT OR IGNORE` + 冲突检测告警 |
| C-17 | 低 | `db.py:67-76` | `PRAGMA foreign_keys` 从未开启，`sessions.user_id`/`manual_trades.user_id` 的 `REFERENCES` 不生效——删用户留孤儿行。开启前需先校验存量无孤儿行 |
| C-18 | 低 | `db.py:143-144, 159-160, 170-171` | `market_data_raw/qfq`、`ex_factors` 的 `PRIMARY KEY (symbol, time)` 已自动建索引，`idx_*_symbol_time` 是重复索引，白付写放大与存储 |
| C-19 | 信息 | `instrument_admin.py:202-245`、`routers/instruments.py:215-275` | 新增标的的 409 预检与 job manager 状态判定之间存在 TOCTOU 窗口（`add_constituent_stock` 已有 ValueError 兜底，后果仅是友好报错）。接受现状，文档化即可 |
| C-20 | 中 | `instrument_jobs.py:432,628`、`data/service.py:571`、`core/jobs.py:84` | **同一标的的「历史起点」三条路径三种口径**：手动新增/ETF 导入回补硬编码 `date(2020,1,1)`；批量回补缺省 `date(2020,1,1)`；而「启用但从未回补」的标的由每日 16:30 任务从 `backtest_start_primary`（默认 2025-01-01，且回测参数当日更起点本身是概念误用）开始拉。结果：标的池内历史深度参差，看板/回测横截面不可比。建议定义独立的 `history_start_default` 配置，三条路径统一引用 |
| C-21 | 中 | `routers/market_view.py:288-307` | **Web 日 K 指标在「截断窗口」上计算——与 MCP 已修复 bug 同源**：接口先按 start/end/limit 截窗再 `build_market_payload` → `compute_market_indicators`（:183,307）；而 `trend_mcp/server.py:223-227` 注释明确记载「EMA 族指标无限记忆，先截断再算会让数值依赖请求窗口——旧的窗口截断 bug」，MCP 已改全历史计算。`limit` 允许 `ge=1`，显式传小 limit 时同一标的同一日期的 MACD/EMA/趋势值随窗口变化，Web 与 MCP 口径不一致（默认 20000 掩盖了问题，潜伏 bug）。建议与 MCP 对齐：指标全历史计算、输出数组再 tail |
| C-22 | 中 | `intraday_service.py:204` | `build_intraday_overlay` 自建 `DataService()` 从不 `close`——Web 标的页（market_view.py:319）与 MCP symbol_detail（server.py:280）都不传 data_service，**每次页面打开/工具调用新建一个 TickFlow/httpx client 泄漏**。对照：`stop_loss._fetch_intraday_bar`（stop_loss.py:82-88）有正确的 try/finally close。建议统一 try/finally close，或配合 Q-18 单例化一并解决 |
| C-23 | 中 | `intraday_service.py:842-846` vs `dashboard.py:83-112` | **EOD 成交额加权 vs 盘中简单平均**：EOD 聚合用成交额加权（`_aggregate_daily`），盘中对 trend_score/涨跌幅用简单平均（`np.mean`）——同一分组在两个视图的趋势值/涨跌幅不可比（盘中已具备权重原料 `trend_series_amounts`，统一成本低）。另：`return_1d` 初始化为 None（:736），`float(rows_df["return_1d"].mean())` 在整组全 None 时产出 NaN，默认 JSON 序列化后浏览器 `JSON.parse` 会抛错——mean 前应 dropna，空则置 None |
| C-24 | 低（待确认） | `dashboard.py:267-275, 376-377`、`intraday_service.py:960-966, 992-993` | 看板 L2 排序疑似错用 `priority_l3`：`_sort_items`/`_sort` 的 key 恒用 `priority_l3`，L2 行排序吃的是「子级 priority_l3 的 min 聚合」——`instrument_admin.category_priorities` 为三级各自返回独立 priority，priority_l2 字段存在的意义就是 L2 排序，当前用法使其**配置实际失效**（也可能是「按最重要子类排」的有意设计，待确认，见 §14-6） |
| C-25 | 低 | `core/calendar.py:88-99` | `is_trading_time` 只查时间窗不校验交易日，与同文件 `is_realtime_available`/`is_past_market_open` 语义不一致；当前唯一调用方自己补了交易日判断所以没炸，属潜伏 API 陷阱。建议函数内补 `is_trading_day` 或改名 `is_continuous_auction_hours` 明示语义 |
| C-26 | 低 | `main.py:64`、`core/settings.py:45`、`audit/app_logger.py:8`、`db.py:54` | **cwd 相对路径散落**：`Path("data")`、`config/app.yaml`、`logs/app`、DB 默认 `data/trend_quant.db` 全部相对当前工作目录——非项目根启动（systemd 不同 WorkingDirectory、其他目录跑脚本）会静默建错目录/新建空库。建议统一基于项目根解析（`__file__` 锚定）或在启动时断言 cwd |

---

## 7. 可靠性与运维

**做得好的**（显式记录，避免高估风险）：结构化日志 + RotatingFileHandler 分 app/access 两文件；5xx 才告警的异常处理器；孤儿批次启动清理；日更启动补偿（含「任务成功但数据落后」双重校验）；job_runs 落库；指标重建前 `VACUUM INTO` 备份；快照/缓存重启可恢复；`RevisionCache` 双检锁单飞；`_update_job_lock` 非阻塞防重入；per-symbol 锁；`create_batch_run_if_idle`/`delete_batch_run` 的 BEGIN IMMEDIATE 事务；每调用新建连接规避了连接线程安全问题。

**缺口**：

| # | 级别 | 问题 | 建议 |
|---|------|------|------|
| O-1 | 高 | **无定时备份机制**：`backup_to` 仅启动重建与手工脚本两个偶发触发点；2.8GB 不可再生单库，丢数据的期望损失高于本报告大多数条目 | 每日更新成功后自动触发备份；磁盘账：主库 2.8GB，`VACUUM INTO` 全量拷贝，建议 keep=3 日备（约 8.4GB）+ 每周一份异机/云盘 |
| O-2 | 高 | **迁移/回填脚本用 `shutil.copy2` 备份 WAL 活库**（`migrate_category_simplify.py:128`、`backfill_batch_excess_metrics.py:179`）：未 checkpoint 的最近数据在 `-wal` 文件里，备份可能缺最近事务甚至不一致；正确实现 `Database.backup_to()` 就在同项目里没被复用 | 统一改 `init_db(...).backup_to()` |
| O-3 | 中 | **生产无创建首个登录用户的入口**：`create_user` 调用方全在 tests/；登录墙上线后全新部署无法产生第一个用户，只能手工 sqlite3 写库（还得自己会算 pbkdf2 格式） | 提供 `scripts/create_user.py`（交互输入密码），README 补一行 |
| O-4 | 中 | 无健康检查端点；`SchedulerManager.jobs_snapshot()`（core/scheduler.py:94-100）实现了却**没有任何路由消费**——死代码 + 运维盲区二合一 | 加 `/healthz`（DB 可读 + 最近 job_runs 状态 + 调度器下次触发 + 快照新鲜度），登录墙豁免或内网限定 |
| O-5 | 中 | 关键后台任务失败只进日志，无主动告警面；指标缓存重建失败时看板静默走实时回退（变慢但无信号） | 失败计数写 job_runs/app_config 并在通知条展示；可配置 webhook/邮件 |
| O-6 | 中 | **带外写库脚本与运行中服务无互斥**：`rerun_daily_update.py`（全文无锁）等服务运行时执行会与 16:30 任务/盘中快照并发写同一 WAL 库；进程内 `_update_job_lock` 管不到它。另：**调度器内部并发**——`BackgroundScheduler` 默认 `max_workers=10`（scheduler.py:41 未设限），日更、月度行业同步、盘中快照三类任务可并发命中同一 WAL 库（月初 1 号与交易日重合时现实发生）。P0-3 修正后实际有驱动默认 5s 等待兜底，长写事务叠加时才可能超时 | 显式 busy_timeout（P0-3 联动）+ 文档写明「跑写入类脚本后重启服务」；评估给调度器加 `max_workers=1` 或对写任务加互斥；长期可加文件锁 |
| O-7 | 中 | `chinese_calendar` 年度数据边界无运维着落：2027-01-01 起法定假日被当交易日，`_daily_update_catchup` 会把假日判为「应更未更」，**每次重启触发一次无效 force 补跑**直到人工升级库；「每年 12 月升级」只写在 calendar.py docstring | 写进 README/运维文档 + 日历降级时通知条告警（warning 现在无人能看见） |
| O-8 | 中 | **无 CI + pyproject 无 dev 依赖声明**（pytest/pytest-asyncio/pytest-cov/ruff 均未声明；`asyncio_mode="auto"` 在无 pytest-asyncio 环境下是未知配置项）——新机器 `pip install -e .` 后测试跑不起来（P0-8 同根因）；`make test-deps` 装未钉版最新包，不可复现 | dev optional-dependencies + 单矩阵 CI（哪怕只跑 `-m "not slow"`） |
| O-9 | 低 | 15:00 收盘快照后盘中看板冻结至 16:30：15:00 报价可能未含收盘集合竞价最终值，`eod_current` 守卫又保证收盘后不再算 | 增加 15:05/15:10 一轮确认快照 |
| O-10 | 低 | 脚本的数据库路径处理不一致：`migrate_raw_qfq.py`/`rerun_daily_update.py` 无 `--db` 参数只能跑默认路径，从非项目根目录执行会在 CWD 下**新建空库**并「成功」跑空迁移；其余脚本有 `--db` | 统一加 `--db` 并在缺省时校验文件存在 |
| O-11 | 低 | 调度器 misfire 只兜启动：运行期间 16:30 任务失败当天再无重试直到次日 | 任务失败后安排 1-2 次延迟重试（date trigger） |
| O-12 | 低 | `backup_to(keep=3)`：启动重建 + 手工脚本都会触发备份，keep=3 容易被快速挤掉更早的好备份 | 与 O-1 合并设计（按「每日一份保留 N 天」策略） |

---

## 8. 测试评估

**首要前提：P0-8（套件当前收集失败）是所有测试工作的地基，以下缺口都排在它之后。**

**覆盖现状（好的一面）**：61 个测试文件，分层清晰（unit/integration/api）；关键正确性有 golden 测试（`test_batch_golden`、`test_p13_memoized_golden`）、新旧实现等价测试、盘中/EOD 一致性测试、登录墙测试、MCP 工具测试、迁移脚本测试。

**缺口**：

1. **P0-1 的 NameError 路径无测试**：`_category_options` 在分类表为空时的兜底分支无人覆盖；
2. **无 SQLite 并发写测试**（P0-3 修复后应配回归测试）；`backup_to` 的 keep 修剪、`delete_batch_run` 的级联删除（含 BEGIN IMMEDIATE 与 running 拒绝）也无直接单测——「出错即数据事故」的高性价比补测点；
3. **P0-7 空因子擦除无测试**：`sync_ex_factors` 对「存量有因子、拉到空」的行为应锁定（修复后断言行不变 + 告警）；
4. **MCP `symbol_detail` 未断言 dates 与 indicators 长度一致**（P0-6 正好暴露）；
5. **调度/任务层基本无测试**：`core/scheduler.py`、`core/jobs.py`（非交易日跳过/失败记录）、`_daily_update_catchup` 的三种漏更场景（进程离线/上次失败/vendor 延迟）均无单测——数据新鲜度的最后防线，错了全天无数据且无告警；
6. **`services/dashboard.py` 聚合层**：`_aggregate_daily` 的成交额加权口径（含 amount 缺失等权兜底）、`_assign_envelope`、强度百分位缺边界单测；
7. **`instrument_jobs` 三个 JobManager**：job 生命周期（并发拒绝、失败落库、rebuild 触发）只有间接覆盖；
8. **`scripts/`**：只有 tushare 类脚本有测试；`migrate_*`、`backfill_batch_excess_metrics`、`import_all_etf_constituents` 的 dry-run/幂等性无测试（这类脚本出错就是数据事故）；
9. **已知失败的测试长期留在套件里**：CLAUDE.md 自述「2 pre-existing failures in tests/integration/test_intraday_service.py」——基线红测试麻痹「全绿」信号，应修掉或 `xfail(strict=True)` 标注；
10. **tests/ 根目录 10 个测试文件无 marker**：`make test-unit`/`-m unit` 永远选不到它们，与 pyproject markers 约定不一致；
11. **前端 JS 零测试**：几千行内联 JS（排序、轮询、弹窗、透视表）全靠手工验证；esc/格式化类纯函数值得随 common.js 抽出后补测；
12. **API 测试 fixture 对全局态的防御 fragile**：靠「导入顺序 + monkeypatch 时机」避免污染（conftest.py 注释自承脆弱），任何新模块顶层值绑定 `get_db` 都会踩坑；长期靠依赖注入根治。

---

## 9. 文档与注释时效

1. **README.md 明显过时**：「MCP 服务（/mcp/sse）：**5 个工具**」——实际已是 7 个（add_trade、open_positions 后加）；架构树缺 `audit/`、`stock_industry`、`dashboard_snapshot`、`manual_trade`、`stop_loss`、`trade_records`、`auth`、`batch_service`、`rule_backtest/sizing/` 等后增模块；「关键设计」未提登录墙（2026-08 落地）、申万行业分类体系、ETF 重仓股、手工交易、仓位策略；`core/jobs.py` 标为「领域核心（纯计算）」但它做编排（§4.4-A4）；
2. **CLAUDE.md**：自述基线 2 个红测试（§8-9），且同样未覆盖近期模块；
3. **TODO.md**：引用不存在的 `/backtest` 页面（实际为 `/rule-backtest`）；
4. `docs/architecture-review-2026-08-01.md` 距今 3 周+，其间落地了登录墙、申万分类、ETF 重仓、批量回测增强，需要刷新或标注历史版本；
5. `config/app.yaml:13` 注释说「当前为付费年会员」但 `plan: starter`——`plan` 字段实际语义是「限额档位」而非会员状态（provider 里 `plan != "starter"` 直接 raise），命名误导；
6. **部署形态三处互相矛盾**：`scripts/deploy.sh`（/opt/trend-quant + nginx + root）vs `main.py:381-383` 注释（frp 直连、无 nginx）vs 运维文档（/srv/trend-quant）——以哪个为准需要落定一份；
7. 代码内注释质量整体很高（设计理由、事故记录、口径约定都写了），未见大面积注释腐烂；个别「future P1」表述指向的 P1 已完成（§5-B14）；
8. git 状态显示有 4 份文档被删除（D）、多份在改（M）——进行中工作，收尾时保证 `verification-and-rollout.md` 与代码行为同步。

---

## 10. UX 与功能建议

1. **U-1【中】标的管理页无法停用/删除标的**：`enabled` 字段决定日更池与看板过滤，但只能在建标时写入，之后 UI 无法改——误加的标的只能动数据库。建议更新接口加 `enabled` 字段 + 列表页加启停开关；
2. **批量回测删除无回收站**：`DELETE /api/runs/{id}` 立即物理级联删除批次+全部格子。建议前端二次确认 + 软删除或至少导出再删；
3. **内存任务重启即丢**：单标的回测 `_rule_jobs`（30 分钟 TTL）重启后 404「任务不存在或已过期」，文案可更友好（「服务已重启，请重新发起回测」）；三个 JobManager 同理（§4.2）；
4. **失败标的的补救路径**：日更/回补失败后只能在 instruments 页逐个手动补；建议列表失败摘要带「重试失败标的」入口；
5. **年化指标短窗口失真**（C-7）也是 UX 问题：天文数字年化会直接误导决策；
6. **ETF 权重股弹窗**：非 A 股行的「不纳入管理」状态可以带原因 tooltip（为什么不可导入）；
7. **看板快照时段外无数据提示**：非交易时段打开看板只看到 EOD 数据，没有「下一快照时间」提示；`refresh-status` 接口已有数据，前端可展示；
8. **U-3【低】subject_market 快照错误信息未转义插入 innerHTML**（§2 XSS 专项的例外点）：统一走 `escHtml`/textContent；
9. **全站轮询无退避/无页面可见性感知**：`base.html:150` 每页 30 秒轮询日更状态直至永远，后台标签页照跑；补 `visibilitychange` 挂起是十行内的改善；
10. **登录页无密码可见性切换、无 Caps Lock 提示**（低优先级增强）；
11. **止损卡两处副本已漂移**（§4.3）：合并前需要产品裁决「以哪份为准」。

---

## 11. 优先级路线图（含来源编号与 Done 定义）

编号说明：P0-x 为「严重问题」分级（§1），S/Q/C/O/U 为专项编号；路线图的 P0/P1/P2 为**调度建议**。每条编号在下表必有去向（含「不采纳 + 理由」见 §14）。

### P0（本周内：改动小、收益大、或是后续一切的地基）

| 来源 | 事项 | Done 定义 |
|---|---|---|
| P0-8 | 补装 `mcp` / 两个 MCP 测试文件加 `importorskip`；pyproject 补 dev 依赖（O-8 的前半） | `pytest --collect-only -q` 零 error |
| P0-1 | 修 `_config_items` NameError（兜底分支直接用已查元数据）+ 空分类表 API 测试 | 新测试通过；清空 `instrument_categories` 后三个接口不再 500 |
| P0-3 | 显式 `PRAGMA busy_timeout = 10000`（消除对驱动默认 5s 的隐式依赖）+ 并发写回归测试 | 新测试模拟双写者通过；grep 确认 PRAGMA 存在 |
| P0-7 | 空因子擦除防护（存量 N>0 拉到空 → 拒绝覆盖 + 告警）+ `replace_ex_factors` 并单事务（C-6）+ 行为锁定测试 | 新测试通过；构造空因子响应后因子表行数不变 |
| P0-2 | MCP 共享密钥（SSE 层校验）+ 配额消耗型工具频限 | 无密钥调用被拒；密钥从环境变量读取不落盘 |
| P0-4 | 登录 + MCP 逐次鉴权统一滑动窗口限流 + `authenticate` 失败 warning 日志 | 连续 N 次失败返回 429/退避；日志出现失败记录（不含密码） |
| P0-6 | `symbol_detail` indicators 截尾 + dates/indicators 长度一致性断言 | 新断言通过 |
| §5 | 死代码 A 组 + B 组清理（ruff --fix 处理大半；空目录/egg-info/工件出库） | ruff F401/F841/F811 零命中；`git ls-files` 无 zip/.coverage |

### P1（两周内）

| 来源 | 事项 | Done 定义 |
|---|---|---|
| O-1+O-2+O-12 | 每日更新成功后自动备份（keep=3 日备 + 每周异机一份）；两个脚本 copy2 改 `backup_to()` | 日更 job_runs 记录备份路径；脚本 grep 无 `shutil.copy2` |
| O-3 | `scripts/create_user.py` + README 指引 | 全新空库可按文档 5 分钟创建首个用户并登录 |
| P0-5 | session token 哈希存储 + 滑动续期绝对上限（90 天）。顺延 P1 的理由：利用前提是库文件/备份泄露（非直接可达面），且涉及存量会话迁移，宜与 S-1 部署整改同窗口做 | 库中无明文 token；存量会话迁移或强制重登 |
| C-21 | Web 日 K 指标改全历史计算、输出再 tail（与 MCP 对齐） | 小 limit 下指标值与默认窗口一致；补口径测试 |
| C-22+Q-18 | DataService 单例化 + `build_intraday_overlay` try/finally close | 重复打开标的页不再新建 client；限流状态进程唯一 |
| C-20 | 定义 `history_start_default` 配置，新增/导入/日更三条路径统一引用 | 三处起点 grep 只剩配置引用 |
| S-13 | MCP 鉴权迁移 token 制（与 P0-2 同窗口） | 工具不再接收 password 参数 |
| C-23 | 盘中聚合改成交额加权 + `return_1d` mean 前 dropna | EOD/盘中同组值可比；全 None 组不再产出 NaN |
| S-1 | deploy.sh 按 §2-S-1 重写或删除改文档；部署形态三处描述对齐（§9-6） | 一份权威部署文档；脚本不存在或不再含 root/rm -rf/listen 80-only |
| Q-1 | 批量回测跨策略复用指标计算（缓存键含指标参数 fingerprint） | 同标的 N 策略耗时显著下降；golden 测试全绿 |
| C-1 | 日更空响应故障识别（全池空/失败率阈值 → 记 failed + 告警） | 构造全空响应测试，任务记 failed |
| C-2 | 时区口径统一到 `market_now()`（含 `_dt_str`/sessions） | grep `date.today()`/`datetime.now()` 裸调用仅剩白名单；UTC 环境单测通过 |
| §4.1 | 阶段一：逐字相同的纯函数抽 `dashboard_common.py` | 两文件 import 共享实现；一致性测试全绿 |
| O-4 | `/healthz` 端点 + `jobs_snapshot` 暴露 | 端点返回 DB/调度器/快照状态；登录墙策略明确 |
| O-7 | chinese_calendar 年度升级写入运维文档 + 降级时通知条告警 | README 有 ritual；降级 warning 触发前端提示 |
| §8-1/2/3/4/5 | 补测试：空分类表、并发写、空因子、MCP 长度断言、catchup 场景 | 各自测试通过 |
| §9 | README/CLAUDE.md/TODO.md 刷新 | MCP 工具数、架构树、页面路径与实际一致 |

### P2（一个月内，结构性）

| 来源 | 事项 | Done 定义 |
|---|---|---|
| Q-2/Q-3/Q-4 | 查询优化：列表 N+1 批量查、revision 去 COUNT(*)、trend_daily 加 `(param_set, time)` 索引 | /api/list 单查询出汇总；revision 无 COUNT；EXPLAIN 命中索引 |
| §4.2 | JobManager 基类抽取 + 状态从 job_runs 恢复 | 三类管理器共享基类；重启后页面显示最近任务 |
| §4.1 | 阶段二（可选）：公式/聚合核心合并 | 一致性测试守门；允许评估后停在阶段一 |
| Q-8+§4.3 | asset_version 扩展到整个 static 目录 + 抽 `common.js`（esc/fmt/止损卡，先做产品裁决） | esc 单份；止损卡单副本 |
| C-3 | 手工交易买入价复权口径（需产品裁决，见 §14） | 裁决落地 + 迁移脚本 |
| Q-7 | 手工交易链路 df/metadata 透传 + 止损双档一次计算 | 列表接口 SQL 计数下降 ≥50% |
| C-4 | 因子日期时区统一 + 除权日连续性回归测试 | 测试锁定 |
| C-9 | 批次标的清单冻结进 config_json + run 开始校验 data_version | 进度不超 100%；数据版本漂移有标记 |
| O-6/O-10 | 带外脚本并发写约束（busy_timeout 联动 + 文档）+ 脚本 `--db` 统一 | 全部脚本支持 `--db` 且缺省校验存在性 |
| O-8 | 单矩阵 CI（`-m "not slow"`） | PR/push 自动跑测试 |
| §4.4-A4/A5 | `core/jobs.py` 上移 services；job manager 领域异常替代 HTTPException | 依赖方向 grep 无 core→data |
| §8-6/7/8 | 聚合层边界单测、JobManager 生命周期测试、脚本 dry-run/幂等测试 | 测试通过 |
| S-3/S-5/S-9 | 安全响应头、logout 改 POST、会话吊销端点 | 逐项落实 |
| §10 | UX 批：enabled 启停 UI、删除软处理、快照时间提示、轮询可见性挂起 | 逐项落实 |
| §4.4-A6 | 文档明确「单进程部署」约束 | README 部署节有说明 |

### 明确降档/冻结（附理由）

- §4.4-A7 db.py 全拆 repository → 冻结为「DDL 分段 + users/sessions 域随 P0-5 迁出」（单人项目无协作冲突面，全拆 2-3 天纯风险工时）；
- S-4 CSRF 自定义头 → 可选（SameSite=lax 已挡常规场景）；
- S-10 计时侧信道 → 仅记录（单用户系统）；
- Q-17 重接口挪 threadpool → 信息级（单用户）；
- Q-5 增量物化 → 记账观察（当前日更耗时可接受时不动）。

---

## 12. 审查过程产物（本目录文件说明）

| 文件 | 内容 |
|---|---|
| `review-report.md` | 本报告（v2.1 最终版，本会话交付物） |
| `_notes.md` | 主审查逐文件原始发现流水账 |
| `independent-review.md` | 独立审查子代理的全量审查（67 条，查漏补缺） |
| `review-a-factcheck.md` | 评审 A：事实核查（约 50 条论断逐条比对） |
| `review-b-coverage.md` | 评审 B：覆盖面与建议质量（18 条补充 + 11 条建议修正） |
| `reconciliation.md` | 三方意见的逐条采纳/驳回记录 + v1→v2→v2.1 修订去向表 |
| `code-review-report.md` 等 | **并行审查产物**（另一会话的独立全量审查：code-review-report.md v2.2 + review-1/review-2/blind-audit 系列 + CHANGELOG/README）。两套审查互为盲测印证；其经我方核实的高价值独有发现已并入本报告（见 §13），其余独有条目（如其附录 A 的补测场景清单）可直接参阅该文件 |

---

## 13. v1 → v2 → v2.1 主要修订记录

**v1 → v2**：
- 事实修正：测试文件数 70→61；src 行数 1.2 万→1.7 万（16,816）；P0-2 的公网暴露证据从「frp（README/部署文档）」更正为 `deploy.sh`（nginx listen 80）+ `main.py` 注释的 frp 自述（两种部署形态矛盾另立 §9-6）；§5 第 12 条行号更正；esc 计数措辞（6 份 esc + 1 份 escHtml）；
- 新增（独立审查）：P0-7 空因子擦除、O-3 无建用户入口、C-5 快照永久 None、C-4 因子 UTC 口径、C-8 首月丢失、C-9 批次宇宙漂移、Q-7 链路三重加载（升级自原 Q-6/Q-7）、Q-11~Q-16、§5 空目录/工件、C-13~C-19、O-9/O-10、§8-9/10/12、U-1/U-3、S-9/S-10/S-11；
- 新增（覆盖面评审）：P0-8 测试收集失败、O-1 定时备份、O-2 copy2 备份、S-1 deploy.sh 专条、S-3 安全响应头、O-8 dev 依赖/CI、C-17 foreign_keys、Q-1 缓存键规格、§4.1 两阶段路径、备份磁盘账、db.py 降档、路线图「来源编号 + Done 定义」；
- 结构调整：§11 路线图重排（P0-2 按评审意见留在 P0；P0-5 顺延 P1 并注明理由）；§5 死代码分 A/B 组；新增 §7 开头正面清单与 §14 待裁决事项。

**v2 → v2.1（回签轮修订）**：
- **P0-3 事实修正（重要）**：v2 称「SQLite 默认 busy_timeout=0」有误——Python `sqlite3.connect` 默认 timeout=5.0s 落实为 busy_timeout=5000（实测）。条目降级为中，表述改为「依赖隐式默认、长写事务可能不足、无并发写测试」，建议值改 10000；O-6 联动措辞同步修正（独立审查与主审查在 v2 犯了同一错误，由独立审查回签时自纠）；
- **补回悬挂条目**：C-20（历史起点三口径，独立审查 B11，v2 误挂到 v1 实际未收录）；独立审查计数 45→67（其自述口算错误）；
- **回签轮新吸纳**（独立审查收尾残渣 + 并行审查 `code-review-report.md` 经我方逐条核实的独有发现）：S-13（MCP 密码作工具参数）、S-14（登录墙边缘行为三小点）、C-21（Web 日K 指标截窗计算，与 MCP 已修 bug 同源）、C-22（build_intraday_overlay 泄漏 client）、C-23（EOD 加权 vs 盘中简单平均 + NaN 风险）、C-24（L2 排序 priority_l3 待确认）、C-25（is_trading_time 不校验交易日）、C-26（cwd 相对路径）、Q-18（DataService 限流稀释）、Q-19（报价缓存键归一化）、§2 XSS 补 3 处未转义 + 1 处双重转义（market_view:1689/1604、batch_backtest:1053/1484）、§5-B15（sqlite path 死字段）、O-6 补调度器内部并发、S-12 补版本约束策略与 asyncio_mode 死配置；
- **路线图同步**：P0-3 建议值改 10000；P1 新增 C-21/C-22+Q-18/C-20/S-13/C-23 五行；P0-5 顺延理由补进 P1 表；§14 新增第 6 项（L2 排序意图）；
- **并行审查披露**：§0 版本说明与 §12 文件表新增并行审查产物（code-review-report.md v2.2 系列）的存在与关系说明；reconciliation.md §3 的程序性说明同步更正（该系列文件确实存在——由并行会话产出，此前「从未存在」的表述系主审查未列目录即断言的错误，向评审 B 致歉并致谢其举证）。

## 14. 需用户裁决的开放事项

1. **C-3 手工交易买入价口径**：买入价按实际成交价（raw）存储与计算，与 qfq 序列存在除权错配。选项：A) 买入时换算为 qfq 口径存储（含存量迁移）；B) 维持现状并在 UI 标注「除权后止损/盈亏为复权口径」。倾向 A，但影响存量数据。
2. **止损卡两副本以哪份为准**（§4.3）：手工交易页版（含硬止损当日明细）还是看板页版。
3. **MCP 对外暴露策略**（P0-2）：共享密钥的方案细节（SSE 连接层校验 vs 工具入参）与是否保留无鉴权只读工具。
4. **部署形态以哪份为准**（§9-6）：deploy.sh（/opt + nginx）、main.py 注释（frp 直连）、运维文档（/srv/trend-quant）三处矛盾。
5. **O-1 备份的异机/云盘目的地**：每周一份放哪里（对象存储/另一台机器/本地另一块盘）。
6. **C-24 看板 L2 排序字段**：当前用「子级 priority_l3 的 min 聚合」排序 L2 行，priority_l2 配置实际失效。需确认是有意设计（按最重要子类排）还是误用（应改 priority_l2）。
