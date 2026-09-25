# Round 12 审查报告（loop-review-ds4f）

> **状态：CLOSED（2026-09-26 闭合）**
> - 修复清单与验收记录：`round12-fixes.md`；
> - 全量回归（修复后）：**1671 passed / 2 failed**（均为既有 Windows 临时文件 flake）；
> - ruff：(file, rule) 集合与基线 78/78 一致（新增 0 条）。
>
> 日期：2026-09-25/26
> 审查对象：commit `f023cfb`（Round 11 闭合后的 HEAD）
> 审查方式：两个独立代理并行——
> **R12A「对抗验证」**（对 Round 11 的 6 组修复逐一证伪 + 13 条新钉子逐条变异，139 次工具调用）
> 与 **R12B「存量面新审」**（专攻此前从未被系统审查过的面：实盘清单/止损链、导出与只读面、
> 调度与冻结链、取数与物化、MCP/CLI 写路径，148 次工具调用）。

## 总体结论

**NOT CLEAN（6 项 P2，其中 1 项在两个修复轮之间已再次复发）**：

- R12A：退化腿缺陷类**第 9 次复发**——这次漏的是"单策略跑批"整条消费面；
  另抓到因子守卫的**边界仍然漏**；
- R12B：批量导出**跨用户泄露实盘成交**（唯一在已部署 HTTP 面上的隐私问题）、
  实盘清单的三处逻辑错误（老持仓止损被静默取消、T+1 可卖量按最早入场日虚增、
  批次缺运行期冻结门）。

同时，R12A 对 Round 11 修复的**对抗证伪全部失败**（即修复成立）：ETF tick 用精确十进制
参考逐笔核对 269/269 相符、板块幅度 20/20、`engine_runs` 守卫与迁移、parity 方向约束
0/39 假阳性、旧栈闸门六面无逃逸、13 条钉子 14/14 变异全红。R12B 的存量面独立复算也
全部为反证：26 笔真实持仓 × 11 字段 0 差异、全池 875 标的 qfq 与指标重建 0 差异、
调度单飞/冻结哨兵行为正确、导出面噪声已清、研究纪律（holdout/attempt/verdict）无缺口。

---

## R12A（对抗验证）

### R12A-F1（P2）退化闸门只落在批量落库路径——单策略跑批仍把噪声直达前端

- 位置：`src/rule_backtest/service.py`（HTTP 透传）→ `web/static/js/market_view.js:1124→1054`
- 事实（真实引擎 + 生产库取数复现）：3 根 bar 的窗口（IPO 一字板形态）给出
  `benchmark_summary.sharpe = 3680.676614495274`，前端表格直接显示 3680.677；
  同一份响应里**同一买持腿的年度块**已被闸门清成 None——**自证漏面**。
- 可达性：单跑路径无最小 bar 数（`MIN_BARS=60` 只挡批量面，恰好把这类短窗排除在外）；
  生产库 123 只 2023+ 新上市标的 × 13 种窗口实测 58 个"买持腿 |sharpe|>50"构造，
  58/58 判退化。
- 性质：与 R11A-F1 同源（同一 `compute_summary`、同样 1e3 级数值），只是消费面从
  "落库"变成"单跑 API + 前端"——**第 9 次复发**。

### R12A-F2（P2）因子守卫的幅值带挡不住带内脏因子

- 位置：`src/gateway/tradability.py`（`1e-3 ≤ f ≤ 1e3` 的纯幅值守卫）
- 事实：`f=1e3`（含等号，通过守卫）→ `limit_up=0.001` 且 `is_limit_up=True`
  （与守卫注释里 1e12 那条完全同形）；`f=1e-3` → `limit_down=554.4` 且判跌停。
  原理：任何与价格序列不一致的脏因子都会在除权日产出假涨停/假跌停。
- 生产库当前 0 条越界因子（f∈[0.2, 9.97]），属**残余口子**。

### R12A 的 backlog（不阻断，已部分处理）

