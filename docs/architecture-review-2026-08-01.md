# Trend Quant 架构与数据准确性审查报告

- **日期**：2026-08-01
- **审查方式**：双盲交叉审查 —— 两轮独立全仓审查（含 4 个并行探索代理分工扫描）+ 所有争议结论逐行人工核实（grep / ruff / 隔离实验 / 实测测试套件）。未改动任何代码。
- **测试实测**：532 项收集（本地缺 `mcp` 包导致 3 项无法收集），524 通过 / 8 失败；失败不可复现（两次运行 5~8 个不等），详见 §5.1。

---

## 1. 总体结论

架构骨架方向正确：**raw 唯一真源 + 除权因子本地物化等比 qfq**（`core/adjustment.py`）、**指标/趋势公式单一实现 + 公式版本号**、**缓存只是加速器、未命中实时回算**、**回测引擎记忆化改造有 bit 级 golden 锁定** —— 这些都是对的，不要动。

但"除权"这个系统最大的不变量，目前只被 `core/adjustment.py` 一个文件守住。它的**下游消费方**（手工交易/止损用错价格空间）、**读端缓存**（失效机制不感知价格内容变化）、**上游生产链路**（16:30 管道无端到端测试、因子写入非原子）都没有被同等级别的纪律保护。测试套件本身也不可信（顺序污染 + 平台脆弱 + 环境依赖）。

**三个系统性缺口**：
1. 复权价格的消费方口径混乱（手工交易/止损 qfq×raw 混用）；
2. 缓存失效与看板 revision 不含价格内容指纹，因子变化后可能静默读旧数据；
3. 复权重做后的核心链路零真实测试，golden 体系以自证为主。

---

## 2. 当前整体架构

```
web/templates + static       服务端渲染 + 内联 JS（~11k 行，无构建、无测试）
app/routers (7)              HTTP 编排，部分含业务逻辑（§4.4）
trend_mcp/server.py          MCP 薄适配（6 工具，与 HTTP 共享服务函数 ✓）
services/                    dashboard / stop_loss / manual_trade / trade_records /
                             indicator_builder(预计算) / instrument_jobs(后台任务)
rule_backtest/               engine(状态机) + condition_engine + value_resolver(记忆化)
                             + sizing/ 插件 + batch_service + metrics
data/                        service(行情门面，TickFlow 单源) / storage/db.py(1522 行
                             全量 DAO) / market_store / indicator_store(缓存门面) /
                             intraday_service(盘中合成) / provider_tickflow
core/                        adjustment / indicators / trend / calendar / jobs / scheduler
audit/                       日志
```

**数据流**：TickFlow → `market_data_raw`（唯一真源，append-only）→ 除权因子 diff → `compute_qfq` 本地物化 `market_data_qfq` → `indicator_daily`/`trend_daily` 预计算缓存 → 读取经 `indicator_store`（缓存优先 + 实时回退）。实时行永不落库；回测/止损只用 EOD 数据。

**依赖方向**（声明）：`app / trend_mcp → services → core / data`，实际存在违规（§4.3）。

---

## 3. 问题清单（按严重度，全部经逐行验证）

### P0 —— 直接污染数据或结果

#### P0-1 手工交易/止损全链路 qfq 与 raw 价格口径混用
用户录入的买卖价是真实成交价（raw 口径，`db.py:969` 原样落库），但 `services/stop_loss.py:128`、`services/manual_trade.py:67`、`services/trade_records.py:154,244` 全部默认读 qfq 行情。买入日之后发生分红除权时（qfq 历史被整体缩小，raw 成本价不变）：

| 位置 | 错误表现 |
|---|---|
| `stop_loss.py:184` | raw 买入价 vs qfq 历史区间 → **合法买入被误拒**（`create_trade` 复用此校验） |
| `manual_trade.py:95-96` | 浮盈忽略分红现金，**系统性失真**（红利类 ETF 年年中招） |
| `stop_loss.py:207` | 硬止损 = raw买入价 − 1.5×ATR(qfq) —— **两个量纲相减** |
| `stop_loss.py:196-224` / `manual_trade.py:104-118` | 吊灯/棘轮的计算与触发判断混用两个价格空间 |
| `trade_records.py:159` | 历史清仓日期若早于后续除权日，合法卖价被误拒 |

三个对应测试文件（`test_stop_loss.py` / `test_manual_trade_service.py` / `test_trade_records_service.py`）**零除权场景覆盖**。

