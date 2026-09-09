# 批量回测功能设计方案 v1.2

> 日期：2026-07-26（v1.2 修订；**当日已实施完成**，见文末「实施完成记录」）
> 状态：三方两轮评审均通过（k3 复审结论「批准实施」），已实施并验证
> 评审文档：同目录 `ds-flash-batch-backtest-review(-v1.1).md`、`ds-review-2026-07-26(-v1.1).md`、`k3-review-2026-07-26.md`
> 范围：Trend Quant 系统新增「批量回测」功能的设计方案

## 修订记录

### v1.1 → v1.2（第二轮复审合并）

**方案级变更：**

1. **钻取链路再简化**：放弃 sessionStorage，URL 直接带 `batch_id + strategy_id + symbol + 起止日期`，market_view 调后端接口回查批次快照策略——彻底解决跨标签页失效（中键/Ctrl+点击），无 URL 长度问题（ds-flash v1.1 #3，优于 DeepSeek 的「同标签页约束」）
2. **`data_version` 降级为静态提示**：每日 16:30 更新必然改变 `MAX(updated_at)`，条件告警会天天误报导致告警疲劳。列保留（V2 批次对比用），MVP 钻取页固定静态提示「基于批次快照重跑，可能因历史数据修正产生轻微差异」（DeepSeek v1.1 §2.3）
3. **bars 列名修正**：`load_history` 返回 `time` 列（datetime64），不是 `date`（DeepSeek v1.1 §4.2，已核实 db.py:691-708）；引擎按 `end_date=anchor` 过滤即可，手动截断只为特征计算做一次（k3 复审 §4-6）

**实施级备注（合入 §5.8 验收清单）：** 409 竞态事务兜底、skipped 逐策略写行且 done_cells 按格递增、特征 LEFT JOIN + trend_score_avg 可空、engine_version MVP 填固定值、取消用内存 threading.Event、随机指标校验放 POST 校验层、golden 用 CSV fixture、明细表增量渲染（弃虚拟滚动）、blob 端点改 query param、特征公式写进 docstring、快照键名 `strategy_config`、hung 批次风险记录、第 0 步分层抽样、README:47 措辞修正。

**核实后不采纳（本轮）：**

| 出处 | 意见 | 不采纳原因 |
|---|---|---|
| ds-flash v1.1 #6 | 确认引擎是否支持 instrument_type/fee_min | 已核实 `models.py:20-33` 现成支持，无需动作 |
| ds-flash v1.1 #7 | monthly_nav 移到 V2 | 存储成本 ~2KB/格可忽略，不存则未来加净值对比视图需整批重跑；视图本身仍是 V2 |
| DeepSeek v1.1 §2.1 | 钻取约束同标签页跳转 | 被更优的 URL+后端回查方案（本条变更 1）取代 |

### v1.0 → v1.1（第一轮评审合并）

**采纳：** ① 钻取透传日期+快照策略；② 修正 golden 表述、拒绝 random_uniform、补端到端 golden；③ `data_anchor_date` 锚定 + `data_version`；④ 孤儿批次启动置 interrupted；⑤ 特征随批计算入库（撤实时端点）；⑥ 假规律防护落地（n 标注/n<5 置灰/只聚合 ok/bar_count≥250 默认过滤）；⑦ excess 为服务层派生、B&H 无摩擦口径接受+注明；⑧ config_json 补 fee_min；⑨ 单 running 批次 409 + 发起区耗时预估；⑩ 月度采样 NAV；⑪ DELETE 级联+running 保护、快照名不联表、默认命名；⑫ 第 0 步耗时实测。

**不采纳（经代码核实）：** DeepSeek 的 indicator_config 机制描述（schema 无此字段）；k3「0.5s 无依据」（README:47 有记载，k3 复审已自认错误）；DeepSeek 的 MAX(date) 当日历（`core/calendar.py:108` 已有 `previous_trading_day`）；URL base64 传快照；后端分页；excess_calmar。

