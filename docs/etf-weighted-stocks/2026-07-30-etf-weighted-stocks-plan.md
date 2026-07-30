# ETF 前十大权重股 —— 数据快照与一键导入标的池 方案 v1

- 日期：2026-07-30
- 状态：待评审
- 前置调研：TickFlow 无成分股/权重接口；akshare 数据源反爬常态化（GitHub Issues 2025-09 ~ 2026-07 持续有封 IP / 滑块 / 断连报告），不作为生产数据源；tushare 为正规授权渠道，采用「每季度临时账号批量快照」模式。

---

## 1. 目标与范围

当一个 ETF 趋势好、热度高时，在标的管理页一键把该 ETF 的**当前前十大权重股**加入标的池（写元数据 → 回补日 K → 重建指标），之后这些股票与手工添加的标的一样被日常维护。

**范围内**：

1. 离线抓取脚本：用 tushare 临时账号把全部在管 ETF 的前十大权重股快照到本地 SQLite（每季度手工跑一次）。
2. 在线功能：预览某 ETF 当前前十大权重股 → 一键导入标的池（已有持仓/已管理的自动跳过）。

**范围外**：

- ETF → 跟踪指数的映射（不需要，见 §3.1）。
- 「某股票是哪些 ETF 的权重股」反向查询（表结构天然支持，但不做 UI/接口）。
- 被踢出前十的股票的清理（**不删除**，继续作为普通标的管理，见 §4.3）。

## 2. 总体架构：在线 / 离线分离

```
┌─ 离线（每季度一次，本地手工跑）──────────────────────────┐
│  scripts/fetch_etf_holdings.py                          │
│    TUSHARE_TOKEN=<临时账号> 环境变量注入                  │
│    tushare fund_portfolio → 每只 ETF Top10 → SQLite     │
│    断点续传 / 幂等 upsert / 限流                         │
└──────────────────────┬──────────────────────────────────┘
                       ▼
        SQLite 表 etf_constituents（快照，含历史期次）
                       ▼
┌─ 在线（应用运行时，永远不访问 tushare）─────────────────┐
│  GET  /instruments/api/etf-constituents/{etf}  预览      │
│  POST /instruments/api/etf-constituents/import 一键导入  │
│    → 复用 _build_new_instrument_record /               │
│      backfill_daily_histories / rebuild_after_backfill │
└─────────────────────────────────────────────────────────┘
```

核心原则：**应用运行时对 tushare 零依赖**。tushare 账号被封、过期、停用都不影响任何在线功能；快照表带期次（`period`），页面上可展示数据新鲜度。

## 3. 数据源设计

### 3.1 接口选择：`fund_portfolio`（不需要指数映射）

| 候选 | 结论 |
|---|---|
| `fund_portfolio`（公募基金持仓，5000 积分，季度更新） | ✅ **选用**。直接以基金代码（`510300.SH`）查询，返回持仓股票、市值、占比，**天然就是「ETF → 前十大权重股」，无需任何指数映射**。季报披露口径即为前十大重仓股。 |
| `index_weight`（指数成分权重，2000 积分，月度更新） | ❌ 弃用。需要额外维护「ETF → 跟踪指数」映射表（几百只 ETF 全靠人工整理，且同指数多 ETF 时还要去重），收益只是数据频率从季度变月度——对「选权重股进池」场景没有实际价值。 |

`fund_portfolio` 关键字段（tushare 文档 doc_id=121）：

- 入参：`ts_code`（基金代码）、`period`（季度末，如 `20260331`），二选一必填。
- 出参：`symbol`（股票代码）、`mkv`（持股市值）、`stk_mkv_ratio`（占股票市值比，**以此排序取 Top10**）、`stk_float_ratio`、`ann_date`、`end_date`。
- 限频：5000 积分每分钟 200 次。448 只在管 ETF × 每季度 1~2 次调用 ≈ **10 分钟内可全量完成**，临时账号 1-3 天窗口绰绰有余。

### 3.2 期次（period）确定与披露滞后

季报披露有滞后（季度结束后 15 个工作日内）。脚本按以下策略确定目标期次：

