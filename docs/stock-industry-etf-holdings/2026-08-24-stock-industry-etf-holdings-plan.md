# 股票行业分类体系重建（申万2021）+ ETF 前十大重仓股导入 方案 v1

- 日期：2026-08-24
- 状态：已实施上线（2026-08-24；评审意见见 `2026-08-24-stock-industry-etf-holdings-plan-review-v1.1.md`：A1/A3/A4 修正、B1-B6 补强全部采纳；A2 评审表述有误已更正——`_category_priority_map` 存在于 `instrument_admin.py:88`，其"抽公共函数"的建议采纳）
- 前置文档：`docs/etf-weighted-stocks/2026-07-30-etf-weighted-stocks-plan.md`（仅覆盖 ETF 重仓股导入的旧方案，已被本方案取代并从仓库移除，见 git 历史；本方案**取代**其 §6.3 默认类目设计，其余离线抓取/表结构/导入 Job 设计沿用）
- 用户已确认的决策（2026-08-24）：
  1. 类目树**全面对齐申万行业分类**，重建「股票」一级类目下的二三级树；
  2. 存量 275 只股票**批量重归类**（迁移前出对照报告确认）；
  3. 「股票 ↔ ETF 重仓」反向关系只维护数据，不做 UI；
  4. 自动归类失败进「待分类」类目 + 后台补数据，不阻断导入。

---

## 0. TL;DR

| 事项 | 结论 |
|---|---|
| 分类体系 | 申万 2021：股票二级 = 申万一级（31 个），股票三级 = 申万二级（134 个）；申万三级（346 个）不进树，存 `stock_industry` 表 |
| 行业分类数据源 | **TickFlow universes**（starter 档已实测可用，免费，覆盖沪深约 85%）为主；**tushare 季度临时账号**（`index_member_all`，2000 积分，官方全量）补齐修正 |
| ETF 重仓股数据源 | tushare `fund_portfolio`（5000 积分），与前方案一致，季度跑一次 |
| tushare 窗口合并 | 每季度买 1 次临时账号，**同一个窗口内**跑两个脚本：ETF 重仓快照 + 申万全量分类同步 |
| 归类落点 | `resolve_category(symbol)` 统一解析：tushare_sw > tickflow > 人工 > 待分类；ETF 导入与手动添加走同一入口 |
| 存量迁移 | 一次性脚本（仿 `migrate_category_simplify.py`：自动备份 + dry-run + 校验），先出对照报告 |

## 1. 背景与目标

当前「股票」一级类目下 275 只标的、10 个二级类目，明显偏科技（科技硬件 123 只、新材料与化工 56 只，而大消费 5 只、汽车与交通 2 只；食品饮料、银行、公用事业、交通运输、煤炭、石油石化等行业完全没有类目）。目标：

1. **ETF 重仓股导入**：在标的管理页对某 ETF 一键预览/导入其前十大重仓股到标的池。
2. **分类体系重建**：用申万行业分类替换现有手工类目树；新导入（ETF 重仓或手动添加）的股票自动归入正确的二三级类目。
3. **ETF ↔ 股票映射维护**：多对多关系完整落库、保留历史期次。
4. **变更语义**：新进重仓 → 可导入；被踢出 → 不删除标的，仅关联记录软失效。
5. **成本约束**：TickFlow starter 能做的事不用 tushare；tushare 用「季度临时账号（1-2 天）」模式，重仓股与行业分类在同一个窗口期内完成。

## 2. 前置调研结论（2026-08-24 实测）

### 2.1 TickFlow universes 原生提供申万三级分类（重大发现）

本机 tickflow SDK（项目已装、starter key 实测可调）的 `client.universes` 资源包含 **1005 个申万行业标的池**：

