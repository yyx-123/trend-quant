# Trend Quant — A 股 ETF 趋势跟踪系统

## 系统概览

FastAPI + SQLite 的单机应用：日 K 行情驱动的趋势看板、单标的分析、配置化规则回测，以及对外提供数据的 MCP 服务。

- **标的看板**（`/subject-market`）：全标的池三级分类趋势看板（趋势值、MA5、强度百分位、相位），EOD + 盘中实时两套视图；
- **标的查看**（`/market-view`）：单标的 K 线 + 全套指标（MA/ATR/RSI/MACD/BOLL/BIAS/趋势值），支持盘中实时叠加；
- **策略管理**（`/rule-backtest`）：配置化规则策略（JSON 条件树）的创建与单标的回测，多策略对比；
- **标的管理**（`/instruments`）：标的增改、分类编辑、历史行情回填；
- **MCP 服务**（`/mcp/sse`）：7 个工具（dashboard / symbol_detail / calc_stop_loss / calc_stop_loss_batch / list_instruments / add_trade / open_positions），MCP 层为纯接口层，取数/计算/聚合全部收口在 `src/services/`；通道级 Bearer token 鉴权（`TREND_MCP_TOKENS=token=用户名`，写工具身份来自 token 映射，不再传密码）；看板工具 dashboard 按时段自动路由口径（交易时段直读每 5 分钟一轮的定时快照——与网页端看板同一份数据、最旧约 5 分钟；其余时段取 EOD 日K，响应带 `data_mode` 标记，可用 `mode` 参数强制口径），支持 `detail="lite"` 瘦身（响应约 1/10 体积），calc_stop_loss_batch 一次批量试算（统一批量行情/日K/ATR）；
- **每日任务**：16:30 增量补齐日 K（raw append-only）→ 除权因子 diff（变化标的本地重物化 qfq）→ 指标缓存重建。

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
- 整标全量重建（复权变化会改写全历史，行级增量不可行）；16:30 日更尾部重建变动标的；启动时 hash/version 校验，漂移自动全量重建（`VACUUM INTO` 备份至 `data/backups/`）。

### 实时叠加

交易时段内，查看类接口通过"EOD 缓存 + 当日实时行"呈现：当日行由实时报价合成 bar + 缓存状态递推（EMA/MACD/RSI 精确递推、有限记忆指标尾窗重算）。**实时行永不落库；回测/止损只用 EOD 数据。**

### 回测性能

规则回测引擎开局一次性构建全序列指标（ValueResolver 记忆化），日循环为纯状态机：典型趋势策略从 ~40-80s/次 降至 ~0.5s/次（实测随 K 线数近似线性增长，约 0.00027s/根，5000+ 根老股单格约 2s），结果与旧实现逐笔一致（新旧实现等价性测试锁定）。

## 数据存储

单一 SQLite（`data/trend_quant.db`）：`market_data_raw`（不复权日 K，**唯一真源**，append-only 永不回溯改写）、`ex_factors`（除权因子）、`market_data_qfq`（等比前复权日 K，由 raw + 因子本地物化，`core/adjustment.py`，全系统读取入口）、`instrument_metadata`（标的唯一来源）、`instrument_categories`、`rule_strategies`、`job_runs`（任务记录）、`app_config`（策略参数）、`indicator_daily` / `trend_daily` / `trend_param_sets`（预计算缓存）。config/ 仅 `app.yaml`（基础设施）。密钥在 `.env`（TICKFLOW_API_KEY）。

## 运行

```bash
# 开发
PYTHONPATH=src .venv/bin/python -m uvicorn app.main:app --reload

# 测试
.venv/bin/python -m pytest tests/ -q

# 部署（systemd）
sudo systemctl restart trend-quant.service
```

## 部署（全新实例）

1. `sudo bash scripts/deploy.sh`（/srv/trend-quant、专用非 root 用户 trendquant、frp 直连 8000 无 nginx；详见脚本头注释）；
2. 编辑 `.env` 补全 `TICKFLOW_API_KEY` 与 `TREND_MCP_TOKENS`（模板见 `.env.example`）；
3. **登录**：内置管理员 `yyx` 随首次启动自动创建（引导密码读 env `TREND_QUANT_BOOTSTRAP_ADMIN_PASSWORD`，缺省为约定默认值，首次登录后请改密；已存在时不重置密码、仅确保 is_admin）；
4. **类目种子**：`instrument_categories` 表在 src 内无种子来源——全新部署需先导入类目（`scripts/migrate_category_sw2021.py` 或从已有实例导出/导入），否则标的的新增/更新会因类目校验为空集而全部 400；
5. 行情数据：启动后经「标的管理」批量回填，或从已有实例拷贝 `data/trend_quant.db`。

## 运维约定

- **chinese_calendar 每年 12 月升级**：`pip install --upgrade chinese_calendar`，否则次年法定假日会被误判为交易日（库数据超界时应用启动与导航栏均有「日历数据过期」提示）；
- 脚本直写库后需重启 web 服务（进程内标的符号缓存跨进程不失效）；
- tushare 镜像站：`scripts/tushare_common.py` 经 tushare 私有属性改写镜像地址——对 tushare 升级脆弱，且 token 与全部请求经第三方镜像，属知情风险，仅在临时账号窗口期使用。

## 备份

数据备份由调度器每日 03:00 自动执行（`data/backups/`，VACUUM INTO 在线备份，只保留最新一份）；另需自行保管 `.env`。代码即 Git 仓库（GitHub 远端为副本），不再使用 git bundle 备份约定。恢复：clone 仓库 → 放回 DB 与 .env → 装依赖 → deploy.sh。