---

## 1. 方案背景

### 1.1 系统现状

Trend Quant 是一个 A 股趋势跟踪系统（FastAPI + SQLite 单文件库 + Jinja2/原生 JS/ECharts 前端，无前端框架），当前已有**单标的规则回测**能力：

- 策略以 JSON 条件树形式定义（entry/exit 条件组 + 指标乐高积木，18 个注册指标），存于 `rule_strategies` 表，可随时在线编辑。
- 仓位策略（sizer）是独立维度（固定比例/风险预算/凯利），回测时与交易策略做笛卡尔积。
- 回测引擎经优化后单次「1 标的 × 1 策略」约 **0.5 秒**（README:47 记载，典型趋势策略；长历史标的显著更慢，见 §5.6 第 0 步实测）。
- 结果**不落库**：只存在内存字典（TTL 30 分钟，重启即丢），前端轮询进度 + 完成后一次性拉取瘦身结果（gzip 后约 200KB）。

### 1.2 标的数据现状

- 标的池 716 个，三级类目体系（`instrument_metadata.category_l1/l2/l3`），7 个一级类目：股票 264（stock）、行业 220、宽基 70、策略 66、跨境 66、债券 25、商品 5（以上为 etf）。
- 行情存于 SQLite `market_data_qfq`（前复权日线，约 120 万行，WAL 模式；`time` 列为 TEXT ISO 日期），单标的数据从十几根（新上市 ETF）到约 7700 根（1993 年上市老股）不等。
- 行情每日 16:30 增量更新；前复权因子随时间可能变化。
- `trend_daily` 表存有每标的每日趋势分（主键含 `param_set`，取数固定用 `'default'`；**新入库或缓存未重建的标的可能无行，特征值须可空**）。

### 1.3 为什么需要批量回测

用户管理数百个标的，希望回答的核心问题是：**策略与标的之间存在适配关系——某些策略特别适合某些类型的标的**。单标的逐个回测无法在合理时间内覆盖全标的池，也无法横向对比挖掘这种共性规律。因此需要：按类目批量执行「标的 × 策略」回测 → 持久化结果 → 提供聚合分析视图挖掘策略-标的适配性。

---

## 2. 需求与已确认决策

| # | 决策点 | 结论 |
|---|---|---|
| 1 | 标的范围 | 按**一级类目多选**筛选 |
| 2 | 回测区间 | **全生命周期**：每标的用其完整数据区间（截至批次锚定日 `data_anchor_date`）；结果记录实际起止日期 |
| 3 | 策略选择 | 多选；批次启动时**快照策略 JSON** 入库，执行全程用快照；**POST /run 参数校验层拒绝含 `random_uniform` 指标的策略**（seed=None 结果不可复现；在策略列表 UI 上加标注防患于未然） |
| 4 | 仓位策略 | **不参与**，满仓买卖（sizer=None → all-in，现有逻辑零成本支持） |
| 5 | 失败处理 | 单格失败/数据不足 → 记 status+error，**continue** |
| 6 | 结果持久化 | **分层存储**（见 §3.1），批次与结果落 SQLite |
| 7 | 明细钻取 | **URL 透传 + 后端回查快照**（v1.2）：跳 `/market-view?drill=1&batch_id=X&strategy_id=Y&symbol=Z`，market_view 调批次 API 取快照策略 JSON 与格子起止日期，跳过 StrategyLoader 直接重跑 |
| 8 | 分析视图 | MVP 4 个：明细大表、策略×类目透视热力图、超额 vs 特征散点、特征分桶。**视图间零耦合**，共用 cells 数据源 |
| 9 | MIN_BARS | **固定 60**（低于 60 根整标的记 skipped）+ 视图层补偿：cells 记 bar_count、明细表默认过滤 bar_count<250（可关）、零交易格子单独标识 |
| 10 | B&H 基准口径 | **接受现状（无摩擦）+ UI 注明**：基准不扣费导致超额系统性略偏低、对交易多的策略不利；tooltip 与文档写明 |