- 结构：335 个申万叶子行业 × 3 个层级标签（`CN_Equity_SW1_*` / `SW2_*` / `SW3_*`，同一 6 位行业码的三条记录成分完全一致，已全量校验 0 例不一致）。
- 6 位行业码为 DDMMSS 结构（如 `340501` = 食品饮料(34) > 白酒(05) > 白酒(01)），与申万官方两位行业编号一致；universe 名称带 `SW1/SW2/SW3` 前缀，剥掉前缀即层级名称（如 `SW3白酒Ⅲ` → 三级「白酒Ⅲ」）。
- **覆盖**：4430 / 5551 只沪深京 A 股（≈85%）；未覆盖 1327 只中 338 只为北交所（申万本就不覆盖，本项目也不管理），其余 989 只为沪深次新股/部分科创创业板（2022 年后上市为主）。
- **准确性**：无一股多挂（0 重复归属）；抽查 贵州茅台→白酒、立讯精密→消费电子零部件及组装、宁德时代→锂电池，全部正确。
- **成本**：335 个 universe 用 `universes.batch` 每 50 个一批 ≈ 7 次请求，starter 限额内随意跑，可按月同步甚至按需同步。
- 获取方式（SDK 现状）：需先 `universes.list()` 建立「行业码 → 三级名称」映射，再 batch 拉成分。每次同步全量拉，不做增量。

### 2.2 tushare 侧接口与积分（官网文档核实）

| 接口 | 用途 | 积分 | 说明 |
|---|---|---|---|
| `fund_portfolio` | ETF 前十大重仓股 | 5000 | 季报口径天然即前十大，无需指数映射（前方案 §3.1） |
| `index_classify` | 申万分类列表（SW2021：31 一级 / 134 二级 / 346 三级） | 2000 | 补分类树元数据用 |
| `index_member_all` | 申万三级成分（含 `l1/l2/l3_code+name`、`in_date/out_date/is_new`） | 2000 | **官方全量**，单次 2000 行需分批，全量 ≈3 次调用 |

一个 5000 积分的临时账号在同一窗口期可完成全部三件事（fund_portfolio 448 只 ETF ≈ 450 次调用 + 分类 ≈ 5 次调用，限频 200 次/分钟，总耗时十几分钟）。

### 2.3 存量 275 只股票的实测对照

用 tickflow 申万数据对存量池做了全量交叉验证：

- **覆盖 215 / 275（78%）**；未覆盖 60 只几乎全是 2022 年后上市的科创/创业次新股（海光信息、华虹公司、拓荆科技、华大九天等——这些官方申万**都有**分类，纯属 tickflow 数据滞后），tushare 窗口可一次补齐到接近 100%。
- **现有手工归类与申万高度一致**（科技硬件→电子 53/通信 15、新材料与化工→基础化工 18/有色金属 11、先进制造→机械设备 13、软件与互联网→计算机 8），说明迁移到申万体系后大部分股票仍与原来的"邻居"在一起，只是分组更细、更全。

## 3. 分类体系设计（功能 2.1）

### 3.1 为什么选申万 2021

| 候选 | 结论 |
|---|---|
| **申万 2021**（31 一级 / 134 二级 / 346 三级） | ✅ 选用。A 股卖方研究事实标准；**两个数据源（tickflow 免费 + tushare 付费）都原生提供**，可交叉校验补齐；颗粒度合适 |
| 中信分类（30 一级） | ❌ 与申万同级质量，但 tickflow/tushare 廉价渠道均不提供 |
| 证监会行业（19 门类） | ❌ 太粗（"制造业"一个门类装下半个市场），对看板分组无意义 |
| tushare `stock_basic.industry` | ❌ tushare 自有口径，非标准、层级缺失 |
| 东财/同花顺概念板块 | ❌ 概念一股多挂、变动频繁，不适合做互斥归类树 |

### 3.2 落地口径：二级 = 申万一级，三级 = 申万二级

项目类目树固定三级（`instrument_categories` 的 level 1/2/3，`instrument_metadata` 只有 l1/l2/l3 字段），申万也是三级，需要做一次取舍：

