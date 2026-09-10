# E-Bias（均线偏离度·减法版）落库 + 展示 设计方案

> 状态：**已落地**（2026-09-10 开发完成，阶段 1-3 全部实现并自测通过；阶段 4/5 仍为可选后续）
> 日期：2026-09-10
> 实施记录与偏差说明见文末 §11；线上部署手册见同目录 `deploy-notes-2026-09-10.md`
> 触发来源：广发策略刘晨明「均线偏离度」指标（除法版 → 减法版两代），经 earletf 复刻与心得传播后，希望纳入本项目。
> 三篇原文：
> - 《【广发策略】如何区分主线是调整还是终结？》2025-09-14（除法版提出）
> - 《【广发策略】6大指标看居民入市：温度几何？》2025-09-21（减法版 + 阈值修订）
> - earletf《刘晨明乖离率怎么用，我的一点心得》（通达信复刻公式 + 用法心得）

---

## 1. 目标与范围

### 1.1 要做

1. 新增指标 **E-Bias**（减法版，见 §2），**落库**到 `indicator_daily`，避免每次重算。
2. 标的查看页（`/market-view`）在 BIAS 副图下方新增 **E-Bias 副图**。
3. 标的大盘（`/subject-market`）热力图新增 **E-Bias 着色维度**（现有着色维度为 趋势 MA5 / 当日趋势值 / 日涨跌幅）。
4. 同步暴露给 MCP `symbol_detail`（随 §4 第 2 步自动获得）。

### 1.2 明确不做

| 不做的事 | 原因 |
|---|---|
| **不实现除法版** `(ln C / EMA20) − 1` | 本项目标的全是 ETF，含 1 元附近低价品种。除法版分母是 `ln(EMA20)`，在 1 附近除零、小于 1 时正负号翻转（earletf 原文明确指出）。减法版是唯一可用口径。 |
| **不动现有 `bias`**（`core/indicators.py:270`，SMA 算术乖离） | 前端 BIAS6/12/24 副图口径不变。 |
| **不动趋势值内部的 bias**（`core/trend.py:136-147`，`(close − MA_n)/ATR`） | 趋势值算法冻结，`TREND_FORMULA_VERSION` 不变，回测 golden 不受影响。 |
| **不做买卖信号 / 不进回测 DSL** | 本次目标为「看得见」，不做交易规则。回测注册表（`rule_backtest/registry.py`）不动。 |
| **不改盘中快照以外的数据链路** | 见 §5 的分工说明。 |

### 1.3 一个必须写进代码注释的提醒

本项目的 `bias` 家族将有**三套语义完全不同的"乖离率"**，极易混淆：

| 名称 | 公式 | 位置 |
|---|---|---|
| `bias` | `(close − SMA(n)) / SMA(n)` | `core/indicators.py:270` |
| `bias_atr_normed` | `(close − SMA(n)) / ATR(m)` | `rule_backtest/indicators.py:72` |
| **`e_bias20`（新增）** | `ln(close) − EMA(ln close, 20)` | 本次新增 |

另注：`batch_backtest_cells` 表里的 `survivorship_bias` / `bias_disclosures` 是**幸存者偏差披露字段**，与乖离率无关，勿混。

---

## 2. 指标定义（冻结口径）

```
E-Bias(period=20) = ln(Close) − EMA(ln(Close), period)
```

### 2.1 口径决策（已定）

| 项 | 取值 | 依据 |
|---|---|---|
| 对数 | **自然对数**（`np.log`） | earletf 原文：「这里使用的都是自然对数，也就是 Ln，除法版没妨碍，但是减法版，用 Log 和 Ln 会有巨大差别」 |
| EMA 作用对象 | **`EMA(ln C)`**，不是 `ln(EMA(C))` | 通达信公式 `EMA20 := EMA(LN(CLOSE), 20); LOGBIAS: (LN(CLOSE) - EMA20) * 100;` 是这一口径。两者近似但不等，要复刻 earletf 的图必须选这个。 |
| EMA 参数 | `span=period`, `adjust=False`，预热 `min_periods=0` | 与 `core_ind.ema()` 默认一致（`core/indicators.py:34`）。`min_periods=0` 下无 NaN 空洞，最早一根起有值。 |
| 周期 | **固定 20** | 刘晨明原文明确用 20 日 EMA；耳熟能详的用法与阈值全部基于 20。与项目既有 `sma20` / `rsi14` 的「周期入列名」约定一致。 |
| 数值单位 | **decimal**（如 `0.05` 表示 +5%） | 遵守 `core/indicators.py:8` 的锁定语义「BIAS: decimal ratio (presentation layer multiplies by 100 when needed)」。**展示层统一 ×100**。这是最容易踩的坑：前端显示 / 参考线用 `%`，而落库值与阈值判断用 decimal。 |
| 非正价格防护 | `close.where(close > 0)` 后再取对数 | ETF 价格恒为正，属防御性处理，避免 NaN/−inf 污染落库值。 |