---

## 3. 关键设计讨论

### 3.1 明细数据存不存？怎么展示？

**结论：分层存储 + 钻取重跑**。

- **存**：summary 指标（平铺列）、年度统计、月度热力图、逐笔交易（含买卖点）、skipped_buys、月度采样 NAV（每月末净值点，供 V2 净值叠加对比视图）。单格几 KB~几百 KB，单批次数十 MB 量级，SQLite 无压力。
- **不存**：daily_nav（以月度采样 NAV 代替）、K线 charts、condition_trace、debug_log。
- **钻取**：用**批次快照策略 + 批次记录的起止日期**实时重跑（见 §2 决策 7）。

**重跑一致性边界（准确表述）**：
- 透传起止日期后，漂移来源从「必然（数据每日变长）」缩小到「仅历史数据被改写（复权因子变化/数据修正）」；
- 现有 golden 测试锁的是「新旧实现等价」而非固定数值——批量功能补一个真·端到端 golden（CSV fixture 固定数据，锁 summary 数值），用于回答「引擎重构后历史批次是否仍可对比」；
- `data_version` 列保留作 V2 批次对比钩子；MVP 钻取页用**静态提示**（不做条件告警，避免每日更新导致告警疲劳）；
- 快照保证策略参数不变，**不保证底层指标实现不变**；批次表 `engine_version` 字段 MVP 填固定值（如 '1.0'），后续接入 git hash/formula_version。

### 3.2 跨标的可比性与超额收益口径

- 横比只用时间归一指标（年化、夏普、卡玛、胜率）+ **超额收益**；
- **超额定义锁定**：`excess_annual_return = summary.annual_return − benchmark_summary.annual_return`（批量服务层派生，非引擎现成字段），正值=跑赢买入持有；
- **口径不对称已确认**：B&H 基准（`engine.py:577-587`）首日按收盘价买入、不扣任何佣金滑点，策略端有摩擦 → 超额系统性偏低，且对交易多的策略不利。决策：**接受现状**，在超额相关 UI 的 tooltip 与文档注明；
- 短区间标的年化不稳定：散点/分桶视图提供「超额年化 / 超额总收益」切换；
- `fee_min=5.0` 使小成价格子实际费率远高于名义费率，分析时注意。

### 3.3 策略-标的适配性挖掘（核心价值）

MVP 4 个描述性视图（均只聚合 `status=ok` 的格子）：

1. **明细大表**：标的×策略一行，排序/筛选/钻取；默认 bar_count≥250 过滤（可关）；零交易格子标识。
2. **策略×类目透视热力图**：中位数聚合，每格标注 n，**n<5 置灰不显示数值**。
3. **超额 vs 特征散点**：x=标的特征、y=超额，按类目着色。
4. **特征分桶**：特征三分位分桶对比各策略表现。

**假规律防护（落地到视图）**：n 标注、n<5 置灰、每个分析 Tab 顶部固定横幅「仅用于描述性观察与假设生成，非统计推断」。

### 3.4 其他设计要点

1. **策略快照**：批次表存 `[{id, name, strategy_config}]` 全文；明细表展示一律用快照 strategy_name，不联表现有策略表。
2. **批内数据一致性**：批次启动锚定 `data_anchor_date = MAX(time)`（全库），引擎请求 `end_date=anchor`，特征计算用 `bars[bars["time"] <= anchor]` 截断后的 bars；16:30 增量更新与批次并发的混数据窗口由此消除（复权因子回溯改写除外，属罕见残余风险）。
3. **结果时效**：批次列表 + 重跑入口 + `data_anchor_date` 展示。
4. **短数据标的**：MIN_BARS=60 记 skipped + §2 决策 9 的视图补偿。
5. **进度与取消**：格子级进度；协作式取消（内存 `threading.Event`，每格间 + 每标的加载行情后各检查一次）；取消后批次置 `cancelled`，已落库格子照常分析，未执行格子不入库不显示。
6. **孤儿批次**：服务启动时一次性把 `status=running` 批次置为 `interrupted`（注明「服务重启导致中断」）。
7. **执行顺序**：按标的分组，每标的只加载一次行情 + 解析一次 instrument_type；特征随标的计算一次入库。
8. **内存**：每格提取所需字段写库（逐格 commit）后显式丢弃完整 result dict。
9. **hung 批次**：协作式取消对引擎级死循环无效（概率低，MVP 不做 watchdog）；风险记录于此，V2 可加 `last_cell_completed_at` 检测或 force 取消。

