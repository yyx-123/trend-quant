# Round 2 审查报告（loop-review-glm53f）

> **状态：CLOSED（2026-09-25 闭合）**
> - 修复与整改：`round2-fixes.md`（含 R2VB 验收 FAIL 后的四项整改）；
> - 验收子代理 R2VA（P1/P2，含 mutation 双向实证）：**R2VA_VERDICT: PASS**；
> - 验收子代理 R2VB（P3 + 回归审查）：首轮 **FAIL**（B-1：`ctx: object` 注解
>   不触发 FastMCP Context 注入，per-user 会话为死代码——以真实 mcp SDK
>   实证；B-2：fixes 文档对集成断言有牙性的表述失实）→ 整改（Context 注解 +
>   AST 钉 + 集成层真实差异归因断言 + 归因器传导如实化 + `__builtins__` 黑名单
>   + 服务面错误归一）→ **R2VB_REVERDICT: PASS**；
> - 最终全量回归：1494 passed / 2 failed（仅已知 Windows flake）/ 6 skipped。
>
> 日期：2026-09-24
> 审查对象：commit `c624456`（Round 1 修复后的全量代码）
> 审查方式：全新视角，3 个独立审查代理分别覆盖 ①L4 台账安全面（verdict/api/sessions/topics/templates/MCP 通道）、②L2 引擎 parity 全文 + scripts + 配置资产 + R1 修复回归面、③测试完备性矩阵 vs 设计承诺（含 2 次真实 mutation 注入实验）；主审另行完成分层铁律 AST 静态检查（0 违规）、代码卫生扫描（无 TODO/FIXME 残留、noqa 全部有据）、verdict confirm 竞态与降级纪律双层闭环核验、CSRF 防线范围与台账表单缺失的亲自复核。
> 测试基线：全量 1474 passed / 2 failed（仅已知 Windows flake）/ 6 skipped。

## 总体结论

**FAIL（1 项 P1 级整改 + 3 项 P2 + 一批 P3）**——但性质与 Round 1 不同：核心资金路径、append-only、状态机、holdout 门、身份归属、XSS/SQL 注入面、模板转义全部复核成立（三代理一致确认），无活性缺陷；问题集中在**验收判据弱于文档承诺**（parity 归因）、**防线不一致**（CSRF）与**测试覆盖缺口**（4 个模块零直测、Sortino 无 golden）。Round 1 修复本身经 R2-B 回归面重查未发现新引入缺陷（双锁无死锁、ATR 掩码对齐无坑、整手与对账语义兼容）。

## P1 级整改（设计承诺字面未成立）

### R2-P1-1 阶段1 验收判据"差异自动归因，超纲即测试失败"未端到端接线
- 位置：`src/engine/parity.py:185-241`；`tests/integration/test_engine_parity.py`
- 事实（R2-C 真实 mutation 实证 + R2-B 探针）：
  1. **NAV 逐点差异既不归类也不进 unexplained**——trades 逐位一致、NAV 漂移 2.74 元（计息类）时 `unexplained==[]`；把 `attribute_diffs` 替换为恒空桩后集成测试仍全绿（被 unit 层 r3_unit 钉兜住，纵深防御成立但集成层无牙）；
  2. `ATTRIBUTION_WHITELIST` 四键中 `cash_interest`/`t_plus` 为**死键**（无代码路径可置 1）；
  3. 卡控场景下级联错位使差异必然落 `unexplained`（`limit_card` 分类判据与 docstring 不符）；现有测试只断言 `violations==[]`（卡控日零违规成交——该判据实现正确），从未断言 `unexplained==[]`。
- 修复：NAV 逐点差异按 `cash_interest_rate/252` 界限归 `cash_interest`、超界进 `unexplained`；count/trade mismatch 锚定卡控日（当日有卡控卡）→ `limit_card`；docstring 如实改写"卡控场景验收以 violations 为准"；集成测试补 `unexplained==[]`（零卡控）与 violations 判据（卡控）断言。

## P2

