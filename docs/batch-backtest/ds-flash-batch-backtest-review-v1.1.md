# 批量回测方案 v1.1 复审意见

> 评审人：ds-flash
> 日期：2026-07-26
> 版本：`2026-07-26-batch-backtest-plan.md` v1.1

---

## 总体评价

v1.1 对评审意见的响应质量很高。**10 条意见中 8 条完整闭环**，2 条明确不采纳并给出代码级理由（分页→虚拟滚动、excess_calmar→暂不加），态度合宜。三方评审意见整合后方案更加严谨，特别是钻取一致性链路、数据锚定、假规律防护落地、孤儿批次恢复等关键点的修正确认了原方案的设计漏洞。

以下仅针对 v1.1 **新增或仍存在的问题**做补充复审。

---

## 🟠 需关注的问题

### 1. 耗时分布不明——最坏场景可能是 10 小时级，而非 40 分钟

**原文**（§5.2）："老股可能 3~5s/格……约 15~40 分钟"

**问题**：这个估算有隐含矛盾。"老股"（1993 年上市）bar_count ≈ 7700，如果单格真的需要 3~5s，那么 **716 标的 × 6 策略 × 5s ≈ 35,800s ≈ 10 小时**，而非 40 分钟。

| 假设 | 1584 格（股票×6） | 4296 格（全标的×6） |
|------|---|---|
| 0.5s/格（典型ETF） | ~13 min | ~36 min |
| 3s/格（老股） | ~79 min | ~3.6 h |
| 5s/格（老股+复杂策略） | ~132 min | ~6 h |

「0.5s 是 README 记载」很可能是在主流 ETF（2-5 年历史）上测的，**老股（1993 年至今 7700 根 bar）可能远高于此**。但方案又写"约 15~40 分钟"让人误以为覆盖了老股。

**建议**：
- 措辞上区分**典型耗时**与**最坏耗时**：比如"老股可能 ~3s/格，极端场景全类目+老股重仓可达数小时，发起区实时警示"。
- 第 0 步耗时实测选 20 个标的偏少——建议按 **bar_count 分层抽样**（<500 / 500-2000 / 2000+ / 5000+ 各 10-15 个），确保每层有统计意义。
- 如果实测后发现老股确实超预期：可考虑在 `config_json` 加 `max_bars` 参数（如截取最后 1500 根，约 6 年），作为 MVP 阶段限制最大耗时的 tradeoff。

### 2. `engine_version` 没有阐明如何自动获取

**原文**（§5.1）："`engine_version`：formula_version / git hash（指标实现追溯）"

**问题**：`formula_version` 是手动维护的字符串吗？如果是，一定会被遗忘更新，失去追溯意义。Git hash 在运行时获取则依赖执行环境是否安装了 git。

**建议**：
- 明确采用**自动方式**：`src/__init__.py` 或 `src/rule_backtest/__init__.py` 中定义一个 `__version__` 或 `ENGINE_VERSION`，由构建/启动时注入 git hash（`git rev-parse --short HEAD`），而非靠人肉维护。
- 若 git 不可用（如 Docker 镜像没装 git），备选方案：写一个 `version.txt` 由 CI 写入，运行时读取。

### 3. 钻取链路的 sessionStorage 有开新 Tab 盲区

**原文**（§5.4）："点击格子→批次页写入 sessionStorage→跳 /market-view"

**问题**：`sessionStorage` 是 tab 隔离的。如果用户**中键点击**（middle-click / Ctrl+click 新标签页打开），新 Tab 读不到原 Tab 写入的 sessionStorage → 钻取失败。

**建议**：
- 双重保障：先用 sessionStorage，新页面检测到缺失则 fallback 到 URL query（把快照 payload 的**摘要/ID**传过去，而非全文 base64）。具体做法：在跳转前把 `{symbol, strategy_id, batch_id}` 作为 query 参数，market_view 用 `batch_id + strategy_id` 从批次快照表回查完整 payload。
- 或约束交互方式：用 `window.open` 或前端 `<a>` 拦截点击统一走 JS 跳转（而非原生链接行为）。

### 4. `data_version` 缺乏操作对象

**原文**（§5.1）："data_version：启动时 MAX(updated_at)，漂移检测"

**问题**：`MAX(updated_at)` 是针对哪个表？`market_data_qfq` 还是 `instrument_metadata` 还是所有行情相关表的综合版本号？如果不明确，实现时会打架。

