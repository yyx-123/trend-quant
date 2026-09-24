# 历史龙头数据补齐（标的池历史扩容）

> 执行日期：2026-09-23　｜　状态：**阶段一（出清单）已完成，阶段二（灌 K 线）未开始**
> 数据源：Tushare Pro（临时账号，5000 积分档，镜像站）

## 这个目录解决什么问题

现有标的池是「**现在视角**的龙头」（约 1000 只，股票为主、ETF 为辅），缺两类：

1. **曾经是龙头、而今没落甚至退市的公司** —— 幸存者偏差的直接来源
2. **行业覆盖偏窄** —— 现在集中在科技，其他行业偏薄

做法：找出**过去每一年市值靠前的股票和 ETF**，取并集作为待补标的池。
**全部走 Tushare 直接口径**（指数成分表 / 基金基础信息），不自己间接推算。

## 目录

```
├── README.md                      ← 本文件
├── STAGE1-中证指数并集.md            ← 股票侧：方法、口径、校验证据、已知局限
├── STAGE1-ETF清单.md                ← ETF 侧：同上（Tushare 直接口径）
├── fetch_tushare_index_members.py  ← 股票侧主脚本
├── fetch_tushare_etf.py            ← ETF 侧主脚本
├── probe_tushare.py                ← 连通性/权限探测（指数成分 + 退市股行情）
├── probe_tushare_fund.py           ← 基金/ETF 接口探测（benchmark / 费率 / 退市 ETF）
├── diff_on_server.py               ← 在服务端库上重跑差分（dev 库标的少于生产库）
└── data/                           ← 全部产物（见下）
```

## 股票侧交付清单

| 文件 | 内容 |
| --- | --- |
| **`data/universe_top1000.csv`** | **2663 只**曾经进过市值前 1000。列：symbol, name, best_band, top1000_years, n_snap_top1000, band_1001_1800_years, n_snap_1001_1800, delisted, in_project |
| **`data/universe_1000_1800.csv`** | **950 只**只进过 1001–1800（与上不重叠） |
| `data/tushare_delisted.csv` | 退市股全名单 **340 只**（含 delist_date） |
| `data/tushare_listed.csv` | 在市名单（补名称用） |
| `data/increment_top1000_symbols.txt` | 上表中 dev 库缺的 2068 个代码 |
| `data/increment_1000_1800_symbols.txt` | 同上，923 个 |
| `data/tushare_index_raw/` | `index_weight` 原始分片（110 个半年度文件，可审计） |

档位划分（每期快照各自判档，跨期取并集）：

| 来源 | 判为 | 市值排名 |
| --- | --- | --- |
| 沪深300 全部 | `top300` | 1–300 |
| 中证500 全部 | `301_800` | 301–800 |
| 中证1000 权重前 200 | `801_1000` | 801–1000 |
| 中证1000 其余 | `1001_1800` | 1001–1800 |

时间覆盖（各指数发布日决定）：2005-04 起有前 300，2007-01 起有前 800，2014-10 起有前 1000
（第二份清单延伸到 1800）。详见 `STAGE1-中证指数并集.md`。

## ETF 侧交付清单

| 文件 | 内容 |
| --- | --- |
| **`data/etf_universe.csv`** | **1720 只** ETF 主表：跟踪主题（Tushare `benchmark` 直接给）、`invest_type`、费率、规模、逐年成交额、是否主题代表 |
| **`data/etf_theme_representatives.csv`** | 592 个跟踪主题各一只代表（按近一年日均成交额） |
| `data/etf_by_year.csv` | 逐年明细长表 7652 行（含未达标，便于换阈值重筛） |
| `data/tushare_etf_basic.csv` | 场内基金官方列表（含 128 只已摘牌） |

口径：**2013–2026 年间至少一年日均成交额 ≥ 5000 万元** → 达标 **694 只**，归并到 **592 个跟踪主题**
（其中 322 个主题有达标 ETF）。详细方法与校验见 `STAGE1-ETF清单.md`。

## 跑法（项目根目录）

```bash
# 股票侧（约 110 次调用）
TUSHARE_TOKEN=xxx TUSHARE_HTTP_URL=https://tuaremax.top \
  .venv/bin/python docs/26-09-23-历史龙头数据补齐/fetch_tushare_index_members.py

# ETF 侧（约 1900 次调用，20 分钟，可断点续跑）
TUSHARE_TOKEN=xxx TUSHARE_HTTP_URL=https://tuaremax.top \
  .venv/bin/python docs/26-09-23-历史龙头数据补齐/fetch_tushare_etf.py

# 服务端重跑差分
.venv/bin/python docs/26-09-23-历史龙头数据补齐/diff_on_server.py
```

约定：token 只通过环境变量注入，脚本内不出现、不落盘、不入库、不进 git（沿用 `scripts/tushare_common.py`）。
`tushare` 是 `pyproject.toml` 的 optional extra，需要时 `pip install tushare`
（**默认 PyPI 源在本机会卡死，用清华源**）。

## 阶段二（未开始）

等你确认清单后：`diff_on_server.py` 重算增量 → 逐标的判断来源
（**TickFlow 能取到完整历史的就用 TickFlow，取不到的（主要是 2021 年前退市的）用 Tushare 的 `daily` + `adj_factor`**）
→ 写 `market_data_raw` + `ex_factors`（`provider` 标明来源，qfq 由项目自己派生）。

**入池方式未定**：退市标的若 `enabled=1`，日更会天天拉、天天失败；若 `enabled=0`，回测又会把它们排除掉
（幸存者偏差就白治了）。需要加第三态（例如 `delist_date` 列），这是灌库前的阻塞项。

## 历史备注

早期曾试过两条被放弃的路线，已删除，结论留此备查：

- **Wind 取中证1000 历史成分**：接口单次上限 100 行且无分页；网关有账户级 5 小时配额（被探测调用耗尽）；
  按日期翻页会系统性丢数据（一次调整约 200 行，一天就撑爆一页）；返回里字面的 `Wind代码` 列其实是指数本身。
  → 被 Tushare `index_weight` 完全取代。
- **TickFlow 全量行情自算成交额排名**：能覆盖在市标的但对退市股无能为力（TickFlow 标的池不含退市股），
  且主题分组只能按名称猜。→ 被 Tushare `fund_basic.benchmark` / `index_weight` 取代。