| 项 | 处置 |
|---|---|
| 因子守卫带值/边界无钉子 | 已改为"两层判据 + 行为钉子"（见下） |
| "calmar 不入闸"的设计无钉子 | 已补 `sanitize_ratio_metrics` 语义钉子（calmar 保留断言） |
| `engine_runs` 守卫的**迁移传播**无钉子 | 已补迁移钉子（DROP 后重开库 → 守卫重装 + DELETE 被拒） |
| `manual_trade` 的幅值兜底是死代码 | 已删（`is_degenerate_summary` 已含同键位判定） |
| round11-review 的"年度噪声进入夏普中位数聚合"说法不准 | 已更正（聚合读 `sharpe`，147 条全在 `benchmark_sharpe`，真实数据从未被污染） |
| manual_trade 注释里 "sharpe=57.4" 不可复现 | 已改为如实记录（R12A 复扫 135 窗口最大 11.2，闸门是防御性的） |
| parity 在真实数据上"一律 saturated / unexplained 含合法项" | 记录在案：与方向约束无关（去约束后逐项相同），判据按 docstring 是 violations+恒等式 |

## R12B（存量面新审）

### R12B-F1（P2）批量导出跨用户泄露实盘成交（已部署 HTTP 面）

- 位置：`src/services/backtest_export.py:106`（`SELECT * FROM manual_trades WHERE status='closed'`）
  + `src/app/routers/batch_backtest.py`（导出端点无权限门）
- 事实（真实库副本 + 新建**非 admin** 用户实测）：`GET /batch-backtest/api/runs/{id}/export?live=true`
  → 200，写出 `live_trades.csv`，15 行覆盖 user_id ∈ {1,3}，而调用者 user_id=4。
- 与全站口径矛盾：其他面都做了逐用户隔离（`close_trade` 403、`list_trades` 按 user_id、
  MCP 按 token 映射）。设计稿把它写成"yyx 那 6 笔的直接量化"= 单用户时代遗留。

### R12B-F2（P2）实盘清单的止损状态用 400 天面板重建——更早的持仓止损被静默取消

- 位置：`src/portfolio/live.py`（`pad_start = 决策日−400 天`）→ `_rebuild_stop_state`
- 事实（真实数据构造）：买于 2025-06-02（早于面板首日）→ 重建账户 `stop=None` →
  该持仓在清单里**永不产生止损卖出意图**（不是显示问题），只有一句 heat 语义 caveat。
  另：买在面板前 ~20 根内时 ATR 不足，止损价与线上持仓页不一致（实测 3.79 vs 3.83）。
- 部署状态：实盘清单链当前未部署（无 `portfolio.live_strategy_version_id`），属潜伏缺陷。

### R12B-F3（P2）同标的多次买入时 T+1 可卖量按"最早入场日"虚增

- 位置：`src/portfolio/live.py`（`sellable = 0 if buy_date >= 最后一日 else qty`，buy_date=最早）
- 事实（真实构造）：`昨日买 1000 + 今日买 1000` → `qty=2000, sellable=2000`，
  清单下发 `executable=true` 的 2000 股卖出（人工照做会被券商拒），且 shadow 现金
  按 2000 股入账 → 买入清单被不存在的现金放大。

### R12B-F4（P2）旧栈批量回测没有任何运行期冻结门

- 位置：`src/rule_backtest/batch_service.py`（`grep run_freeze` = 0 命中；新栈有 4 处）
- 事实：批次实测 37~44 分钟且**逐格惰性读行情**；16:30 日更会整段重写 qfq
  （实测一次因子更新改写 1551/1632 行）→ 批次跨 16:30 时先/后跑的格子可能落在两版价格上，
  而格子行不带版本（只有批次头一个 `data_version`）→ 血缘失真。当前生产批次多在 22:00 后
  跑（未造成实际污染），但无任何机制阻止。

### R12B 的 backlog（不阻断）

未复权实盘价与 qfq 行情混用（既定口径，已在 docstring/manifest 声明）、
`_atr_through` 逐日重算的 O(n²) 性能、`run_post_update_pipeline` 异常只进日志、
哨兵"skipped_already_running 就假定他人会跑 pipeline"的毫秒级窗口、
导出目录相对 CWD 且无体积上限（单批 206MB）、cells.csv 字段口径未文档化。

## 闭合

修复见 `round12-fixes.md`；6 条新钉子全部变异实证；R12A 的 7 项 backlog 中 4 项已处理、
3 项记录在案（含 1 项诚实记录：引擎出口对**策略腿**的接线没有独立夹具）。