### R2-P2-1 台账 3 个变更 POST 绕开全站 CSRF 防线（与存量页防线不一致）
- 位置：`src/app/main.py:545-553`（X-Requested-With 仅查 `_is_api_path`）；`src/app/routers/research_ledger.py:146,164,182`
- 事实（主审亲自复核确认）：confirm/conclude/holdout-grant 是全站仅有的三个不带 `/api/` 的变更 POST，仅靠 SameSite=Lax（Chrome Lax+POST 2 分钟豁免窗口）；confirm 不可逆（final 落定后库触发器拒改）。
- 修复：三个端点拒收 `Sec-Fetch-Site: cross-site` 的 POST（现代浏览器跨站表单必带该头，与 SameSite=Lax 互补、零 UI 改动）；钉子：带 `sec-fetch-site: cross-site` 的 POST → 403。

### R2-P2-2 §5.14 承诺"每个预置模块带 golden 测试"——4 个模块零直接行为测试 + 3 处弱断言
- 事实（R2-C 逐模块 grep+抽读）：`random_entry`（signal）、`by_slope_r2`（rank）、`vol_target`（portfolio_risk，开发日志"四组合门"实际只有三门）、`breakeven`（position_risk 包装层）零直测；弱断言：rank `random` 未断言输出是输入的置换、`donchian_exit` 未锁数值、`none` 无"永不离场"语义钉、`all_in` 无单元钉。
- 修复：补 4 个行为钉 + 补强 4 处弱断言（`test_module_behaviors.py`）。

### R2-P2-3 判定器全家桶 golden 缺口：`psr_sortino` 零断言
- 位置：`src/research/evaluations/backtest.py:563`（evidence.stats.psr_sortino）；tests/ 全库零覆盖。
- 修复：补 golden（固定序列手算锚 + PSR 框架代数性质：Sortino 相等 → 0.5）。

## P3（本轮修复 10 项，其余记录在案）

| # | 位置 | 问题 | 处置 |
|---|---|---|---|
| R2-P3-1 | topics.py:118-149 | conclude_topic TOCTOU：UPDATE 无 `AND status='open'`，并发下"已关题带在途实验"/结论双写 | 修：rowcount 守卫（沿用 R1-P1-2 先例） |
| R2-P3-2 | experiments.py:341-385 | rerun 可向已 conclude 课题追加在途实验（唯一绕过 require_open_topic 的入口） | 修：rerun 前校验课题 open，已关则拒绝 |
| R2-P3-3 | research_tools.py | MCP 错误处理把内部异常细节透给 AI 客户端；4 个读工具完全不捕获 | 修：ResearchError 精确翻译，其余记日志返笼统错误 |
| R2-P3-4 | research_tools.py:38-41 | MCP 研究工具忽略 token→用户映射，多 token 部署归属不可分 | 修：按 mcp_user 派生独立 AI 会话（`ai-mcp-<user>`，沿用 server.py `_token_user` 模式） |
| R2-P3-5 | modules.py 预筛 | f-string/format 字符串内的 dunder 访问（`"{0.__class__}"`）绕过 AST 属性扫描 | 修：字符串常量 format-spec dunder 预筛 |
| R2-P3-6 | research_ledger.html:29-30 | 关题表单 JS 拼 action，JS 失效时误关硬编码课题（结论不可改） | 修：改每行独立小表单；顺补 holdout 放行表单（当前 UI 缺失=端点不可达，开发日志"放行表单"与实际不符） |
| R2-P3-7 | research_cli.py:134-180 | confirm/rerun/recompute/promote/conclude 缺失 id 裸 traceback | 修：统一异常→`{"ok":false}` 打印 |
| R2-P3-8 | run_base_v1_sample.py:33-37 | `--db` 末位无值静默回退默认（生产）库 | 修：值缺失报错退出 |
| R2-P3-9 | parity.py:30-39 | bars 无 date/time 列 KeyError 而非明确 ValueError | 修：补 ValueError |
| R2-P3-10 | bench_simple_momentum_rotation.yaml + signal.py | 动量基准实为"月度入场+日度退出"，与"月度检查"注释口径差 | 修：YAML 注记补明（行为变更走实验，不改实现） |

## P3 记录在案（本轮不修，理由）

