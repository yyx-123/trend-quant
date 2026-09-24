# Round 2 修复清单（loop-review-glm53f）

> 对应审查报告：`round2-review.md`。状态：**CLOSED**（2026-09-25）——
> R2VA PASS（P1/P2 mutation 双向实证）；R2VB 首轮 FAIL（B-1 Context 注解死代码、
> B-2 文档判据失实）→ 整改后 **R2VB_REVERDICT: PASS**（真实 SDK 注入探针 /
> `__builtins__` 逃逸探针 / 归因界限数值仿真 7.2× 余量 / 门行为矩阵五象限）。
> 最终全量回归：1494 passed / 2 failed（仅已知 Windows flake）/ 6 skipped。

## P1 级整改

| 项 | 修复落点 | 钉子 |
|---|---|---|
| R2-P1-1 parity 归因判据未接线 | `engine/parity.py::attribute_diffs`：①NAV 逐点差异按 `cash_interest_rate/252`（2 倍容差）界限归 `cash_interest`，超界进 `unexplained`（kind=nav_point_diff_beyond_interest）；②docstring 如实重写两腿判据：零卡控 → `trade_diffs==[]` 且 `unexplained==[]`；卡控场景 → 路径级联错位使位置归因不可用，机器判据=`violations==[]`，unexplained 必然非空不作验收断言；③`t_plus` 注明结构性零差异键 | `test_loop_review_r2.py::test_parity_nav_point_diff_within/beyond_bound`（界限内外双向）；`test_engine_parity.py::test_parity_identical_when_all_deltas_off` 补 `unexplained==[]` 断言（mutation 有牙性交由验收代理复验：恒空桩替换后该测试必须变红） |

## P2

| 项 | 修复落点 | 钉子 |
|---|---|---|
| R2-P2-1 台账 POST 绕开 CSRF 防线 | `routers/research_ledger.py`：新增 `_reject_cross_site_form`（拒 `Sec-Fetch-Site: cross-site`），confirm/conclude/holdout-grant 三端点接线——与 SameSite=Lax 互补、零 UI 改动 | `test_research_ledger_api.py::test_confirm_rejects_cross_site_form_post`（cross-site 403 / same-origin 303 双向） |
| R2-P2-2 §5.14 模块覆盖缺口 | `test_module_behaviors.py` 新增 4 钉：random_entry（数量/去重/确定性/per_day 上限）、by_slope_r2（趋势质量排序+置换性）、vol_target（超目标收缩+gate_log、达标放行）、breakeven（包装层 estimate=None/激活/触发价=买入价）；补强 4 处弱断言：random rank 置换性、donchian 数值锁窗最小值、none 永不离场、all_in=全部现金 | 同左（`test_r2_*` 8 项） |
| R2-P2-3 psr_sortino 零 golden | `test_loop_review_r2.py::test_psr_sortino_golden_and_property`：固定序列独立公式重算锚（NormalDist 参考实现）+ 代数性质（Sortino=基准 → 0.5） | 同左 |

## P3 修复（10 项）