### 2.2 数学性质（决定了它与现有 bias 不可互换）

1. **近似等于对数收益率偏离**：`ln(C) − ln(E) = ln(C/E) ≈ C/E − 1`。当偏离 5% 时两者差 0.12%，量级上可直接读作「price 高于均线百分之几」。
2. **尺度无关**：加法版不随标的价格水平漂移。以 `ln(EMA20) ≈ 6.9`（千点级指数）换算，除法版的 `2%` 过热线 ≈ 减法版的 `15%`，`0.6%~1.8%` 入场区 ≈ `5%~15%` —— 两版阈值差约 6~7 倍正是这个换算系数，**验证了两版是同一件事的两种刻度**，也解释了除法版为何在低价标的上刻度会失效。
3. **与 `bias` 的差别有两处**：EMA 替代 SMA（更灵敏，刘晨明选它的理由），以及对数替代算术差（尺度无关）。

### 2.3 参考线（仅作图，非信号）

earletf 给出的参考线：`过热线 15` / `失速线 5` / `止损参考线 −5` / `零轴 0`。

> ⚠️ **阈值未经本项目标定**：刘的阈值来自 **2012 年以来 A 股行业指数**（半导体、新能源车、光模块、PCB、创新药等）的分组统计，原文风险提示亦自认「行业自身属性的不同，可能也会导致均线偏离度的波动不同」。本项目是 ETF 池（含宽基、跨境、债券、商品），波动特性不同。
> **本次处理**：参考线照画，但图例/说明文字必须标明来源与未标定状态（见 §4.3），不得暗示这是本项目验证过的信号线。标定工作列入 §10 阶段 4。

---

## 3. 落库设计

### 3.1 缓存表变更（`indicator_daily`）

新增一列 `e_bias20 REAL`。变更点共 **5 处**，缺一不可：

| # | 文件:行 | 改动 |
|---|---|---|
| 1 | `src/data/storage/db.py:317-334` | `indicator_daily` DDL 加列（新库路径）。加在 `macd_ema26 REAL,` 之后。 |
| 2 | `src/data/storage/db.py:506-511` | `indicator_columns` 字典加 `"e_bias20": "REAL"`（存量库 `ALTER TABLE ADD COLUMN` 幂等迁移，`db.py:559-565` 执行）。 |
| 3 | `src/data/storage/db.py:1900-1907` | `save_indicator_daily` 的 `columns` 元组加 `"e_bias20"`。 |
| 4 | `src/data/storage/db.py:1916-1924` | INSERT 的列名列表加 `e_bias20`，**且 `VALUES` 的 `?` 个数从 28 加到 29**（易漏，两处必须同步）。 |
| 5 | `src/data/indicator_store.py:28-37` | `INDICATOR_COLUMNS` 加 `"e_bias20"`。 |

**无需改动**（已确认）：
- `load_indicator_daily_many`（`db.py:1939`）：用 `isidentifier()` 白名单，任意合法列名可用。
- `load_indicator_latest`（`db.py:2006`）：`SELECT t.*`。
- `load_indicator_daily`（`db.py:1929`）：`SELECT *`。

### 3.2 计算实现

**`src/core/indicators.py`** 新增（放在 `bias()` 之后，`momentum_return()` 之前）：

```python
def e_bias(close: pd.Series, period: int = 20) -> pd.Series:
    """ln(close) − EMA(ln(close), period) — decimal ratio.

    减法版均线偏离度。取 EMA(ln C) 而非 ln(EMA C)（通达信口径）。
    与 ``bias`` 的区别：EMA 替代 SMA，对数替代算术差 → 尺度无关。
    """
```

