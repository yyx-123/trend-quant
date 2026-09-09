# 评审意见 1（第二轮）：v2 复审

- 评审人：事实核查型评审（与 round1 同人）
- 日期：2026-08-25
- 核查对象：`code-review-report.md` v2 + `CHANGELOG.md`；对照 round1 的 4 项交付条件、5 项遗漏补充、盲审新增条目抽查
- 核查基线：当前工作区（与 round1 相同状态；auth.py/main.py 仍为已回滚态）

---

## 一、R-0（原 P0-1）表述核验 —— **属实，认可闭环处理**

- v2 R-0 称「当前工作区 auth.py 仅 65 行无此端点」：`wc -l` 实测 **65**（round1 我按 Read 行号误述为 66，v2 的 65 更准）。
- 「main.py 豁免名单无此路径、全仓 grep 与 git log --all -S 均无匹配」：与 round1 我的亲核结果一致。
- 关于时间线（端点曾存在于未提交工作区、审查期间被回滚）：我无法独立证实「当时存在」，但该解释与全部可观察证据相容（git status 中两文件消失、-S 零匹配因从未提交），且 v2 的处理方式——降为「已闭环事项」、移出 P0 与行动清单、保留教训——是事实正确的写法。round1 我在信息不全时断言「显然未亲核」，此点收回；R-0 的表述与当前代码状态完全一致。

## 二、round1 交付条件 4 项 —— **全部落实**

| # | 条件 | 结论 | v2 落点与亲核 |
|---|---|---|---|
| 1 | P0-1 改写 | 落实 | R-0（见上） |
| 2 | P1-14 instruments 子条修正 | 落实 | v2 P1-12 第 146 行：「instruments.html:750-756：有 resp.ok 检查会显示错误文案（v1 误报为『已加载 0 个标的』，已修正），但同样不跳登录」——与代码（752 行 ok 检查、756 行 catch 文案）吻合 |
| 3 | P1-20 死代码清单修正 | 落实 | v2 P2-7：剔除 `nextStartDateForRow` 并明示「是活的（被 runBackfillAll:1302 调用）」；补入 331、770 两处 `querySelectorAll` 残渣（grep 复核 331/770 行均存在）；「约 80 行」下修为「约 60 行」 |
| 4 | 行号偏差修订 | 落实 | 抽查全部到位：`_date_span` data/service.py:400 ✓；`renderStopStats` 309/1146 ✓；engine if 283-286 ✓；calendar 静默吞 50-51 ✓；esc「6 份 function esc + 1 份箭头函数 escHtml（subject_market.html:120）」✓ |

## 三、round1 遗漏补充 5 项 —— **全部吸收**

1. **logout 在豁免名单**：v2 P1-10 已写入「logout 为 GET（auth.py:55）且在登录墙豁免名单内（main.py:312）」✓（行号亲核精确）。
2. **server.py:88 注释矛盾**：v2 P2-10「注释写『shared RevisionCache from services.dashboard』——注释与实现矛盾」✓。
3. **P1-10 影响面限定**：v2 P2-6 已注明「只在非 tight 分支触发」✓。
4. **instruments.html 331/770 残渣**：已补入 P2-7 ✓。
5. **asyncio 风险**：v2 P0-4 表述「async 测试实际走 `unittest.IsolatedAsyncioTestCase`（stdlib）正常运行，`asyncio_mode` 属死配置」——**亲核属实**：tests/test_market_view.py:181 `class MarketViewApiTest(unittest.IsolatedAsyncioTestCase)`（v2 引 182 为首个 async 测试方法行，可接受）；stdlib 基类不依赖 pytest-asyncio，700 passed 实跑佐证其正常运行。v2 结论比我 round1 的担心更精确，认可。asyncio_mode 确为死配置（该配置只对 pytest-asyncio 插件生效）。

## 四、v2 新增条目抽查（12 条，超出要求的 5 条）

