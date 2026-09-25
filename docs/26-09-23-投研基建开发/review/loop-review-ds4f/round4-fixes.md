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


## V10 独立验收（第 10 个代理）：判 FAIL（1 项 P2 回归）→ 二次修复

V10 逐项反证：P1 `target_weight.mode` **真修好且完整**（并实证**恢复了已发布数字的
可复现性**：修复后复制库重跑 `bench-60-40` 2381/2431… 实为 2431/2431 行 NAV 与已发布
run 逐位一致，年化回到 2.50%、MDD −30.99%、成交 3 笔；修复前是 4.09%/−43.78%/10 笔
且 0/2431 行吻合）；P2 `listing_known` 行为逐位未变（旧列 A/B 零差异）；P3-4/P3-5/P3-7
全闭合。但抓到 **1 项 P2 回归**：

| # | 项 | V10 证据 | 二次修复 |
|---|---|---|---|
| **ND-1**（P2，回归） | 我的 worker 回灌只把 id 加进 `_queued_ids`、**没放回 `_queue`** → ①`stop()` 早于 `start()` 时 2/2 实验被"搁死"（修复前 0/2，重启后能派发）；②`submit()` 因"已在集合里"返回 False、`submit_all_queued()` 返回 0、`status()["queued"]`（qsize）显示 0 → 监控说谎，只能靠重启进程恢复 | `stop()` 前 counter 归零✓、4/4 回到集合✓，但 `start()` 后**没有任何实验被派出**；对照修复前行为相反 | 三条回灌路径（drain / 被取消的 future / `_pool is None` 退出）全部 **`_queue.put(...)`**；drain 改为"先全取出再统一放回"（否则边取边放自旋——实测会把测试挂死）。新增钉子：`stop()` 后重启必须**真的重新派发**（用 spy 断言 `run_experiment` 被调用）|
| ND-3（P3） | `join` 超时后 `_dispatcher` 永不释放 → 本进程内再也起不来（"直到它退出"的日志承诺不成立） | 旧线程已死时清引用并允许重启；新增钉子 |
| ND-4（P3） | 机制钉的指纹是**手写白名单**（25 个属性名），66 个带默认值的字段里只覆盖 38 个 → 4/7 的"默认值矛盾"变异逃逸 | 指纹改为枚举 `vars(instance)` 的**全部标量属性**（整类可见） |
| ND-5（P3） | `_empty_frame()` 未加新列；`listing_known` 无任何钉子 | 空帧补列 + 两条钉子（列集一致 / 旗标语义） |
| ND-6（P3，文档） | 开发日志仍记着"target_weight schema 显式化"（本轮已推翻）；R4-D-1 关于"需重铸版本+重跑产物"的前提被证伪 | 待最终报告一并说明（修复反而**恢复**了原 hash 与原数字，无需重跑） |

**另记**：V10 确认 `tests/unit/test_db_path_anchoring.py` 会打开并迁移**生产库**
（既有测试卫生问题，历轮已记录）；以及我此前误提交的 `.tmp_r3a/` 已在 `c9a006d` 清除，
HEAD 树中 `.tmp_*` 计数为 0。

## 回归结果

- 全量（V10 修复后终跑）：**1629 passed / 1 failed**（唯一失败为既有 Windows 临时文件 flake；另有 1 项
  由本轮新加校验暴露的既有用例已按真实形态修正：holdout 绑定用例改为绑定到真实实验行）；
- 新增钉子：`tests/unit/test_loop_review_ds4f_r4.py` 9 项；
- ruff：与基线逐条对比新增 0 条（本轮引入的 5 条已全部收口）。
