# 多周期趋势相位（日/周/月三态 + 持续天数）暴露到看板

> 日期：2026-09-14
> 状态：已上线（core 计算 + 看板装配 + MCP 透传；前端展示待定）
> 需求来源：MCP 标的大盘看板（`dashboard`）此前只有基于日K 的趋势值与趋势
> 相位；周/月滚动趋势值 2026-09-13 已落库（`trend_rolling_daily`）但只服务于
> `/market-view` 的参照线。现将「日/周/月三维度的趋势值 + 趋势相位 + 相位
> 持续天数」一并透出，并做好前端展示的准备。

## 1. 口径

### 1.1 三态相位

相位 = 趋势值相对阈值的三态离散（与相位迁移研究的 `_states_from_scores`
同一口径）：

| 相位 | 条件 | 字段值 |
|---|---|---|
| 正趋势 | v > +τ | `positive` |
| 负趋势 | v < -τ | `negative` |
| 无趋势 | \|v\| ≤ τ | `none` |

阈值按周期取（`core/trend_phase.PHASE_THRESHOLDS`）：

| 周期 | 阈值 τ | 依据 |
|---|---|---|
| 日 | ±5 | 看板/相位研究沿用的既有口径（日K 上 ~2/3 样本落在无趋势） |
| 周 / 月 | ±9 | 滚动口径分布标定：滚动周/月 σ 11.5~12.3 vs 日K 9.0~9.1，沿用 ±5 会把无趋势占比从 ~2/3 压到 ~1/2（2026-09-13 rolling-trend-calibration 研究 §6.1，已归档于 `research/README.md`） |

### 1.2 持续天数与前一相位

`phase_days` = 截至**最新一根有效 bar**（非 NaN），与最新状态相同的连续 bar
数；状态当日首次转入即为 1，「无趋势」同理。计数单位与序列一致——日线按
交易日、周线按周、月线按月。NaN（预热期、聚合无权重日）视为断点。

`phase_since` 是该段相位首个 bar 的日期（与前序 `trend_phase_signal_date`
同一表达习惯）。

`previous_phase` 是**紧邻的前一根** bar 的相位（日线 = 前一交易日、周/月维度
= 上一交易日的周/月相位——滚动表本身就是逐日物化的）：

- `previous_phase != phase` ⟺ 当日发生相位切换（此时 `phase_days` 必为 1），
  两者拼起来就是「从什么相位变化到什么相位」；按 positive > none > negative
  即可判断当日走强还是走弱；
- `previous_phase == phase` 表示昨日同相位（无切换）；
- 前一根不存在（当日就是序列首根）或前一根无有效值时（预热期、聚合无权重日）
  为 null —— **不跳过断点去取更早的值**，「前一根」就是字面意思。

注意 `previous_phase` 是「昨天」，不是「本段相位之前的那一根」：若某相位已
持续 5 天，则 `previous_phase` 与 `phase` 相同，而「该相位是从什么状态转来
的」需要看 `phase_since`/`phase_days` 回推（如需直接给出，可在 `phase_run`
里多读 `first - 1` 一根，属一行改动）。

## 2. 数据来源

| 维度 | 来源 | 说明 |
|---|---|---|
| 日 | `trend_daily` 缓存 / 盘中实时序列 | EOD 走展示帧的趋势值序列；盘中走含当日合成 bar 的实时序列 |
| 周 / 月 | `trend_rolling_daily` | 逐日物化的滚动锚定趋势值（`core/rolling_bars`），阈值 ±9 |

**盘中口径的两条时间线**：日线维度含当日实时值（当日转入当天即可见）；
周/月维度无「在途周期 bar」的概念，取最近收盘口径（与 `/market-view` 的
滚动参照线、`trend_rolling_daily` 的落库节奏一致）。

## 3. 响应形态

标的行与类目行（L2/L3）都带 `trend_periods`，三个维度**恒定存在**（无数据
时各字段为 null，形状稳定）：

```json
"trend_periods": {
  "daily":   {"trend_score": -0.371226, "threshold": 5.0, "phase": "none",
              "previous_phase": "none", "phase_days": 4,  "phase_since": "2026-09-08"},
  "weekly":  {"trend_score": -0.706706, "threshold": 9.0, "phase": "none",
              "previous_phase": "none", "phase_days": 29, "phase_since": "2026-08-04"},
  "monthly": {"trend_score": -5.110899, "threshold": 9.0, "phase": "none",
              "previous_phase": "none", "phase_days": 31, "phase_since": "2026-07-31"}
}
```