**`src/data/indicator_store.py:48` `compute_indicator_frame`** 的 `pd.DataFrame({...})` 里加一行 `"e_bias20": core_ind.e_bias(close, 20),`。

**`src/data/indicator_store.py:123` `compute_live_series`** 加分支：

```python
elif indicator == "e_bias20":
    out = core_ind.e_bias(close, 20)
```

> **必须加这个分支**。漏掉的话，缓存未命中时的 live 回退会走 `else: raise ValueError(f"unknown indicator: ...")`（`indicator_store.py:157`）。

### 3.3 ⚠️ 必须 bump `INDICATOR_FORMULA_VERSION`

`src/core/indicators.py:24` 由 `2` → `3`。

**这不是可选项，是正确性要求。** 原因：`_cache_fresh`（`indicator_store.py:186`）判定新鲜度的条件是「版本号一致 + 缓存末尾日期 ≥ 行情末尾日期」。若不 bump：

1. `ALTER TABLE` 已把 `e_bias20` 列加进表（值全为 NULL）；
2. 版本号没变 → `_cache_fresh` 返回 True → `get_series` 走缓存分支；
3. `frame["e_bias20"]` 存在但全 NaN → **`get_series` 静默返回全 NaN 序列，永远不会触发 live 回退**。

即：指标会「安静地永远为空」。bump 之后由 `rebuild_if_needed`（`src/services/indicator_builder.py:109-129`）在服务启动时检测到 `indicator_stale` → 自动 `backup_to()` 备份 → `rebuild_all()` 全量重建，行为与既有公式变更完全一致。

### 3.4 大盘命中路径（满足「避免每次重算」）

落库后的读取走既有的 cache-first 门面，不新增读取机制：

- **单标的**：`get_series(symbol, "e_bias20", db)`（`indicator_store.py:193`）。
- **全市场（标的大盘用）**：`get_series_bulk(symbols, "e_bias20", db, bars_map)`（`indicator_store.py:271`）。这个批量接口就是为「一次上百只标的」场景写的（原用途是 ATR 批量止损试算），SQL 次数与标的数解耦。**标的大盘应走这条，不要逐只 `get_series`。**

---

## 4. 展示层 A：标的查看页副图

### 4.1 后端

| # | 文件:行 | 改动 |
|---|---|---|
| 1 | `src/services/market_indicators.py:18-23` | 加常量 `E_BIAS_PERIOD = 20` 与参考线常量 `E_BIAS_LINES = (15.0, 5.0, -5.0, 0.0)`。 |
| 2 | `src/services/market_indicators.py:107-116` | 返回 dict 加 `"e_bias": {"series": _series(core_ind.e_bias(close, E_BIAS_PERIOD) * 100), "period": E_BIAS_PERIOD, "lines": list(E_BIAS_LINES)}`。`×100` 转百分比，与 `bias` 的处理（`:92`）一致。 |
| 3 | `src/app/routers/market_view.py:211-222` | `meta` 加 `"e_bias_period": E_BIAS_PERIOD`（并在 `:21` 的 import 名单里加常量）。 |

改完第 2 步，MCP `symbol_detail` 自动获得该字段（`services/symbol_detail.py` 与 Web `/market-view/api/daily` 共用 `compute_market_indicators`）。
另需更新 MCP 契约文档字符串 `src/trend_mcp/server.py:141-146`（该 docstring 是唯一的字段契约声明处）。

**无需改动**：`market_view.py:110 _tail_payload_arrays` 的截尾白名单按「长度 == 全历史长度」通用判定，新等长序列自动被正确处理。

### 4.2 前端

| # | 文件:行 | 改动 |
|---|---|---|
| 1 | `web/templates/market_view.html:132-135` 之后 | 仿 `mvBiasWrap` 加 `mvEBiasWrap` 块（含 `.market-subchart-title` 文字「E-BIAS」+ `<div id="mvEBiasChart">`）。CSS 复用既有 `.market-subchart-wrap` / `.chart-md`，无需新增。 |
| 2 | `web/static/js/market_view.js:64-80` | 加 `let eBiasChart = null;`、`echarts.init`、并入 `charts` 数组（`:78`），共用 `group='market-view-time'` 的时间轴联动。 |
| 3 | `web/static/js/market_view.js`（`renderBias` 之后，约 `:984`） | 新增 `renderEBias(payload, zoom)`，仿 `renderBias`（`:964-983`）骨架：`payload.indicators?.e_bias?.series`、`yAxis.axisLabel` 用 `%`、tooltip `valueFormatter` 用 `%`。 |
| 4 | `web/static/js/market_view.js` `renderAll`（`:1503-1512` 调度区） | 在 `renderBias(payload, zoom);` 之后加 `renderEBias(payload, zoom);`。 |

