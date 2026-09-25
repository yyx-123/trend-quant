# Round 22 审查（loop-review-ds4f）

> **状态：CLOSED（2026-09-26 闭合）** —— 修复与回归见 `round22-fixes.md`
> 日期：2026-09-26
> 代理：R22A（前端展示数字一致性）、R22B（MCP/CLI 通道与台账写路径）
> 范围：R21 提交后的工作树；重点=**前一轮修复的同类缺陷其余实例**与**两条通道的口径一致性**

## 0. 本轮方法

R21 的结论是"显示口径必须落在数据源侧"，R22 据此设两个独立方向：

- **R22A**：把"浏览器显示的数字"逐个回溯到后端权威字段。方法=读后端真实载荷
  （生产库只读 + 真实批次 JSON + 仓库自带 ECharts SSR）后**逐字复刻前端 JS 逻辑**，
  两侧数字必须一致；怀疑点必须实测（含反证）。
- **R22B**：把同一操作的 **MCP 与 CLI 两条路径**逐项对照（校验强度/错误分类/
  返回字段/副作用/留痕），并在临时库上**真调用**（含并发），专找"绕过纪律、
  记错账、把失败说成成功"的路径。

## 1. R22A 发现（前端展示数字）

| 编号 | 级别 | 现象 | 证据 |
|---|---|---|---|
| **R22A-F1** | P2 | 同一个"交易数"标签混用**成交笔数**（买卖各一笔）与**平仓回合数**（只算卖出）：市场页汇总表与年度表上下相邻，批量页格子弹窗头与年度表同屏 | 生产批次 `20260906231040735709` 的 `510300.SS`：`trade_count=290`（145 买 + 145 卖）而 `win_rate=49/145`、年度 `trade_count` 合计 = 145 → 同屏差 2 倍 |
| **R22A-F2** | P2 | L4 组合报告 `summary` 的交易类字段全 0，而同载荷内有真值（`trades=26`、`cost.total_fees=309.06`）——"这笔实验没交易、没成本"的相反结论 | `research_verdicts.report_json`：V00001/V00003/V00004/V00005/V00006/V00007 六份落库产物同形；根因 `reports.build_report` 固定 `trades=[]` 喂 `compute_summary`（核心函数零成交语义按 GLM53F R1-D-5 不动，问题在调用侧） |
| R22A-F3 | P3 | 跨批次对比的"Δ年化"是**两个中位数之差**，与后端既定口径（逐格作差再取中位数，`batch_service.compare_batches` + `/api/compare`）不一致 | 真实批次对（tight `20260909220536739646` vs loose `20260909220536956141`）：`趋势进-止损出` 页面 −0.97pp vs 逐格中位数 −1.74pp（差 25%） |
| R22A-F4 | P3 | 看盘页"我的止损线"只取硬/吊灯较高者，**忽略棘轮档**——棘轮是引擎的独立出场条件（`STOP_EXIT_REASONS`、手工交易页三档卡）；且 `trade_records._ANNOTATION_STOP_FIELDS` 白名单根本没有棘轮字段 | 字段白名单缺 2 项；库内 26 笔未平仓中未统计到"棘轮更高"实例，故 P3 |
| R22A-F5 | P3 | `SKIP_REASON_TEXT` 缺 `below_min_order`（R17 新增的原因码）→ 页面对科创板最小申报量拒单显示英文枚举 | `market_view.js` 映射表只有 `insufficient_cash`；引擎 `engine.py:32` |
| R22A-F6 | P3 | 看盘页 MACD 用 `warmup=False`（图表口径），与缓存/相位权威（`indicator_daily` 与 `detect_macd_phase` 都是 `warmup=True`）不同源：长历史差 1e-5 级，**短历史标的整段为空** | 875 标的全量复算 max\|Δhist\|=1.7e-5；7 根历史的 `551030.SS` 在 False 下 dif/dea 全 None（看盘页副图空白、金叉标记消失）而看板/相位有值；`core/indicators.macd` 注记本就写明"要统一时应固定用一种模式" |

观察项（未单列为发现）：`positionPctCell` 把"仓位%"硬编码为"全仓"，真实建仓权重
（整手取整 + 费用）实测 99.94%。

## 2. R22B 发现（MCP/CLI 通道与台账）

