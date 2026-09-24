# Round 5 审查报告（loop-review-glm53f，确认轮 2）

> **状态：CLOSED（2026-09-25 闭合）**
> - R5A-1（性能预算缺自动守护）已修：slow 钉补 elapsed_s 存在性 + ≤240s 断言
>   （阈值口径注释在位）；
> - 验收子代理 R5V：变异双向实证（elapsed=999 与缺键两形态均被断言捕获）、
>   slow 钉实跑通过、skip 守卫确保干净环境零 flake → **R5V_VERDICT: PASS**；
> - 验收子代理 R5W：改动范围仅限目标测试文件断言增强、integration 219 passed、
>   四策略 elapsed_s 24.4~52.6s 对守护线余量充足、无冲突断言 → **R5W_VERDICT: PASS**。
>
> 日期：2026-09-25
> 审查对象：commit `23617ac`（Round 4 闭合后全量代码）
> 审查方式：2 个独立确认审查代理——R5A 全景式决策-代码映射审计（架构稿 20 条已定论决策逐条映射 + §7.1 性能预算守护 + §2.8 存量影响表逐行核对）、R5B 随机化属性测试扫荡（6 大不变量 × 4400+ 次随机迭代，全部使用 src/ 真实函数）。

## 结论

**有 1 项新的 P3 问题（当轮修复）**；其余全部确认无问题。

## 发现与修复

| # | 级别 | 问题 | 修复 |
|---|---|---|---|
| R5A-1 | P3（Low） | 详设 §7.1 性能预算（5y×874 ≤2min，决策 C5 写死目标）缺自动化守护：comparison 产物已带 elapsed_s 却无消费方断言，内存预算无观测点——未来改动悄然击穿预算时零信号 | slow 钉 `test_v1_sample_acceptance_artifacts` 补 elapsed_s 存在性 + ≤240s（10 年窗口按 §7.1 的 120s/5y 比例放宽）断言；内存观测记为运行期增强（R5-N-1，resource.getrusage 为 Unix-only，Windows 需 psutil，不引依赖） |

R5B 附带一条非阻塞建议已记录：`portfolio/reports.py::pair_round_trips` 无直接确定性单测（现有覆盖为 cost_drag 钉间接覆盖 + R5B 800 次随机验证），建议后续补直接单测（R5-N-2，随触碰处理）。

## 确认无问题的方面

1. **20 条决策审计表（R5A）**：17 条已落实（逐条给出文件:行号级证据），3 条涉二期豁免（决策 2 数据线、决策 12 因子工业化、决策 17 ignore 口径挂 R4-D-1）——"无一条声明落实但查无实据"。
2. **§2.8 存量影响表逐行核对**：L1 迭代（新栈期间 core/adjustment 未动）、L1.5/L2/L3/L4 纯新增、仓位管理删净、旧引擎/止损双实现原样保留——逐行一致。
3. **分层铁律独立复扫**：engine/portfolio/research 零条 src.data 直连。
4. **6 大不变量随机扫荡（R5B）**：费用守恒、现金不变量（成交 cash_after≥0 精确成立、unfilled 零副作用、max_affordable"可负担且最大"双向验证）、T+1（sellable≤quantity、当日买入 sellable==0）、PSR/DSR/MinTRL 值域（含对抗性输入）、round trips FIFO 手算一致、涨跌停推导（limit_up≥limit_down、分位整数倍、标志等价）——4400+ 迭代零违规。
5. **既有测试双保险核对**：六类不变量均有既有确定性测试覆盖（唯 pair_round_trips 直测缺口已记录）。

## 回归

全量 `pytest tests/ -q`（Round 4 修复后基线）：1497 passed / 2 failed（已知 Windows flake）/ 6 skipped。R5 修复为 slow 钉内断言，仅影响 slow 路径（实跑通过）。