**参考线写法**：项目已有现成模式 —— `renderRsi`（`market_view.js:852-861`）用 `markLine` 画 70/50/30 三条横线。照此写 4 条：`15`（过热，红）/ `5`（失速，黄）/ `-5`（止损参考，绿）/ `0`（零轴，灰），与 earletf 通达信公式的配色一致。

### 4.3 参考线的诚实标注（重要）

因为阈值未在本项目标定（见 §2.3），图例与 tooltip 必须体现来源。建议：

- 图例条目命名为 `15 过热*` / `5 失速*` / `-5 止损*`；
- 图下方或 `title` 属性加一行说明：**「参考线取自广发策略行业指数口径，未在本 ETF 池标定」**；
- 不要用「止损线」「买卖线」这类暗示已验证的措辞。

理由：本项目是自用实盘系统，一条被误读为「已验证信号」的线比没有线更危险。

---

## 5. 展示层 B：标的大盘热力图

### 5.1 关键分工：热力图靠**盘中快照**，副图靠**实时计算**

先明确两条独立链路（**互不复用**，这是本项目的既有结构）：

| 链路 | 入口 | 数据来源 | 本次是否要改 |
|---|---|---|---|
| 标的查看页副图（§4） | `market_view.py` → `services/market_indicators.py` | **每次请求全历史实时重算**，不读缓存 | 只加计算与渲染 |
| 标的大盘热力图（§5） | `dashboard_snapshot.py:402 dashboard_payload` | 交易时段 = **每 5 分钟盘中快照**（`intraday_service.py`）；盘后 = EOD（`services/dashboard.py`） | **两条分支都要加字段** |

所以「落库避免重算」的收益落在**大盘扫描侧**，而查看页副图遵循既有行为（实时算）即可保持一致性 —— 这点需要在评审时对齐预期：**落库不是为了省查看页那一次计算**。

### 5.2 EOD 分支（`src/services/dashboard.py`）

| # | 文件:行 | 改动 |
|---|---|---|
| 1 | `src/services/dashboard.py:287-320` | `build_subject_dashboard_payload` 里，用 `get_series_bulk(symbols, "e_bias20", db=db)` 取全市场序列（与 `trend_score` 的 bulk 取值并列），按日期对齐写入 `data["e_bias_pct"]`（decimal 缓存列 ×100 转百分比）。 |
| 2 | `src/services/dashboard.py:82-90` | `_aggregate_daily` 的 `metrics` 字典加 `"e_bias_pct": "e_bias_pct"` —— **这一步让 L2/L3 类目行也获得成交额加权的 e_bias**，与 `trend_score` 的聚合口径一致。 |
| 3 | `src/services/dashboard.py:130-158` | `_metrics_summary` 返回 dict 加 `"e_bias_pct": _number(latest["e_bias_pct"])`。 |

> 字段名定型为 `e_bias_pct`（不是方案初稿写的 `e_bias`）：看板 payload 里
> 百分比字段用 `_pct` 后缀（`daily_change_pct`），而查看页 payload 里百分比
> 无后缀（`bias`）—— 各自与**同一 payload 内的兄弟字段**保持一致，且两处
> 都是百分比，可跨接口直接比较。详见 §11 第 2 条。

### 5.3 盘中快照分支（`src/data/intraday_service.py`）

| # | 文件:行 | 改动 |
|---|---|---|
| 1 | `:674-688` 附近 | 计算标的级 e_bias。**复用已有的 `bars`**（1 年 qfq 尾部 + 当日合成K线），一行 `core_ind.e_bias(bars["close"], 20)` 的最后值即可 —— 与 MACD 相位/K线 mini 同一份数据，增量成本近乎为零。 |
| 2 | `:697-747` | `instrument_rows.append({...})` 加 `"e_bias_pct": e_bias_pct`（decimal ×100 后的百分比）。 |
| 3 | `:803-823` `_weighted_avg_intra` | 无需改动（通用函数，传列名即可）。 |
| 4 | `:825-870` `_metrics_summary_intra` | 加 `avg_e_bias = _weighted_avg_intra(rows_df, "e_bias_pct")`，并写入结果 dict：类目行走加权值，标的行走原值。 |