#### P0-2 回测执行模型的系统性乐观偏差（三点）
1. **收盘信号、收盘成交（前视偏差）**：入场条件用当日收盘及当日指标（`engine.py:134-144`），成交价为当日收盘×(1+滑点)（`engine.py:147,419`）。实盘看到收盘信号时最早次日开盘才能成交。`models.py:23-24` 有 `signal_timing/fill_timing` 字段但**从未实现**（死字段，且有误导性）。
2. **无涨跌停/停牌约束**：`engine.py:391-447` 全链路无校验，涨停日仍按 close×1.002 买入、`volume=0` 照样成交。系统已支持 stock 标的（asset_type + 印花税），影响真实存在。
3. **止损触发按止损价成交**：`_resolve_sell_reference_price`（`engine.py:494-518`）在收盘跌破止损价时按止损价成交；跳空低开日真实成交远低于此 —— 系统性低估止损损失。

**叠加效应：现存所有回测的年化/回撤数字都是上界。**

#### P0-3 缓存失效不感知价格内容变化
`_cache_fresh`（`indicator_store.py:160-170`）只比日期与版本号；除权后 qfq 全史价格重写但日期范围不变 → 检查全通过，旧指标继续被读。dashboard 的 `get_market_dashboard_revision`（`db.py:680-693`）同理只看 `MAX(time)+COUNT+metadata.updated_at`。恢复完全依赖编排路径（`run_post_update_pipeline`），而 `detect_adjustment_breaks` 吞异常返回 `[]`（`indicator_builder.py:144-146`）—— **任何一步失败/跳过，读端静默返回旧数据，无告警**。盘中递推（`intraday_service.py:512` 及未接入的 `indicator_store.py:209`）同样无缓存新鲜度校验。

#### P0-4（已验证 bug）`instruments.py:90` NameError
`_category_options()` 回退分支调用未导入的 `_config_items()`（定义在 `services/instrument_admin.py:62`）。`instrument_categories` 表为空时 `/api/categories` 必 500。生产上表非空所以未炸；`replace_instrument_categories`（`db.py:731`）会先 DELETE 全表，清空即触发。

---

### P1 —— 数据管线健壮性

| # | 问题 | 位置 | 说明 |
|---|---|---|---|
| P1-1 | `replace_ex_factors` 非原子 | `db.py:897-901` | DELETE 与 INSERT 分属两个事务；中间崩溃 → 因子表空 → 下次物化出"qfq=raw"错数据，且自愈逻辑（`service.py:327`）检测不到。对比同文件 `replace_market_data` 是单事务 |
| P1-2 | 因子同步失败时静默用旧因子物化 | `service.py:310-318` | 除权日当天造出假性跳空并污染指标缓存，job_runs 记 success |
| P1-3 | 因子快照全量信任 | `service.py:209-225` | vendor 返回不完整快照（新因子延迟/截断）会覆盖删除本地正确因子 |
| P1-4 | raw 中间缺口永不修复 | `service.py:288-292` | 日更只 append 尾部，历史缺口一旦形成永远跳过 |
| P1-5 | 止损公式双实现无等价性护栏 | `services/stop_loss.py:190-224` vs `rule_backtest/state_values.py:26-107` | 回测里的止损与盘中提示的止损随时可能分叉（棘轮一个 cummax 回放、一个 running max） |
| P1-6 | EOD 与盘中看板聚合口径分叉 | `dashboard.py:71-100` vs `intraday_service.py:699-716` | EOD 成交额加权、盘中简单平均；`trend_ma5` 缺失时盘中兜底 avg_trend、EOD 返回 None —— 同一类目两个入口两个数 |
| P1-7 | 趋势公式第二份完整实现 | `intraday_service.py:164-286` | `compute_intraday_trend_cached` 内联重写全套趋势公式（生产链路在用），与 `core/trend.py` 平行演化；RSI 同理 ×3 份（`indicator_store.py:51,110` 内联拷贝） |
| P1-8 | `update_pool_daily` 全败也记 `partial` | `service.py:788` | 状态栏永远显示"已完成" |
| P1-9 | `DataService` 多实例不 close + 实例级限速 | `stop_loss.py:66`、`intraday_service.py:127`、`trend_mcp/server.py:175`、`provider_tickflow.py:86-97` | HTTP 客户端泄漏；并发突破 vendor 限额 → 429 → 静默回退 EOD |
| P1-10 | 批量回测硬编码 `sizer=None` | `batch_service.py:369` | 批量与单次（可带 sizer）结果不可比 |