1. 按当前日期推算最近一个已结束的季度末 `Q`（0331 / 0630 / 0930 / 1231）。
2. 若今天 < `Q + 20 个自然日`，目标期次取上一个季度（给披露留窗口）。
3. 单只 ETF 查询目标期次返回为空时（新成立 ETF、披露延迟），**自动回退到再上一季度重试一次**，并在结果中记录实际使用的期次。
4. 仍为空（货币基金、债券 ETF、QDII 等无 A 股持仓）→ 标记为 `no_data`，跳过，不算失败。

### 3.3 代码格式转换

| 方向 | 规则 | 复用 |
|---|---|---|
| 项目 → tushare | `510300.SS` → `510300.SH`（tushare 上交所后缀为 `.SH`） | 与 `provider_tickflow.py:46-58` 同一套 `.SS`↔`.SH` 转换逻辑 |
| tushare → 项目 | `600519.SH` → `600519.SS`；`000001.SZ` 不变 | 同上，再走 `core.symbols.normalize_symbol` 归一化 |

**过滤规则**：仅保留上交所（`.SH`/`.SS`）与深交所（`.SZ`）A 股。QDII 持仓的港股/美股、北交所（`.BJ`，TickFlow Starter 不覆盖）直接丢弃并记日志。

### 3.4 临时账号使用方式

- token 通过**环境变量** `TUSHARE_TOKEN` 传入脚本，不写入 `config/app.yaml`、不入库、不进 git。
- 调用方式即 tushare 标准用法：`ts.pro_api(token)` → `pro.fund_portfolio(...)`。
- 脚本内置限流（默认每次调用间隔 0.4s，约 150 次/分钟，低于 5000 积分的 200 次/分钟上限），避免触发限频。
- 灰产账号窗口期内被封的应对：**断点续传**（§5.3），大不了再买一个接着跑，已落库数据不丢。

## 4. 数据模型

### 4.1 新表 `etf_constituents`

加进 `Database._init_tables()`（`src/data/storage/db.py:56`），与现有建表方式一致（`CREATE TABLE IF NOT EXISTS`，无需迁移逻辑）：

```sql
CREATE TABLE IF NOT EXISTS etf_constituents (
    etf_symbol   TEXT NOT NULL,          -- 项目内格式，如 510300.SS
    stock_symbol TEXT NOT NULL,          -- 项目内格式，如 600519.SS
    stock_name   TEXT NOT NULL DEFAULT '',
    weight       REAL,                   -- stk_mkv_ratio，占股票市值比（%），用于排序展示
    rank         INTEGER NOT NULL,       -- 1..10
    period       TEXT NOT NULL,          -- 报告期，YYYYMMDD，如 20260331
    ann_date     TEXT,                   -- 公告日
    is_current   INTEGER NOT NULL DEFAULT 1,  -- 该 ETF 最新一次快照=1，历史=0
    source       TEXT NOT NULL DEFAULT 'tushare_fund_portfolio',
    fetched_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (etf_symbol, stock_symbol, period)
);
CREATE INDEX IF NOT EXISTS idx_etf_constituents_current
    ON etf_constituents(etf_symbol, is_current, rank);
```

对应读写方法（`db.py` 新增，风格对齐 `save_instrument_metadata`）：

- `save_etf_constituents(etf_symbol, rows, period)`：**单事务**内先把该 ETF 全部行 `is_current=0`，再 upsert 本期行（`is_current=1`）——保证「查询当前前十」永远只命中一个期次。
- `list_current_etf_constituents(etf_symbol)` → `is_current=1 ORDER BY rank`。
- `list_etf_constituent_periods()` → 每只 ETF 的最新期次与抓取时间（新鲜度展示用）。
- `has_etf_constituents_for_period(etf_symbol, period)` → 断点续传判断用。

### 4.2 不入库的东西

- tushare token（环境变量，§3.4）。
- 抓取进度：脚本内根据 `has_etf_constituents_for_period` 自行推断断点，不需要单独的 checkpoint 表/文件；一次运行的汇总走已有的 `job_runs`（`record_job_run_safely`，job_type=`etf_constituents_fetch`）。

### 4.3 权重股变更的语义（重要）

某次季度更新后：

- **新进入前十的股票**：本期新行，`is_current=1`；导入功能会把它纳入。✅
- **被踢出前十的股票**：该股票在本期没有新行，其旧期次行随整只 ETF 一起被置 `is_current=0`。**不做任何删除**——
  - `instrument_metadata` 中已导入的股票继续作为普通标的管理（行情回补、指标、看板照常）；
  - 只是「获取该 ETF 当前前十大权重股」时不再出现它。