> ⚠️ **实现注意（`bars` 的作用域）**：`bars` 在 `:677-684` 的 `if macd_src is not None and not macd_src.empty:` 块内赋值。若某标的在该迭代命中 else 分支，`bars` 会**残留上一轮迭代的值**（Python 循环变量泄漏）。既有代码对此是安全的，因为它把 `macd_phase_info` / `kline_payload` / `macd_mini_payload` 的默认值提到了 `if` 之前（`:672-674`）。**e_bias 必须照做**：在 `if` 之前先 `e_bias_pct = None`，仅在块内计算赋值。已按此实现，并有专门的回归测试 `test_e_bias_does_not_leak_across_symbols` 钉住。

### 5.4 前端

| # | 文件:行 | 改动 |
|---|---|---|
| 1 | `web/templates/subject_market.html:11` | `heatmapDim` 里加第 4 个按钮：`<button type="button" role="radio" aria-checked="false" class="heatmap-dim-btn" data-dim="ebias">E-Bias 偏离度</button>`。 |
| 2 | `web/static/js/subject_market.js:63-69` | `HEAT_COLOR_DIMS` 加一条 `ebias: { field: 'e_bias_pct', maxAbs: 20, note: '最新 E-Bias 均线偏离度（%）', fmt: (m) => fmtEBias(m.e_bias_pct) }`；新增 `fmtEBias`（带正负号、1 位小数、`%`）；叶子与类目 tooltip 各加一行「E-Bias 偏离度」。 |

- `maxAbs: 20` 与既有两个维度一致。换算关系：5% → 色阶 5（中间档），15% → 色阶 15（接近最深）。若希望 15% 即饱和，改 `maxAbs: 15` 即可（一行）。
- `fmtScore`（`subject_market.js:11`）是 `toFixed(1)` 无单位，不适合偏离度，故新增 `fmtEBias` 输出带符号百分号（如 `+5.2%`）。
- 叶子/类目取值路径已通用（`buildHeatTree` 读 `inst[dim.field]`，`:123` / `:134` / `:142`），无需为 E-Bias 特判。
- **图例文案问题**：现有图例是「下跌 / 上涨」（`subject_market.html:14-15`），对偏离度语义不准确（正偏离不等于「上涨」）。本次可接受的折中是在 `heatmapColorNote`（`:23`）里把维度说明写清；若要严谨，需按维度切换图例文案，属可选优化。

---

## 6. 改动清单汇总

### 计算与落库（6 处）

| 文件 | 改动 |
|---|---|
| `src/core/indicators.py:24` | bump `INDICATOR_FORMULA_VERSION` 2 → 3 |
| `src/core/indicators.py`（`bias` 后） | 新增 `e_bias()` |
| `src/data/indicator_store.py:28-37` | `INDICATOR_COLUMNS` 加 `e_bias20` |
| `src/data/indicator_store.py:48` | `compute_indicator_frame` 加计算 |
| `src/data/indicator_store.py:123` | `compute_live_series` 加分支（漏则 live 回退 raise） |
| `src/data/storage/db.py:317 / 506 / 1900 / 1916` | DDL + 迁移字典 + columns 元组 + INSERT 列名与 `?`（28→29） |

### 标的查看页（7 处）

`services/market_indicators.py:18-23`、`:107-116`｜`app/routers/market_view.py:21`、`:211-222`｜`trend_mcp/server.py:141-146`（docstring）｜`web/templates/market_view.html:135` 后｜`web/static/js/market_view.js:64-80`、`renderBias` 后新增函数、`renderAll` 调度

### 标的大盘热力图（7 处）

EOD：`services/dashboard.py:82-90`、`:130-158`、`:287-320`

盘中：`data/intraday_service.py:674-688`、`:697-747`、`:825-870`

前端：`web/templates/subject_market.html:7-12`、`web/static/js/subject_market.js:63-68`（+ 新增 `fmtEBias`）

### 测试（5 个文件）

见 §7。

---

## 7. 测试计划

