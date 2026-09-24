# Round 4 审查报告（loop-review-glm53f，确认轮 1）

> **状态：CLOSED（2026-09-25 闭合）**
> - 3 项 Low 修复全部通过验收：R4V_VERDICT: PASS（数值锚 100/12/0.12 精确命中、
>   gitignore 生效无 tracked 残留、AccountView 只读契约成立）；R4W_VERDICT: PASS
>   （13 处消费点 MappingProxyType 全兼容、cost_drag 零旧断言冲突、全量
>   1377 passed / 0 failed）。
> - 全量回归：1497 passed / 2 failed（仅已知 Windows flake）/ 6 skipped。
>
> 日期：2026-09-25
> 审查对象：commit `8f38c0a`（Round 3 闭合后全量代码）
> 审查方式：2 个独立确认审查代理——R4A 量化研究员视角（f93031e..HEAD 全量差异 55 文件 + 21 个前轮未深审角落抽查）、R4B 工程可靠性视角（全量测试 2 连跑、端到端冒烟链路、8 个可靠性角落）。裁决口径：只报可复现、可行动的新问题；风格项与已声明豁免项不计。

## 结论

**有 3 项新的 Low 级问题（全部当轮修复）**；两代理合计扫过 30 个角落，其余全部确认无问题。R4B 独立实证：全量测试 2 连跑逐位一致（1375 passed / 0 failed × 2）、端到端冒烟链路全通、worker 生命周期幂等、run_freeze 计数不漂移。

## 发现与修复

| # | 级别 | 问题 | 修复 |
|---|---|---|---|
| R4-P3-1 | P3（Low） | `portfolio/reports.py:132` cost_drag 的 `gross_pnl_before_fees` 把 total_fee 又加了一遍（pnl_gross 本身已是费前毛利）——报告字段虚高，与同函数 cost_to_gross 分母自相矛盾；探针复现 100→112 | 改回 `gross_pnl`；数值锚钉（100/12/0.12 三断言） |
| R4B-1 | P3（Low） | `.gitignore` 漏 `research/topics/`——课题文件夹为平台生成可再生产物（与 data/research/ 同性质），首次生产 confirm 后 git status 被污染 | .gitignore 增加 `research/topics/`；"是否入库为阅读面"列为待决策 R4-D-1 |
| R4B-2 | P3（Low，加固） | `context.py::AccountView.positions` 直接返回可变 dict，与"只读视图"docstring 契约不符（模块可 clear/乱插直改引擎账户） | 改返回 `MappingProxyType`（读用法 in/len/迭代/取项零影响）；钉子断言变更方法被拒、读用法正常 |

## 确认无问题的角落（30 项，两代理合计）

engine/store 并发写、engine/models 不变量（heat 缺价路径经查不可达）、gateway/audit flush fail-loud、gateway/metadata、gateway/service bind/审计约定、research/runs 枚举、research/errors、research/worker 生命周期（双 start/stop 幂等实证）、research/pipeline 失败路径、research/ledger alloc_id、research/topic_files slug/截断、research/api promote 门、research/experiments 重复检测、web 模板空数据页、routers 状态码映射、sample 脚本产物结构、research_cli 修复在位、research_tools Context 注解、db.py 触发器整改、jobs/main 锁链、R1/R2 小修 diff 复核、测试资源隔离、run_freeze 并发语义、AccountView 消费点只读性、Makefile 入口、SQLite 并发配置。

## 待决策（新增）

- **R4-D-1**：`research/topics/` 课题文件夹按产物 ignore（本轮实现），还是入库作为阅读面？二选一需用户一句话定夺（现按"可再生成产物"处理，与 data/research/ 同口径）。

## 回归

全量 `pytest tests/ -q`：**1497 passed / 2 failed / 6 skipped**（2 failed 仅已知 Windows flake）。零新增失败。