---

## 4. 备选方案与权衡（未采用）

| 方案 | 放弃原因 |
|---|---|
| 全量瘦身结果落库（钻取不重跑） | 每批次 160MB+；透传日期+快照后重跑一致性已够用 |
| 只存 summary 指标行 | 年度拆解/交易复盘做不了 |
| 仓位策略参与笛卡尔积 | 格子翻倍、维度变难读；本期聚焦策略-标的适配 |
| 统一回测窗口 | 浪费长历史；年化/超额归一已够 |
| 多线程/多进程并行 | CPU 密集 + GIL；串行可接受；循环边界预留分片接缝（见 §7） |
| sessionStorage 传递钻取快照 | 跨标签页失效（中键新 Tab 读不到）；被 URL+后端回查取代（v1.2） |
| 钻取 data_version 条件告警 | 每日更新必触发，告警疲劳；改静态提示（v1.2） |
| 批次页内嵌钻取（不跳 market_view） | 需复制 market_view 渲染逻辑，双份维护 |
| 给 B&H 基准补摩擦（改口径） | 会改变现有单标的回测展示口径；以注明代替 |
| 显著性检验/统计建模 | 样本小、多重比较；以 n 标注+置灰+横幅代替 |
| 断点续跑（resume） | 语义复杂；预留 `interrupted` 状态枚举，重跑即可 |
| 明细大表虚拟滚动 | 原生 JS 实现成本高；改增量渲染（滚动到底追加 200 行）（v1.2） |

---

## 5. 详细设计

### 5.1 数据库（`src/data/storage/db.py` 新增三表）

**`batch_backtest_runs`（批次表）**

| 字段 | 说明 |
|---|---|
| `batch_id` PK | 时间戳 ID |
| `name` | 默认 `{类目摘要}×{策略数}策略-{日期}`，可改 |
| `status` | running / completed / error / **interrupted** / cancelled |
| `categories_json` / `strategy_snapshot_json` / `config_json` | 类目 / 策略快照 `[{id,name,strategy_config}]` / 执行参数（含 fee_min、MIN_BARS 等） |
| `total_cells / done_cells / ok_cells / failed_cells / skipped_cells`、`current_symbol` | 进度（done_cells 按**格子**递增） |
| `data_anchor_date` | 批次数据锚定日（批内一致性） |
| `data_version` | 启动时 `MAX(updated_at)`（market_data_qfq），V2 批次对比钩子 |
| `engine_version` | MVP 填固定值 `'1.0'`，后续接 git hash/formula_version |
| `created_at / finished_at / error` | |

**`batch_backtest_cells`（格子表，PK = batch_id+symbol+strategy_id）**

- 维度列：`symbol, symbol_name, strategy_id, strategy_name（快照名）, category_l1/l2/l3, asset_type`
- 状态列：`status(ok/failed/skipped), error, start_date, end_date, bar_count`
- 指标平铺列：`total_return, annual_return, max_drawdown, sharpe, sortino, calmar, win_rate, profit_factor, trade_count, final_equity, benchmark_total_return, benchmark_annual_return, excess_annual_return（服务层派生）`
- blob 列：`annual_returns_json, monthly_heatmap_json, trades_json, skipped_buys_json, monthly_nav_json`
- 索引：`(batch_id)`、`(batch_id, annual_return)`，其余按需后加