---

### P2 —— 口径与边界

1. 趋势相位扫描 `range(n-1, 3, -1)`（`trend.py:329,352`）使索引 0-3 永不参与，贯穿全窗口的趋势起点算错；
2. 手工交易可录入未来日期（`create_trade` 无守卫），此后持仓永久报错；
3. 除权因子日期取 UTC 日期（`provider_tickflow.py:291`）vs K 线 Asia/Shanghai（`:122`）—— 注释称已与 vendor 核对但无测试锁定，vendor 变更即静默错一天；
4. RSI docstring 声称 "Wilder smoothing"，实际无 SMA(14) seed —— 与通达信 `SMA(X,N,1)` 口径**一致**（另一份审查报告此处失实，特此纠正），仅与 TA-Lib/TradingView 经典 Wilder 早期有差且收敛。属文档注释问题；
5. debug 自动开关（`engine.py:293-300`，<31 天走 legacy 路径）使结果在脏数据下依赖区间长度；memoized 路径 `safe_float` 不拦 inf（`rule_backtest/indicators.py:10`）—— DB 已拦非正价格，现实可达性低；
6. 时区假设：`calendar.py` 全用 naive `datetime.now()`，`app.yaml` 的 timezone 只传给 apscheduler，服务器非东八区即全盘错位；
7. users 表明文密码（`db.py:231`）+ 每请求明文比对（`trade_records.py:54-60`）—— 内部口径，但 MCP 已对外暴露，建议至少哈希。

---

## 4. 架构不合理、重复实现与死代码

### 4.1 `db.py` 1522 行上帝类 + 全局单例
12 个表领域塞在一个 `Database` 类；`_db_instance` 全局单例（`db.py:15`）是测试顺序污染的根源之一（§5.1）。

### 4.2 重复实现（高价值清单）

| 内容 | 位置 | 风险 |
|---|---|---|
| 趋势公式 ×2 | `core/trend.py:59` vs `intraday_service.py:164-286` | 盘中/EOD 分叉（P1-7） |
| RSI Wilder ×3 | `core/indicators.py:71` / `indicator_store.py:51,110` | 参数/边界漂移 |
| ATR 真实波幅 ×4 | `core/indicators.py:43` / `rule_backtest/indicators.py:62` / `indicator_store.py:251` | 同上 |
| 止损公式 ×2 | 见 P1-5 | 回测与实盘提示分叉 |
| 看板聚合 helper ×2 | `dashboard.py:41-68` vs `intraday_service.py:366-415` | 已分叉（P1-6） |
| K 线 MA 周期集已分叉 | `engine.py:617`（无 MA40）vs `market_indicators.py:18,77`（含 MA40） | **已经在漂移的活例** |
| 买卖价区间校验 ×2 | `stop_loss.py:177-188` vs `trade_records.py:113-126` | 低 |
| `safe_float` ×3 / `_num` ×4 / `_category_path` ×5 / 日期工具 ×5 | 多处 | 语义各异 |

### 4.3 分层违规
- `core/jobs.py:17-18` 顶层导入 `data.*`（core→data）；`core/strategy_config.py:51`、`core/display.py:37` 懒导入 `data.*`；
- `data/intraday_service.py:549` 函数内导入 `services.market_indicators`（方向反转）；
- `services/instrument_jobs.py:15,78,341` 服务层抛 `fastapi.HTTPException`；
- 路由含业务逻辑：`instruments.py:83-122,333-391`、`batch_backtest.py:54-105`（与 `batch_service.py:95-121` 重复）、`market_view.py:133-203`；
- `src/__init__.py:1` `from app.main import app` —— import src 即启动整个应用；
- 异常处理四套模式并存（吞掉返回空 / 裸宽 except / 领域异常 / status 字符串），下游靠字符串匹配分支。

### 4.4 死代码
- 生产死、仅测试存活：`compute_intraday_row`/`get_series_with_intraday`（`indicator_store.py:209,298`，P1.4 盘中递推从未接入路由）、分钟 K 链、`fetch_trading_calendar`；
- 完全无调用：`params_hash`、`_ordered_providers`、`jobs_snapshot`、`indicator_cache_symbols`、`clear_indicator_caches`、`signal_timing/fill_timing` 字段等 13 项；
- 陈旧 `__pycache__`：7 个无源码脚本 + 5 个已删包（`src/backtest|engine|notify|portfolio|strategy`）；
- 单 provider 脚手架残留（`IDataProvider`、`provider_priority` 参数、不可达 getattr 回退）；18 个 ruff 确认的未用导入；
- `scripts/migrate_raw_qfq.py` 一次性迁移脚本无引用，建议归档。

