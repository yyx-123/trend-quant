# v1 → v2 修订记录

## v2.2 → v3（2026-08-25：修复决策与实施方案，新增第十二节）

作者对全部条目给出决策（明确指示 / 授权「看着改」/ 明确不改三类）；「看着改」条目的修改路径经独立子代理对照实际源码逐条评审并修正后定稿。要点：

- P0-1/P0-2 最终建议：/mcp 前置静态 Bearer token 中间件 + add_trade/open_positions 去密码化（身份来自 token→用户映射，身份通路经 FastMCP Context 已验证 mcp 1.28.1 可行）；DNS rebinding 保护恢复开启，上线顺序先 token 后 DNS 保护。
- 作者明确决策：P0-3 删 metadata 反推 fallback；P1-2 每日 03:00 备份 keep=1（不做异机/演练）；P1-3 内置管理员 yyx（is_admin 列已存在，子代理纠正「需迁移」前提；挂点定 lifespan 而非 init_db）；P1-5 全局东八区；P1-7 按线上真实配置重写 deploy.sh；P1-9/P2-8/P2-14/P2-17 不改。
- 子代理评审并入的关键修正：P2-11 必须先 P2-12 且删除三个现存 close() 调用点、限流锁随状态模块级化；P2-23 foreign_keys 实测生产库零孤儿、挂点 _connect 与 timeout=30 合并为一次改动；P0-3 删 fallback 后空表行为契约（/api/add 与 update 全部 400、类目无种子来源）补种子与文档约定；P2-29 两端点全仓核实零调用方均删除（test_auth_wall 探针同步换）；asset_version 简化实现（启动内容 sha1 + 1s TTL mtime 复查）；P1-2 存量手工备份 pre-auth-wall-20260824.db 不匹配修剪 glob 需单独处置。
- 行动清单变更：#5 改 keep=1、#14 移除 P1-9 部分、#19 移除、#32 移除 DDL 分段/域迁出；新增顺序约束（P2-12→P2-11、Bearer→DNS 保护、P1-15 前置→P1-12/P1-10→P2-27 阶段二）。
- 仍待作者确认：P2-4（附例说明中）、附录 B N1-N5 默认处理、P1-3 密码是否强制重置、覆盖率门槛数字（起步 70% 目标 85%+）。
- 本节仅为方案定稿，尚未进入开发。

## v3 定稿（2026-08-25 同日：开放点全部闭环）

作者对 4 个开放点逐一回复，全部闭环：**P2-4 不改**（确认看板按强弱排序、priority 仅打平决胜即有意设计）；**附录 B N1-N5 全做**（N1 删冗余索引/N2 备查/N3 并入 P2-18/N4 并入 P2-25/N5 改名）；**P1-3 采用默认方案**（yyx 已存在不强制重置密码，仅不存在时以 20160702 创建）；**覆盖率暂不设 fail_under 硬门槛**（尽可能做高，达成后作者再指定数字）；P0-2 调用方式变化作者知悉接受。第十二节为实施方案定稿，**尚未进入开发**。

## v2.1 → v2.2（盲审 round-2 补充收录，纯增量）

盲审代理 round-2 确认「全部条目收录无误、降调均接受、无 P0-P2 级遗漏」；其第二轮定向深挖的 5 条 P3 级微调（N1 冗余索引/读侧缺索引、N2 冷路径全表查询、N3 管理路径重复全表加载、N4 API 测试 pbkdf2 拖慢、N5 误导性变量名）收录为附录 B；M1（P2-4 排序字段）补强证据（priority_l2 实为 L2 排序专用配置，当前用法使其失效）已并入 P2-4 条目。

## 最终状态

- 评审代理 1（事实核查）：round-2 结论「达成一致」（review-1-factcheck-round2.md）
- 评审代理 2（架构）：round-2 结论「达成一致，v2 可作为终版」（review-2-arch-round2.md）
- 盲审代理（独立同范围重审）：round-2 结论「达成一致，审查闭合，无更多可挖掘点」（blind-audit-round2.md）
- 主审查人：三方意见全部处理完毕，无未决分歧。**审查关闭。**

---

## v2 → v2.1（round-2 复审收尾修订）

评审代理 1 复审结论「达成一致」前提下的 3 处非阻塞轻微修订，均已落实：

1. P2-19 注释引用更正：`service.py:174` → `routers/rule_backtest.py:174-175`（注释实际位置）。
2. P1-6 表述精确化：pyproject 实有 tushare 可选组（原文「只有运行时 dependencies」不准）；mcp 在运行时依赖中，P0-4 收集错误源于本地 venv 未安装它，与 dev 组缺失无因果——两处措辞已更正。
3. 评审代理 1 收回 round-1 对 P0-1「显然未亲核」的断言（R-0 时间线与全部可观察证据相容，认可闭环处理）。