| 条目 | 结论 | 亲核证据 |
|---|---|---|
| P1-3 无用户创建入口 | 属实 | `db.py:1428` `def create_user` ✓；src/+scripts/ 全仓 grep 零调用方（仅 tests/api 两处测试夹具使用）✓；README 53-64「运行」章节确无首个用户引导 ✓ |
| P1-4 Web 指标窗口截断 | 属实 | `market_view.py:289-290` 先 `data.tail(limit)`；`:183` `indicators = compute_market_indicators(data, ...)` 确在截断窗口上计算；`:250` `limit: int = Query(DEFAULT_LIMIT, ge=1, ...)` 允许小 limit ✓；`server.py:223-227` 注释对照（MCP 已改全历史）逐字吻合 ✓ |
| P1-13 DataService 泄漏 | 属实 | `intraday_service.py:204` `ds = data_service or DataService()`，函数体（172 起）grep 无 close ✓；两调用方 `market_view.py:319`、`server.py:280` 均不传 data_service ✓；对照 `stop_loss.py:82-88` try/finally close ✓ |
| P2-3 伪造时间戳 | 属实 | `provider_utils.py:55` `pd.Series([datetime.now()] * len(data))` 逐字精确——无 time 列时全行打当前时间 |
| P2-4 L2 排序 priority_l3 | 属实 | `dashboard.py:267-275`（key 含 `item["priority_l3"]`，272 行）与 `intraday_service.py:960-966`（964 行）两份一致 ✓；L2 层级排序调用点 `dashboard.py:376-377`、`intraday_service.py:992-993` 全部精确 ✓ |
| P2-5 看板口径（3 子条） | 属实 | EOD 加权 `_aggregate_daily`（dashboard.py:83-112，94-100 行 amount 加权）vs 盘中简单平均 `np.mean`/`.mean()`（intraday_service.py:841-846）✓；`:736` `"return_1d": None` 初始化 ✓（mean 在 842 行，v2 引 843，1 行偏差）；`dashboard_snapshot.py:52-60` `_snapshot_loaded=True` 先置位、异常后 None 永久缓存逐字精确 ✓ |
| P2-12 模块级单例固化 | 属实 | `routers/rule_backtest.py:20` `service = RuleBacktestService()` ✓；`market_store.py:11-16` `_get_db()` 首次调用永久固化 ✓ |
| P2-19 result_full 常驻 | **结论属实，引用张冠李戴** | `rule_backtest.py:176` `job["result_full"] = result` ✓，全仓无读取方（grep 仅此一处）✓；但注释「future on-demand detail endpoints」实际在 **routers/rule_backtest.py:174-175**，v2 写成 `service.py:174`——rule_backtest/service.py:174 是 batch-drill 快照注释，引用错位（见第五节错误 1） |
| P1-1 新证据（4xx 不可见/共用 authenticate） | 属实 | `main.py:249-265`：253 行 `if exc.status_code >= 500` 才记日志，4xx 刻意不记 ✓；`trade_records.py:58-68` authenticate 被登录与 MCP 共用（61 行注释自述）✓；66 行用户不存在时短路不跑哈希（计时侧信道）✓ |
| P1-9 会话硬化证据 | 属实 | `services/auth.py:8`「活跃用户实际永不过期」逐字 ✓；`:27` `SESSION_TTL = timedelta(days=30)` ✓；`db.py:1467-1473` create_session 明文 INSERT token ✓ |
| P1-5 时区补强（jobs.py:63） | 属实 | `core/jobs.py:63` `today = date.today()` 逐字精确——每日补库主任务的交易日门控确实用宿主机本地日期 |
| P2-13 cwd 依赖 + gitignore 陷阱 | 属实 | `main.py:14` load_dotenv、`:64` `Path("data")`、`settings.py:45`、`app_logger.py:8` 全部精确 ✓；`git check-ignore -v .env.example` 实测命中 `.gitignore:16:.env.*`，v2 的「必须加 `!.env.example` 例外」属实 ✓ |
| P2-25 脆弱导入 | 属实 | `tests/test_subject_market.py:8` `from app.routers.subject_market import build_subject_dashboard_payload` 逐字精确；函数实体确在 services/dashboard.py，路由层为偶然再导出 ✓ |

## 五、v2 新引入的错误（均轻微，不阻塞交付）

1. **P2-19 引用张冠李戴**：「service.py:174 注释自述『future on-demand detail endpoints』」——该注释在 `src/app/routers/rule_backtest.py:174-175`；`src/rule_backtest/service.py:174` 是无关的 batch-drill 注释。建议改为「rule_backtest.py:174-175 注释自述」。
2. **P1-6「pyproject.toml 只有运行时 dependencies」表述不准**：实际存在 `[project.optional-dependencies]`（pyproject.toml:24-29），内含 `tushare` 可选组。核心结论（无 dev 组、pytest/pytest-cov/ruff 未声明）不受影响，但「只有运行时 dependencies」与「无 dev 可选组」应分开表述（前者不成立，后者成立）。
3. **P1-6 因果表述不准**：「P0-4 的 mcp 收集错误只是这个根因的症状之一」——`mcp>=1.0.0` 已在 pyproject **运行时** dependencies（21 行）中声明，按声明完整安装后 P0-4 不会发生；P0-4 的真正根因是本地 venv 未按 pyproject 安装 + 测试文件缺 importorskip 守卫。dev 依赖缺失与 mcp 收集错误之间无因果关系，建议删去该句或改为「本地 venv 与声明依赖不一致正是依赖管理缺失的症状」。
4. 轻微行号：intraday mean 聚合 v2 引 843 实际 842；登录墙 startswith v2 引 main.py:339 实际 338；test_market_view 类声明 181（v2 引 182 为首个 async 方法，可接受）。

## 六、结论

**达成一致。** round1 的 4 项交付条件全部落实、5 项遗漏补充全部吸收且表述更精确（asyncio 一项的修正方向正确，已亲核确认）；R-0 的闭环写法与当前代码状态一致，时间线解释与全部可观察证据相容。v2 新增条目抽查 12 条，11 条完全属实，1 条（P2-19）结论属实但引用错位。第五节列出 3 处轻微表述/引用错误，建议顺手修订但不构成交付阻塞——**v2 可作为后续修复工作的正式基线交付。**