**`batch_backtest_symbol_features`（标的特征表，PK = batch_id+symbol）**

- `ann_volatility`（年化波动率）、`momentum_250`（锚定日前 250 交易日价格收益率，不足用全部可用数据）、`bh_max_drawdown`（全周期 B&H 回撤）、`trend_score_avg`（锚定日前 250 日 trend_daily 均值，param_set='default'，**可空**）、`amount_ma20`（SMA(amount,20) 锚定日末值，流动性代理）、`bar_count`
- 特征基于锚定日截断后的 bars 计算，与回测区间一致；批次执行时随标的算一次；公式写进 `compute_features()` docstring
- cells 端点 join 特征表用 **LEFT JOIN**，视图对特征 null 降级（散点剔除/分桶归「无特征」）

### 5.2 批量执行服务（新建 `src/rule_backtest/batch_service.py`）

```
run_batch(batch_id, cancel_event):              # cancel_event: threading.Event（内存）
    batch = db.get_batch(batch_id)
    symbols = 按 categories 查 instrument_metadata（含 name/l2/l3/asset_type）
    anchor  = batch.data_anchor_date            # 建批次时已锚定
    for symbol in symbols:
        if cancel_event.is_set(): break
        db.update(current_symbol=symbol)
        bars = MarketStore().load_history(symbol)        # 返回 time 列（datetime64）
        feat_bars = bars[bars["time"] <= anchor]         # 特征计算用截断
        if len(feat_bars) < MIN_BARS(60):
            for strategy in snapshot:                    # 逐策略各写一行
                write_cell(skipped); db.update(done_cells+=1, skipped_cells+=1)
            continue
        if cancel_event.is_set(): break                  # 加载后再查一次
        features = compute_features(feat_bars, symbol)   # 写特征表
        instrument_type = "stock" if asset_type=="stock" else "etf"
        for strategy in batch.strategy_snapshot:
            try:
                result = engine.run(RuleBacktestRequest(
                    strategy=strategy["strategy_config"], bars=bars,
                    start_date=None, end_date=anchor,      # 引擎内部过滤，无需手动截断
                    sizer=None,
                    execution=BacktestExecutionConfig(instrument_type=..., fee_min=5.0, ...)))
                write_cell(ok, 提取指标列+派生 excess+blobs)   # 逐格 commit
            except Exception as e:
                write_cell(failed, error=str(e))              # continue
            del result                                       # 显式释放大 dict
            db.update(done_cells+=1, ok/failed_cells+=1)
            if cancel_event.is_set(): break
    finish(cancelled if cancel_event.is_set() else completed)
```

- 执行参数与单标的回测默认值对齐：initial_capital=100000、slippage=0.002、fee_rate=0.0000854、fee_min=5.0、lot_size=100、stock_stamp_tax_rate=0.001（`BacktestExecutionConfig` 已含 instrument_type/fee_min，`models.py:20-33` 现成）。
- **耗时预估（v1.2 已实测回填）**：单格耗时随 bar 数近似线性（约 0.00027s/根）：<500 根 ~0.09s、500-2000 根 ~0.35s、2000-5000 根 ~0.82s、5000+ 根 ~1.71s（max 2.13s，2026-07-26 分层抽样 20 标的实测，`scripts/bench_backtest_timing.py`）。全标的池 713 × 6 策略外推约 **30~35 分钟**；股票类目 264 × 6 约 12~20 分钟。极端场景（全类目+10 策略）约 1 小时级，非数小时。发起区按 bar 数四档预估（0.1/0.4/0.9/1.8s）。

### 5.3 API（新建 `src/app/routers/batch_backtest.py`）