评审代理 2 复审结论「达成一致」，3 条非阻塞备注（轮询退避行动行、编号映射、部署演练组合验证）已在行动清单备注中体现或以 CHANGELOG 为准，不要求修改正文。

---

# 以下为 v1 → v2 修订记录

v2 修订输入：评审代理 1（逐条事实核查，review-1-factcheck.md）、评审代理 2（架构/完整性，review-2-arch.md）、独立盲审代理（全程未接触 v1，见 blind-audit-comparison.md）。所有修订点均由主审查人打开代码二次亲核后才采纳。

## 事实性修正

1. **P0-1 删除并改写为 R-0（已闭环）**：`/ __dev_set_session` 端点在 v1 审查时存在于**工作区未提交改动**中（会话起点 git status 显示 `M src/app/routers/auth.py`、`M src/app/main.py`）。审查期间该未提交改动被回滚，当前工作区与全部 git 历史（`git log --all -S` 零匹配）均无此端点。v2 将其改写为「已消失的安全教训 + 闭环确认」，不再作为现役漏洞与行动项。教训（GET 写 cookie + 登录墙豁免 = session fixation）保留。
2. **P1-14 子条修正**：`instruments.html:752` 实际有 `resp.ok` 检查，401 会显示错误文案而非"已加载 0 个标的"（v1 该子条来自扫描代理，张冠李戴）。条目主旨（无统一 401 跳登录）保留。
3. **P1-20 修正**：`nextStartDateForRow` 被活功能 `runBackfillAll`（instruments.html:1302，事件绑定在 1527）调用，从死代码清单剔除；死代码补入 331/770 两处残渣（评审 1 补充）。死 JS 量从"约 80 行"下修为"约 60 行"。
4. **P1-7 机制描述修正**：第一轮循环体内无任何赋值，是纯粹死循环体（副作用仅重复打 error 日志），不存在"赋值被覆盖"。
5. **行号小偏差修订**（评审 1 第五节）：`_date_span` 399→400；`renderStopStats` 304/1128-1159→309/1146；engine if 284-286→283-286；calendar 静默吞 51-52→50-51；esc「7 份」细化为「6 份 function esc + 1 份箭头函数 escHtml（subject_market.html:120），至少三种写法」。
6. **审查基线声明补入**：v1 未声明基线；v2 声明以 2026-08-24 工作区（含 8 个未提交修改 + 1 个未跟踪脚本）为准。

## 分级调整（采纳评审 2，主审查人复核同意）

| 条目 | v1 | v2 | 理由 |
|---|---|---|---|
| 迁移脚本 copy2 备份 | P0 | P2 | 一次性脚本已执行完毕，风险只在重跑时兑现；仍要修 |
| build_sw_tree 死循环体 | P1 | P3（死代码节） | 纯编辑残留，无正确性影响 |
| stop_loss 全表扫元数据 | P1 | P3 | 表仅数百行，微秒级代价；一行修复 |
| 报价缓存键不一致 | P1 | P2 | 潜伏问题，当前调用链安全 |
| subject_market 2s 无限轮询 | P1 | P2 | 单用户系统，innerHTML 子问题并入 XSS 条目 |
| **无定时 DB 备份** | P2 | **P1** | 2.94GB 主库是单人项目唯一不可再生资产，且代码（git bundle 断更一个月）与数据两条备份线同时失效 |
| P0-6 捆绑的 pytest-asyncio 警告 | P0 内 | 并入新条目「dev 依赖缺失」（P1） | 经查 async 测试走 `unittest.IsolatedAsyncioTestCase`（stdlib），实际正常运行；`asyncio_mode` 是纯死配置 |

## 新增条目（主要来自盲审代理，全部亲核属实）

