# Round 21 审查（loop-review-ds4f）

> **状态：CLOSED（2026-09-26 闭合）** —— 修复与回归见 `round21-fixes.md`
> 日期：2026-09-26
> 代理：R21A（证伪 R20 修复：因子链路 + 告警面）、R21B（前端通道 + MCP 通道专项）
> 范围：R20 提交后的工作树（`src/data/service.py`、`src/portfolio/backtester.py`、
> `src/rule_backtest/{metrics,service}.py`、`src/trend_mcp/research_tools.py`、
> `web/static/js/market_view.js`）

## 0. 本轮方法

R20 的两个 P1/P2 都属于"**上一个修复没有真正修完**"的类型（守卫只拦住一半、
告警读错参数名），所以本轮定为**证伪轮**：不再找新面，而是把 R20 修复逐条
搬到**真入口**上重放，看问题是否还在。

- R21A：以 `DataService` 真实调用链（`sync_ex_factors` → 调用方 → `rematerialize_qfq`
  → qfq 表）为对象，逐位复现 R20 描述的 −66.94% 假断裂；对告警面用真配置
  （多 heat_cap 门）核对数字。
- R21B：以浏览器**实际拿到的载荷**（slim 结果）为对象，逐列核对成交明细的
  持有天数/浮盈/回撤来源；对 MCP 通道用真调用（策略晋升失败）核对错误文案。

## 1. R21A 发现（证伪 R20 修复）

### R21A-F1（P1）R20 的因子清空守卫只拦住一半：返回值仍把空列表交给 qfq 物化

R20 的守卫拦住了 `db.replace_ex_factors` 这一步，但 `sync_ex_factors` **返回值**仍是
上游给的空列表；日更调用方拿返回值去 `rematerialize_qfq(symbol, factors)` →
`compute_qfq(raw, [])` 把整段历史写成**不复权** → R20 描述的同一根真除权 bar
（2026-08-12 附近）仍表现为 −66.94% 的假断裂。

- 复现：本地有因子 `[(2024-03-04, 2.0)]`、上游返回 `[]` → 因子表保住了，
  但 qfq 表被整段改写为不复权。
- 同一入口的第二半：`rematerialize_qfq(symbol, factors=[])`（显式空列表，
  例如调用方从"上游返回空"推导而来）与 `factors=None` 语义不一致——
  `None` 会回读库内因子，`[]` 则按"无因子"物化。

### R21A-F2（P1）判据只看"列表为空"，上游**部分**截断可绕过

守卫判据是 `not factors`（空列表/缺键），而上游最常见的缺口形态是**部分返回**：
仍给若干条，但丢掉大因子那条（例如只回 `[(2024-06-03, 1.1)]`，丢掉 `2.0`）。
这类响应"非空"，一路通过守卫 → 本地因子日期集合被**缩短** → 与 R20 同源的
假断裂，且更隐蔽（因子表非空，事后核对看不出来）。

### R21A-F3（P2）多个 heat_cap 门时告警被整条抑制

`heat_cap_of(config)`（R20 新增）取**第一个** heat_cap 门的 `max_heat_pct`。
声明多个门时实际生效的是**最紧**的那个（准入时每个门都会卡控），取第一个会
把告警按宽松阈值判 → 实测两门 `0.25 / 0.06`：0 条告警，而按 0.06 判真值
110/243 天越线。

## 2. R21B 发现（前端通道 / MCP 通道）

### R21B-P2-1（P2）成交明细的持有天数/浮盈/回撤在浏览器按**当前 K 线**现算

`web/static/js/market_view.js` 的成交明细表里，三列（持有天数、最大浮盈、
最大回撤）和本次收益都取自 `currentPayload`（屏幕上那根 K 线的载荷）：

- 后端把 `daily_nav` / `charts` 在传输时剥掉了（`slim_backtest_result`，slim 的
  初衷是控制 frp 链路体积），浏览器**根本拿不到回测自己的日线**；
- 于是这些数字随**显示周期**变化：切到月 K 后，`date → index` 的映射落空，
  持有天数退化成**自然日**天数（月 K payload 下实测 2/3/3，引擎真值 44/60/60），
  浮盈/回撤按月 K 的 high/low 算（实测 0.5% vs 引擎 6.05%）；
- 图表的区间与回测区间不一致时（常见：图看 1 年、回测 5 年）同样静默错数。

### R21B-P2-2（P2）MCP 通道把业务错误当"内部错误"

`src/trend_mcp/research_tools.py::_error_payload` 的白名单里没有
`StrategyConfigError` / `ModuleRegistrationError`（策略晋升/载入路径抛的就是这两个，
都是 `ValueError` 家族的业务错误）→ MCP 回 `internal error (see server logs)`，
而 CLI 同一次调用给出可读原因（如 `position_risk: param atr_mul: -1.0 < min 0.1`）。
对 AI 客户端的后果是**误判成平台故障并盲目重试**，而不是修正配置。

## 3. 顺带发现（全量回归暴露，非本轮修复引入）

### R21-巡-1（P3，测试卫生）集成测试漏出真实"当日补跑哨兵"线程 → 整目录跑必红

`tests/integration/test_critical_paths.py::test_freeze_gate_applies_even_for_force`
真起哨兵线程；用例结束后 monkeypatch 解除，线程看到"已解冻"→ **真跑一次日更**
并长期占住 `jobs._catchup_sentinel` 单例。之后
`tests/integration/test_review_r3.py::test_freeze_defer_spawns_same_day_catchup`
的 `_spawn_same_day_catchup` 被幂等早返回，join 到的是**别人的线程** → 必超时。

- 现象：`tests/integration` 整目录跑 `1 failed`，单跑全绿；
- 定位：`--deselect` 掉上述用例后 269 passed（0 failed）；
- 归属：HEAD 基线同样成立（既有测试卫生问题，与 R21 修复无关）；
- 附带风险：漏出的线程理论上会去写真库（本轮核实：**生产库未被写入**，
  最后 bar 仍 2026-09-23、`job_runs` 最后一行 2026-09-23 17:40）。

### R21-巡-2（P3，环境）`test_instruments_bulk_backfill` 在 Windows 上删除临时库失败

后台回填线程持有的 sqlite 句柄活到 `tearDown` 之后 → `WinError 32` 删除失败
（HEAD 基线同样红，属环境差异而非断言失败）。

## 4. 判定

| 项 | 级别 | 结论 |
|---|---|---|
| R21A-F1 | **P1** | 成立（真入口复现：qfq 整段被写成不复权） |
| R21A-F2 | **P1** | 成立（部分截断可绕过守卫） |
| R21A-F3 | P2 | 成立（多门时告警数 0 vs 真值 110/243 天） |
| R21B-P2-1 | P2 | 成立（数值随显示周期变化，静默错数） |
| R21B-P2-2 | P2 | 成立（MCP/CLI 文案不一致） |
| R21-巡-1 / 巡-2 | P3 | 成立（既有测试卫生，本轮一并收口） |

无"记录为待决策点"的新增项（本轮问题均为实现缺陷，无口径/产品决策成分）。