```
股票（L1，项目固有）
 ├─ 申万一级 31 个   → 项目 L2   （电子、计算机、医药生物、食品饮料、银行……）
 │   └─ 申万二级 134 个 → 项目 L3 （半导体、软件开发、白酒、证券……）
 └─ 待分类（L2）→ 待分类（L3）    兜底桶
申万三级 346 个 → 不进树，存 stock_industry 表（fact 层），可选写入 factor_tags 供检索
```

**为什么三级用申万二级而不是申万三级**：标的池规模是几百只，346 个三级类目平均每类不足 1 只，看板/热力图会碎成大量孤格子；134 个二级平均每类 2-4 只，分组感刚好。且申万二级名称（半导体、白酒、证券、电池、光伏设备、医疗器械）辨识度已经足够。申万三级信息不丢——存在 `stock_industry` 表里，未来若想加四级树或做更细的筛选有数据基础。

**名称规范化**：申万官方二三级名称带罗马数字后缀（白酒Ⅱ、家电零部件Ⅱ、游戏Ⅱ、白酒Ⅲ）。进类目树时**统一剥掉 `Ⅰ/Ⅱ/Ⅲ` 后缀**（展示干净）；`stock_industry` 表保留官方原始名称。

### 3.3 新类目树（股票一级下，预建全量）

31 个二级类目全量预建（含暂时无标的的——看板 SQL 是 JOIN `instrument_metadata`，空类目自然不显示；但添加标的时下拉框完整可选）：

> 农林牧渔、基础化工、钢铁、有色金属、电子、汽车、家用电器、食品饮料、纺织服饰、轻工制造、医药生物、公用事业、交通运输、房地产、商贸零售、社会服务、银行、非银金融、综合、建筑材料、建筑装饰、电力设备、机械设备、国防军工、计算机、传媒、通信、煤炭、石油石化、环保、美容护理（+ 待分类，priority 9999 排末位）

134 个三级类目按申万 2021 官方列表全量预建（从 tushare `index_classify(level='L2', src='SW2021')` 或 tickflow universe 名录生成，以第一次 tushare 窗口拉取的官方名录为准）。

**ETF 一级类目的树不动**（用户确认现有 ETF 分类已够全）。

### 3.4 与旧手工类目的关系

- 旧三级细分（半导体-设计/制造/封测、光模块、氟化工……）**不进新树**。其中大部分与申万二/三级天然对应（半导体-设计 ≈ 申万二级「半导体」下的三级「集成电路设计」），信息损失有限。
- 迁移前把每只股票的旧 `(category_l2, category_l3)` 归档到 `stock_category_archive` 表（§4.3），可随时回溯；不回写 `factor_tags`（避免污染因子语义）。

## 4. 数据模型

### 4.1 新表 `stock_industry`（行业分类 fact 表，功能 2 的核心）

```sql
CREATE TABLE IF NOT EXISTS stock_industry (
    symbol      TEXT PRIMARY KEY,        -- 项目格式，如 600519.SS
    sw_l1_name  TEXT NOT NULL,           -- 申万一级（去后缀），如 食品饮料
    sw_l2_name  TEXT NOT NULL,           -- 申万二级（去后缀），如 白酒
    sw_l3_name  TEXT NOT NULL DEFAULT '',-- 申万三级（官方原名），如 白酒Ⅲ
    sw_l3_code  TEXT NOT NULL DEFAULT '',-- 行业码（tushare 为 850xxx.SI，tickflow 为 6 位内部码，按 source 解释）
    source      TEXT NOT NULL,           -- tushare_sw2021 | tickflow_universe | manual
    updated_at  TEXT DEFAULT (datetime('now','localtime'))
);
```

写入/读取方法加在 `db.py`（风格对齐 `save_instrument_metadata`）：

