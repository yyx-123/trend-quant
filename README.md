# Trend Quant — A 股 ETF 趋势跟踪系统

## 系统概览

FastAPI + SQLite 的单机应用：日 K 行情驱动的趋势看板、单标的分析、配置化规则回测，以及对外提供数据的 MCP 服务。

- **标的看板**（`/subject-market`）：全标的池三级分类趋势看板（趋势值、MA5、强度百分位、相位），EOD + 盘中实时两套视图；
- **标的查看**（`/market-view`）：单标的 K 线 + 全套指标（MA/ATR/RSI/MACD/BOLL/BIAS/趋势值），支持盘中实时叠加；
- **策略管理**（`/rule-backtest`）：配置化规则策略（JSON 条件树）的创建与单标的回测，多策略对比；
- **标的管理**（`/instruments`）：标的增改、分类编辑、历史行情回填；
- **MCP 服务**（`/mcp/sse`）：5 个工具（trend_dashboard / intraday_dashboard / symbol_detail / calc_stop_loss / list_instruments）；
- **每日任务**：16:30 增量补齐日 K → 除权检测（必要时整标重拉）→ 指标缓存重建。

## 架构

```
src/
├─ core/            领域核心（纯计算）：indicators（统一指标库）、trend（趋势值）、
│                   symbols、calendar、benchmarks、strategy_config、settings、jobs、scheduler
├─ data/            数据层：db（SQLite）、indicator_store（缓存读取门面）、service（行情）、
│                   provider_tickflow、intraday_service（盘中合成）
├─ services/        应用服务：market_indicators、dashboard、instrument_jobs、
│                   instrument_admin、indicator_builder（预计算管线）
├─ rule_backtest/   规则回测领域：engine、condition_engine、value_resolver（全序列记忆化）、
│                   indicators（core 薄适配）、registry、loader、service、metrics
├─ app/             HTTP 层：main + routers（只做编排）
└─ trend_mcp/       MCP 薄适配层
```

依赖方向单向：`app / trend_mcp → services → core / data`。

## 关键设计

### 指标唯一实现与预计算缓存

- 所有指标/趋势值只有一份实现（`core/indicators.py`、`core/trend.py`），带 `INDICATOR_FORMULA_VERSION` / `TREND_FORMULA_VERSION`；
- 预计算表：`indicator_daily`（含盘中递推状态列）、`trend_daily`（按参数集）、`trend_param_sets`（default 参数集 hash 注册）；
- 读取门面 `data/indicator_store.py`：**缓存优先、未命中实时算**——缓存只是加速器，回退是永久特性；
- 整标全量重建（qfq 除权会回溯改写历史，行级增量不可行）；16:30 日更尾部重建变动标的；启动时 hash/version 校验，漂移自动全量重建（`VACUUM INTO` 备份至 `data/backups/`）。

### 实时叠加

交易时段内，查看类接口通过"EOD 缓存 + 当日实时行"呈现：当日行由实时报价合成 bar + 缓存状态递推（EMA/MACD/RSI 精确递推、有限记忆指标尾窗重算）。**实时行永不落库；回测/止损只用 EOD 数据。**

### 回测性能

规则回测引擎开局一次性构建全序列指标（ValueResolver 记忆化），日循环为纯状态机：典型趋势策略从 ~40-80s/次 降至 ~0.5s/次（实测随 K 线数近似线性增长，约 0.00027s/根，5000+ 根老股单格约 2s），结果与旧实现逐笔一致（新旧实现等价性测试锁定）。

## 数据存储

单一 SQLite（`data/trend_quant.db`）：`market_data_qfq`（前复权日 K，主数据）、`market_data_raw`、`instrument_metadata`（标的唯一来源）、`instrument_categories`、`rule_strategies`、`job_runs`（任务记录）、`app_config`（策略参数）、`indicator_daily` / `trend_daily` / `trend_param_sets`（预计算缓存）。config/ 仅 `app.yaml`（基础设施）。密钥在 `.env`（TICKFLOW_API_KEY）。

## 运行

```bash
# 开发
PYTHONPATH=src .venv/bin/python -m uvicorn app.main:app --reload

# 测试
.venv/bin/python -m pytest tests/ -q

# 部署（systemd）
sudo systemctl restart trend-quant.service
```

## 备份

代码用 git bundle（仓库根目录带时间戳的 `.bundle`）；数据 = `data/trend_quant.db`（含全部行情/策略/缓存）+ `.env`。恢复：clone bundle → 放回 DB 与 .env → 装依赖 → deploy.sh。