| 编号 | 级别 | 现象 | 证据 |
|---|---|---|---|
| **R22B-F1** | **P1** | 并发下发时 `attempt_index` 撞号：COUNT 与 INSERT 分属两个连接 → 多个**真正计数**的实验拿到同一个试次号，而它是 DSR/配对检验的试验次数输入（`n_trials<=1` 直接退化为 PSR(0)，多重检验校正静默失效），且该列在 append-only 保护列内**事后不可修** | 6 线程同线并发（`allow_duplicate=True`、互不相似的 spec）：6 个实验**全部** `attempt_index=1`；串行不撞号。触发面=AI 客户端并行 tool call / 双击 / shell 并行 |
| **R22B-F2** | P2 | 运行结果字段语义错位：成功时 `dispatch.status=null`（verdict 行没有 status），失败时无原因；顶层 `status` 是**运行前快照**（恒 `queued`）；CLI `--run` 失败时**退出码恒 0** | MCP 实测 `{"status":"queued","dispatch":{"mode":"sync","status":null}}`（库里其实是 `evaluating`）；CLI 实测 `{"status":"failed","suggested":null}` + traceback + `code 0` |
| **R22B-F3** | P2 | 查重与 INSERT 之间的竞态输家撞 `UNIQUE(name,version)` → sqlite 异常冒到通道层 → MCP 回 `internal error (see server logs)`（同一业务条件两种口径；R21 同类缺陷未闭合） | 3 线程 × 6 轮命中 1 次；`_error_payload` 直接分类实测：`IntegrityError`/`OperationalError`/`KeyError`/`TypeError`/`AttributeError`/`DslError`/`BacktestError` 全部 → `internal error` |
| **R22B-F4** | P2 | MCP 通道**没有 holdout 通路**（工具签名无 token 位），且"绑定原实验的 token"救不了复现：复现是新 id，按 id 自动带出查不到，显式传入又被绑定校验拒 → 空烧一次试次、token 仍未消费 | MCP 实测 E0008 失败（`touches holdout`）→ 绑 token 到 E0008 → 复现 E0009 仍失败且 `consumed_at` 为 null；CLI 侧 `--token` 正常消费 |
| **R22B-F5** | P2 | `run=False`（"攒一批再跑"）产出的实验**没有任何派发入口**（除进程启动补投）→ 停在 `queued`，而关题要求课题内无在途实验 → 把关题卡死 | MCP 实测 `dispatch={"mode":"parked"}` 后无工具可跑；`research_conclude_topic` 报 `topic has non-terminal experiments` |
| R22B-F6 | P2→**待决策** | 归属与留痕：CLI 通道把动作记成 `created_by='human'`（默认人类会话，也是 human-only 治理门 `recompute` 的通行证）；晋升/关题不记操作者 | 临时库实测：CLI 建的实验 `owner_session=human-default`、MCP 建的 `ai-mcp-default`；`portfolio_strategy_versions` 无晋升者列、`research_topics` 无关题者列 |
| R22B-F7 | P3 | `confirm_verdict` 非法枚举被"可降不可升"分支接住 → 报"platform 纪律不许升格"，真因只是取值非法；CLI 由 argparse 挡住（两侧口径不同） | CLI `rc=2 invalid choice` vs MCP `final_verdict upgraded exceeds platform suggestion rejected (downgrade only)` |
| R22B-F8 | P3 | 同一非法输入两侧口径不同：MCP 由 schema 拒并给明确文案；CLI 把裸 Python 异常当业务原因（`'list' object has no attribute 'get'`） | `--spec '[1,2]'` / `--spec '"abc"'` 实测 |
| R22B-F9 | P3 | 台账检索行缺 `holdout_touched` / `is_reproduction` → 工具文档要求"先读台账再提假设"，但读不出"哪条经过样本外验证/哪条是复现" | `research_search_ledger` 行字段清单实测；`is_reproduction` 在两个模板中都未出现 |
| R22B-F10 | P3→**待决策** | "失败但未取证"的实验照旧占用 DSR 试次并阻塞同 spec 重提（方向保守，但把环境失败与研究尝试混在一个计数里，且计数不可改） | E0003（缺 token 失败、`research_runs` 为空、`holdout_touched=0`）拿到 `attempt_index=3`；同 spec 重提被 `duplicate_of` 硬拒 |

## 3. 已测**不成立**（有价值结论，防止后轮重复劳动）

- R22A：箱线图 tooltip 索引（实测 ECharts 会给 boxplot 行补前导类目索引，6 元素取值正确）、
  散点 `excess_total_return` 现算（与后端 `excess_annual_return` 同构）、
  单跑/批量费用参数一致（`fee_rate/slippage/lot_size/stop_gap_fill` 全一致）、
  看板类目聚合加权口径（EOD 与盘中同规则）、`instruments` 权重列（库内已是百分数）。
- R22B：intake 五条骨架拒绝文案两侧一致；`TopicError`/`LifecycleError` 两侧一致；
  verdict 升降（含方向翻转、同向约束）两侧同判；holdout token 跨实验绑定拦截有效；
  台账 append-only 保护（UPDATE/删除全被触发器拦，全仓写点排查无越权路径）；
  并发 confirm 行级认领有效；并发晋升未复现 IntegrityError；
  `engine_runs`/`research_runs`/`gateway_audit` 的填充与通道无关。

## 4. 判定

| 项 | 级别 | 结论 |
|---|---|---|
| R22B-F1 | **P1** | 成立（6/6 撞号，DSR 校正失效且不可事后修正） |
| R22A-F1 / R22A-F2 / R22B-F2 / R22B-F3 / R22B-F4 / R22B-F5 | P2 | 成立（均有真实载荷/真调用复现） |
| R22A-F3~F6、R22B-F7~F9、观察项（仓位%） | P3 | 成立，本轮一并收口 |
| R22B-F6 / R22B-F10 | — | **记录为待决策点**（归属策略 / 试次计数口径），交用户决策，本轮不修 |