- `upsert_stock_industry(rows, source)`：**按来源分优先级合并**——`tushare_sw2021` 全量覆盖（官方事实源）；`tickflow_universe` 只填空缺或更新同为 tickflow 来源的行（不覆盖 tushare 行）；`manual` 优先级最高且不被任何同步覆盖。
- **删除语义：同步只增/改、从不删行**。最新一期 `index_member_all` 拉不到的股票（退市、被调出指数）保留旧行——对在管标的无害（metadata 不自动跟随，§8），且避免退市股行业信息丢失。
- `get_stock_industry(symbol)` / `list_stock_industry(symbols)`。
- 两源合并时按**名称**对齐（统一规范化后比对，见下），不按 code（两边 code 体系不同，`sw_l3_code` 列混存两套码、消费方须按 `source` 解释；该列当前无功能消费方，仅留档）。
- **名称规范化是两源对齐的前提**，同步脚本入口处统一做：① 剥罗马数字后缀时同时处理 Unicode（`Ⅱ` U+2161 等）与 ASCII（`II`）写法——tickflow universe 名与 tushare `index_classify` 的字符习惯未必一致；② 去首尾空白、全半角统一。

### 4.2 新表 `etf_constituents`（沿用前方案 §4.1，一处修正）

`(etf_symbol, stock_symbol, stock_name, weight, rank, period, ann_date, is_current, source, fetched_at)`，主键 `(etf_symbol, stock_symbol, period)`。多对多关系天然支持（一只股票出现在多只 ETF、多个期次）；软失效靠整 ETF 的 `is_current` 翻转，**从不删行**（功能 3、4 的语义都在这里）。

**与前方案的唯一差异**：`fetched_at` 不能用前方案 DDL 的 `DEFAULT CURRENT_TIMESTAMP`（UTC）。项目自 commit `42ccaa1` 起全库时间戳统一为本地时间，本表必须为 `DEFAULT (datetime('now','localtime'))` 或 INSERT 时显式写入（对齐现有代码风格），否则新鲜度判断（期次距今月数）差 8 小时起跳。

### 4.3 新表 `stock_category_archive`（一次性迁移归档）

```sql
CREATE TABLE IF NOT EXISTS stock_category_archive (
    symbol      TEXT PRIMARY KEY,
    category_l2 TEXT,
    category_l3 TEXT,
    migration   TEXT NOT NULL,           -- 如 'sw2021_2026_q3'
    archived_at TEXT DEFAULT (datetime('now','localtime'))
);
```

### 4.4 `instrument_categories` 重建

- 删除「股票」一级下全部旧二三级节点（路径前缀 `股票-%`），插入 §3.3 新树（31 + 134 + 待分类 ≈ 166 节点）。
- ETF 子树、两个一级节点不动。
- **建树前校验（防静默写坏）**：① 同 L1 下去后缀后的 L2 名必须唯一，撞名（剥罗马数字后缀导致）时保留后缀消歧并告警，而非 INSERT 撞主键半途失败；② 断言类目名不含路径分隔符 `-`（现有树已有含 `/` 的名字如 `检测设备/仪器仪表`，`/ `无害但 `-` 会静默破坏 path 解析）。
- `instrument_metadata` 的 `priority_l1/l2/l3` 按新树重算。priority 计算逻辑的实际位置在 `src/services/instrument_admin.py`（`_category_priority_map()` :88 读类目树 + `_build_new_instrument_record()` :120 中的推 priority 段），db 层只存不算。**实施时先把这段抽成公共函数**（如 `instrument_admin.category_priorities(l1, l2, l3)`），迁移脚本、待分类回补、ETF 导入 Job 三处共用，避免三份平行实现各自演化。

## 5. 数据流与刷新机制

