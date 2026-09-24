# Round 3 审查报告（loop-review-glm53f）

> **状态：CLOSED（2026-09-25 闭合）**
> - 修复清单：`round3-fixes.md`；
> - 验收子代理 R3VA（P2/F1-F5）：首轮 **FAIL**（event 接线缺失——panel_warnings
>   收集后未并入 evidence；R3-P2-1 "parse 即拒"声明与实现不符）→ 整改（event
>   补 1 行合并 + 五槽 meta 引用载入期直接拒绝 + AST 钉）→ **R3VA_REVERDICT: PASS**；
> - 验收子代理 R3VB（P3 批 + 回归）：**R3VB_VERDICT: PASS**（13 项全部实证；
>   根目录 2 个失败经 HEAD worktree 对照证实为预先存在的 Windows 环境 flake）；
> - 最终全量回归：1495 passed / 1 failed（已知 Windows flake，仅复现 1 次）/ 6 skipped。
>
> 日期：2026-09-25
> 审查对象：commit `ab53bd7`（Round 2 闭合后全量代码）
> 审查方式：3 个独立审查代理——R3A 评估模块数值深审（event/bucket/distribution/_common 全文 + 74 项合成数据探针）、R3B 策略库与服务面（library/registry/seed/service 全文 + 临时库探针）、R3C 文档-代码一致性与测试稳定性（15 条修复抽查 + 连续 3 次运行稳定性）。

## 总体结论

**有新增问题（R3B 2 项 P2 + R3A 1 项中等级 + 若干 Low/Info + R3C 3 处文档计数漂移）**，但性质继续收敛：R3A 的数值主体（前瞻收益/无条件对照/分桶/regime/分布统计）经 74 项逐值对拍**全部正确**；R3B 的策略库核心契约（config_hash 唯一、版本不可变、promote 血缘门、seed 幂等、可复现性落库）全部成立；R3C 确认 15 条抽查修复全部真实存在、无虚假声明、测试三连跑全绿无顺序依赖。本轮发现集中在边界静默行为与参数域校验。

## 发现清单（已在本轮内全部修复或记录）

### P2（本轮修复）

| # | 位置 | 问题 | 修复 |
|---|---|---|---|
| R3-P2-1 | slots/execution.py + strategy.py + registry.py | 元模块 any_of/all_of "全插槽通用"只注册了 signal/position_risk 两槽；跨槽引用（如 universe: any_of@1）被 meta 豁免放行、registry.get 兜底返回错误槽 spec——"载入即拒绝"推迟成日循环首日 AttributeError | 七槽全部注册（无成员语义槽用显式拒绝工厂）；strategy 层取消 meta 跨槽豁免 |
| R3-P2-2 | registry.py validate_params | 布尔字符串静默归 False：`use_exit: "bogus"` → False 无报错——笔误无声改变策略语义 | 字符串仅接受 true 集合（1/true/yes/on）与 false 集合（0/false/no/off），其余报错；非 bool/str 类型拒绝 |

### 中低级（本轮修复）