| 端点 | 说明 |
|---|---|
| `GET /batch-backtest` | 页面 |
| `GET /batch-backtest/api/meta` | L1 类目（含标的数）+ 策略列表 |
| `POST /batch-backtest/api/run` | {categories, strategy_ids, name?} → **校验层**：拒绝含 random_uniform 的策略 → **事务内** check-and-insert（无 running 否则 409，防并发竞态）→ 锚定 data_anchor_date/data_version → 建批次起线程 |
| `GET /batch-backtest/api/progress/{batch_id}` | 轻量进度（含 current_symbol、各状态计数） |
| `POST /batch-backtest/api/cancel/{batch_id}` | 置内存 cancel_event |
| `GET /batch-backtest/api/runs` | 历史批次列表 |
| `GET /batch-backtest/api/runs/{batch_id}/cells` | 全部格子维度+指标列（不含 blob）LEFT JOIN 特征——4 视图共用 |
| `GET /batch-backtest/api/runs/{batch_id}/cell?symbol=&strategy_id=` | 单格 blob 明细（query param 风格，避免路径特殊字符问题） |
| `GET /batch-backtest/api/runs/{batch_id}/snapshot?strategy_id=` | 返回批次快照中某策略的 strategy_config + 格子起止日期（钻取链路用） |
| `DELETE /batch-backtest/api/runs/{batch_id}` | 级联删 cells+features；running 批次拒绝（先取消） |

### 5.4 前端（新建 `web/templates/batch_backtest.html`，导航加入 `base.html`）

- **发起区**：类目/策略 checkbox（含随机指标策略标注）、**分档耗时预估**（超阈值警示）、运行按钮（running 期间禁用）、进度条（含当前标的）、取消按钮
- **批次列表**：时间/名称/类目/策略数/状态（含 interrupted）/成败计数/数据锚定日；点击载入；重跑（预填配置新建批次）；删除（确认）
- **结果区 4 个零耦合 Tab**：
  1. **明细大表**：**增量渲染**（首屏 200 行，滚动到底追加；非虚拟滚动）；排序/筛选；默认 bar_count≥250 过滤（可关）；零交易格子标识；点击行钻取
  2. **透视热力图**：中位数聚合；每格标 n；n<5 置灰；指标/类目级可切
  3. **散点**：x=特征、y=超额（年化/总收益切换）；按类目着色；特征 null 的标的剔除
  4. **特征分桶**：三分位分桶对比；无特征标的归「无特征」组
  - 每个分析 Tab 顶部固定描述性提示横幅
- **钻取链路（v1.2）**：点击格子 → 同/新标签页均可 → `/market-view?drill=1&batch_id=X&strategy_id=Y&symbol=Z` → market_view 调 `GET /batch-backtest/api/runs/{batch_id}/snapshot?strategy_id=` 取快照 strategy_config + 格子起止日期 → 预填并用快照直接构造回测请求（跳过 StrategyLoader）自动运行；快照接口 404/数据异常 → toast 提示并回退常规模式（不白屏）。

### 5.5 测试

- `tests/unit/test_batch_backtest.py`：格子级失败隔离、短数据 skipped（逐策略写行+计数）、快照执行、指标列提取与 excess 派生、blob 不含大字段、DB 往返、取消竞态、空数据 graceful、随机指标策略拒绝、特征计算（含 trend_daily 无行 → null）
- `tests/unit/test_batch_golden.py`：**真·端到端 golden**——`tests/fixtures/golden_bars.csv` 固定行情 + 固定策略快照 + 固定区间，锁定 summary 数值（CSV fixture 保证任何环境可复现）
- `tests/api/test_batch_backtest_api.py`：run（含 409 冲突与随机指标 400）/progress/runs/cells/cell/snapshot/cancel/delete 保护
- 小规模端到端 smoketest：商品类目 5 标的 × 1 策略全流程

### 5.6 实施顺序