```
┌─ 高频/免费（TickFlow starter）──────────────────────────────┐
│ scripts/sync_stock_industry.py（或并入调度器月度任务）        │
│   universes.list + batch(335) → 解析层级名 → upsert          │
│   （source=tickflow_universe，只填空，不覆盖 tushare/manual） │
└──────────────────────────┬──────────────────────────────────┘
                           ▼
                    stock_industry（fact 表）
                           ▲
┌─ 季度窗口（tushare 临时账号，1-2 天）───────────────────────┐
│ scripts/fetch_etf_holdings.py     scripts/sync_sw_tushare.py │
│  fund_portfolio → etf_constituents  index_member_all ────────┘
│                                   （source=tushare_sw2021，全量覆盖非 manual 行）
└──────────────────────────────────────────────────────────────┘
                           ▼
        resolve_category(symbol)  —— 唯一归类入口
          ① stock_industry 命中 → (股票, sw_l1, sw_l2)
          ② 未命中 → (股票, 待分类, 待分类)，记入待补清单
                           ▼
   ETF 重仓导入 Job · 手动添加 suggest · 存量迁移脚本 · 待分类回补
```

**刷新节奏**：

- `sync_stock_industry.py`（tickflow）：月度跑（可挂调度器，也可手动）；数据便宜，无成本压力。申万官方每年 6/12 月调整成分，新股上市陆续纳入，月度足够。
- tushare 窗口：每季度一次，与 ETF 重仓快照**同一个账号窗口**完成（先跑 `sync_sw_tushare.py` 再跑 `fetch_etf_holdings.py`，导入时分类数据最新）。
- **应用运行时对两个数据源零在线依赖**：suggest/导入只查本地 `stock_industry` 表；未命中进待分类，不阻塞、不实时调外部 API（符合项目"DB 优先"的新鲜度约定）。

**待分类回补**：每次 tickflow/tushare 同步后，自动对所有当前类目为「待分类」的在管股票重跑 `resolve_category`，命中的批量改类目（写 metadata + 重算 priority）；仍不命中的列入同步脚本的汇总输出，提示人工处理（`manual` 来源）。回补属于"自动改看板分组"，与 §8 的保守原则不冲突（待分类是占位符不是用户选择），但**必须输出移动清单**（哪只从待分类去了哪个行业）写进同步脚本汇总与 `job_runs`，可追溯——迁移后第一次回补可能一次性移动几十只股票，看板分组会突变，用户需要能查到原因。所有 metadata UPDATE 必须保证 `updated_at` 刷新（见 §7.2 的实现要求），否则看板缓存不会失效。

## 6. 功能实现

### 6.1 功能 1：ETF 前十大重仓股导入

离线抓取与在线导入**完全沿用前方案** §3（`fund_portfolio`、期次推算、断点续传、限流）、§4（表结构、is_current 语义）、§5（`scripts/fetch_etf_holdings.py`）、§6（预览/导入 API、`EtfConstituentImportJobManager`、UI 弹窗），仅两处修改：

1. **导入类目不再用预置的「股票-ETF权重股-综合」**，改为逐股票调 `resolve_category`：
   - 命中 → 自动带上正确的（申万一级， 申万二级）类目；
   - 未命中 → 待分类，导入结果里单列提示"N 只待分类，下次 tushare 窗口后自动回补"。
2. 预览接口每行除 `already_managed` 外增加 `resolved_category` + `hit` 标志展示（导入前就能看到会归到哪；待分类行 UI 置灰/标黄，与 §6.3 手动添加的提示样式一致）。

其余细节（`.SS↔.SH` 转换、BJ/HK/US 过滤、单事务 is_current 翻转、限流、断点续传、临时账号环境变量注入）不再重复，见前方案。

### 6.2 功能 2.2a：ETF 重仓股的自动归类

即 §6.1 的修改 1——导入 Job 里 `_build_new_instrument_record({symbol, name, category_l1/l2/l3 = resolve_category(symbol), ...})`，`source="etf_constituent"`。无额外机制。

### 6.3 功能 2.2b：手动添加股票的自动归类建议