| # | 位置 | 问题 | 修复 |
|---|---|---|---|
| R3-P3-1 (F1, Medium) | event.py:209 | context_filter benchmark 校验是死路——gateway 对无数据标的保留全 NaN 列，全部事件被静默剔除成"假 inconclusive" | benchmark close 全 NaN 时真 raise ValueError |
| R3-P3-2 (F2, Low-Med) | _common.py | 流动性过滤在垫片期数据不足（pre_idx<20）时静默跳过，低流动性标的原样入池 | 显式警告经 warnings_out 通道进 evidence warnings；event/bucket 接线 |
| R3-P3-3 (F3, Low) | distribution.py + bucket.py | distribution 的 liquidity_default 不做流动性过滤（全池化口径未声明）；bucket 用字面量 1e8 | distribution 声明"全市场池化口径（§6.5.4）"入 warnings；bucket 改用 DEFAULT_MIN_AMOUNT20 常量 |
| R3-P3-4 (F4, Low) | _common.py | 300 自然日垫片在节假日密集段仅 194~205 交易日，窗口头 1~6 日 regime 不可用 | 垫片 300→320 自然日 |
| R3-P3-5 (F5, Low) | bucket.py（经 core/indicators） | er_10 的 fillna(0) 把 IPO/复牌 warmup 行伪装成"完美无趋势"落最低桶 | _feature_matrix 内加"最近 11 行 close 全有限"有效性掩码（不触存量 core） |
| R3-P3-6 (F6, Info) | event.py | 去重键 (symbol, 事件日) 不含 kind 的口径注释声明 | 已注释声明 |
| R3-P3-7 | registry.py | 数值参数接受布尔（atr_mul=True→1.0）；integer 接受非整 float（12.7→12 静默截断） | bool 显式拒绝；integer 拒非整值 |
| R3-P3-8 | sizing.py target_weight | mode 的 equal 缺省只在工厂内隐式成立 | schema 显式 default=equal |
| R3-P3-9 | service.py | window_kind 未在 L3 卡枚举，直调可写任意值进血缘 | L3 侧枚举校验（与 research_runs CHECK 同源） |
| R3-P3-10 | db.py | engine_runs（可复现性锚点 resolved_config_yaml）无白名单守卫触发器 | 补 guard_update 触发器（DROP+CREATE，白名单=status/finished_at/error） |
| R3-P3-11 | modules.py | load_reviewed_modules 的 register 不幂等且在守卫外 | replace=True + try 包裹逐条隔离 |
| R3-P3-12 | library.py | retired 策略线仍可 add_version（软删除语义=不再生长未显式化） | 退役线新版本显式拒绝（已有版本不受影响） |
| R3-P3-13（R3C） | round1/round2-fixes、开发日志 | P3 计数三说不一（19/20/22、10/12）、弱断言 3/4 处 | 文档计数订正 |

### 记录在案（本轮不修，理由）

| # | 问题 | 理由 |
|---|---|---|
| R3-N-1 | promote 以 spec 重推配置而非比对 run 的 config_hash（"晋升物==已执行物"无字节级保证） | 模块 name@version 不可变使当前结果确定；交叉校验属增强，记运行期改进 |
| R3-N-2 | 同一实验可晋升进多条策略线（配置 name 随线变→hash 不同） | 设计自由度的显式化问题，非缺陷；建议在 promote 文档注记（随触碰处理） |
| R3-N-3 | §5.6 文内契约与实现落位偏差（get_run_status 等在 ResearchService 而非 portfolio.service） | 文档内部张力，行为与 §6.7 一致；设计文档修订走架构流程，不本轮改 |
| R3-N-4 | monotonicity≥0.8 在 5 桶下等价于全一致（阈值从严） | 判定阈值平台持有（§6.10.3），调整走架构修订流程；现行为从严方向，保守无害 |
| R3-N-5 | forward_returns 的 start/end 死参数；事件池按 max(horizons) 统一截断未在 docstring 声明 | 随触碰相应模块时顺手处理（docstring 级） |
| R3-N-6 | 写工具 `_ai_session()` 仍在 try 外（逃逸面极小）；mcp≤1.12 不支持（既有约束）；数量界限极端 ε 下线性低估（误报方向） | R2VB 复验残余项，已在其报告中记录在案 |

## 已验证无问题的方面（本轮新增确认）

1. **评估模块数值主体**（R3A 74 项探针）：前瞻收益对齐 1e-12、NaN 双侧对称、越界截断、无条件对照同窗同池同口径、bootstrap 种子/带宽/p 值一致、分桶插值与 qcut 全等、spread 方向语义、打乱对照保池毁关联、SMA200 预热与边界日归属、event_side 语义、transition_matrix fail-loud 属实、distribution 指标/年度分组/criterion 透传。
2. **策略库核心契约**（R3B 探针）：config_hash 碰撞守卫、版本不可变触发器、promote 门（verdicted+confirmed）+ 血缘完整 + 同线幂等、软删除不伤引用、seed 幂等双调、resolved_config_yaml + run_params 全量落库、L4→L2 转发面最小化、registry 实例级隔离。
3. **文档-代码一致性**（R3C 15 条抽查）：全部属实，无虚假声明；CLOSED 标记与 VERDICT 行一致；R1-D/R2-D 待决策清单与后续TODO 分期口径逐字吻合。
4. **测试稳定性**：单元组合 3 连跑 + 逆序 + 单文件全绿；集成/API 185 passed；无顺序依赖、无共享状态、无固定共享 tmp 路径。

## 修复与验收计划

1. 本轮全部 P2/P3 修复 + 钉子（bucket 空桶已有、er 掩码、benchmark 止路等在集成测试覆盖）；
2. 全量回归零新增失败；
3. 2 个验收子代理逐项实证后本报告置 CLOSED，提交推送。
