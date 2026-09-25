# Round 18 审查报告（loop-review-ds4f）

> **状态：CLOSED（2026-09-26 闭合）**
> - 修复清单与验收记录：`round18-fixes.md`；
> - 全量回归（修复后）：见 `round18-fixes.md`；
> - ruff：(file, rule) 集合与基线 78/78 一致（新增 0 条）。
>
> 日期：2026-09-26
> 审查对象：commit `8eee575`（Round 17 闭合后的 HEAD）
> 审查方式：**R18A「证伪 R17 三项修复」**（判 NOT CLEAN，1 P2）与
> **R18B「研究结论可信度专项（统计判据）」**（判 NOT CLEAN，2 P2）。

## 总体结论

**NOT CLEAN（3 项 P2）**——三条都是材质性的：

| # | 问题 | 影响 |
|---|---|---|
| R18A-F1 | 旧栈**买入持有基准**仍以 100 股为科创板建仓（`_buy_and_hold_benchmark` 拿不到 symbol） | 生产 **42 个格子**的 `benchmark_*`/`excess_*` 列偏差；至少 1 格 `excess_annual_return` **变号** |
| R18B-P2-1 | `confirmed` 门里的 **DSR（尝试次数校正）条件恒真** | 平台**实际上没有任何多重检验折扣**（实测 `dsr=1e-300` 仍判 confirmed；论文口径 ≥0.95） |
| R18B-P2-2 | plateau 1σ 判据在邻域点 <3 时**双向失效** | 单点邻域（合法配置）→ 孤立峰被**静默**记"高原"；2 点邻域 → 真高原 **56%** 被判"孤峰"并写入 append-only 台账的"疑似过拟合" |

### R18A-F1（P2）旧栈基准腿的科创板建仓

- 事实（真行情重放）：`688802.SS`（首收 829.9）与 `688795.SS`（首收 600.5）在 10 万初始资金下
  只够 100 股 → 基准腿以 100 股"买入持有"，而 100 股在科创板不可申报。修复后同口径应为
  **全程现金**（`benchmark_total_return=0`）→ `688795.SS` 的
  `excess_annual_return` 由 −0.0927 → 约 +0.02（**变号**）。
- 性质：与 R17A-F2 同文件同类（第二条定量路径），R17 的修复只覆盖了策略腿。

### R18B-P2-1（P2）DSR 门恒真

- 位置：`src/research/verdict_rules.py`（`min_dsr_on_diff: 0.0`）+ `paired.py`（`dsr_on_diff`）
- 事实（判定器实证）：`suggest_backtest_verdict({delta_sharpe:0.5, plateau:plateau,
  paired:{t_stat:2.5, dsr_on_diff:1e-300}})` → **confirmed**；DSR = Φ(z) ∈ (0,1] 与阈值 0 做
  `>` 比较恒真，且 t 门通过已蕴含 z>0。按论文口径（Bailey & López de Prado 2014）N=10 时
  DSR=0.527、N=1000 时 0.054，均不显著，而当前实现照判 confirmed。
- 影响：`confirmed` 落定时**不做选择偏差折扣**；本项与"课题级 BH-FDR 仅展示"叠加后，
  平台全链没有有效的多重检验闸门。当前库内唯一定论为 inconclusive，**暂无物化结论被翻转**。
- **处置**：阈值属于研究纪律（会改变平台能确认什么）→ 记为 **R18-D-1**（含建议）；
  本轮做**机械且无损**的一半：把"该门在阈值 0 下无约束力"写进规则注释、
  新增 `dsr_gate_binding()` 并把该标记落进 verdict 证据（可审计，避免把"无折扣"误读成"已校正"）。

### R18B-P2-2（P2）plateau 判据的双向失效

- 单点邻域：合法配置（`max_positions=1`/`top_n=1`/`per_day=1`/`n=1`）使 `plateau_neighbors`
  只剩 1 点 → `neighbor_std=0` → 旧实现 `deviates=False` → 判定仅由单点符号决定 →
  孤立峰被静默记为"高原"、台账永记"非孤峰"。
- 两点邻域：σ 由 df=1 估计，H0 下 1σ 规则的误判率实测 **56%**（与噪声尺度无关：
  真效应 0.2/0.5/1.0 分别 0.526/0.561/0.565）→ 真高原半数被挡、并写错误定论进台账。
- **处置**：邻域点 <3 时**停止判定**（`unknown` + `reason=neighbor_points_insufficient(n)` +
  `insufficient_neighbors=True`），与该函数对"无邻域点"的既有口径一致（可见告警、不当作已通过）；
  ≥3 点保留设计口径的 Alvarez 1σ 判据。

## R18B 的其余核验（正面结论）

统计件的**公式层逐位正确**：PSR/DSR/MinTRL 按论文从零实现对拍（差异仅来自偏差校正与 ddof=1，
第 4 位小数）；CSCV 的 S 块枚举/IS argmax/OOS 相对秩/并列半步/退化列归 NaN 全部正确
（真实 10×2430 矩阵，S=10/16 逐位一致）；BH-FDR 与 BH1995 原文在 m=5~1000 与边界
（并列/全 0/全 1/0.049-0.051/1e-300）**逐位相同**；holdout 窗口判定与 token/attempt_index 口径
经真实实验复算一致；**E0001 物化 verdict 的每个数字**（t_stat/ΔSharpe/置信带/自举点估计/回撤）
都能独立复现。

## R18B 的 backlog（不阻断）

`pbo_cscv` 的 `lambda_median` 是 0-based rank 而非论文的 logit 口径（PBO 本体等价）；
`_PCT20_PARAMS` 是死常量；`same_direction` 把邻域 Δ 恰为 0 判成"反向"（保守）；
`research/topics` 下三个同号 T001 目录（含测试遗留夹具产物）；
`mintrl` 以"基准已实现 Sharpe"为 SR*（仅展示件）。

**E0002 卡在 `running`（01:0x 起）**：经查是本次审计期间代理跑真实验留下的
（进程结束而实验未收口）——平台有**启动收割**（`lifecycle`：进程死亡遗留的 running 实验 →
failed），下次服务启动会自动清理；与"日更漏跑"同因（服务不在线）→ 一并进待决策。