- **P1-新增 A：无用户创建入口**（`db.py:1428` create_user 全仓零调用方，deploy.sh 不建用户不配 .env，全新部署无法登录且无文档记载引导流程）
- **P1-新增 B：Web 日 K 指标在截断窗口上计算**（market_view.py:288-290 先 tail(limit) 再算指标，与 MCP server.py:223-227 已修复的窗口截断 bug 同源；默认 limit=20000 掩盖，传小 limit 即口径漂移）
- **P1-新增 C：`build_intraday_overlay` 自建 DataService 从不 close**（intraday_service.py:204，每次标的页打开/MCP 调用泄漏一个 client；对比 stop_loss.py:82-88 正确 close）
- **P1-新增 D：dev 依赖未声明**（pyproject 无 dev 组：pytest/pytest-cov/ruff 均未声明，阻塞 CI 与新环境验证）
- **P2-新增 E：provider_utils.standardize_ohlcv 缺时间列时伪造当前时间**（provider_utils.py:55，脏数据静默变"今日 K 线"）
- **P2-新增 F：看板 L2 排序错用 priority_l3**（dashboard.py:267-275 与 intraday_service.py:960-966 两处同错，L2 层级语义上应用 priority_l2）
- **P2-新增 G：EOD 成交额加权 vs 盘中简单平均的聚合口径不一致**（dashboard.py:83-112 vs intraday_service.py:842-846）
- **P2-新增 H：盘中聚合全 None 时 mean 产出 NaN/非法 JSON 风险**（intraday_service.py:843）
- **P2-新增 I：dashboard_snapshot 首次读库失败后永久不重试**（dashboard_snapshot.py:52-60，_snapshot_loaded 先置位）
- **P2-新增 J：`_rule_jobs` 的 result_full 常驻内存 30 分钟且无任何读取端点**（rule_backtest.py:176-177）
- **P2-新增 K：get_strategy_config 无缓存逐次查库**（strategy_config.py:45-65）
- **P2-新增 L：模块级单例固化 get_db 的测试替身捕获风险**（rule_backtest.py:20 模块级 service——代码库自己在 main.py:59-63 警告过这个模式）
- **P2-新增 M：cwd 相对路径依赖**（main.py:14,64、settings.py:45、app_logger.py:8；非项目根启动静默读写错位）
- **P2-新增 N：并发专项结论**（正面为主 + `_market_symbols_cache` 跨进程不失效 db.py:57-62 + 调度器三类任务并发 × 无显式 busy_timeout）
- **P2-新增 O：chinese_calendar 年度边界运维仪式**（calendar.py:60-85；2027 起假日判为交易日，叠加启动补偿每次重启触发无效 force 补跑；升级仪式仅在 docstring）
- **P1-13 证据补强**：补 `core/jobs.py:63`、`main.py:167,175`、`routers/rule_backtest.py:72-73` 的裸 `date.today()`（评审 2 指出 v1 恰好漏了自己定级 H1 的模块）
- 死代码补充：benchmarks.py 大片零调用 API、分钟 K 死链路、fetch_trading_calendar 恒空、DIVIDEND_CHECK_BARS 残留、YAML fallback（config/rule_strategies 不存在）、plan:starter 无配置意义、src/engine/ 也是 pycache 残骸
- 测试补充：CLAUDE.md 自述 2 个长期失败未打 xfail；auth 墙测试未覆盖 /mcp 豁免；test_subject_market.py:8 依赖路由层偶然再导出
- 文档补充：CLAUDE.md:30 同样写 5 工具；main.py:304-305 注释「MCP 工具调用自带 username/password 逐次鉴权」与实现矛盾（7 个工具只有 2 个验密码）；server.py:88 注释「shared RevisionCache」与新实例实现矛盾
- 运维补充：instrument_jobs 硬编码回填起点 2020-01-01 与 instrument_metadata.start_date 字段建而未用；三个 JobManager 的中断任务无 job_runs/interrupted 标记（批量回测有 mark_interrupted 兜底，它们没有）；tushare 镜像站私有属性 hack 的供应链脆弱性
- 安全补充：登录计时侧信道（trade_records.py:66，P3）；登录墙 `/api` 无尾斜杠与 startswith 前缀匹配的边缘行为（main.py:331,339）；4xx 完全不可见与爆破监测的关系

## 方案修正（采纳评审 2）

- `.env.example` 行动补 `.gitignore` 增加 `!.env.example` 例外（`git check-ignore` 实测会被 `.env.*` 忽略）
- 定时备份改 keep=3 日备 + 异机周备（2.94GB×7≈20GB 磁盘账）
- qfq 增量物化补边界条件「新 bar 日期 > max(ex_date) 才走 append，回补历史必须全量重写」，成本改估半天
- 前端公共 JS 抽取补两个前置：asset_version 扩展为跟踪所有静态资源；先裁决止损卡/postJson 的口径以哪份为准
- Database 不全拆：只做 `_init_tables` DDL 分段 + users/sessions 域随 token 哈希改造顺手迁移
- EOD/盘中聚合合并改两阶段：阶段一抽 5 个纯函数，阶段二以双实现一致性测试为守门员，允许停在阶段一
- CI 明确单矩阵；前置依赖 dev 依赖条目