| 文件 | 补什么 |
|---|---|
| `tests/unit/test_core_indicators.py` | ① 在测试内**冻结一份参考实现**做对拍（仿 `:95 ref_market_bias` + `:209 test_bias_decimal_vs_market_percent` 的 golden-master 约定）；② 断言 `EMA(ln C)` 口径（对拍 `ln(EMA(C))` 会不同，用一条会区分的用例锁死）；③ 边界用例进 `TestEdgeCases`（`:242`）：空输入、全 NaN、单元素、`period` 大于数据长度、含非正价格。 |
| `tests/integration/test_indicator_store.py` | 落库往返（save → load 值一致）、缓存命中、版本不匹配触发重建、`compute_live_series("e_bias20")` 不 raise。 |
| `tests/test_market_view.py:34`、`:56` | 把 `"e_bias"` 加进「指标序列长度必须与 dates 对齐」的 group 元组。 |
| `tests/api/test_market_view_window.py:51`、`:69` | 同上，并把 e_bias 纳入「指标不依赖 limit、先全量算再截尾」的断言。 |
| 大盘侧（建议新增或扩展 `tests/unit/test_intraday_trend_consistency.py` 一类的双实现一致性测试） | EOD 与盘中两条分支的 `e_bias` 字段都存在、量级一致（不要求 bit-identical：盘中用 1 年尾部，缓存用全历史，EMA20 数百根即收敛，差异可忽略 —— 这与 MACD 相位/K线 mini 的既有口径相同）。 |

**注意**：本次不进 `rule_backtest`，因此 `tests/unit/test_p13_memoized_golden.py` 与回测 golden 不受影响；但 `INDICATOR_FORMULA_VERSION` 的 bump 会让 `indicator_daily` 全量重建，需确认 `tests/integration/test_indicator_builder.py` 中的版本相关断言同步更新。

---

## 8. 风险与陷阱清单

1. **不 bump 版本号 → 指标静默全空**（§3.3）。最容易漏、后果最隐蔽的一条。
2. **`VALUES` 的 `?` 个数**：INSERT 列名列表与占位符必须同步，`db.py:1916-1924` 两处相邻但易只改一处 → 运行时 `sqlite3.ProgrammingError`。
3. **decimal vs percent 口径**：`bias` 的既有处理是「core 出 decimal、展示层 ×100」（`market_indicators.py:92`）。E-Bias 必须一致。若落库值用百分比而展示层又 ×100，会差 100 倍 —— 而参考线 15/5/−5 是按百分比画的，症状是「线永远在最上面/最下面」。
4. **盘中快照的 `bars` 变量作用域**（§5.3）。照既有 `macd_phase_info` 的模式，默认值提前初始化。
5. **`get_series_bulk` 只支持 `INDICATOR_COLUMNS`**（`indicator_store.py:284` 硬校验），trend 列不支持。`e_bias20` 进 `INDICATOR_COLUMNS` 后即可用，但要记得它是 indicator 列而非 trend 列。
6. **前端检查脚本**：`scripts/check_frontend_js.py` 会用 DOM stub 加载全部 `web/static/js/*.js`，新增函数若在加载期抛错会被 CI 抓到（这是好事，不用额外处理）。
7. **阈值误读风险**（§2.3 / §4.3）：参考线必须标注未标定。

---

## 9. 待拍板的开放问题

| # | 问题 | 倾向 |
|---|---|---|
| 1 | 列名与展示名。你提议 `E_Bias`。DB 列名受 snake_case 约束，建议列名 `e_bias20`（周期入名，与 `sma20`/`rsi14` 一致），API/前端字段 `e_bias`，展示标签「E-BIAS（均线偏离度）」。 | 按此执行 |
| 2 | 是否屏蔽 EMA 预热区（最早约 60 根）。既有 `ema()` 默认 `min_periods=0`，不产生 NaN。 | **不屏蔽**，与既有指标口径一致；仅需知晓最早若干根不可信 |
| 3 | 热力图 `maxAbs` 取 20 还是 15。 | **20**（与既有两维一致，且保留 20%+ 极端值的区分度） |
| 4 | 热力图「下跌/上涨」图例文案是否按维度切换。 | **本次不改**，在 `heatmapColorNote` 写清即可；作为可选优化 |
| 5 | 是否顺带实现「最近 20 日中收盘价有 10 日在上方」的广度条件（刘原文里与偏离度并列的一个独立转弱信号）。 | **本次不做**；若要做，宜做成独立指标列（如 `above_ema20_cnt`），不与 e_bias 混 |

