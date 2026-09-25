# Round 8 审查报告（loop-review-ds4f）——确认轮 3

> **状态：CLOSED（2026-09-25 闭合）**
> - 修复清单与验收记录：`round8-fixes.md`；
> - 全量回归（修复后终跑）：1646 passed / 2 failed（均为既有 Windows flake）；
> - ruff：(file, rule) 集合与基线完全一致（新增 0 条）。
>
> 日期：2026-09-25
> 审查对象：commit `8a5aca2`（Round 7 修复后的 HEAD）
> 审查方式：**两个独立确认代理**。R8B 负责"**存量改动是否被破坏**"（逐 hunk 行为保持、
> 迁移路径、跨进程确定性、随机化不变量、失败模式探针、生产库与代码 DDL 一致性）；
> R8A 负责"**退化腿类是否仍有漏网消费面**"。两位代理互不通气。

## 总体结论

**R8B：CLEAN（零新增真实缺陷）。R8A：NOT CLEAN（退化腿类第 5 次复发，3 个新消费面）。**

---

## R8B 的正面结论（CLEAN 的完整证据）

- **逐 hunk 行为保持**：所有存量改动在真实路径上行为不变；**迁移路径**单独验证——
  用 `f93031e` 的代码建的库被 HEAD 打开后 11 个 engine 守卫全部装上、2 个冗余索引
  被删除、**旧数据 0 丢失**。
- **跨进程逐字节确定性**：NAV / fills / verdict / 物化产物 **4 个文件全等**。
- **随机化不变量 ~1500 条断言零不匹配**：费用守恒、报告指标、统计件、可交易性、
  指标原语。
- **38 条失败模式探针**（holdout 边界与 token、启动收割、坏 `spec_json`、入口拒绝）
  全部符合预期。
- **生产库**：49 张表行数与 03:00 备份一致；29 个触发器定义与代码 DDL **逐字节相同**。

## R8A 的发现（NOT CLEAN）

### R8-F1（P2）`build_report` 绕过 L4 闸门 → 6.1e12 级 Sharpe 与 394 条滚动噪声被发布

- 位置：`src/portfolio/reports.py`
- 事实（真 run 的 `research_verdicts.report_json` + HTTP + 物化文件）：`build_report`
  **从 NAV 重新派生**指标，不经过 L4 的退化闸门 → 持久化/发布 **6.1e12** 级 Sharpe +
  **394 条**滚动 Sharpe 噪声。触发条件很普通：**前 255 日无成交**。
- 修复：新增单点实现 `rule_backtest.metrics.is_degenerate_nav`（相对方差判据 +
  **幅值闸门 `|sharpe| > 50`**）；`build_report` 清零 summary 与滚动 Sharpe；
  `_common` 改为委托同一实现。

### R8-F2（P2）PBO 变体矩阵里全现金列的 1e12 "Sharpe" 赢下每个 CSCV 组合 → `pbo=0.0`

- 事实（真 pipeline 持久化 `pbo=0.0`）：全现金列在 IS argmax 与 OOS 最优上都"胜出"
  → λ≡1 → `pbo=0.0`，即**假的"绝不拟合"**。
- 修复：`_sharpe_vec` 把退化列记 NaN；比较仅在有序列上进行；全退化 →
  `pbo=None` + `degenerate_variants=True`。

### R8-F3（P3）相对判据有"近失配带"：合法低波路径被判退化、噪声路径逃逸

- 事实（真 run 四处）：`std/|mean| ∈ 5.7e-6…1.8e-5` 的**近失配带**里，单调低波路径的
  **8.7e5 级** Sharpe 反而把判定翻成 `rejected`；walk-forward 折 +2.8e6；
  regime 段 Δ −3.9e6 且 `sufficient_sample=true`。
- 修复：把**幅值闸门**（`|sharpe| > 50` 即判退化）接入 `_nav_summary`、regime 段、
  `build_report`；**阈值只剩 1 份实现**。

### R8-F4（P3）`up_capture`/`down_capture` 在退化基准上除以噪声均值

- 事实：实测 capture = 11.4（噪声均值作分母）。
- 修复：与 beta/alpha 同口径——退化腿整组记 `None`。

### R8-F5（P3）钉子缺口：行为的回退可存活 1185 个用例

- 事实：`_regime_segment_metrics` 的**行为**回退（保留标识符、改语义）在变异后
  **1185 个用例仍全绿**。
- 修复：新增 4 条**载荷级**钉子（幅值闸门 / `build_report` / PBO 退化列 / capture）。

## 至此"退化腿"类的单点收口链

判据 → `rule_backtest.metrics.is_degenerate_nav`（唯一实现）；Δ 语义 →
`_delta_or_none`/`_deltas_from_summaries`；持久化可见 → `evidence["degenerate_legs"]`
+ warnings；**全部消费面** → summary / stats / 滚动 Sharpe / regime 段 / collapse 门 /
`benchmark_relative`（beta·alpha·capture）/ `head_to_head` / PBO / 课题 FDR。

## 闭合

修复见 `round8-fixes.md`，提交 `eb1650c`；回归 1646 passed / 2 failed；新增钉子 4 项。
