# 批量回测止损宽度诊断 + 分析数据导出 设计方案

> 状态：方案待评审（未开发）
> 日期：2026-08-30
> 触发问题：小规模实盘使用紧止损（tight：硬止损 1.0×ATR / 吊灯 2.0×ATR），部分标的止损出局后迅速反弹（踏空），另一部分标的紧止损明显更优。需要一个能用数据回答「什么情况下该用多宽的止损」的回测诊断体系，并把结果导出给 AI 做二次分析。

---

## 0. 实证锚点：yyx 实盘清仓记录的事后表现

从 `data/trend_quant.db` 的 `manual_trades`（user_id=3，yyx）取出 6 笔已清仓交易，用 `market_data_qfq` 复查卖出后走势（截至 2026-08-28 收盘）：

| 标的 | 买→卖 | 已实现盈亏 | 卖出后表现 |
|---|---|---|---|
| 农业ETF富国 159825.SZ | 08-19 @0.7261 → 08-24 @0.714 | **-1.67%** | 4 个交易日内 **+6.02%**，显著踏空 |
| 中国石化 600028.SS | 08-25 @5.3606 → 08-27 @5.25 | **-2.06%** | 次日 **+4.00%**，立即反弹 |
| 紫光股份 000938.SZ | 08-17 @40.7546 → 08-19 @37.63 | **-7.67%** | 继续下跌至 **-8.34%**（vs 卖价），止损正确；若用松止损约多亏 50% |
| 半导体ETF 512480.SS | 08-13 @1.1051 → 08-20 @1.033 | -6.52% | 先下探 -2.4% 后回升 +2.9%，中性偏踏空 |
| 港股红利低波ETF 159569.SZ | 08-20 @1.4151 → 08-27 @1.407 | -0.57% | 基本持平（+0.57%） |
| 北京银行 601169.SS | 08-25 @5.1506 → 08-27 @5.08 | -1.37% | 基本持平（+0.20%） |

两个值得注意的事实：

1. **ATR 占比差异巨大**：紫光股份入场时 ATR/股价 ≈ 7.5%，农业ETF ≈ 2.1%，中国石化 ≈ 2.0%。高波动标的紧止损躲过大亏，低波动标的紧止损被噪音扫出——「按入场波动率分桶看松紧胜负」这个切片假设在 6 笔样本里已现端倪。
2. **当前系统算不出"如果当时用松止损会怎样"**：回测引擎的止损倍数藏在策略 exit spec 的 `state_value.params.atr_mul` 里（`src/rule_backtest/state_values.py:91-100`），批量回测接口没有紧/松档位概念，想对比只能手工克隆策略改参数。

这正是本方案要解决的两件事。

---

## 1. 设计目标

1. **诊断而非寻优**：不追求「1.0 还是 1.5 倍 ATR 最优」的单一答案，而是产出分布、切片、稳健性证据，回答「在哪种标的/哪种市场状态下，哪种止损宽度更好」。
2. **一切建立在逐笔 round-trip 日志上**：先补齐逐笔字段（MAE/MFE/R 倍数/出场原因/入场语境），所有上层指标都是日志的聚合，可任意二次切片。
3. **导出优先于人看**：所有分析结果以 long-format CSV + manifest 导出，供 AI 离线分析；页面展示不是本期重点。
4. **不动既有口径**：存量批次、既有指标列全部保留，新字段纯追加。

参考报告（另一 AI 的建议）的采纳情况：**全部采纳**，其中三项在代码库中发现了实证支撑或偏差，见 §7。

---

## 2. Part A：逐笔 round-trip 交易日志（地基）

### 2.1 现状问题

- 引擎（`src/rule_backtest/engine.py`）把买、卖记成两条独立 trade（`_execute_buy` :409-447 / `_execute_sell` :449-492），没有"一次往返一条记录"的概念；配对逻辑散落在 metrics 的 holding-days 计算里。
- **MAE / MFE / R 倍数全库零记录**。入场 ATR 在 `PositionState` 里现成可得（`initialize_stop_state`，state_values.py:68-107），但不落盘。
- 出场原因已有（reason ∈ {hard_stop, chandelier_stop, chandelier_stop_ratchet, exit_conditions_passed, end_of_data}），可用。

### 2.2 新增 RoundTrip 记录