---

## 10. 实施阶段

| 阶段 | 内容 | 交付判据 |
|---|---|---|
| **1** | §3 全部（内核 + 落库 + 版本 bump）+ §7 前两个测试文件 | 重启服务后 `indicator_daily.e_bias20` 全市场有值；`get_series`/`get_series_bulk` 返回非空；单测过 |
| **2** | §4 标的查看页副图（后端 + 前端 + 测试） | 打开任一 ETF 详情页，BIAS 副图下方出现 E-BIAS 副图与 4 条参考线；MCP `symbol_detail` 返回该字段 |
| **3** | §5 标的大盘热力图（EOD + 盘中两分支 + 前端） | 盘中与盘后切换着色维度均可用；类目行显示成交额加权值；下钻/悬停正常 |
| **4（可选，另行立项）** | 阈值标定：把 `e_bias` 接入 `rule_backtest` 注册表（`registry.py` + `indicators.py` + `value_resolver.py` 三处 memoized/legacy/warmup + `_MEMOIZABLE_INDICATORS`），用批量回测扫描 3%/5%/7% × −3%/−5%/−7%，在**本项目 ETF 池**上重标 §2.3 的参考线 | 得到本项目口径下的经验阈值，并据此决定是否更新参考线 |
| **5（可选）** | 参考 §2.3 之外的用法落地：盘中快照加入「20 日中 10 日在上方」广度字段；「回抽两步走」三段式形态检测（可仿 `core/indicators.py:117 detect_macd_phase` 的相位检测模式） | 按需 |

> 阶段 4 的定位说明：本次方案让指标「看得见」，但刘的阈值在本项目 ETF 池上是否成立**未知**。阶段 1~3 完成后若你发现参考线读数与实际走势对不上，阶段 4 就是回答这个问题的工具；它不阻塞前三个阶段。

---

## 11. 实施记录（2026-09-10）

### 11.1 完成情况

阶段 1 / 2 / 3 全部实现，阶段 4 / 5 未做（按方案保留为可选项）。
代码改动 **21 个文件、+553/−9 行**（含测试），新增 **20 个测试**
（全量 1080 → 1100 通过）。

### 11.2 实施中相对方案的三处定型决策

1. **`e_bias20` 缓存列是 decimal，两个 payload 都是百分比。**
   `indicator_daily.e_bias20` 遵守 `core/indicators.py` 的锁定约定存 decimal
   （`0.05` = 高于 EMA 5%）。转换只发生在三个出口：`market_indicators.py`
   （查看页/MCP `symbol_detail`，×100）、`dashboard.py` 与
   `intraday_service.py`（看板/MCP `dashboard`，×100）。
2. **看板字段名用 `e_bias_pct`，查看页用 `e_bias`。**
   两个 payload 各自与**内部兄弟字段**保持一致：看板里百分比字段带 `_pct`
   （`daily_change_pct`），查看页里百分比字段无后缀（`bias`）。
   两处**都是百分比**，可跨接口直接比较 —— 已用真实数据验证：同一标的
   在 `/market-view/api/daily` 与 `/subject-market/api/dashboard` 上返回
   `8.076633`，完全一致（不存在 100 倍口径错位）。
3. **参考线标签加半透明底衬。**
   曲线右端就是最新值，指标贴近阈值时 5% 线的标签必被曲线压住 —— 而那时
   恰恰最需要看清。给 `markLine.label` 加了 `backgroundColor:
   rgba(255,255,255,0.82)` + padding，任何取值下都可读。

### 11.3 自测与验证证据

**单测**（新增/扩展）：

| 文件 | 内容 |
|---|---|
| `tests/unit/test_core_indicators.py` | 通达信公式逐值对拍；尺度无关性（乘性缩放逐值不变）；**除法版不具尺度无关性**；**1 元以下标的减法版符号正确而除法版符号颠倒**；`EMA(lnC) ≠ ln(EMA C)` 口径锁定；无预热空洞；空/全 NaN/单元素/短序列/非正价格边界 |
| `tests/integration/test_indicator_store.py` | 落库往返一致；live 回退不 raise；**「列存在但全 NULL」的静默为空回归钉子** |
| `tests/integration/test_db_bulk.py` | `get_series_bulk` == 逐只 `get_series` |
| `tests/integration/test_intraday_service.py` | 盘中值 == 独立复算（历史收盘 + 当日合成K线）；**跨标的 `bars` 残留回归钉子**；类目成交额加权 |
| `tests/test_subject_market.py` | 标的级/类目级取值与加权聚合；`e_bias_pct == core.e_bias × 100` 单位契约 |
| `tests/test_market_view.py`、`tests/api/test_market_view_window.py` | 序列对齐；截尾只截 `series`、保留 `lines`/`period`；盘中叠加含当日合成K线 |
| `tests/unit/test_mcp_symbol_detail.py` | MCP 契约（`series`/`period`/`lines`） |

