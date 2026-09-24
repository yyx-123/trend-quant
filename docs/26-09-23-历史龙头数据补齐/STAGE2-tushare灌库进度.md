# STAGE2：灌库进度（2026-09-24）

> 状态：tushare-only 部分**已完成**；tf 侧灌库未开始（等入池方式决策）
> 数据源决策（用户定）：**tf 优先** —— tf 能覆盖的一律走 tf 保持源统一，tushare 只补 tf 拿不到的。

## 一、服务端差分（生产库 data/trend_quant.db，1364 只 = 股票 1162 + ETF 202）

| 清单 | 总数 | 库已有 | 需补 |
| --- | --- | --- | --- |
| universe_top1000 | 2663 | 927 | **1736**（在市 1539 + 退市 197） |
| universe_1000_1800 | 950 | 102 | **848**（在市 810 + 退市 38） |
| etf_candidates_final | 175 | 91 | **84**（在市 82 + 摘牌 2） |
| **合计（去重）** | | | **2668** |

退市股合计 235 只（197 + 38）。仓库里的 `increment_*.txt` 是对 dev 库差分的旧结果（2068+923），偏大，以上表为准。

## 二、字段映射验证（test_tushare_field_mapping.py，已实测通过）

Tushare 取数**能填满**现有 `market_data_raw` / `ex_factors` 结构：

| 字段 | 映射 | 证据 |
| --- | --- | --- |
| OHLC | 直取（未复权） | 与库内 tickflow 行逐日一致 |
| volume | 直取（同为手） | 库/tushare 比值 1.0000 |
| amount | **×1000**（千元→元） | ×1000 后与库一致 |
| symbol | `.SH→.SS`（core.symbols.from_vendor_symbol） | — |
| time | `YYYYMMDD` → `'YYYY-MM-DD 00:00:00'`（raw）/ `'YYYY-MM-DD'`（因子） | 对齐现有表格式 |
| 复权因子 | 累积 `adj_factor` 相邻比值，**事件日 = 跳变日 − 1 个日历日** | 茅台 30/30 事件日期全交集，值偏差 ~4e-4（tushare 精度） |

端到端：tushare raw + 换算因子本地物化 qfq（core.adjustment.compute_qfq）vs 库内
`market_data_qfq`，重叠 1633 天收盘相对偏差中位 0.01%、最大 0.02%。
ETF 侧 `fund_adj` 接口存在可用（510880 换算出 11 个除权事件）。
退市股全历史可取（乐视退 1700 行 + 9 个除权事件）。

## 三、tf 退市覆盖探测（tf_probe_delisted.py → data/tf_probe_delisted.csv）

**推翻阶段一"TickFlow 不含退市股、取不到退市 K 线"的结论**（方案 §二）。
2026-09-24 实测：武钢股份 tf 4063 行、乐视退 tf 1700 行，与 Tushare 行数完全一致。

对全部 237 只退市/摘牌候选逐一探测（K 线行数/跨度 + 因子批量）：

| 分类 | 只数 |
| --- | --- |
| tf 全有（K线+因子，历史深度无缺口） | **229** |
| tf 有 K 线但无因子 | 0 |
| **tf 无 K 线 → tushare-only** | **8** |

tushare-only 8 只（`data/tushare_only_symbols.csv`）：

| 代码 | 名称 | 退市日 |
| --- | --- | --- |
| 000406.SZ | 石油大明 | 2006-04-21 |
| 000956.SZ | 中原油气 | 2006-04-21 |
| 000763.SZ | 锦州石化 | 2006-01-04 |
| 000817.SZ | 辽河油田 | 2006-01-04 |
| 000618.SZ | 吉林化工 | 2006-02-20 |
| 000515.SZ | 攀渝钛业 | 2009-05-06 |
| 510420.SS | 景顺长城上证180等权重ETF | 2019-11-08（摘牌） |
| 510260.SS | 诺安上证新兴产业ETF | 2020-01-15（摘牌） |

规律：tf 缺失的都是 **2010 年前退市**的老股（中石化/中石油系私有化）与摘牌 ETF。

## 四、tushare-only 已灌临时表（load_tushare_staging.py，已完成）

均在 `data/trend_quant.db` 内，表名隔离，**现有代码不读这些表，对大盘/日更/qfq 物化零影响**：

| 表 | 内容 |
| --- | --- |
| `staging_ts_market_data` | 16209 行 / 8 只，结构同 market_data_raw（amount 已换算元，provider='tushare'） |
| `staging_ts_ex_factors` | 38 条 / 6 只（2 只摘牌 ETF 经查确无分红，fund_adj 为空属正常） |
| `staging_ts_instruments` | 8 只元数据（name/list_date/delist_date/来源清单） |
| `staging_ts_fetch_log` | 逐标的抓取状态，重跑自动跳过 ok（断点续跑） |

已处理的坑：镜像站对个别老退市股的**无日期区间调用返回空**（000406.SZ），
loader 已加"空结果→显式全区间重试"兜底。

## 五、剩余 TODO

1. **tf 侧灌库（未开始）**：229 只退市股 + 2349 只在市股 + 82 只在市 ETF ——
   走项目 tf 管线，不依赖 tushare 账号有效期。需先决策入池方式：
   退市标的 `enabled=1` 会让日更天天失败、`enabled=0` 又被回测排除（幸存者偏差白治），
   需要第三态（如 `delist_date` 列）—— 原方案 §七的阻塞项仍然成立。
2. **staging → 正式库合并**：等入池方式与对现有流程的影响评估后再做。
3. （可选）tushare 账号 3 天有效期内，备份 229 只退市股的 `adj_factor` 进 staging 作交叉校验。