在引擎 `_execute_sell` 成交时，把该仓位从入场到出场的信息合成一条 round-trip 记录：

```python
# src/rule_backtest/models.py 新增
@dataclass
class RoundTrip:
    symbol: str
    entry_date: str
    entry_price: float          # 含滑点成交价
    exit_date: str
    exit_price: float
    exit_reason: str            # hard_stop / chandelier_stop / chandelier_stop_ratchet / signal / end_of_data
    qty: int
    pnl: float                  # 净盈亏（扣费用）
    r_multiple: float           # pnl / 初始风险 = pnl / (qty * hard_stop_atr_mul * atr_at_entry)
    mae_pct: float              # 持有期最低价相对入场价的最大不利偏移 %
    mfe_pct: float              # 持有期最高价相对入场价的最大有利偏移 %
    mae_atr: float              # 同上，单位 = 入场 ATR
    mfe_atr: float
    holding_days: int
    entry_atr: float            # 入场时 ATR(20)，T-1 收盘口径（见 §7.3）
    entry_atr_pct: float        # entry_atr / entry_price，波动率分桶的直接依据
    asset_type: str             # stock / etf
    # —— 以下由批量服务层补记（引擎不关心）——
    category_l1: str = ""
    hard_stop_atr_mul: float = 0.0     # 本笔实际使用的硬止损倍数（紧/松档溯源）
    chandelier_atr_mul: float = 0.0
```

**MAE/MFE 的采集**：引擎已有逐日循环（`update_position_state_for_day`，state_values.py:26-65），在持仓期间每天用当日 high/low 更新 `mfe_price`/`mae_price` 两个标量即可，成本可忽略。

**出场后漂移（post-exit drift）**：引擎持有全量 bars，出场后顺势继续扫 20 根 K 线，补记：

```python
    post_exit_ret_5d: float | None    # 出场后第 5/10/20 个交易日收盘相对出场价
    post_exit_ret_10d: float | None
    post_exit_ret_20d: float | None
    reentry_above_entry_5d: bool | None   # 5/10/20 日内收盘是否重新站回入场价（假止损判据）
    reentry_above_entry_10d: bool | None
    reentry_above_entry_20d: bool | None
    days_to_trigger: int              # 入场到出场相隔交易日数（止损触发时间分布用）
```

> 在回测区间末尾出场的交易这些字段为 None，聚合时跳过并在导出 manifest 注明。

### 2.3 存储

`batch_backtest_cells` 表新增一个 blob 列，与既有 5 个 JSON blob 同级（db.py:387-429）：

```sql
ALTER TABLE batch_backtest_cells ADD COLUMN round_trips_json TEXT;  -- NULL = 旧批次未回填
```

- 新批次由 `extract_cell()`（batch_service.py:204-261）顺手写入。
- 旧批次提供回填脚本 `scripts/backfill_round_trips.py`：读 `trades_json` + qfq 行情重放配对推导（幂等，标记回填来源 `round_trips_source ∈ {engine, backfill}`），让历史批次也能参与诊断；不愿回填就重跑。
- **引擎单场回测（非批量）同样输出 round_trips**，单票回测页面/接口自然受益。

---

## 3. Part B：分布类指标（替代"只看中位数"）

### 3.1 格子级（batch_backtest_cells 平铺列追加）

对每个格子的 round-trip R 倍数序列计算：

| 新列 | 说明 |
|---|---|
| `r_mean` | 逐笔 R 均值（期望值的核心） |
| `r_p5 / r_p25 / r_p75 / r_p95` | 分位数，看分布形状 |
| `r_skew` | 偏度：紧止损典型特征是大量小亏堆积 + 右尾被吊灯砍 |
| `tail_ratio` | P95 ÷ \|P5\|，直接回答「紧吊灯是不是把右尾砍了」 |
| `exit_efficiency` | mean(max(pnl,0) / MFE金额)，量化「到嘴的肉吐回去多少」 |
| `max_losing_streak` | 最长连亏笔数（心理资本消耗） |
| `cvar_5` | 日度收益 5% CVaR |
| `ulcer_index` | 回撤的深度×持续综合指标 |
| `max_dd_duration_days` | 水下时间（比深度更折磨人） |
| `stop_exit_ratio` | 被 hard_stop 送走的交易占比 |
| `chandelier_exit_ratio` | 被吊灯送走的交易占比 |

