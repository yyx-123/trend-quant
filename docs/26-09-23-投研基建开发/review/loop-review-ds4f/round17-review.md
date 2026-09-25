# Round 17 审查报告（loop-review-ds4f）

> **状态：CLOSED（2026-09-26 闭合）**
> - 修复清单与验收记录：`round17-fixes.md`；
> - 全量回归（修复后）：**1693 passed / 2 failed**（均为既有 Windows 临时文件 flake）；
> - ruff：(file, rule) 集合与基线 78/78 一致（新增 0 条）。
>
> 日期：2026-09-26
> 审查对象：commit `40b855e`（Round 16 闭合后的 HEAD，审到的是 `ac0dd7e` 的代码态）
> 审查方式：**R17A「证伪 R16 两项修复」**（判 NOT CLEAN，2 项 P2）与
> **R17B「出口数字一致性审计」**（见文末补记）。

## 总体结论

**NOT CLEAN（2 项 P2）**——两条都直接关系到"我刚才那一轮修得对不对"：

### R17A-F1（P2）我自己的补丁：`quantity` 路径的现金递减后未复检最小申报

- 事实（对**已提交的 `ac0dd7e`** 独立复核）：`match_buy` 的数量意图路径把
  `qty < min_qty` 的检查放在**现金递减之前** → `688498.SS, bar_close=10.0, quantity=1000,
  cash=1500`（可负担 100 股）→ **`filled qty=100`**；同一现金下 `target_value` 路径
  （检查在递减之后）→ `unfilled(below_min_order)`。**两条路径语义不对称**。
- 处置：这条漏点我在 R17 开工时已自查并修（工作树里 `qty = min(qty, affordable)` 之后
  补了复检），但 R17A 指出两点要认：① 修复**未提交**，所以"Round 16 已闭合"的说法在
  提交态上不成立；② 我的钉子断言过松（`reason in ("below_min_order","insufficient_cash")`），
  返回 `insufficient_cash` 的变异也能过 → 已收紧为 `== "below_min_order"` 并补
  "意图 200 股 + 现金只够 199 股"的精确用例。

### R17A-F2（P2）旧栈 `rule_backtest` 是**第二个下单入口**，336 笔 100 股科创板"成交"

- 位置：`src/rule_backtest/engine.py` 的 `_resolve_buy_qty` / `_max_buy_qty`
  —— 签名里**没有 symbol**（`(cash, reference_price, day_str, execution)`），
  `lot_size` 默认 100、无最小申报概念 → 结构上无法分品种。
- 事实（生产库只读扫描）：`batch_backtest_cells` 的 688/689 格子 2688 个、买入成交
  141,663 笔，其中 **qty=100 的 336 笔**，涉 5 只：688802.SS 131、688795.SS 165、
  688498.SS 5、688809.SS 33、688111.SS 2（例：688802.SS 2026-06-16 买 100 @728.76）。
- 影响：旧栈是**活跃消费面**（单策略 API / 批量回测 / 前端 / CSV），这些是用户看到的
  回测结论数字。R16 的修复只覆盖了新栈引擎与实盘清单，旧栈未被覆盖。

## R17A 的其余核验（正面结论）

- 科创板 200 的判定正确：688/689 与 `asset_type="Stock"`（大小写）→ 200；588 ETF 与
  主板/ETF → 100；**科创板卖出不受限**（`match_tail_sell` 100 股、`match_intraday_stop`
  50 股均正常成交，零股一次性卖出合法）。
- `target_value` 路径：预算 1003（≈100 股）→ `unfilled(below_min_order)`，**不自动抬到
  200**（不超预算）；预算够 200 → `filled 200`。
- 撮合入口穷举：`match_buy`/`match_tail_sell`/`match_intraday_stop` 之外无其它调用点；
  intent 生产者只产出 `quantity|target_value`。
- R16B-B2 的修复成立：三个部分入口都正确传 `partial=True`，`register_default_param_set`
  全仓仅 1 个调用点；"部分重建保持标记 → 每次启动全量重建"的担忧不成立
  （生产库 `trend_param_sets.default` 与当前配置逐字节一致 → 漂移标记当前为 False）。

## R17B（出口数字一致性审计）：**CLEAN**

用生产库真实数据逐个出口对差，**未发现会改用户判断/交易动作的数字缺陷**。样本量与结论：

| 出口面 | 样本量 | 差异 |
|---|---|---|
| `/batch-backtest/api/runs/{id}/cells` | 6111 格 × 35 数值列 vs DB（三面对差 HTTP/CSV/DB） | 0 |
| `/annual-aggregates` | 238 个（策略×年份）独立复算 | 0 |
| `/api/compare` | 6097 共同格 × 8 指标 × 3 值 + 6 条 bootstrap CI 精确复现 | 0 |
| 钻取链 `/snapshot → /rule-backtest/api/run → /api/result` | 16 项指标逐位 | 0 |
| `/rule-backtest/api/result` 自洽 | summary vs trades 独立复算（含佣金/印花税/换手） | 0 |
| `/manual-trade/api/trades|evaluate` | 20 笔实盘 + 3 笔持仓 × 多字段 | 0 |
| `/subject-market/api/dashboard` | 874 标的 × 7 数值 | 0 |
| MCP vs HTTP | 874 标的 × 10 字段；指标序列逐点 | 0 |
| 物化产物 vs DB | TOPIC.md/report.json/comparison.json|md 全部一致 | 0 |

backlog（不阻断，均为文案/标注/陈旧产物）：manifest 的 `calibers.fees` 写"pnl 已扣费"但
`realized_pnl` 是毛额；持仓表"仓位占比"分母是**时间筛选后**的持仓（有 >3 个月持仓时会被夸大）；
MCP 与 HTTP 的 `meta.rows` 含义不同（全历史 vs 窗口）；实验 run 的
`resolved_config_yaml.description` 仍是 base 的描述；创建型实验的 `baseline.base` 写
`blank-base@1` 但 `summary` 实为买入持有基准；`pf=999` 直接渲染；`/cells` 的 1 小时缓存与
回填写入不一致；`research/topics` 下三个同名 `T001_*` 文件夹（DB 只有一个）。

**B9（运维，需你处理）**：库内最后一根 K 线为 **2026-09-23**，而 **2026-09-24 是交易日**且
`daily_update` 最后一次 `run_date=2026-09-23` → **日更已漏跑**。所有出口都如实标注了
09-23 截止（无标签与真值不符），但这与"生产服务自 Round 3 起未重启"直接相关：
**服务不在线 → 16:30 定时任务不会触发**。建议尽快重启服务并补跑。

## R17A 的 backlog（不阻断）

- **B1** 清单侧守卫在 `instrument_metadata` 缺失时静默退化到 100（生产不可达：874 行
  元数据 asset_type 无 NULL；但与回测侧"缺元数据兜底 stock"的口径不对称）→ **本轮已修**
  （`min_buy_qty` 改为按代码判定，688/689 一律 200，与 asset_type 是否缺失无关）。
- **B2** `rebuild_all` 的 `partial` 条件有死析取项，且登记不看 `rebuilt/failed`
  → **本轮已修**（`if not partial and rebuilt > 0 and failed == 0:`）。
- **B3** 若修 F2，旧栈那 336 笔所在格子的数字需重述（属重基线成本）。