**建议**：
- 明确为 `SELECT MAX(updated_at) FROM market_data_qfq`（或 `(SELECT MAX(updated_at) FROM market_data_qfq) UNION (SELECT MAX(updated_at) FROM ...)`）。
- 在 `config_json` 或批次元数据中记清楚这个值的含义和来源。

### 5. 前端虚拟滚动在原生 JS 下的实现成本被低估

**原文**（§5.4）："明细大表：虚拟滚动"

**问题**：系统技术栈是 Jinja2 + **原生 JS**（无框架）。一个可排序、可筛选、含钻取点击的 4000+ 行虚拟滚动表格，在原生 JS 下实现复杂度不低（事件委托、行高同步/估算、滚动位置恢复、排序时重新计算可见行……）。

**建议**：
- 在实施顺序中为"明细大表"分配**独立工时**，不要低估。
- 如果发现原生实现成本过高，备选方案：
  - 使用轻量虚拟滚动库（如 Clusterize.js，vanilla JS，无依赖，只处理视觉复用）；
  - 或后端分页 + 前端增量滚动（每次加 200 行，`scroll` 事件触发加载，简单很多且无需虚拟滚动）。

---

## 🟡 微调建议

### 6. `BacktestExecutionConfig` 的 `instrument_type` 字段需确认引擎已支持

**原文**（§5.2 伪代码）：`execution=BacktestExecutionConfig(..., instrument_type=..., fee_min=5.0)`

`engine.py` 当前是否已消费 `instrument_type` 来区分股票印花税与 ETF 免印花税？该配置参数的 schema 是否已含 `fee_min`？建议实作前确认接口签名，避免 batch_service 写完后才发现引擎侧参数未定义。

### 7. 月度 NAV 的存储成本与收益需平衡

月度采样 NAV（每月末净值点）新增后，如果标的 20 年 ≈ 240 个点，每点一个浮点数 ≈ 2KB JSON/格。4296 格 ≈ **8-10MB 额外**。这本身不大，但应确认：
- 「多策略净值叠加对比」是否真的是 MVP 需求？从需求文档 §2 决策 8 来看，MVP 4 个视图不含净值叠加。
- 如果是 V2 功能，建议把 `monthly_nav_json` 移到 V2 再加，MVP 阶段减负。

### 8. 拒绝含 `random_uniform` 的策略——检查点位置

**原文**（§2 决策 3）："拒绝含 random_uniform 指标的策略"

建议在 `POST /run` 接口的**参数校验层**做（而非在 batch_service 执行时才报错），让用户立即获知，而不是等 20 分钟后发现批次被拒绝。此外，应同步在策略编辑器中增加标注（策略列表标识"含随机指标"），防患于未然。

### 9. 第 0 步的 20 个标的抽样方案

**原文**（§5.6 第 0 步）："20 个不同 bar 数标的 × 1 策略，产出耗时分布回填方案"

20 个标的分 5 个 bar_count 层，每层 4 个，统计意义偏弱。建议：
- 至少按 **bar_count 四分位数分层**（Q1/Q2/Q3/Q4 各 5-10 个），共 20-40 个标的。
- 或者直接用已有数据做**轻量诊断**：`SELECT bar_count FROM (SELECT count(*) as bar_count FROM market_data_qfq GROUP BY symbol) ORDER BY bar_count` 一眼看出分布，再用等距抽样。

---

## ✅ 本次复审确认无问题的设计点

- 孤儿批次清理 ✓
- 标的特征随批次执行入库（撤销实时端点）✓
- 超额口径锁定 + 注明 B&H 不对称 ✓
- 假规律防护三级落地（n 标注 + n<5 置灰 + 横幅）✓
- 单 running 批次 409 拦截 ✓
- 命名默认规则 ✓
- 逐格 commit + 显式 del result ✓
- `data_anchor_date` 保证批内一致性 ✓

---

## 总结

**v1.1 是对 v1.0 的扎实改进，方案已具备可实施性。** 上述 9 条均为增量优化，无需阻挡实施。建议按严重度选择性处理：

- **实施前处理**：第 4 条（data_version 操作对象）、第 6 条（引擎接口确认）、第 8 条（校验位置）
- **实施过程中关注**：第 2 条（engine_version 自动化）、第 3 条（钻取 fallback）、第 5 条（虚拟滚动工时）
- **实测后决策**：第 1 条（老股耗时分层实测决定了整个方案的执行架构是否需提前调整）

进入实施后建议使用 **实施决策日志（ADR）** 记录实测耗时分布和虚拟滚动方案选择，方便追踪。
