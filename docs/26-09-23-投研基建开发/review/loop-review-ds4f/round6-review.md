# Round 6 审查报告（loop-review-ds4f）——确认轮 1

> **状态：CLOSED（2026-09-25 闭合）**
> - 修复清单与验收记录：`round6-fixes.md`；
> - 全量回归（修复后终跑）：1637 passed / 2 failed（失败均为既有 Windows 临时文件 flake）；
> - ruff：(file, rule) 集合与基线完全一致（新增 0 条）。
>
> 日期：2026-09-25
> 审查对象：commit `f89c913`（Round 4 闭合 + Round 5 新面审查修复后的 HEAD）
> 审查方式：**两个独立确认代理（R6A/R6B）**，任务是复核 Round 5 的 5 项修复与上一轮
> 遗留的 3 项"钉子缺口"是否真的闭合，并做端到端与随机化对拍。所有结论都要求在**真引擎、
> 真 pipeline、真库**上复现；钉子有效性一律用**变异实证**（改坏实现看测试是否变红）。
> 主审人复核全部结论并自行二次复现关键项。

## 总体结论

**NOT CLEAN（1 项 P1 + 1 项 P2 + 1 项 P3 + 3 项钉子缺口）**。

两个代理都判 NOT CLEAN。最有价值的一条是 **R6-P1-1：我在 Round 5 引入的 P1 回归**——
退化腿修复把 `sharpe` 记 `None` 之后，仍有两处消费点直接 `float()` 相减，**恰好把"被修
的那个场景"打成 `status=failed`（TypeError）**。这说明"逐点修"的做法会把修复本身变成
新缺陷，成为后续 Round 7~9 改为单点收口的直接动因。

---

## P1

### R6-P1-1 退化腿置 None 后，两处 Δ 仍直接相减 → 零成交/全现金场景反而变成 `failed`

- 位置：`src/research/evaluations/backtest.py:460`（walk-forward 折 Δ）、`:727-729`（高原探针 Δ）
- 事实（真引擎 + 真 pipeline 对照，A/B）：同一实验在父提交 `f89c913` 上落到 `evaluating`，
  在 Round 5 修复后的 HEAD 上落到 **`failed`**，异常为 `TypeError`（`None - float`）。
  触发条件是**零成交/全现金腿**——正是 Round 5 专门要修的那一类。
- 性质：**Round 5 修复引入的回归**，且是"修复只改了生产点、没改消费点"的典型形态。

## P2

### R6-P2-2 `head_to_head@1` 仍用原始 `compute_summary` 作差 → 同一类噪声决定其判定

- 事实（真 pipeline 探针）：正常腿 vs **全现金腿** → 判定 `rejected`，ΔSharpe **−6.09e12**，
  置信带整段是浮点噪声。即"用噪声差值否决一条正常策略"。
- 与 R6-P1-1 同根：退化判定的"生产点"与"消费点"不同步。

## P3

### R6-P3-4 `import sys` 位置引入 ruff I001

- 位置：`tests/integration/test_critical_paths.py`（Round 5 的 `sys.executable` 修复附带）。
- 修复：移入 stdlib 块；ruff 与基线持平。

### 钉子缺口（3 项，均为"删掉实现仍绿"）

| # | 缺口 | 变异实证 | 修复 |
|---|---|---|---|
| 1 | engine 子表守卫只钉了 2/5 张 | 删掉 `orders/unfilled/positions` 的守卫后测试**仍绿**（M11 存活） | 钉子扩展为**五张子表**逐表 UPDATE/DELETE |
| 2 | `stop()` 的"被取消 future 回灌"只断言了集合、没断言 `_queue` | 删掉回灌那一行**仍绿**（M15 存活）——**正是 V10 抓到的缺陷形态** | 追加"回灌必须落回 `_queue`"的 FIFO 断言（变异后失败） |
| 3 | "已落定 `final_verdict` 不可改写"的**库层**守卫只被源码注释钉住 | 删掉 WHEN 子句后 3 条相关钉子**仍绿**，实测**可改写已定论行**（M24 存活） | 新增**行为**钉子：已落定后直改 `final_verdict` 必须被库层拒绝，白名单列仍可写（变异后失败） |

## 代理同时确认的正面结论

- Round 5 的 5 项修复在**端到端层面成立**：真引擎零成交场景现在记 `None` 并落警告；
  `target_weight.mode` 与机制钉、五表守卫、`listing_known`、表单上限、MCP 无 SQL、
  `sys.executable` 全部通过。
- **4 类随机化不变量对拍，0 不匹配**：150 随机 NAV × 18 指标、400 随机订单的费用模型、
  200 随机序列的指标原语、150 随机序列的统计件。
- 跨进程确定性：NAV 2400 行 × 6 字段**位级一致**。
- 敌意输入：106 个敌意 HTTP 请求（无 5xx）、135 个敌意 MCP 调用、9 个敌意 CLI 调用
  全部符合预期。
- 与 `f93031e` 的 A/B：**12000/12000 NAV 单元格位级一致**。

## 闭合

修复见 `round6-fixes.md`（`_delta_or_none` 统一 None 语义并接入三处 + `skipped` 计数 +
持久化 `degenerate_leg(...)` 警告 + h2h 强制 `inconclusive` + 3 项钉子补强），
提交 `cd09ddc`；回归 1637 passed / 2 failed（既有 flake）；新增钉子 8 + 3 项。