### 4.5 前端无人区
4273 行单文件 CSS、1981 行 `market_view.html` 内联 JS、无构建、无 lint、无测试。后端字段改名靠人肉对齐，是当前变更成本最高、回归风险最不可控的一层。

---

## 5. 测试覆盖现状与风险

### 5.1 实测状态：套件现在不是绿的，且结果不可复现
- **收集中断**：`tests/unit/test_mcp_symbol_detail.py` 因环境缺 `mcp` 包中断整个 pytest 运行（应 `pytest.importorskip`）；
- **2 个 Windows 文件锁失败**：`test_instruments_bulk_backfill.py` teardown 删临时目录时 WinError 32（后台线程持有 SQLite 连接）—— Linux 绿、Windows 必红；
- **4~6 个顺序依赖污染失败**：`test_rule_backtest_engine.py` / `test_rule_backtest_progress.py` / `test_instruments_update_api.py` 的失败用例**单独跑全绿、全量跑红**（隔离实验证实）。两次全量运行失败数不同（5 vs 8）—— **红灯无法区分真回归与污染，长期必然导致红灯被习惯性忽视**。头号嫌疑：`db.py:15` 全局单例 + monkeypatch 未完全复原。

### 5.2 覆盖地图：没测的恰是最危险的
**测试质量三层**：
- 真 golden（独立手算钉死）：`test_adjustment.py`、`test_batch_golden.py`（15 位有效数字）、`test_stop_loss.py`、`test_indicators.py` —— 好测试；
- parity/金主（冻结旧实现）：`test_core_indicators.py`、`test_core_trend.py`、`test_p13_memoized_golden.py` —— 防漂移一流，但**冻结时刻若就有 bug 会奉为标准**；EMA/RSI/MACD 首条播种无手算小序列 golden；
- 形状测试（数学全错也能过）：`test_market_view.py` 大部分、provider 层、多数 API 测试。

**零覆盖/高风险盲区**（按危险度）：
1. **16:30 主管道端到端**：raw append → 因子 diff → `rematerialize_qfq` 真库重写 → 指标重建，只在 FakeDataService 下测过编排；`core/jobs.py`、`core/scheduler.py` 零覆盖 —— **上次复权事故走的就是这条链**；
2. **除权场景下游一致性**：无任何"除权日后回测/止损/看板/手工交易读到正确 qfq"的测试（P0-1 在真空中存活的原因）；
3. **并发**：WAL 只有 pragma 断言，无双写者测试；`database is locked` 路径无人管；
4. `compute_qfq` 无真实 vendor 因子 golden（测试因子全是 2.0/1.5 合成整数，没有 1.0314 类真实分红因子；停牌缺口、同日重复因子、NaN raw 价未覆盖）；
5. `services/dashboard.py` 的 `RevisionCache` 零测试；`trend_mcp/server.py` 6 个工具只测了 1 个；
6. fixture 合成数据无停牌/涨跌停/除权跳空/0 成交量形态；
7. **Makefile 分层失效**：50 个测试文件仅 10 个打标，`-m unit` 实测选中全部 532 条。

---

## 6. 改进方向与路线图

### 第一梯队：数据准确性防线（立即）
1. **修 P0-1 价格口径**：买入/卖出价按因子换算进 qfq 空间（除权日当日边界语义写 golden 钉死），补"除权后浮盈/止损/校验"端到端测试（用 1.0314 类真实形态因子）；
2. **修 P0-3 缓存指纹**：qfq 物化时写价格内容指纹（见 §7 决策 D2），`_cache_fresh` 与 RevisionCache 纳入比对；`detect_adjustment_breaks` 失败显式化；
3. **修 P0-2 执行模型**（范围见 §7 决策 D1）；
4. **修 P0-4** NameError（一行导入）；
5. **16:30 管道真库端到端 golden 测试**：合成含一次除权的行情 → 跑 `update_pool_daily` 全链 → 断言 qfq 逐行 == `compute_qfq(raw, factors)`、缓存 == 实算。作为数据层的合并门禁；
6. **日更产物数据自检**：qfq 连续性（无因子日 |涨跌幅| 超阈值告警）、非正价格计数、缺口检测（对比交易日历）、抽样与 vendor forward 对账，结果写 `job_runs` —— 让数据事故当天报警。

