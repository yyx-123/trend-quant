# Round 16 修复与回归（loop-review-ds4f）

> **状态：CLOSED（2026-09-26 闭合）** —— 审查报告见 `round16-review.md`
> 日期：2026-09-26

## 修复总览

| 项 | 级别 | 修复 | 触及文件 |
|---|---|---|---|
| **R16-D-1** 科创板最小申报 200 股未实现（产出必被拒的 100 股"成交"） | P2 | 新增 `engine.profiles.min_buy_qty(symbol, asset_type=..., lot_size=...)`（科创板股票 200，其余 100）；`match_buy` 在**数量意图**与**金额意图**两条路径上都拒绝低于最小申报的委托（新增 `unfilled.reason = "below_min_order"`，并保留"不足一手 → `lot_rounding`"的原语义，不把数量自动抬到 200 以免超预算）；清单侧同样不下发 <200 股并在 `caveats` 留可见理由 | `src/engine/profiles.py`、`src/engine/matcher.py`、`src/portfolio/live.py` |
| **R16B-B2** 部分 `rebuild_all` 冲掉"需全量重建"标记 | P3（存量缺陷） | `rebuild_all(..., partial=False)`；部分入口（`rebuild_after_backfill`、日更尾 `targets`、`scripts/migrate_raw_qfq.py`）传 `partial=True` → **只有全量重建才登记 default 参数集** | `src/services/indicator_builder.py`、`scripts/migrate_raw_qfq.py` |
| R16-D-1b 2023-08-10「100+1」递增未建模 | P3 → **待决策** | 本轮不改（会改变 2023-08-10 之后全部买入股数 → 属重基线范畴），记入 R16-D-1 | — |

## 新增钉子（3 条，全部变异实证）

| 钉子 | 断言 | 变异实证 |
|---|---|---|
| `test_star_min_order_qty_is_enforced` | `min_buy_qty` 分品种正确；科创板：金额意图只够 100 股 → `unfilled(below_min_order)`、数量意图 150 股 → 同、预算够 200+ → 成交且 qty ≥ 200；非科创板 100 股照常成交；不足一手仍是 `lot_rounding` | 最小申报退回 `lot` → **红**；去掉 `below_min_order` 分支 → **红** |
| `test_live_buy_list_respects_star_min_order` | 打桩 `select_entries` 产出"科创板 100 股 + 对照 ETF 100 股"：科创板**不下单**且 `caveats` 含"最小申报"，ETF 照常下单 qty=100 | 清单侧守卫去掉 → **红** |
| `test_partial_rebuild_does_not_clear_drift_flag` | 部分重建后漂移标记仍为 True；全量重建后清掉 | 部分重建也登记 → **红** |

## 回归结果

- 相关面：引擎（fees/matcher/golden/stops-parity/parity 集成）+ live_runner + r11~r16 钉子文件全绿；
- 全量套件：**1693 passed / 1 failed**（既有 Windows 临时文件 flake）；
- ruff：`(file, rule)` 与基线 78/78 一致（新增 0 条）；
- 变异实证：本轮 **4 个**语义变异全部被抓住（含 B2 的那条）。

## 待决策新增

- **R16-D-1（2023-08-10「100+1」）**：非科创板买入应"100 股起、1 股递增"。现实现按 100 股整数倍
  向下取整 → 系统性少买 ≤99 股（≈≤0.1% 仓位）。**建议**：与历史重基线（R15-D-1）一并做——
  改它会让 2023-08-10 之后的所有买入股数、进而 NAV 变化，属"更准确但需重述数字"。
- **R16-D-2（科创板 ETF 的申报单位）**：本轮只按"股票 688xxx = 200 股"判定；科创板 ETF
  （588xxx）与其它 ETF 仍按 100 份（当前生产池无 588 成交，且 ETF 申报单位规则与股票不同，
  若将来交易科创板 ETF 需再核对该品种的申报单位）。
