# 滚动周/月趋势值落库（trend_rolling_daily）

> 日期：2026-09-13
> 状态：已上线（表结构 + 日更维护 + 全量回填）
> 需求来源：相位迁移研究（rolling 口径）需要逐日的滚动周/月趋势值全历史；
> 每次用 core.rolling_bars 现算全池太重，物化落库按 (symbol, time) 直接取用。

## 1. 口径

滚动锚定（`core/rolling_bars.py`，与 natural 周/月锚定的拟合表不同）：
第 k 根 bar = 截至当日的倒数第 k 个 D 交易日窗口（右闭），D：周=5、月=22，
整条 bar 序列每天重新锚定到当日。趋势值复用 canonical 公式全部结构
（MA/EMA 3/5/8、tanh 除数、权重、0.3/0.7 指数、±100 clip 均不动），
仅窗口参数按周期折算：

| 参数 | 周 | 月 |
|---|---|---|
| bar 天数 D | 5 | 22 |
| atr_period | 8 | 6 |
| vol_ma_period | 8 | 6 |
| er_period | 4 | 3 |

回看 K=16 根 bar（月口径最多 16×22=352 个交易日）。本表只落库原始趋势值，
不含状态离散化；研究/信号层的三态阈值（2026-09-14 起）：日 ±5，
滚动周/月 ±9（依据 2026-09-13 rolling-trend-calibration 研究的
分布标定，±9 使「无趋势」恢复约 2/3 多数语义；研究已归档浓缩于
`research/README.md`）。

趋势值是确定性 PIT 函数：第 t 日的值只依赖截至 t 的日K。预热期内
（周 <50 个交易日 / 月 <220 个交易日）为 NaN，双 NaN 行不落库。

## 2. 表结构与派生关系

| 表 | 内容 |
|---|---|
| `trend_rolling_daily` | 逐日滚动周/月趋势值（qfq 日K 派生） |

- `PRIMARY KEY (symbol, time)`，`time` 为交易日（19 字符
  `'YYYY-MM-DD HH:MM:SS'` 文本，与 market_data 同格式——混用
  `'YYYY-MM-DD'` 会产生双主键）；
- 列：`w_trend` / `m_trend`（REAL，单侧预热期为 NULL，读出为 NaN）；
- **派生表，不是真源**：真源仍是 `market_data_raw` + 除权因子（本地物化
  qfq），趋势值永远可以由 qfq 日K 重建。

## 3. 维护路径（不需要手工干预）

`DataService.ensure_daily_history`（日更 16:30 任务的单标的路径）在拟合
周/月K 刷新之后，同一触发口径：

- 除权因子变化 / qfq 自愈重写 → `refresh_rolling_trend(full=True)` 整段
  重建（趋势值是价格比值函数，全局水位平移不变，但分段因子变化会改变
  跨界窗口的值，故仍整段重建）；
- 仅日K 增量 → `refresh_rolling_trend(since=fetch_start)`：取 since 往前
  450 个交易日（覆盖月口径 352 的回看上界，留缓冲）的日K 重算，只
  upsert time >= since 的行（重叠区值由 PIT 确定性保证逐点一致）；
- 表内无存量时即使给了 since 也整段重建（否则历史永远缺段）；
- 刷新失败只记日志，不影响日更主结果（与拟合表同策略）。

首次铺开 / 修复性重建：

```bash
sudo -u trendquant .venv/bin/python scripts/backfill_rolling_trend.py            # 全池
sudo -u trendquant .venv/bin/python scripts/backfill_rolling_trend.py --symbols 510300.SS,600036.SS
sudo -u trendquant .venv/bin/python scripts/backfill_rolling_trend.py --symbols 510300.SS --dry-run
```

幂等（按标的整段 replace），可中断重跑；结尾打印标的数/行数/耗时/失败列表。

## 4. 读取

```python
db.load_rolling_trend("510300.SS")                                  # 全段
db.load_rolling_trend("510300.SS", start="2026-07-01", end="2026-07-31")
db.load_rolling_trend_many(["510300.SS", "600036.SS"])              # 批量
```

返回 DataFrame `time, w_trend, m_trend`（time 为 pd.Timestamp，NULL → NaN）。

前端消费（2026-09-14）：标的查看页 `/market-view` 的 TREND 副图在**日K视图**
下把周/月滚动趋势值作为两条参照线画出（`indicators.trend_rolling.weekly/monthly`，
按交易日对齐 dates，缺日补 null），与当期趋势值（按数值区间着色）以颜色 + 线宽
区分（日 2.2 / 周 2.6 / 月 3.0）；趋势值 MA5/MA10 默认不画。周/月K 视图不带出
——那里日期轴是周期 bar 标注日，副图趋势值本身已按该周期 K 线重算。

看板消费（2026-09-14）：标的大盘看板（`/subject-market` 与 MCP `dashboard`
工具）的每个标的/类目行新增 `trend_periods`——日/周/月三维度的趋势值 + 三态
相位（周/月阈值 ±9）+ 相位持续天数，周/月取值即来自本表（`build_period_phase_index`
按 600 自然日窗口读入、按层级成交额加权聚合）。口径与实现见
`docs/26-09-14-trend-periods/`。

## 5. 与研究目录的关系

- 滚动口径的参数标定（D=5/22、周 8/8/4、月 6/6/3 的来源）与相位迁移
  分析出自 2026-09-13 的 rolling-trend-calibration / phase-migration-rolling
  两项研究；研究目录已删除（2026-09-16），背景与结论浓缩存档于
  `research/README.md`。落库参数与标定口径一致。回填脚本的标的池与研究
  脚本同一口径（instrument_metadata 中 asset_type 为 stock/etf）。
  趋势值落库后，
  后续研究可直接读表，无需重复现算。

## 6. 测试

- `tests/integration/test_db_rolling_trend.py` — 建表、写入/读取往返
  （NULL↔NaN）、replace 幂等、time 19 字符格式、load_many；
- `tests/unit/test_rolling_sync.py` — service 维护路径（增量与全量重算
  逐点一致、预热期不落库、无存量回退全量、除权后值更新、日更钩子失败隔离）；
- 口径等价性仍由 `tests/unit/test_rolling_bars.py` 钉死（与
  `calculate_trend_score_series` 逐点比对）。