其中 ulcer/cvar/dd_duration 基于日度 NAV（引擎已有），其余基于 round_trips。`compute_summary()`（metrics.py:59-194）扩展，纯追加。

### 3.2 批次聚合级（annual-aggregates 平行的诊断聚合）

现有 `aggregate_annual_returns()`（batch_service.py:298-350）只聚合中位数。新增 `aggregate_stop_diagnostics(batch_id)`，按「策略 × 分桶维度」聚合 round-trips：

- 均值 / P25 / P75 / 胜率 / profit factor / 假止损率 / 平均 post-exit drift / 平均 exit_efficiency
- 每桶附 n（样本数），n < 30 的桶在导出中标注 `low_confidence=true`

---

## 4. Part C：止损专项诊断（本次核心）

以下指标全部从 §2 的 round-trip 字段直接聚合，无新增采集成本：

| 诊断 | 计算 | 回答的问题 |
|---|---|---|
| **假止损率** | hard_stop 出场且 `reentry_above_entry_Nd=True` 的占比（N=5/10/20），分止损档位统计 | 止损架在噪音区的比例 |
| **止损后漂移** | hard_stop 出场的 `post_exit_ret_Nd` 均值/中位数 | 紧止损的踏空成本（农业ETF +6% 的群体版） |
| **触发时间分布** | hard_stop 交易中 `days_to_trigger ≤ 3` 的占比 | 高比例 = 止损放在噪音区的铁证 |
| **出场原因构成** | 紧/松档各自 hard vs chandelier vs signal 的占比 | 验证两档交接点（吊灯何时接管） |
| **吊灯回吐** | chandelier 出场的交易：`mfe_atr` 中位数 vs 实际兑现 R 中位数 | 大量 MFE>2R 只兑现 0.5R → 吊灯太紧实锤 |

新增一个**紧/松并排对比端点**（见 §6.3），同一批标的、同一策略族、两档止损的上述全部指标并列输出，差值列由服务层派生。

---

## 5. Part D：紧/松档位跑批机制（让对比可复现）

### 5.1 方案

`BatchRunRequest`（batch_backtest.py:41-46）新增可选字段：

```python
stop_profile: Literal["default", "tight", "loose", "sweep"] = "default"
sweep_atr_muls: list[float] | None = None   # sweep 时用，默认 [0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0]
```

- `prepare_batch()`（batch_service.py:370-435）冻结策略快照时，对 `tight`/`loose` 覆写 exit spec 中 hard_stop/chandelier_stop 的 `params.atr_mul`，档位定义**直接复用实盘口径**（`src/services/stop_loss.py:185-199`：tight = 1.0/2.0，loose = 1.5/2.5 + 标的级覆盖），保证回测与实盘同一把尺。
- `sweep` = 对每个策略按 `sweep_atr_muls` 每个值生成一份快照（chandelier 倍数按实盘 tight:loose 的比例联动缩放），批次格子数 ×N，用于 §6.4 参数敏感性热力图。
- 批次名自动加后缀（`xxx [tight]`），`batch_backtest_runs` 表加 `stop_profile` 列便于配对查询。

### 5.2 前端

`batch_backtest.html` 运行表单加一个止损档下拉（默认 default，文案四个字以内，遵守低频操作 UI 文案要短的约定）。sweep 是极低频操作，**不做页面入口**，走 `scripts/run_stop_sweep.py`（符合 scripts 约定并显式提示"可安全关闭"）。

---

## 6. Part E：异质性切片、稳健性与显著性

### 6.1 分桶切片

分桶维度**优先复用已有数据**，避免新指标依赖：

| 维度 | 数据来源 | 分法 |
|---|---|---|
| 波动率 | `entry_atr_pct`（round-trip 自带） | 五等分桶 |
| 标的类型 | `asset_type`（格子已有） | stock vs etf |
| 趋势 regime | `batch_backtest_symbol_features.trend_score_avg`（已有，db.py:435-446） | 三档 |
| 年度 | exit_date 年份 | 逐年 |

Kaufman 效率比（KER）/ ADX 作为二期可选维度（`indicator_daily` 体系里加一列即可），一期不做。