- 新增 `GET /instruments/api/suggest-category/{symbol}`：查 `stock_industry` → 返回 `{category_l1, category_l2, category_l3, source, sw_l3_name, hit}`；未命中返回待分类 + `hit=false`。
- 前端 `instruments.html`：添加表单的名称是**代码输入后自动查询**的（名称框 readonly，无独立按钮），suggest 调用挂在该自动查询的回调之后，**预填三级级联下拉**（用户可改——下拉本来就是完整的 166 节点树）；`hit=false` 时预填待分类并显示一行提示"暂未识别行业，可手动选择或保持待分类（后续自动回补）"。注意边界：**名称查询失败（TickFlow 未覆盖/网络失败）时 suggest 仍应照常调用**——行业分类在本地表，不依赖名称查询成功。
- 纯本地表查询，毫秒级，不加远程调用。

### 6.4 功能 3/4：映射维护与变更语义

- 多对多：`etf_constituents` 主键含 `(etf_symbol, stock_symbol, period)`，一只股票是 N 只 ETF 的重仓 = N 行，天然维护。
- 新进重仓：新期次新行，`is_current=1`，导入功能可纳入。
- 被踢出：整只 ETF 旧行随快照翻转 `is_current=0`（软失效），**标的本身不删**、行情/指标/看板照常；未来重回前十插新行即可。
- 反向查询（某股票当前是哪些 ETF 的重仓）：`SELECT etf_symbol, weight, rank, period FROM etf_constituents WHERE stock_symbol=? AND is_current=1` —— SQL 层面随时可查，**本期不做 UI**（用户已确认）。
- 新鲜度：预览接口/UI 展示期次与距今月数（前方案 §6.4 的黄色提醒保留）。

## 7. 存量迁移（功能 2.2 存量部分）—— 利害关系与影响面

### 7.1 影响面盘点（已全量 grep 核实）

类目字段（`category_l1/l2/l3`）在项目里的全部用途：

| 使用方 | 用途 | 迁移影响 |
|---|---|---|
| 看板/热力图/侧边导航（dashboard、subject_market、intraday_service） | 按 l1→l2→l3 分组聚合展示 | **这正是迁移目的**。`updated_at` 变化 → RevisionCache 自动失效重建一次，无需手工处理 |
| market_view、core/display | 展示类目名 | 同上，实时读 metadata |
| 批量回测（batch_service/batch_backtest） | 按 l1 过滤 + 结果快照存类目 | l1 不变（仍是"股票"），过滤不受影响；**历史结果快照刻意不回迁**（沿用 `migrate_category_simplify` 先例），新回测用新类目 |
| MCP server | 按类目名关键词搜索标的 | 新类目名更标准（"银行""半导体"），搜索体验反而变好 |
| 策略/交易/止损/风控 | **无任何依赖**（`risk_budget_pct`、`stop_atr_mul` 是标的级字段，与类目无关；manual_trade 不读类目） | 零影响 |

结论：**类目在本项目里纯粹是展示/分组维度，不驱动任何交易逻辑**，重归类是低风险操作。真正需要守住的是"任何在管标的三级类目非空"（看板 SQL 硬要求）——待分类桶保证了这一点。

### 7.2 迁移脚本 `scripts/migrate_category_sw2021.py`

仿 `migrate_category_simplify.py` 的结构（自动备份、`--dry-run`、跑后校验）：

```
1. 自动备份 data/trend_quant.db 到 data/backups/
2. 前置检查：stock_industry 覆盖率报告（命中/待分类各多少只）
   —— 强烈建议在第一次 tushare 窗口之后执行正式迁移（覆盖率 ~100%）；
      若提前迁移，60 只次新股会先进待分类，窗口后自动回补（§5）
3. 旧类目归档 → stock_category_archive
4. 重建 instrument_categories 的 股票 子树（ETF 子树不动；先过 §4.4 的名称唯一性/分隔符校验）
5. 逐股票 resolve_category → 更新 metadata 的 l2/l3 + 重算 priority_l1/l2/l3
6. 输出对照报告：旧类目 → 新类目 的完整映射表（按旧类目分组，类 §2.3 的交叉表）
7. 校验：在管标的三级类目全非空；新树无孤儿节点；metadata priority 与新树一致
```

