# Round 7 审查报告（loop-review-ds4f）——确认轮 2

> **状态：CLOSED（2026-09-25 闭合）**
> - 修复清单与验收记录：`round7-fixes.md`；
> - 全量回归（修复后终跑）：1643 passed / 1 failed（既有 Windows flake）；
> - ruff：(file, rule) 集合与基线完全一致（新增 0 条）。
>
> 日期：2026-09-25
> 审查对象：commit `cd09ddc`（Round 6 修复后的 HEAD）
> 审查方式：**两个独立确认代理（R7A/R7B）**。R7A 沿"退化腿"主题追消费面；R7B 从
> **统计/纪律侧**入刀（PBO、FDR、置信带）。全部结论在真 pipeline、真物化产物、
> 真实落库记录上复现；钉子有效性用变异实证。

## 总体结论

**NOT CLEAN（3 项 P2 + 3 项 P3）**——全部属于同一个缺陷类的**第四次显形**：退化腿
（零成交/全现金/极短窗口）的浮点噪声仍在部分消费面上决定判定与产物。

**根因诊断（本轮最重要的产出）**：Round 5/6 的做法是"**逐点修**"——每发现一个消费面
就补一处判断。而退化判定天然有**两条腿**（NAV 判据 + Sharpe 判据）且消费面有十几处，
逐点修必然漏。因此本轮起改为"**单点收口**"：判据单点实现 + Δ 语义单点实现 +
持久化可见性统一 + 逐消费面接线。

---

## P2

### R7-F1 `head_to_head` 的退化分支只清了局部变量，持久化证据里噪声原样留存

- 位置：`src/research/evaluations/head_to_head.py`
- 事实（真 pipeline）：退化分支只把**局部变量** `d_band` 置 `None`，而
  `evidence["paired"]["delta_sharpe_band"]` 与 `summary_a`/`summary_b` 的
  **6.1e12** 级 Sharpe **仍被持久化**（与父提交逐字节相同）；且该修复当时只有
  **源码文本**钉子。
- 修复：清零动作前移到 **evidence/report 组装之前**（`d_band`/`psr_ab`/两侧 summary
  的 sharpe·sortino 全清）；钉子改为**顺序断言 + 载荷字段断言**。

### R7-F2 噪声经 `stats.*` → `1-psr` 作 p 值 → 课题级 BH-FDR 把零成交实验算成"显著"

- 位置：`src/research/stats/`（psr/dsr/mintrl/sharpe_bootstrap）+ `src/research/conclusion.py`
- 事实（真 pipeline + 物化产物）：退化腿的噪声不止在 summary——`stats.psr`/`dsr`/
  `mintrl_days`/`sharpe_bootstrap` 同样是 6e12 级，经 `conclusion.py` 的 `1-psr`
  当作 p 值 → 课题级 BH-FDR 把**零成交**实验算成显著（实测 `still_significant=2`，
  并写进 `TOPIC.md`）。**这是本轮最危险的一条：噪声反向污染研究结论的物化产物。**
- 修复：`stats.*` 退化时统一记 `None`；`evidence["degenerate_legs"]` 机器可读；
  `conclusion` 跳过退化腿的 p 值；`benchmark_relative` 的 beta/alpha 加相对方差守卫
  （此前实测 beta=8.2e12）。

## P3

### R7-F3 段内退化：`_regime_segment_metrics` 的 1.7e13 噪声把 `confirmed` 压成 `inconclusive`

- 事实（真 run）：整段零成交时段内 Sharpe = 1.7e13，段 ΔSharpe 实测 **−1.88e13**，
  collapse 门据此把 `confirmed` 降级。
- 修复：段内退化判定 → `sharpe=None` + `sufficient_sample=False`（复用 R1-P3-15 的
  排除机制）。

### R7B #1 `pbo_cscv` 把 OOS 名次并列计为"更差" → `pbo=1.0`（假的"必然过拟合"）

- 事实（真 pipeline）：4/6 探针 NAV 与主选**逐位相同** → OOS 名次全并列 → λ=0 →
  持久化 `pbo=1.0`，与同批证据里的 `plateau=plateau` **自相矛盾**；极端情形是
  "所有变体是同一条 run"。
- 修复：并列按**半步**计（λ=0.5）；新增 `degenerate_variants` 标记；钉子覆盖
  "全并列"与"真过拟合"两侧。

### R7-F4 三处钉子失效

| # | 形态 | 证据 |
|---|---|---|
| 1 | engine 守卫钉用了**违反 CHECK 约束**的值 → 测的是列约束不是触发器 | 2/10 条腿可删而测试仍绿（变异实证） |
| 2 | 退化告警与 h2h 只用**源码文本**断言 | 改实现不改文案仍绿 |
| 3 | `_pool is None` 回灌点**无钉子** | 删掉后无测试变红 |

修复：engine 钉改合法值（`status='rejected'`/`reason='limit_down'`）；h2h 钉改顺序 +
载荷断言；补 h2h/告警/FDR 的载荷钉子。

### R7-F5 退化判定公式在两个模块各抄一份

- 同类问题（同一公式多处实现）已复发三次 → 抽 `_common.is_degenerate_leg` /
  `null_degenerate_metrics` 单点实现。

## 本轮"单点收口"清单（回答"为什么反复复发"）

| 环节 | 单点实现 |
|---|---|
| 判据 | `research/evaluations/_common.is_degenerate_leg` |
| Δ 语义 | `_delta_or_none` / `_deltas_from_summaries` |
| 持久化可见 | `evidence["degenerate_legs"]` + warnings（含 `plateau.skipped`） |
| 下游消费 | `conclusion`（FDR）、`_regime_split`（collapse 门）、`benchmark_relative`（beta·alpha）、`head_to_head`（置信带/PSR/summary） |

## 代理同时确认的正面结论

- 生产库与 03:00 备份 **50/50 张表行数一致**；11 个 `trg_engine_*` 守卫与已落定
  verdict 子句**均在生产库生效**；冗余索引已删。
- 代理的独立数值复算（reports / parity / nav-summary / stats / tradability）**零不匹配**。
- 两轮全量套件零漂移（1637/2 → 1642/2），失败均为既有 flake。

## 闭合

修复见 `round7-fixes.md`，提交 `8a5aca2`；回归 1643 passed / 1 failed；新增钉子 5 项。