0. **耗时实测脚本**：先 `SELECT count(*) FROM market_data_qfq GROUP BY symbol` 看 bar 数分布，按 bar_count 分层抽样（<500 / 500-2000 / 2000-5000 / 5000+ 各 5-10 个）× 1 策略计时，产出耗时分布回填方案并校准分档预估；同步修正 README:47「golden-master 锁定」措辞为「新旧实现逐笔一致（等价性测试锁定）」；启动时孤儿批次清理（app lifespan）
1. db.py 三表 + CRUD → 2. batch_service.py + 单测 → 3. router + API 测试 → 4. 批次页（发起/进度/批次列表/明细大表）→ 5. market_view 钻取模式 → 6. 三个分析 Tab 逐个加 → 7. 小类目端到端验证

### 5.7 明确不做（本期）

多进程/分布式；批次间对比视图；统计显著性检验；断点续跑（预留 interrupted 状态）；B&H 基准口径修正（仅注明）；hung 批次 watchdog；不存 daily_nav/K线/condition_trace。

### 5.8 实施验收清单（评审备注汇总，实施时逐条核对）

- [ ] 409 并发保护在**事务内** check-and-insert（或部分唯一索引兜底）
- [ ] skipped 标的：逐策略各写一行，done_cells/skipped_cells 按格子递增（进度不停滞）
- [ ] cells 端点 LEFT JOIN 特征表；trend_score_avg 可空；视图 null 降级
- [ ] engine_version MVP 填 `'1.0'`，取 git hash 失败有 fallback（不得 500）
- [ ] 取消标志为内存 threading.Event（不落库；重启走 interrupted 路径）
- [ ] 随机指标校验在 POST /run 校验层（立即 400），策略列表 UI 加标注
- [ ] bars 用 `time` 列；引擎侧只传 end_date=anchor；手动截断仅用于特征计算
- [ ] blob 明细端点用 query param（?symbol=&strategy_id=）
- [ ] 特征公式写进 compute_features() docstring（momentum_250/amount_ma20/trend_score_avg 口径如 §5.1）
- [ ] golden 测试用 tests/fixtures/golden_bars.csv，不依赖真实 DB
- [ ] 钻取快照接口异常时 market_view 回退常规模式 + toast，不白屏
- [ ] 明细大表增量渲染（200 行/批），不做完整虚拟滚动
- [ ] 发起区耗时按 bar 数分档预估（<1500 根 0.5s、≥1500 根 3s，实测后校准）
- [ ] 逐格 commit + del result；data_anchor_date 在建批次时锚定

---

## 6. 风险与已知限制

1. **钻取重跑漂移**：透传日期+快照后，残余来源为历史数据被改写（复权因子/数据修正）与指标实现变更——静态提示 + engine_version 追溯。
2. **可比性残余**：年化/超额归一不能完全消除区间差异；B&H 无摩擦口径使超额系统性偏低（已注明）；视图展示区间/bar_count 供人工判断。
3. **假规律风险**：以 n 标注+n<5 置灰+横幅+默认过滤做最低限度防护，不含显著性控制。
4. **批次中断**：逐格 commit 保证已算格子不丢；重启后批次标 interrupted，可重跑（无续跑）。
5. **SQLite 并发**：WAL 模式下批量写与每日更新并发预计无碍，真实环境验证。
6. **耗时不确定**：典型场景 15~40 分钟，最坏场景（全类目+长历史标的集中）可达数小时——分档预估警示 + 预留分片接缝 + 第 0 步实测校准。
7. **hung 批次**：引擎级死循环下协作式取消无效（概率低）；V2 加 watchdog/force 取消。

---

## 7. 待解决的开放问题（v1.2 收敛后）

1. **并行扩展接缝**：BatchBacktestService 循环边界预留「按标的分片」的 executor 抽象（默认串行实现），待实测耗时分布后决定是否在 V2 启用多进程（多进程下 SQLite 写需切队列+单 consumer 模式）。
2. **批次间对比**：同一策略跨批次的结果演变（DB 设计已支持，含 data_version 钩子，视图 V2）。
3. **断点续跑**：interrupted 批次的 resume（V2，需跟踪未完成格子）。
4. **B&H 基准口径**：是否给基准补首日买入摩擦（会改变现有单标的回测展示，需单独评估）。
5. **alpha/beta 回归超额**：需基准指数，V2 增强。
6. **engine_version 真实化**：git hash 注入方式（部署脚本写文件 vs 构建时注入），V2。