**实现要求（防止"校验通过但页面还是旧的"）**：

- 第 5 步的 metadata UPDATE 必须显式刷新 `updated_at`。推荐直接走 `save_instrument_metadata`（其 UPSERT 冲突分支已写死 `updated_at=datetime('now','localtime')`）；若用原生 SQL UPDATE 则必须显式写 `updated_at`——看板 revision 只看 `MAX(updated_at)`，漏写会**静默继续读旧分组缓存、无任何报错**。
- 执行环境：本地库 `data/trend_quant.db` 即 127.0.0.1:8000 的生产库。**迁移在停服状态执行**（避免迁移中途服务读到半新半旧的类目树、避免看板/回测并发），跑完重启服务兜底清进程内缓存。
- 迁移后人工冒烟：打开看板确认分组确实变成新行业树。

`--dry-run` 只输出第 2、6 步的报告不写库，**先给用户确认对照报告再正式跑**。

### 7.3 复杂度评估

| 部分 | 工作量 | 风险 |
|---|---|---|
| `stock_industry` 表 + db 方法 + 单测 | 小 | 低 |
| tickflow 同步脚本 + 单测 | 小（≈100 行，SDK 现成） | 低 |
| tushare 分类同步脚本 | 小（复用前方案的 token/限流/代码转换骨架） | 低 |
| 类目树重建 + 存量迁移脚本 | 中（有 `migrate_category_simplify.py` 模板可抄） | 中——靠备份 + dry-run + 校验三重兜底 |
| suggest API + 前端预填 | 小 | 低 |
| ETF 导入（前方案全套） | 中 | 低（前方案已做过完整设计） |

## 8. 边界情况

| 情况 | 处理 |
|---|---|
| tickflow 未覆盖的次新股 | 进待分类；下次 tickflow 月度同步或 tushare 季度窗口自动回补 |
| tushare 与 tickflow 归类冲突 | tushare（官方）覆盖 tickflow 行；`manual` 行任何同步都不动 |
| 申万官方调整行业归属（每年 6/12 月） | tushare `index_member_all(is_new='Y')` 全量刷新自然生效（只更新 `stock_industry`，§4.1 删除语义）；已归类股票的类目**不自动跟着改**（避免用户看板分组悄悄变化）。同步脚本输出"归属变更清单"，比较口径 = **新拉取的 `stock_industry`（官方最新归属）vs `instrument_metadata`（当前类目）**，用户在标的管理页自行决定去留 |
| 北交所股票 | 不管理（TickFlow starter 不覆盖），过滤丢弃 |
| ETF 无 A 股持仓（债券/货币/QDII） | 快照标记 `no_data`，UI 按钮置灰（前方案 §7） |
| 待分类桶里的股票参与看板/回测 | 与正常类目一样参与（三级非空即合法），分组显示为"待分类" |
| 用户手动改过某股票类目 | metadata 为准，`resolve_category` 只在**新增**和**待分类回补**时生效，永远不主动覆盖非待分类的现有归类 |
| 申万二级名与 ETF 现有三级重名（如"通信"在两个一级下都有） | 类目按 path 区分（`股票-通信-...` vs `ETF-制造工业-通信`），无冲突 |

## 9. 测试计划