### 第二梯队：管线健壮性（两周）
7. `replace_ex_factors` 事务化（P1-1）；因子同步失败拒绝物化标 degraded（P1-2）；因子快照完整性防御（P1-3）；
8. 止损公式收敛为一份 core 实现 + 双路径等价性测试（P1-5）；EOD/盘中看板聚合统一（P1-6）；趋势公式唯一化（P1-7）；
9. 测试套件可信化：`importorskip("mcp")`、修 Windows teardown、定位修复单例污染、markers 补齐；
10. `DataService` 生命周期与限速治理（P1-9）；`update_pool_daily` 状态语义修正（P1-8）；批量回测支持 sizer（P1-10）。

### 第三梯队：结构性还债（随做随改）
11. `db.py` 按领域拆 Repository + 依赖注入（见 §7 决策 D3）；
12. 路由业务逻辑下沉；异常协议统一（领域异常 + router 映射）；
13. 死代码与陈旧 `__pycache__` 清除（§4.4）；
14. P2 各项口径修正；
15. 前端治理（见 §7 决策 D4）。

---

## 7. 待决策的重大选型

### D1 回测执行模型改多大
- **A. 全量改（推荐）**：实现 `fill_timing: next_open`（死字段位置已留好）+ 涨跌停/停牌约束（默认开）+ 止损成交价保守化 `min(止损价, 当日open)`；存量策略跑双口径对照量化偏差。代价：所有历史回测数字作废，需要重新建立业绩基准认知。
- **B. 只加对照**：保留现状为默认，新增可选严格模式，仅用于敏感性分析。代价小，但日常使用口径仍是上界。
- **C. 只修止损跳空**：最小改动，前视与涨跌停维持现状。

### D2 缓存失效的内容指纹方案
- **A. qfq 表加指纹列（推荐）**：物化时写 `price_hash`（如 sha1(Σclose·权重) 或 COUNT+SUM(close)），`_cache_fresh`/RevisionCache 比对。需 schema 迁移。
- **B. 独立指纹表**：不动行情表 schema，单独一张 `symbol_data_fingerprint`。
- **C. 不改 schema**：只强化编排告警（失败即 job_runs 标红 + 看板 banner）。最便宜但读端仍无兜底。

### D3 `db.py` 拆分的力度
- **A. 全拆 Repository（推荐）**：按领域拆 5~6 个 Repository 共享连接管理器，`get_db` 单例改可注入 —— 根治测试污染。
- **B. 只治单例**：保留上帝类，仅把单例改为可注入/可 reset —— 80% 的测试收益，20% 的成本。
- **C. 维持现状**：只靠 fixture 纪律约束测试。

### D4 前端治理路线
- **A. 维持无构建**：只拆文件 + 加 ESLint 基础检查。
- **B. 引入轻量构建（推荐）**：模板内联 JS 抽到静态文件，按页面拆分，加 lint + 最少的冒烟测试（如 Playwright 关键路径 3 条）。
- **C. 大改框架（Vue/React）**：不推荐现阶段做。

---

## 附录 A：测试实测记录

```
环境：Windows / Python 3.12.10 / pytest 9.1.1
全量：532 collected（tests/unit/test_mcp_symbol_detail.py 因缺 mcp 包收集中断，已排除）
结果：524 passed, 8 failed, 133s
失败定性：
  - 2 × Windows 文件锁（test_instruments_bulk_backfill.py，teardown WinError 32）
  - 6 × 顺序依赖污染（相关文件单独跑全绿，已用隔离实验证实）
另一环境（含 mcp）：535 collected, 530 passed, 5 failed —— 失败数不一致本身即不可复现证据
```

## 附录 B：审查方法说明

本报告合并两轮独立审查：第一轮（本文作者）亲自精读全部数据准确性核心链路并实测测试套件；第二轮（另一 AI）由 3 个并行代理分扫应用层/回测层/测试套件。两份报告的全部分歧点与独家发现均经第三轮逐行人工核实：属实者采信（§3 已合并），失实者纠正并记录（如"RSI 与通达信存在系统性差异"实为一致；"5 个测试失败全是 Windows 文件锁"实为含顺序污染）。
