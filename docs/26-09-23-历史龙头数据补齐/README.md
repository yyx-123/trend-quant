# 历史龙头数据补齐（标的池历史扩容）

> 执行日期：2026-09-23 起　｜　状态：**阶段一（出清单）已完成；阶段二（灌 K 线）进行中 —— tushare-only 8 只已入 staging 表，见 `STAGE2-tushare灌库进度.md`**
> 数据源：Tushare Pro（临时账号，5000 积分档）

## 先读这份

**`2026-09-23-历史龙头数据补齐-方案.md`** —— 背景、为什么用 Tushare、股票侧与 ETF 侧的完整取数口径、交付文件与复现步骤，都在里面。

本文件是目录导航与清单速查。

## 这个目录解决什么问题

现有标的池是「**现在视角**的龙头」（约 1000 只，股票为主、ETF 为辅），缺两类：

1. **曾经是龙头、而今没落甚至退市的公司** —— 幸存者偏差的直接来源
2. **行业覆盖偏窄** —— 现在集中在科技，其他行业偏薄

做法：找出**过去每一年市值靠前的股票和 ETF**，取并集作为待补标的池。
**全部走 Tushare 直接口径**（指数成分表 / 基金基础信息），不自己间接推算。

## 目录

```
├── README.md                          ← 本文件（导航）
├── 2026-09-23-历史龙头数据补齐-方案.md    ← 背景 + 完整取数口径 + 复现（先读这份）
├── STAGE1-中证指数并集.md                ← 股票侧：方法、口径、校验证据、已知局限
├── STAGE1-ETF清单.md                    ← ETF 侧：同上
├── fetch_tushare_index_members.py      ← 股票侧主脚本
├── fetch_tushare_etf.py                ← ETF 侧主脚本
├── dedup_etf_themes.py                 ← ETF 主题/赛道去重
├── probe_tushare.py                    ← 连通性/权限探测（指数成分 + 退市股行情）
├── probe_tushare_fund.py               ← 基金/ETF 接口探测（benchmark / 费率 / 退市 ETF）
├── diff_on_server.py                   ← 在服务端库上重跑差分（dev 库标的少于生产库）
├── test_tushare_field_mapping.py       ← 阶段二：Tushare→库字段映射验证（已通过）
├── tf_probe_delisted.py                ← 阶段二：tf 退市/摘牌覆盖探测（推翻"tf 无退市股"）
├── load_tushare_staging.py             ← 阶段二：tushare-only 灌 staging 表（断点续跑）
├── STAGE2-tushare灌库进度.md            ← 阶段二进度与证据（阶段二先读这份）
└── data/                               ← 全部产物（见下）
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
| **`data/etf_candidates_final.csv`** | **最终清单 175 只**：赛道归并后每赛道一只（含 sector / theme_norm 两列） |
| `data/etf_candidates_dedup.csv` | 318 只全部，带 `theme_norm`（机械归并后）与 `sector`（赛道）两列，便于自行重组 |
| `data/etf_candidates.csv` | 318 只（去重前，每个 `benchmark` 主题一只，已排除货币型） |
| `data/etf_universe.csv` | **1720 只** ETF 全量主表：跟踪主题（Tushare `benchmark` 直接给）、`invest_type`、费率、规模、逐年成交额 |
| `data/etf_by_year.csv` | 逐年明细长表 7652 行（含未达标，便于换阈值重筛） |
| `data/etf_dedup_preview.md` | 赛道归并明细（每个赛道列出被合并的标的与代表） |
| `data/tushare_etf_basic.csv` | 场内基金官方列表（含 128 只已摘牌） |

口径：**2013–2026 年间至少一年日均成交额 ≥ 5000 万元** → 达标 **694 只**（排除货币型后 669 只）→
按 `benchmark` 归并成 318 个主题 → 机械归并 **289** → 赛道归并 **175**。
最终 175 只的大类分布：宽基 25 / 行业主题 92 / 跨境 29 / 债券 25 / 商品 3 / 策略 1。
详细方法与校验见 `STAGE1-ETF清单.md` 与方案文档。

## 跑法（项目根目录）

```bash
# 股票侧（约 110 次调用，几分钟）
TUSHARE_TOKEN=xxx TUSHARE_HTTP_URL=https://tuaremax.top \
  .venv/bin/python docs/26-09-23-历史龙头数据补齐/fetch_tushare_index_members.py

# ETF 侧（约 1845 次调用，20 分钟，可断点续跑）
TUSHARE_TOKEN=xxx TUSHARE_HTTP_URL=https://tuaremax.top \
  .venv/bin/python docs/26-09-23-历史龙头数据补齐/fetch_tushare_etf.py

# ETF 主题/赛道去重（纯本地计算，秒出）
.venv/bin/python docs/26-09-23-历史龙头数据补齐/dedup_etf_themes.py

# 服务端重跑差分
.venv/bin/python docs/26-09-23-历史龙头数据补齐/diff_on_server.py
```

约定：token 只通过环境变量注入，脚本内不出现、不落盘、不入库、不进 git（沿用 `scripts/tushare_common.py`）。
`tushare` 是 `pyproject.toml` 的 optional extra，需要时 `pip install tushare`
（**默认 PyPI 源在本机会卡死，用清华源**）。

## 阶段二（进行中）

**进度与证据详见 `STAGE2-tushare灌库进度.md`。** 要点：

- 服务端差分（生产库）：需补 2668 只（top1000 缺 1736、1001-1800 缺 848、ETF 缺 84），其中退市股 235 只
- 数据源决策（用户定）：**tf 优先**，tushare 只补 tf 拿不到的
- tf 覆盖探测推翻阶段一结论：tf 现在能取退市股；237 只候选中 229 只 tf 全覆盖，
  **tushare-only 仅 8 只**（6 只 2010 年前私有化退市股 + 2 只摘牌 ETF）
- 这 8 只已灌入临时表 `staging_ts_market_data` / `staging_ts_ex_factors` / `staging_ts_instruments`
  （结构对齐正式表，现有流程零影响）
- 字段映射已实测验证：amount ×1000、因子 = 累积 adj_factor 相邻比值且事件日 −1 天、qfq 重建偏差 ~1e-4
- 阶段二新增产物：`data/tf_probe_delisted.csv`（237 只探测明细）、`data/tushare_only_symbols.csv`（8 只）

**入池方式未定（仍是阻塞项）**：退市标的若 `enabled=1`，日更会天天拉、天天失败；若 `enabled=0`，
回测又会把它们排除掉（幸存者偏差就白治了）。需要加第三态（例如 `delist_date` 列），
这是 staging 合并进正式库、以及 tf 侧 229+2349+82 只灌库前的阻塞项。