## 结构调整

- 合并 P1-13 与 P2-6（同一时区问题的条目级与项目级重复陈述）
- 消除 P1-17 与 P1-18 关于 `except AttributeError` 的重复
- 「真实 Bug 与潜伏缺陷」一节中的纯性能条目（P1-9/P1-10）移归性能节，分级与第六节标准对齐
- 行动清单按评审 2 的 4.2 表重排为 22+ 条，补全 v1 遗漏的 P1-3/P1-4/P1-6 等行动行，测试兜底排在所有重构之前

## 未采纳/保留分歧

- 评审 1 称 P0-1「显然未亲核」——不属实：v1 审查时端点确实存在于工作区（主审查人 Read 全文亲见 auth.py 82 行版本），系审查期间工作区回滚所致。v2 已在 R-0 中如实记录时间线与证据，并将「审查基线声明」补入报告头部防止此类争议。
- 盲审 M1（priority_l3 排序）标为「疑似复制粘贴错误」——主审查人复核后认为语义存疑但非显然错误（L2 的 priority_l3 是其子级 min 值，排序确定但字段意图不符），报告按「疑似字段误用，建议确认意图」表述。

## v3 → 开发完成（2026-08-25：第十二节方案全量落地）

按第十二节实施方案完成全部条目的开发。要点与验收证据：

- **P0 安全**：/mcp 前置 Bearer token 中间件（`app/mcp_auth.py`，失败关闭、401 可读 detail、hmac 常量时间比对）+ add_trade/open_positions 去密码化（身份经 FastMCP Context 来自 token→用户映射）；DNS rebinding 保护按 `TREND_MCP_ALLOWED_HOSTS` 配置开启；两个 skill（daily-trade-report / trend-quant-mcp）同步为 token 制。
- **P1 全部**：登录限流+审计、每日 03:00 备份 keep=1（真实 2.7GB 库跑通验证）、内置管理员 yyx（lifespan ensure，不动已有密码）、Web 日K 指标全历史计算（窗口不变性测试锁定）、时区统一 market_now、deploy.sh 按线上现状重写、3 处 XSS+1 处双重转义修复、CSRF 头校验+logout 改 POST、批量回测标的快照冻结、DataService 泄漏修复。
- **P1-14/P1-15**：safe_float/category_path/_number/SH↔SS/_date_span/看板共用件收口（阶段一完成，阶段二按方案允许未做）；app-common.js（esc/postJson/止损卡 manual_trade 口径/排序/侧栏/日期/金额）+ fetch 全局拦截（CSRF 头注入 + 统一 401 跳登录）；asset_version 扩展为全静态资源 sha1+mtime 复查。
- **P2 全部**：盘中聚合成交额加权+NaN 防护、死代码全清（分钟K链路/benchmarks 死API/plan 校验/死 CSS 106 项/死 JS）、RevisionCache 模块级单例、DataService 进程级单例（限流状态模块级、close 调用点全清）、MarketStore/路由 service 去固化、env 收口 core/env.py + core/paths.py 锚定、引擎 itertuples、revision 免 COUNT(*)、指标只算一遍、stop_loss 预加载 df、批量 counts 每 20 格 flush、result_full 删除、慢请求中间件、失败哨兵、busy_timeout=30+foreign_keys=ON、N1 迁移式删 3 冗余索引、N3/N4/N5、trading-status 与 /api/auth/me 死端点删除。
- **测试**：附录 A 全量补测（jobs/scheduler/lifespan/MCP 7 工具/service/provider/db/RevisionCache/冷却期特判/engine 边界）；P2-25 测试卫生（弱断言/全局状态泄漏/重复测试合并/根目录 marker/slow/pbkdf2 提速）；857 passed 连跑一致；整体覆盖率 89%、咽喉模块全部 ≥85%。
- **CI**：GitHub Actions 单矩阵（pytest+ruff+死 CSS 检查+前端 JS 加载检查），无 fail_under（按作者决策）。
- **E2E**：独立实例全流程验证（登录墙/登录/7 页面渲染/CSRF/MCP 官方客户端 7 工具含 token 身份与缓存命中）；浏览器 GUI 验证发现并修复 3 个前端运行时残留 bug（stopPill 孤立片段、HEAT_COLOR_DIMS 误删、止损 tip 别名缺失），新增 scripts/check_frontend_js.py 防复发。
- **数据库变更**：见 db-changes-2026-08-25.md（仅删 3 个冗余索引，启动自动迁移，线上零手工 SQL）。
- **两轮独立代码审查**（方案符合性 + 新问题排查）均已终审通过。