**真实数据验证**（开发库只读取样，未改动）：

- 30 只标的完整跑「升级前 v2 → `rebuild_if_needed` → 重建 v3」流程：
  落库值与 core 实时计算**逐值一致**，无 NULL，自动备份正常生成，
  30 只耗时 2.27 s（外推 874 只 ≈ 66 s）。
- 852 只标的的偏离度分布（这组数字是参考线是否可用的直接依据）：

  | 指标 | 值 |
  |---|---|
  | 最新值 P5 / P50 / P95 / P99 | −7.27% / −1.34% / +6.64% / +13.07% |
  | 最新值 min / max | −12.69% / +22.87% |
  | 当前 >15%（过热） | 7 / 852（**0.8%**） |
  | 当前 >5%（失速线之上） | 66 / 852（**7.7%**） |
  | 当前 <−5%（止损参考之下） | 126 / 852（14.8%） |
  | 全历史逐日落在 ±15% 内 | 94.4%（超过 15% 的交易日占 4.09%） |

  **解读**：5% 线大致对应「最强的十分之一」，与 earletf 说的「不上 5% 不够强 /
  龙头气质」用法吻合；15% 线在真实标的上是极端事件（0.8%）。所以参考线**不是**
  完全跑偏的刻度 —— 但仍未做盈亏标定（不知道按它做交易赚不赚钱），
  页面上的「未在本 ETF 池标定」说明必须保留。

**页面可视验证**（隔离库 + 真实数据 + 浏览器实操）：

- `/subject-market`：第 4 个着色按钮「E-Bias 偏离度」存在且可切换；
  色块按偏离度染色（正红负绿），60 个标的全部有值、无「数据不足」灰块；
  悬停/标签显示带符号百分号。切换后说明文字正确更新。
- `/market-view`：E-BIAS 副图出现在 BIAS 副图**下方**；canvas 正常渲染；
  4 条参考线带标签（过热 15 / 失速 5 / 零轴 0 / 止损参考 −5）；y 轴与
  tooltip 为 `%`；右上角来源说明可见。
- API 层：`limit=5` 时 `e_bias.series` 截到 5 根而 `lines` 保持 4 条
  （截尾白名单行为正确）；看板 60/60 个标的带 `e_bias_pct`；
  `detail="lite"` 保留 `e_bias_pct` 且不污染 full 缓存。

> 盘中（intraday）路径本次**未做可视验证**：验证当天（2026-09-10）的日K已
> 于 16:30 落库，`build_intraday_overlay` 按设计不再叠加合成K线
> （`meta.is_intraday = false`，属正确行为）。盘中正确性由上面两个集成测试
> 覆盖（它们直接对「历史 + 当日合成K线」独立复算并比对）。

### 11.4 顺带修复的独立缺陷（超出本方案范围，已单独报告）

**`init_db` 的默认库路径不锚定项目根**（`src/data/storage/db.py:2430`）：
默认参数是硬编码的 CWD 相对字符串 `"data/trend_quant.db"`，未走
`core/paths.default_db_path()`。后果是 `TREND_QUANT_HOME` 对主应用
（`app.main` 的无参 `init_db()`）**完全失效** —— 从非项目根目录启动会静默
连到另一个库，容器/多环境部署时尤其危险。这也与 `core/paths` 文档里
「已消除 CWD 相对路径」的说法相矛盾（`scripts/_common.py` 已用
`default_db_path()`，是同一意图的另一处落地）。

已改为 `default_db_path()`（1 行），并补 `tests/unit/test_db_path_anchoring.py`
（4 个用例，覆盖项目根锚定、CWD 无关、`TREND_QUANT_HOME` 生效、显式路径优先）。