- 若该股票未来重回前十，新期次插入新行即可（主键含 `period`，无冲突）。

## 5. 离线抓取脚本 `scripts/fetch_etf_holdings.py`

### 5.1 流程

```
1. 读 instrument_metadata 中 asset_type='etf' AND enabled=1 的全部标的（当前 448 只）
2. 推算目标期次 Q（§3.2），--period 可手工覆盖
3. ts.pro_api(os.environ["TUSHARE_TOKEN"])
4. 对每只 ETF（已存在 Q 期次数据且未指定 --force 则跳过 → 断点续传）：
   a. fund_portfolio(ts_code=..., period=Q)，空则回退上一季度重试
   b. 过滤 SH/SZ A 股，按 stk_mkv_ratio 降序取前 10
   c. db.save_etf_constituents(...) 单事务落库
   d. sleep 0.4s（限流）
   e. 单只失败仅记录，继续下一只（网络错误连续 ≥5 次则中止，提示账号/网络问题）
5. 汇总输出：成功 / 回退期次 / no_data / 失败 各多少只；record_job_run_safely 落 job_runs
```

### 5.2 命令行参数

| 参数 | 说明 |
|---|---|
| `--period YYYYMMDD` | 手工指定报告期，默认自动推算 |
| `--force` | 忽略已有期次数据，全量重抓 |
| `--symbols 510300.SS,159915.SZ` | 只抓指定 ETF（调试用） |
| `--dry-run` | 只打印不入库（验证账号可用性：先跑 `--symbols` 指定 1 只 + `--dry-run`） |
| `--interval 0.4` | 调用间隔秒数 |

### 5.3 断点续传

因为每期数据按 `(etf_symbol, period)` 幂等 upsert，且跳过逻辑基于落库结果本身：脚本在任何时刻中断（账号被封、网络断、Ctrl-C），重新执行同一条命令即可从断点继续，无重复扣额度之虞（重抓同一期次也只是覆盖写）。

### 5.4 依赖

`tushare` 仅被该脚本 import，应用代码不依赖。`requirements.txt` 中加入并注释说明「仅季度快照脚本需要」；脚本顶部做 `ImportError` 友好提示（`pip install tushare`）。

## 6. 在线功能：预览与一键导入

### 6.1 新增接口（挂在 `src/app/routers/instruments.py`）

| 接口 | 说明 |
|---|---|
| `GET /instruments/api/etf-constituents/{etf_symbol}` | 预览：当前前十（rank/代码/名称/权重/期次/公告日），每行带 `already_managed` 标记与数据新鲜度（期次距今月数）。ETF 无快照数据时返回明确提示（"请先运行季度快照脚本"）。 |
| `POST /instruments/api/etf-constituents/import` | 一键导入，请求体 `{etf_symbol, category_l1?, category_l2?, category_l3?, end_date?, adjust?}`，类目缺省用 §6.3 的默认类目。返回 job 快照。 |
| `GET /instruments/api/etf-constituents/import/status` | 轮询导入任务进度（对齐现有 `/api/add/status` 模式）。 |

### 6.2 导入任务 `EtfConstituentImportJobManager`（`src/services/instrument_jobs.py` 新增）

仿照 `InstrumentAddJobManager` / `BulkBackfillJobManager` 的锁 + 线程 + snapshot 模式，job_type=`etf_constituent_import`：

```
1. 读 list_current_etf_constituents(etf_symbol)，空 → 400
2. 逐个股票：
   - 已在管理（_known_managed_symbols）→ 标记 skipped，跳过
   - 否则 _build_new_instrument_record({symbol, name=stock_name, 类目...})
     → _append_instrument_config，source="etf_constituent"
3. 对全部新增股票一次性调 data_service.backfill_daily_histories
   （items=[{symbol, start_date: 2020-01-01}...]，batch_size=100 —— 10 只一次请求）
4. rebuild_after_backfill(成功更新的股票)
5. 汇总：added / skipped / failed，落 job_runs
```

关键复用点（均为现有代码，无需改动）：

- `_build_new_instrument_record`（`instrument_admin.py:120`）：自动判 `asset_type=stock`（类目 l1=="股票"）、自动推 priority、`risk_budget_pct=0.01`、`stop_atr_mul=1.5`。
- `backfill_daily_histories`（`instrument_jobs.py:187` 同款批量通道）：TickFlow 批量日 K 100 标的/请求。
- `rebuild_after_backfill`：增量重建指标与趋势缓存。