**输出形态**：每个维度一张「分桶 × 止损档」表，单元格 = {r_mean, 假止损率, post_exit_drift_10d, n}。如果切片出现「低波动+紧、高波动+松」式稳定规律，最终交付物就是一条**选档规则**而非一个参数——这比任何单一最优参数值钱。

### 6.2 显著性

- 紧 vs 松的 R 均值差：逐标的配对的 bootstrap 95% 置信区间（1000 次重采样，脚本内实现，约 30 行，不引新依赖）。
- 每桶必须带 n；n < 30 标 `low_confidence`。

### 6.3 对比端点

```
GET /batch-backtest/api/compare?base_batch_id=..&alt_batch_id=..
```

要求两批次同标的池同策略（服务层校验 symbol×strategy 交集），返回逐格差值 + §4 诊断指标并排 + 分桶切片表。这就是「紧松并列四件套 + 专项诊断」的 API 形态。

### 6.4 稳健性（附录级）

1. **参数敏感性**：sweep 批次（§5.1）结果导出后画 7 点折线即可判断尖峰 vs 平台——数据由导出提供，图交给 AI/线下画。
2. **成本压力测试**：`BacktestExecutionConfig` 已参数化（batch_service.py:411-424），sweep 脚本加 `--cost-multiplier 2` 重跑一遍即可，无需新代码。
3. **样本内外**：脚本支持 `--train-end 2023-12-31` 把 sweep 跑两遍（样本内/外），比对选档规则是否稳定。

---

## 7. 回测框架本身的三个坑（审查中实证）

参考报告列的三个坑，逐一核对了代码，结论比报告更具体：

### 7.1 止损成交价假设 —— 属实，需要压力版本

引擎止损触发时 `reference_price = 止损价`（engine.py:494-518），**假设永远能在止损价成交**。现实中跳空会直接穿价。修复很便宜：卖出执行时若当日 `open < stop_price`（做空反向），按 `open` 成交（更差价格）。作为执行配置的开关 `stop_gap_fill: bool`，**建议默认开**——紧止损对这个假设最敏感，不开等于系统性美化紧档。

### 7.2 幸存者偏差 —— 属实，本方案无法消除但要明示

标的池来自 `instrument_metadata.enabled=1`（batch_service.py:96-122），是"现在还活着的票"。退市/腰斩的昔日龙头不在池中，而暴雷股恰是松止损亏最多的地方——**系统性美化松档**。一期对策：导出 manifest 写明该偏差；二期可选：把历史曾入池后剔除的标的纳入（需要归档表，`stock_category_archive` 已有类似结构可参考）。

### 7.3 ATR 口径 —— 发现真实问题，建议修

入场 ATR 取的是**含入场日当根 K 线**的值（engine.py:189-196 传 `day_bars = all_bars.iloc[:idx+1]`；实盘 stop_loss.py:203 同样含买入日）。入场决策在盘中做出时当根 K 线尚未收完，严格说这是轻微未来函数，且波动大的日子 ATR 被当日拉大、止损被放得更松。

建议：回测引擎改为入场日取 **T-1 收盘已完成的 ATR**（`day_bars.iloc[:idx]`）。实盘止损线是盘中实时守护，含当根有其合理性，**实盘口径不动**，但导出 manifest 要写明两套口径的差异。这个改动会轻微改变所有止损相关回测结果，需单独一个 commit 并在批次 `stop_profile` 旁记 `atr_basis: prev_close`。

---

## 8. Part F：数据导出（AI 消费）

### 8.1 形态：脚本为主，接口为辅

依据「低频批量操作走 scripts/」的约定，主入口是脚本：

```bash
python scripts/export_backtest_analysis.py <batch_id> [--compare <alt_batch_id>] [--out exports/]
```

输出一个目录（不打包，AI 直接读）：

```
exports/batch_<id>[_vs_<id>]/
├── manifest.json          # 字段口径、复权方式、ATR口径、止损成交价假设、幸存者偏差声明、生成时间、参数快照
├── cells.csv              # 每行 = 一个标的×策略格子，全部平铺指标列（含 §3.1 新列）
├── round_trips.csv        # 每行 = 一笔往返交易，§2.2 全部字段 ← 给 AI 二次切片的核心
├── annual_aggregates.csv  # 策略×年份聚合（既有）
├── stop_diagnostics.csv   # 分桶×止损档 诊断表（§4 + §6.1）
└── live_trades.csv        # 可选：manual_trades 实盘逐笔 + 同口径 post-exit 漂移（yyx 那 6 笔的直接量化）
```