| 项 | 修复落点 |
|---|---|
| R2-P3-1 conclude TOCTOU | `topics.py::conclude_topic` UPDATE 加 `AND status='open'` + rowcount 判定（并发第二写者拒） |
| R2-P3-2 rerun 关题缺口 | `experiments.py::rerun_experiment` 补 `require_open_topic(原课题)`，已关课题拒绝 |
| R2-P3-3 MCP 错误泄露 | `research_tools.py` 新增 `_error_payload`：IntakeRejected 带 reasons/experiment_id、ResearchError 透业务文案、其余记日志返 `internal error (see server logs)`；全部 12 工具接线（含 4 个此前零捕获的读工具） |
| R2-P3-4 MCP 归属粒度 | `_ai_session(db, ctx)`：经 `scope.state.mcp_user` 派生 `ai-mcp-<user>` 独立会话（沿用 server.py `_token_user` 模式），无 ctx 回退共享默认；全部写工具加 `ctx: object = None` 参数 |
| R2-P3-5 format-string dunder 逃逸 | `modules.py` 预筛：字符串常量含 `.__` 记号即拒（真向量是 `"{0.__class__}".format(x)`——dunder 在字符串常量内，AST 属性扫描不可见；f-string 内表达式本就被 Attribute 扫描抓住） |
| R2-P3-6 关题表单 JS 拼 action | `research_ledger.html`：改服务端渲染的 open 课题下拉 + 独立表单（路由改 `/topics/conclude`，topic_id 走表单）；顺补 holdout 放行表单（开发日志所称"放行表单"此前实际缺失，端点不可达） |
| R2-P3-7 CLI 裸 traceback | `research_cli.py`：confirm/rerun/recompute/promote/conclude 统一异常 → `{"ok":false}` 打印 + exit 1；rerun --run 的 run_error 并入输出不吞 |
| R2-P3-8 sample --db 静默回退 | `run_base_v1_sample.py::_arg_value`：值缺失（末位无值/后随 flag）`SystemExit("requires a value (refusing to fall back to default db)")` |
| R2-P3-9 parity KeyError | `_normalize_bars` 缺 date/time 列 → 明确 ValueError（与旧引擎同口径） |
| R2-P3-10 动量基准口径 | YAML 补注记："月度入场+日度退出"实态（action_gate 只门控入场），对比读数须带注记；真月度检查臂=实验素材 |
| R2-P3-12 round-trip ATR 口径 | `backtester.py::_round_trips_enriched` 入场 ATR close 先 ffill（与 _precompute_atr 同口径） |
| R2-P3-14 recompute 吞异常 | runs 补录 except 改 warning 日志（不再静默） |

## 记录在案不修（round2-review.md P3-11/13/15/17 + 待决策 R2-D-1..3）

哨兵补跑日缺 post-update 管线（跨层回调注入违背 jobs 边界，次日自愈）；id 零填充宽度
溢出（万级不可达）；Web 操作归属共享 human-default（设计声明取舍）；sma200 事件化
（已注记）；R2-D-1..3 待用户决策。

## 验收整改（R2VB FAIL 项，2026-09-25）

| 项 | 整改 |
|---|---|
| R2VB B-1（R2-P3-4 生产失效） | 7 处 `ctx: object = None` → `ctx: Context = None`（守卫导入，FastMCP 只认 Context 子类触发注入；验收代理以真实 mcp SDK 实证 object 注解=死代码+schema 污染）；新增 AST 钉 `test_mcp_write_tools_annotate_ctx_as_context` 防注解回退 |
| R2VB B-2（文档判据失实 + 集成断言缺口） | ①集成层补"带真实差异 → 归因必须分类"断言：tail_slippage 用例断言 `classified["tail_slippage"] >= 18` 且 `unexplained==[]`；②归因器如实理解传导：数量漂移界限=尾滑点比例×累积笔数+一手（购买力复利传导）；NAV 漂移在已有 trade 级白名单归类时同归下游类，零差异语境超界才判负；③本文件验收判据表述修正：恒空桩下集成测试变红（KeyError 形态）；形状完好恒干净桩由 **unit 层 NAV 轴钉**承担（该场景集成层固有盲区，纵深防御如实声明） |
| R2VB B-6（预存：`__builtins__["__import__"]` 下标逃逸） | modules.py Name 黑名单补 `__builtins__`/`__globals__` |
| R2VB B-7（工具入口异常透出 + logger 惯例） | `_service()` 装配失败转 ResearchError（干净文案+logger.exception）；run_status/list_topics 的 service 调用移入 try；recompute 改 audit.app_logger |

## 回归结果（2026-09-25 主审实测）

- `pytest tests/unit tests/api tests/integration -q`（整改前基线）：**1373 passed / 0 failed / 6 skipped**。
- 整改后最终全量：**1494 passed / 2 failed / 6 skipped**（2 failed 仅已知
  Windows flake 文件 test_instruments_bulk_backfill.py；全库 mcp auth 用例已转
  条件跳过）。验收代理独立复跑：unit+api+integration 1374 passed / 0 failed。