| # | 位置 | 问题 | 不修理由 |
|---|---|---|---|
| R2-P3-11 | jobs.py 哨兵 | 补跑日缺 post-update 管线（除权/指标重建滞后一日，次日自愈+启动补偿兜底） | 修复需把 services 层回调注入 jobs（违反"core/jobs 不 import services"的既有边界），收益/风险比不划算；记入已知取舍 |
| R2-P3-12 | backtester.py:557-559 | round-trip 入场 ATR 未 ffill（仅诊断字段 r_multiple 分母） | 与 R2-P1-1 同文件重构一并处理成本高；诊断字段，不影响交易/净值 |
| R2-P3-13 | ledger.py:22 + verdict.py | id 零填充宽度溢出后字符串排序倒挂（V 五位/E 四位/T 三位上限） | 理论不可达量级（万级实验），触碰时再扩位 |
| R2-P3-14 | recompute.py:164 | runs 补录 `except Exception: pass` 吞真实错误 | 收窄为本轮修复附带（改插桩逻辑时一并做）→ 实际已修，见 fixes |
| R2-P3-15 | router:153,171,185 | Web 操作归属共享 human-default（多用户不可区分） | 设计文档已声明的取舍（登录墙即身份边界）；单用户系统 |
| R2-P3-16 | parity 卡控级联归因 | 见 R2-P1-1 修复中的判据文档化方案（完整级联切分归因属过度工程） | 并入 R2-P1-1 |
| R2-P3-17 | sma200_timing 事件化表达 | YAML 已注记 | 已注记，无需动作 |

## 已验证无问题的方面（Round 2 新增确认）

1. **L4 安全面**（R2-A 三重复核）：XSS（autoescape 全生效、无 |safe）、SQL 注入（全参数化）、AuthWall 豁免名单不含台账、路径穿越不可达、grant_holdout 不在 MCP 且 human 门双层、AI/human 会话归属服务端常量不可冒充、可降不可升三重锁（rank 表 + 状态机无 verdicted 出边 + 库触发器）、confirm 竞态三道防线。
2. **parity 驱动九项语义**与旧引擎逐段一致（金叉/死叉式、预热 i≥35、卖日禁回买、全进全出、递减定量、费用、T+1 零差异）；R2-B 独立重推预热对齐结论与 R1 相符。
3. **R1 修复回归面**：jobs 双锁单向嵌套全非阻塞（无死锁/饥饿）、live_daily_list_job 无越锁写路径、ATR 掩码位置对齐无坑（连续序列位级相同）、live 整手与对账语义兼容、9 份策略 YAML 全部真实 REGISTRY 载入成功、random 系基准日序数锚定位级确定。
4. **模板**：两个新模板无 XSS 面；report.json 下载为 JSONResponse。
5. **分层铁律**（主审 AST 全量检查）：研究栈→app/services 零反向依赖，各层 import 全部在许可集内（`rule_backtest.metrics` 为 §5.8 明文豁免）。
6. **代码卫生**：新栈无 TODO/FIXME/HACK 残留、无裸 print、noqa 全部有理由注释。
7. **测试反脆弱**（R2-C mutation 实验）：1428 测试/3914 断言（2.74/测试）；零空体、零 try/except pass 吞噬；收割谓词/重复检测/晋升门钉子经真实 mutation 验证有牙；"判据阉割"由 unit 层纵深兜住。

## 修复与验收计划

1. R2-P1-1 + R2-P2×3 + R2-P3 修复 10 项，每项配钉子；
2. 全量回归（1474+ 基线上零新增失败）；
3. ≥2 个验收子代理逐项实证（含 mutation 复验 R2-P1-1 的集成断言有牙）；
4. 通过后 round2-review.md 置 CLOSED，提交推送。

## 待决策点（新增，最终报告统一提交）

- **R2-D-1** `live_daily_list_job` 写路径是否需要纳入日更单飞锁/手动触发入口（当前仅调度器单实例调用，无并发面）——确认维持现状即可；
- **R2-D-2** 动量基准"月度入场+日度退出"口径是否需要一只真"月度检查"的对照臂（属新实验素材，非缺陷）；
- **R2-D-3** MCP/会话归属粒度升级（每登录用户独立会话）是否纳入二期（当前单用户无实际影响）。