### 8.2 关键设计决策

1. **long-format，不透视**。透视留给分析端；导出保持一行一事实，AI 随便切。
2. **CSV 用 `utf-8-sig`**（Excel 兼容），数值不格式化（保留全精度，格式化是展示层的事）。
3. **manifest.json 是 AI 友好的关键**：每个字段一行口径说明（单位、是否含费、复权方式、None 的含义），加上 §7 的三个偏差声明。AI 没有口径说明会自己脑补，这是导出质量的分水岭。
4. **数据源零重跑**：round_trips 走新 blob 列，其余全部读 `batch_backtest_cells` 现有列/blob 与 `batch_backtest_symbol_features`，导出纯查询+写文件，数 MB 级无流式需求。
5. `live_trades.csv` 复用 `src/services/trade_records.py` 的口径，额外补 post-exit 漂移（查询 qfq 行情即可）——直接把 §0 的手工分析产品化。
6. API 侧补一个等价端点 `GET /batch-backtest/api/runs/{batch_id}/export`（触发同样的导出，返回目录路径），页面不加入口，供远程/自动化调用。

### 8.3 实施任务分解

| # | 任务 | 涉及文件 | 验证 |
|---|---|---|---|
| 1 | RoundTrip 采集（MAE/MFE/R/post-exit） | `rule_backtest/models.py`, `engine.py`, `state_values.py` | 单票回测单测：构造已知走势断言 MAE/MFE/R |
| 2 | ATR T-1 口径 + stop_gap_fill 开关 | `engine.py`, `state_values.py`, `models.py` | 单测：跳空日按开盘价成交；入场 ATR 不含当根 |
| 3 | round_trips_json 落库 + extract_cell | `data/storage/db.py`, `batch_service.py` | 迁移幂等性测试；小批次端到端 |
| 4 | 格子级分布指标 | `rule_backtest/metrics.py` | 单测：已知 R 序列断言分位/偏度/tail_ratio |
| 5 | stop_profile/sweep 跑批 | `routers/batch_backtest.py`, `batch_service.py`, `batch_backtest.html` | 紧/松两批 ×2 标的冒烟 + 快照断言 atr_mul 被覆写 |
| 6 | stop_diagnostics 聚合 + compare 端点 | `batch_service.py`, `routers/batch_backtest.py` | 聚合单测 + 端点 200/校验 409 |
| 7 | 导出脚本 + manifest | `scripts/export_backtest_analysis.py` | 对真实批次导出，逐文件 schema 断言 |
| 8 | 旧批次回填脚本（可选） | `scripts/backfill_round_trips.py` | 幂等；抽样与重跑结果比对 |
| 9 | live_trades 导出 | 同 7 | 复算 §0 六笔数字一致 |

每个任务 TDD：先写失败测试→实现→通过→commit。测试注意 Windows 已知的 tempfile PermissionError 是既有 flake，不算回归。

---

## 9. 明确不做（YAGNI）

- 不做页面上的分布图/热力图可视化（导出给 AI 分析是主路径，页面只加档位下拉）。
- 一期不做 KER/ADX regime 指标（先用已有 trend_score_avg）。
- 不解决幸存者偏差的数据层（只声明）。
- 不动实盘 stop_loss.py 的口径与 manual_trades 表结构。
- 不做 walk-forward 框架本体（用样本内外两次跑批替代）。

## 10. 开放问题（实施前需拍板）

1. **旧批次回填 vs 重跑**：回填脚本（任务 8）能救历史批次，但 trades_json 推导的 MAE/MFE 是重放近似；追求口径纯净就全部重跑。倾向：回填，标注 `round_trips_source`。
2. **sweep 的 chandelier 联动比例**：tight 1.0/2.0、loose 1.5/2.5 的比值不同（2.0 vs 1.67），sweep 时 chandelier 按哪个比例缩放？倾向：固定 chandelier = hard × 2（贴近 tight 比例），在 manifest 记录。
3. **导出目录位置**：`exports/` 需加入 .gitignore。