- **单测**：
  - tickflow 同步：universe 名解析（`SW3白酒Ⅲ` → l1=食品饮料/l2=白酒/l3=白酒Ⅲ）、6 位码层级推导、罗马数字后缀剥离——**含 Unicode（`Ⅱ` U+2161）与 ASCII（`II`）混写用例**（§4.1 规范化）；
  - 来源优先级合并（tushare 覆盖 tickflow、manual 不被覆盖、tickflow 只填空）、同步不删行（§4.1 删除语义）；
  - `resolve_category` 命中/未命中/待分类；
  - 建树校验：同 L1 剥后缀撞名时消歧+告警；**类目名含 `-` 时拒绝执行**；
  - 迁移脚本：dry-run 不写库、归档完整、新树 166 节点、无空类目、metadata UPDATE 后 `updated_at` 已刷新（fake db，对齐现有迁移脚本测试写法）；
  - 前方案既有：fund_portfolio Top10 截取、is_current 事务翻转、导入幂等。
- **登录墙**：全站 cookie session 登录墙已于 2026-08-24 落地（`src/app/routers/auth.py`）。新增的 `suggest-category`、`etf-constituents` 预览/导入三个接口必须验证**未登录访问返回 401/重定向**（对齐 `tests/api/test_auth_wall.py` 写法）——ETF 导入是写操作，这条不能省。
- **联调（手工）**：tickflow 同步脚本直接跑（免费）；tushare 两个脚本在临时账号窗口先 `--symbols/--dry-run` 小范围验证。
- **UI 冒烟**：手动添加一只银行股 → 类目预填"银行-城商行"（验证预填与可改、名称查询失败时 suggest 仍生效）；ETF 导入弹窗 → 待分类行标黄 → 导入后看板分组出现新行业。

## 10. 实施步骤

| 阶段 | 内容 | 依赖 |
|---|---|---|
| P0 数据底座 | `stock_industry` 建表 + db 方法 + `scripts/sync_stock_industry.py`（tickflow）+ 单测；跑一遍落 4400+ 只股票分类。**同时解决 tushare 依赖**：本仓库无 requirements.txt，依赖在 `pyproject.toml` 且 tushare 目前未安装——在 pyproject 加 optional 依赖组（如 `tushare = ["tushare"]`）或写明"脚本运行前手动 `pip install tushare` 到本地 .venv"（部署即本地 127.0.0.1:8000 的 .venv，装一次即可） | 无 |
| P1 首个 tushare 窗口 | `scripts/sync_sw_tushare.py`（index_classify + index_member_all）+ `scripts/fetch_etf_holdings.py`（前方案）+ 单测；**买临时账号全量跑** | P0、买账号 |
| P2 存量迁移 | `scripts/migrate_category_sw2021.py`：先 dry-run 出对照报告给用户确认 → **停服**正式迁移 → 重启 + 看板冒烟 | P1（覆盖率 ~100% 时迁移最干净） |
| P3 在线功能 | suggest API + 手动添加预填；ETF 预览/导入 API + 导入 Job + UI 弹窗；三个新接口过登录墙测试 | P0（P2 之后体验最佳） |
| P4 调度运维 | tickflow 月度同步挂调度器（落点 `src/core/scheduler.py` 的 `start()`，目前只挂每日更新与盘中快照，加月度 `CronTrigger(day=...)` 需动签名/配置）；待分类自动回补接入同步脚本（含移动清单 → `job_runs`）；运维文档 | P0-P3 |

P0 完成后「手动添加自动归类」即可用；P1 完成后 ETF 导入可用；P2 完成后分类体系重建闭环。

## 11. 运维节奏与成本

- **每月**（免费）：调度器自动跑 tickflow 行业同步（≈10 次请求，秒级）。
- **每季度**（几块钱）：买 tushare 临时账号 → 依次跑 `sync_sw_tushare.py` + `fetch_etf_holdings.py`（合计十几分钟）→ 检查汇总输出（待分类回补结果、no_data ETF、期次覆盖率）。建议窗口：季度末后第 20-30 天（4/7/10/1 月下旬），与季报披露节奏对齐。
- 申万半年度成分调整（6/12 月）由 tushare 窗口自然吸收。
