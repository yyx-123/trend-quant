# Round 4 修复与回归（loop-review-ds4f）

> 日期：2026-09-25
> 对应审查：`round4-review.md`（新面审查轮：1 项 P1 + 1 项 P2 + 6 项 P3）

## 修复总览

| 项 | 修复 | 钉子 |
|---|---|---|
| **R4A-P1-1**（P1）`target_weight.mode` schema 默认与实现的条件默认相反 → `bench-60-40` 变成满仓 | 删掉 `mode` 的 schema `default`（保留 `choices`），条件默认留给实现 | 三条：①**机制钉**（对每个内置模块用"必填-only / 必填+单兄弟 / 必填+全部"三组参数比较"原样实例化 vs schema 物化后实例化"的行为指纹——整类守卫）；②直测 `mode` 语义；③`bench-60-40` 端到端权重断言 |
| **R4A-P2-2**（P2）`start_date` 当上市日 → 真实库上"新股无涨跌幅限制"整条失效 | **不改行为**（字段语义需数据侧裁决，两种口径各有错向）；新增 `listing_known` 列**可见化**（不再静默） | 列存在性 + 决策点 R4-D-2。已否决的"首根 bar 当上市日"方案记录在案（会把窗口起点附近的标的全判成新股，实测 40 个用例失败） |
| R4A-P3-3 | worker `stop()` 丢派发 + 泄漏会话计数（`cancel_futures` 取消的 future 无人回灌；`_pool is None → break` 路径同样丢） | `_inflight` 跟踪 future→(id,owner)；stop 时未开跑的取消并**回灌队列 + 回退计数**；drain 原始队列；退出路径还回 id/计数 | `test_worker_stop_requeues_cancelled_dispatches`（5 实验 / 1 worker / 突发 stop：计数归零 + 4 个回队列；变异反证：去掉回灌即失败） |
| R4A-P3-4 | `join` 超时后置空 `_dispatcher` → `start()` 起第二个调度线程 | 超时保留引用 + 告警；`start()` 见活线程即拒 | `test_worker_start_refuses_second_live_dispatcher` |
| R4A-P3-5 | MCP 通道自己写库（会话策略落在通道里） | 新增 `sessions.ensure_channel_session`（服务面），通道只调它 | `test_mcp_channel_does_not_write_the_db_directly` + `test_session_face_ensure_channel_session_is_idempotent` |
| R4A-P3-7 | 台账表单无上限/无存在性校验（70k reasoning、200k purpose、指向不存在实验的 token 均可落库） | `grant_token`：purpose ≤200 + 绑定实验必须存在；`confirm_verdict`：reasoning ≤4000 | `test_grant_token_rejects_oversize_and_unknown_experiment` + `test_confirm_rejects_oversize_reasoning`（并把既有的 holdout 绑定用例改为走真实课题+实验行） |
| R4A-P3-6 | `core/jobs.py` 引 L1.5/L3（分层口径与 `main.py` 自陈矛盾） | **记录在案**（属分层文档口径 + 迁移成本，进 R4-D-4） | — |
| R4A-P3-8 | 验收 slow 钉只断言结构、从不重算；开发日志阶段 2 快照是修前数字 | 处置进 R4-D-3（重跑产物 + 更新日志 + 钉子升级） | — |

## 变异反证

- 机制钉（schema 物化不改变行为）：把 `default: "equal"` 加回去 → **3 条钉子全红**（含机制钉）；
- worker 回灌：删掉回灌分支 → 计数泄漏断言**失败**；
- `start()` 拒绝：伪造活线程 → 断言不得被替换。

## 回归结果

- 全量：**1625 passed / 1 failed**（唯一失败为既有 Windows 临时文件 flake；另有 1 项
  由本轮新加校验暴露的既有用例已按真实形态修正：holdout 绑定用例改为绑定到真实实验行）；
- 新增钉子：`tests/unit/test_loop_review_ds4f_r4.py` 9 项；
- ruff：与基线逐条对比新增 0 条（本轮引入的 5 条已全部收口）。
