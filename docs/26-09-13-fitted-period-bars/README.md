# 拟合周/月K（每日在途 bar 快照）

> 日期：2026-09-13
> 状态：已上线（表结构 + 日更维护 + 全量回填）
> 需求来源：回测/研究在任意交易日 t 只能依赖「t 及以前可见」的信息。
> vendor 的已收盘周/月K 在周期结束后才知道；要在 t 日引用周/月级别信息，
> 必须用「截至 t 的在途周/月 bar」。本功能把它物化落库，按 (symbol, time) 直接取用。

## 1. 语义

对每标的、每个**有日K 的交易日 t**，存一行「t 所在周/月的在途 bar」：

- open = 周期内首个交易日 open；
- high / low = 周期内截至 t 的累计极值；
- close = t 当日收盘；
- volume / amount = 周期内截至 t 的累计值；
- `period_start` = 周期的日历起点（ISO 周周一 / 每月 1 日），便于按周期分组。

停牌日无日K 则无拟合行（消费方按自身交易日历 left-join）；上市首周/首月的
半截周期如实保留（这正是 point-in-time 语义）。

## 2. 表结构与派生关系

| 表 | 内容 |
|---|---|
| `market_data_qfq_weekly_fitted` | 逐日拟合周K（qfq 日K 派生） |
| `market_data_qfq_monthly_fitted` | 逐日拟合月K（qfq 日K 派生） |

- `PRIMARY KEY (symbol, time)`，`time` 为交易日（与 market_data 同格式文本）；
- `provider` 恒为 `'local_fitted'`；聚合的唯一实现在 `core/bars.py::fitted_period_rows`
  （与 `normalize_period` 周期体系同文件，DB/provider/service 不各写一套）；
- **派生表，不是真源**：真源仍是 `market_data_raw`（日K 不复权）+
  除权因子（本地物化 qfq）。拟合表永远可以由 qfq 日K 重建。

## 3. 维护路径（不需要手工干预）

`DataService.ensure_daily_history`（日更 16:30 任务的单标的路径）在 remat 决策点后：

- 除权因子变化 / qfq 自愈重写 → `refresh_fitted_period_bars(full=True)`
  整段重建（价格水位随新因子平移；收益与趋势值对因子平移不变）；
- 仅日K 增量 → `refresh_fitted_period_bars(since=fetch_start)` 只重建新 bar
  所在周/月覆盖的拟合行（常态 = 当周 ≤5 行 + 当月 ≤23 行）；
- 刷新失败只记日志，不影响日更主结果（与 remat 失败同策略）。

首次铺开 / 修复性重建：

```bash
.venv/bin/python scripts/backfill_fitted_period_bars.py            # 全池
.venv/bin/python scripts/backfill_fitted_period_bars.py --symbols 510300.SS,600036.SS
```

幂等（按标的整段 replace），可中断重跑。

## 4. 读取

```python
db.load_fitted_period_bars("510300.SS", "1w")                       # 全段
db.load_fitted_period_bars("510300.SS", "1M", start="2026-07-01", end="2026-07-31")
```

返回 DataFrame 含 `time, period_start, open..amount, symbol, provider`。

## 5. PIT 注意（重要）

拟合表解决的是「周期内前视」（在 t 日看到完整的当月 bar）这一真问题；
但它**不是冻结的历史价格快照**：除权因子变化时 qfq 日K 与拟合表都会按
新因子整段重建——t 日的拟合 bar 价格水位会随之后发生的除权平移。
由于趋势值/收益率对该平移不变（公式全是比值），研究与回测结论不受影响；
若未来需要严格的价格级冻结快照，要做因子历史版本化（超出本功能范围）。

另注意与 vendor 周期表的口径缝：除权日附近，vendor 前复权周/月 bar 与
「qfq 日K 聚合的拟合 bar」可能有微小差异（周月K方案 §8.3 实证）；已收盘
周期请以 vendor 表为准，拟合表只服务「在途」场景。

## 6. 测试

- `tests/unit/test_fitted_bars.py` — 聚合正确性（边界：首周/首月半截、
  ISO 周年界、停牌缺口、输入不原地修改）；
- `tests/integration/test_db_fitted_bars.py` — 建表、save/load/replace/many、
  周期隔离、非正价格拦截；
- `tests/unit/test_fitted_sync.py` — 日更钩子（增量只写受影响周期、除权整段
  重建、失败不拖垮日更）。