- `detail="lite"` 同样保留该字段（全是标量，不含长序列）；
- 类目行是成员成交额加权聚合后的相位（与类目 `trend_score` 同一把尺子）。

## 4. 分层实现（MCP 工具是纯透传）

```
core/trend_phase.py               纯计算：三态离散、持续天数、bundle 组装
      ↑
services/dashboard_common.py      取数（trend_rolling_daily 长窗口 + 成交额）
                                  + 按分类层级成交额加权聚合 → 周/月相位索引
      ↑                    ↑
services/dashboard.py    data/intraday_service.py
（EOD 看板）              （盘中快照看板）
      ↑                    ↑
services/dashboard_snapshot.dashboard_payload（口径路由）
      ↑
trend_mcp/server.py dashboard 工具 —— 只声明参数并把异常翻译成 ok=False
```

- **core**：`phase_state` / `phase_run` / `period_state` / `trend_periods`，
  canonical 唯一实现；不碰数据库、不碰展示结构。
- **services**：`build_period_phase_index` 产出 `{"instruments"|"l3"|"l2": {键:
  {"weekly","monthly"}}}`；**日线维度不进索引**——两条构建路径各自已有更
  合适的日线序列（EOD 用展示帧、盘中用实时序列），日线 bundle 由路径自己
  用同一个 core 函数算出，再与索引的周/月 bundle 组装。
- **MCP**：`dashboard` 工具零改动（参数与异常翻译除外），新字段随
  `services.dashboard_snapshot.dashboard_payload` 的返回值自动透出；工具
  docstring 里补了字段说明（工具描述是消费方看到的契约）。

## 5. 窗口与截断

周/月相位的回扫窗口 `PHASE_LOOKBACK_DAYS = 600` 自然日（≈410 个交易日），
取自实测：滚动月状态最长持续段约 296 个交易日（33 年全历史），窗口留足
余量。表内没有更早的行时按窗口内计数，`phase_since` 会落在窗口首日——
消费方可据此识别截断。

日线维度的回扫窗口是各路径自己的序列长度：EOD 用 90 个交易日的展示帧、
盘中用约 1 年的实时序列。实测日线状态最长持续段：标的级 45 个交易日
（33 年全历史）、类目级 17~18 个交易日（聚合序列穿越阈值更频繁，反而更
短）——90 日窗口有 2 倍余量。

窗口只影响相位判定，不影响趋势值：`trend_score` 永远是该周期最新值。

## 6. 聚合口径

- 标的层：直接用该标的自己的滚动序列（单成员，不做聚合）；
- 类目层（L2/L3）：按交易日做**成交额加权**平均（`aggregate_daily`，与类目
  `trend_score` 同口径），再对聚合序列求相位。成交额缺失/≤0 的成员当日不
  计入；全部成员都无权重时该日为 NaN（相位按断点处理）。

聚合与展示共用 `services/dashboard_common.aggregate_daily`（原
`services/dashboard._aggregate_daily` 迁入并参数化 metrics）——同一个函数
既产出展示帧的日频指标，也产出相位索引的周/月序列，避免第二份聚合实现。

## 7. 测试

- `tests/unit/test_trend_phase.py` — 三态边界（阈值本身算「无」）、持续天数
  （转入当日 = 1、NaN 断点、预热期忽略、末尾 NaN 取最后一个有效值）、
  `previous_phase`（持续多日时等于当前相位、切换当日与当前不同、前一根无值
  时为 null）、空序列的稳定形状、`trend_periods` 三维度恒在。
- `tests/integration/test_dashboard_phase_index.py` — 标的层用自身序列；类目层
  是**加权**而非简单平均（权重悬殊时结论相反）；无权重的类目为 null；空表 /
  空标的映射的退化；窗口外的旧行不参与。
- `tests/test_subject_market.py` — EOD 看板装配：字段存在、三维度阈值、标的
  级与类目级的周/月相位来源。
- `tests/integration/test_intraday_service.py` — 盘中口径：日线维度取自实时
  序列（末值 = 行内 `trend_score`）、周/月取自物化表、缺滚动数据的标的回落
  空 bundle。

## 8. 后续（前端展示）

标的大盘页面（`/subject-market`）暂未展示上述信息。前端可用素材已经齐备：
每个标的/类目行的 `trend_periods` 是三个标量 bundle，适合做「三维状态徽标」
或按某维度着色的热力图。若采用新的着色维度，注意类目行进而是加权聚合值。