---

## 实施完成记录（2026-07-26）

**交付物**：

- 后端：`src/rule_backtest/batch_service.py`（执行服务）、`src/app/routers/batch_backtest.py`（路由）、`src/data/storage/db.py`（三表 + CRUD + `mark_interrupted_batch_runs` + `get_market_data_anchor` + `count_bars_by_symbol`）、`src/rule_backtest/service.py` + `src/app/routers/rule_backtest.py`（`strategy_config` 快照回测支持）、`src/app/main.py`（路由注册 + 启动孤儿批次清理）
- 前端：`web/templates/batch_backtest.html`（发起/进度/批次列表/明细大表/透视热力图/散点/分桶四个零耦合 Tab）、`web/templates/market_view.html`（钻取快照模式 + 横幅）、`web/templates/base.html`（导航）、`web/static/style.css`（样式）
- 测试：`tests/unit/test_batch_backtest.py`（20）、`tests/unit/test_batch_golden.py`（2，CSV fixture 端到端 golden）、`tests/api/test_batch_backtest_api.py`（8）
- 工具：`scripts/bench_backtest_timing.py`（分层耗时实测）

**第 0 步实测回填**（20 标的分层抽样）：单格耗时 ≈ 0.00027s/根 —— <500 根 0.09s、500-2000 根 0.35s、2000-5000 根 0.82s、5000+ 根 1.71s（max 2.13s）。全标的池 713×6 策略外推约 30~35 分钟。ETA 按四档（0.1/0.4/0.9/1.8s）落入 meta 端点与发起区预估。

**端到端验证**（真实库，商品类目 5 标的 × MACD 策略）：批次 5/5 ok 完成；格子指标/特征/blob 齐全；**快照重跑与批次格子完全一致**（年化 0.14806639 逐位一致、交易 133 笔一致）；随机指标策略 400 拦截；浏览器实测钻取链路（URL → 快照 → 自动重跑 → 渲染）成功。

**实施中发现并修复的问题**：

1. **`time` 列格式**：生产库存的是 `'YYYY-MM-DD 00:00:00'`，`date.fromisoformat` 会报错，锚定解析改用 `pd.Timestamp`（复审只猜到格式是日期，实际带时间后缀）。
2. **顶层 `from data.storage.db import get_db` 的测试陷阱**：API 测试 conftest 在 patch 生效后才导入 app，顶层绑定会捕获**上一个测试的 monkeypatch lambda** 并随模块缓存常驻，导致请求读到上一个测试的 DB。新代码一律用 `db_module.get_db()` 惰性属性访问；`main.py` 的 lifespan 同步改为 `db_module.init_db()`（顺带消除了 API 测试期间 lifespan 打开生产库并写入的隐患）。
3. **测试基线**：`tests/api/test_instruments_update_api.py`、`test_trade_records_api.py`、`test_manual_trade_api.py`、`tests/test_instruments_bulk_backfill.py`、`tests/test_rule_backtest_engine.py`、`tests/unit/test_rule_backtest_progress.py` 在 master 上即不稳定（隔离问题，失败集合随运行变化），与本功能无关；本功能 30 个新测试全绿，全量 459 passed 无回归。
4. **IAB 浏览器输入投递在此环境不可用**（playwright/dom_cua 点击均无法到达页面，读取/快照/导航正常）：批次页的点击交互（载入批次、Tab 切换、行钻取）未能自动化验证，逻辑为标准 DOM 代码，建议人工验收时点一遍。

**§5.8 验收清单落实情况**：全部 14 条已落实，例外说明——明细大表用增量渲染（200 行/批滚动加载）；engine_version 按 MVP 决策填固定 `'1.0'`；取消用内存 `threading.Event`（不落库，重启走 interrupted）。