### 6.3 默认类目

新增标的必须有一二三级类目（`_build_new_instrument_record` 强制校验）。方案：

- 预置类目路径 `股票-ETF权重股-综合`，随功能上线在 `instrument_categories` 中插入（脚本或应用启动时 `INSERT OR IGNORE`，priority 排在「股票」类目末尾）。
- 导入接口允许调用方覆盖类目；UI 上高级选项里可改，默认就用预置类目。导入后用户可在标的管理页正常改类目。

### 6.4 UI（`web/templates/instruments.html`）

- 在管标的中 `asset_type='etf'` 的行操作区增加「权重股」按钮 → 弹窗：
  - 顶部：数据期次 + 公告日 + 新鲜度提示（期次距今 >4 个月显示黄色提醒"数据可能过期，建议重新运行季度快照"）。
  - 表格：rank / 代码 / 名称 / 权重(%) / 状态（已管理 / 待导入）。
  - 底部：「导入全部未管理标的」按钮 + 类目高级选项 + 进度条（轮询 import/status）。
- 已管理的股票行禁用勾选；导入完成后刷新标的列表。

## 7. 边界情况

| 情况 | 处理 |
|---|---|
| ETF 无快照（新加的 ETF 没跑过脚本） | 预览接口返回空 + 提示先跑脚本；也可本地临时跑 `scripts/fetch_etf_holdings.py --symbols <该ETF>` 即时补 |
| 债券/货币/QDII ETF | 快照期即为 `no_data`，UI 按钮置灰并提示"该 ETF 无 A 股持仓快照" |
| 股票已在管理 | 导入时 skipped，不覆盖用户已有的类目/参数配置 |
| 权重股含 ST / 停牌股 | 照常导入（行情回补会反映真实状态），是否交易由策略层决定 |
| TickFlow 回补失败 | 走 `backfill_daily_histories` 自带的多轮重试（max_attempts=4），失败标的记入汇总 |
| 同一 ETF 重复点导入 | 全部 skipped，幂等无副作用 |
| 快照期次与当前差异大 | UI 新鲜度黄条提示，不强制阻断（旧前十多数仍在前十） |

## 8. 测试计划

- **单元测试**（`tests/`）：
  - 代码格式转换（`.SS`↔`.SH`、normalize、BJ/HK/US 过滤）。
  - `save_etf_constituents` 的 `is_current` 切换事务语义（两期数据交替写入后查询正确）。
  - Top10 截取与排序（含并列权重、不足 10 只）。
  - 导入 job：已管理跳过、类目缺省、幂等重复导入（DataService 用 fake，对齐现有 job manager 测试写法）。
- **脚本联调**（手工，需临时账号）：
  - `--symbols 510300.SS --dry-run` 验证 token 可用与返回结构；
  - 全量跑一遍 → 中断后重跑验证断点续传。
- **UI 冒烟**：预览弹窗 → 导入 → 标的列表出现新股票 → 看板/详情页数据正常。

## 9. 实施步骤

| 步骤 | 内容 | 依赖 |
|---|---|---|
| 1 | `etf_constituents` 建表 + db 读写方法 + 单测 | 无 |
| 2 | `scripts/fetch_etf_holdings.py` + 单测（mock tushare） | 1 |
| 3 | **用临时账号全量跑一遍脚本，落真实数据** | 2、买账号 |
| 4 | 预览/导入接口 + `EtfConstituentImportJobManager` + 单测 | 1 |
| 5 | UI 弹窗与按钮 | 4 |
| 6 | 预置类目 `股票-ETF权重股-综合` 入库 | 1 |

步骤 3 之后功能即可用（哪怕 4/5 还没做，也能先在库里看到数据）；4/5 做完才是完整闭环。

## 10. 运维节奏

- **每季度一次**：买临时账号（几块钱）→ `TUSHARE_TOKEN=xxx .venv/bin/python scripts/fetch_etf_holdings.py` → 几分钟跑完。
- 建议窗口：季度末后第 20~30 天（季报披露完成后），如 4 月下旬 / 7 月下旬 / 10 月下旬 / 1 月下旬。
- 脚本支持 `--force` 全量重抓，账号内可随时补数据。
