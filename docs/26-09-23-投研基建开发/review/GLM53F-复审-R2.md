# GLM53F 复审 R2（投研基建一期修复轮，2026-09-24）

> 复审人：GLM-5.3-Flash（对本人 R1 盲审报告 `GLM53F盲审报告.md` 的修复验证轮）
> 复审对象：修复轮改动的 15 个源文件 + 新增钉子 tests/unit/test_review_r3_unit.py
> （15 项）与 tests/integration/test_review_r3.py（16 项）。
> 方法：逐条对照 R1 报告亲验修复代码本体（非只看日志声明）、核验每条钉子是否
> 复刻真实调用形态、专查修复是否引入新问题；实测跑新钉子 31 项与 unit/api/integration
> 全量回归。全程只读。

## 0. 结论

**PASS（有条件）。** R1 全部 4 项 P1 代码缺陷与 1 项 P1 测试缺口均已修复且
本体验证有效、钉子到位；R1 的 P2 批量修复 19 项中 18 项验证关闭。修复本身
未发现引入新缺陷（1335 项测试本机复跑通过）。三个附带条件（均为文档/口径
收口，非代码正确性阻断）见 §5。

---

## 1. P1 复核（5/5 已修，逐条本体验证）

| R1 编号 | 修复验证 | 关键证据 | 钉子 |
|---|---|---|---|
| P1-1 ETF 涨跌停幅度 | ✅ 已修 | `tradability.py:41-62`：`board_limit_pct(symbol, asset_type=, name=)`——沪 588xxx 与名称含"创业板/科创"的 ETF ±20%；`compute_tradability` 接入 `asset_info`（缺省读 instrument_metadata，backtester 路径经 gateway 自动覆盖） | r3_unit:36/51（159915 +12% 不判涨停、+20% 判涨停）✅ |
| P1-2 元模块丢 gateway 转发 | ✅ 已修 | `execution.py:171-175`：`_MetaBase.prepare_with_gateway` 逐子转发，any_of/all_of/position_risk any_of 三槽元模块全部经基类命中顶层 hasattr 探测 | r3_unit:130（探针模块经 any_of 接线后才出事件）✅ |
| P1-3 live 止损重建违反 T-1 口径 | ✅ 已修 | `live.py:89-171`：末根 provisional bar 排除出 highest/ATR（盘后重跑不误排）；吊灯 = highest(T-1)−mul×ATR(T-1)；棘轮按持仓期逐日候选累计 max 重建——与回测 ratchet 的 max 链数学等价（我逐步推导核对）；同日买入保留入场根（符合决策 6 入场初始化口径）；ATR 周期取模块参数 | r3_unit:208/239（999 虚高排除 + 独立重算对拍、棘轮 running max）✅ |
| P1-4 复核 verdict 劫持摘要/展示 | ✅ 已修 | `verdict.py:99-106` `canonical_verdict()`（supersedes IS NULL 优先、同类链取新）；conclusion.py 内联同语义选择器（:70-78）；api.get_run_report / research_ledger 两处 / topic_files 全部切换；全仓 grep 证实新栈无残留 `verdicts[-1]` | r3 集成:277（复核稿 final=rejected 不改课题 confirmed 计数与分级）✅ |
| P1-5 concentration_cap 零有效测试 | ✅ 已修 | 真断言落两处：`test_module_behaviors.py:321-355` 与 `test_review_r3_unit.py:275-330`——第三只同 l2 被拦、异 l2 放行、gate_log 留痕，且复刻无参 `instruments()` 真实调用形态（DS-P2-7 教训落实） | ✅ |

P1-1 残留（P3）：ETF 幅度靠**名称匹配**（"创业板"/"科创"子串），"双创"类合成
指数 ETF（如 159780 双创 ETF，真实 ±20%）不命中；metadata name 缺失时回落
±10%。长尾边缘，建议后续按指数跟踪表维护，不阻断。

---

## 2. P2 复核（R1 的 22 项：19 项修复声明全部验证关闭，3 项未修留账）

### 2.1 已验证关闭（18 项）

| R1 项 | 验证要点 |
|---|---|
| P2-1 判定门与文档不符 | ✅ `verdict_rules.py:43-54` Cornish–Fisher t 临界（我手算 ν=29 → 1.7007 vs 真值 1.6991，保守方向可用），`suggest_backtest_verdict:149` 按 n_pairs−1 取临界面且不低于配置下限；plateau 缺席/换手 >50% 跳变分别落 `plateau_evidence_absent`（backtest.py:645）/`turnover_jump`（:652）显式警告；docstring 重写并澄清"差序列 vs 序列级 ΔSharpe"两口径；BACKTEST_RULES 别名删除。注："换手成本可解释"由 §6.5.1 的 confirmed 硬条件降级为人工判读警告——口径决策合理但**详设正文未回写**（见 §5 条件②） |
| P2-2 方向一致率口径 | ✅ conclusion.py:91-97 改 spec.expect 同向（缺省 positive），"与多数方向"口径删除 |
| P2-3 regime 假证据 | ✅ backtest.py:481-485：`bench_nav_for_regime` 不再 `or base_nav` 回退——基准不可用即禁用拆分 + `regime_benchmark_unavailable` 警告 |
| P2-4 重复检测类型逃逸 | ✅ experiments.py:77-89 `_coerce_scalar` 归一（"2.0"≡2.0、"true"≡True、2≡2.0）后递归；`_normalize_spec_value` 浮点 4 位 + 列表排序，exact 签名同步收口 |
| P2-5 全局 token 被自动消费 | ✅ pipeline.py:71-90：只自动带出**绑定本实验**的 token；全局 token 须显式透传。钉：全局不抢/显式透传可用/绑定自动带出 三向覆盖（r3:190/216） |
| P2-6 strategy_line attempt 虚高 | ✅ api.py:228 排除 is_reproduction |
| P2-7 dict 形态 to 过 intake 但 resolve 不支持 | ✅ strategy.py:190 apply_diff 支持 dict 形态；钉 r3_unit:406 |
| P2-9 dispatcher 无守卫 | ✅ worker.py:127-135 循环体 try/except + 0.5s 退避。残留（P3）：瞬时异常时该项被**丢弃**（不重排队），恢复依赖重启补偿——比杀线程好，重排队更优 |
| P2-10 非 queued 一律判死 | ✅ worker.py:163-168 只收仍 queued 的崩溃孤儿，不代判 running |
| P2-11 CLI/MCP 绕过冻结 | ✅ research_cli.py:118 与 research_tools.py:53 均包 `frozen_writes()` |
| P2-12 lifespan 无错误隔离 | ✅ modules.py:243-251 逐草稿 try/except，坏草稿跳过不再拖垮启动；钉 r3:475 |
| P2-13 冻结顺延饿一天 | ✅ jobs.py:127-166 当日补跑哨兵：60s 轮询解冻（上限 2h）→ 经冻结门补跑；幂等（同刻一个哨兵）；跨日让位。残留（P3）：哨兵放行与 run 再冻结之间的窄竞态会二次顺延且哨兵已退场，概率极低 |
| P2-14 worker 线程非 daemon | ✅ `_DaemonThreadPoolExecutor`（仅覆写 _adjust_thread_count）+ stdlib 变动回退普通池 |
| P2-16 停牌垫片 fail-open | ✅ 垫片 30→250 自然日（>250 天极端停牌仍 fail-open，可接受并已注释） |
| P2-17 主板新股豁免 | ✅ `_ipo_no_limit_days` 分板块/分时代：ETF 首日即受限、科创 5 日、创业板 2020-08-24 起 5 日（此前首日）、主板 2023-04-10 起 5 日（此前首日）；按交易日历计 elapsed。钉 4 项（r3_unit:97-129） |
| P2-18 对账分母/qty | ✅ live.py:402-441：分母改 ref_price（收盘基准价 = slippage_tail 标定样本），买卖双侧补 qty_mismatches；两个既有钉改口径重锚 |
| P2-19 元模块成员参数绕过校验 | ✅ execution.py:175-180 成员参数过 validate_params，非法即拒 |
| P2-20 market_gate 静默空转 | ✅ portfolio_risk.py:157-160 基准不在面板时 `_log` 留痕 |
| P2-22 every_n 文档 | ✅ docstring 明示"日历日序数取模 ≈每 0.7N 个交易日"及取舍理由 |

附带验证（R1 P3 中被顺手修掉的）：engine.py:145 缩进残留已清；holdout docstring
"默认关"矛盾已修；research_cli 补 `--db`；bucket 事件 (symbol, 事件日) 去重 +
特征/前瞻取事件日（K3-R2 残留），相关锚重钉有据（样本集变化已在日志声明）；
paired 检验改**按日期 inner join**（backtest.py:534-549，dates_dropped 留痕，
修掉了 R1 P3"尾部截断对齐"）；评估面板垫片 120→300 自然日 + regime warmup
警告；长窗口三注记抽共享件接入 event/bucket/distribution。

### 2.2 未修（留账，2 项 + 1 项部分收口）

| R1 项 | 状态 |
|---|---|
| P2-21 momentum benchmark 名不副实 | **未修**。`bench_simple_momentum_rotation.yaml` 仍写"月度检查"，实际行为是 exit 每日（跌出 top 即卖）、entry 月度——`allows_action` 只 gate 买入未改。基准语义与文档二选一收口（改 YAML 描述为"日退出+月进入"，或给 signal/execution 加月度 exit 门控） |
| P2-15 server.py 模块级 import 耦合 | **未修**（main.py/server.py 本轮未动）。本轮实测中该风险面以另一种形式显形：见 §4——`mcp` 包缺失时 /mcp 整体 404 且日志文案误导。维持 R1 建议：research_tools 注册改惰性/受控导入 |
| P2-8 启动清扫跨进程误杀 | **以"单实例运行约定"文档化收口**（开发日志口径注记），机制未强制（无启动锁；engine_runs 收割仍无 kind 过滤，重启撞 14:00 live run 的窄窗口仍在）。单机部署下可接受，留作运行期观察项 |

---

## 3. 本轮新发现（修复轮引入或暴露；均非阻断）

1. **promote 入库门的文档-代码矛盾（需用户裁决并对齐文档）**：`api.py:239-241`
   docstring 写"入会话不设 human 门（2026-09-24 用户决策：AI 全流程闭环自动
   晋升）"；而开发日志修复段写"promote_to_library 仅 human session……**待用户
   确认**"。代码与日志必有一处过期。行为本身：血缘门（verdicted + confirmed +
   parent/experiment 血缘）完好，recompute/grant_holdout 仍仅 human——这是产品
   决策而非缺陷，但**两处文本必须统一**，且"AI 可自助晋升"若为最终决策，建议
   在详设 §5.5 入库规则处补一句会话口径。
2. **`mcp` 包缺失时 auth_wall 两用例红**：本环境 `import mcp` 失败（可选依赖
   未装）→ main.py 走 `except ImportError` 不挂载 /mcp → `tests/api/
   test_auth_wall.py` 的 `test_mcp_requires_bearer_token`/`test_mcp_invalid_token_401`
   得 404 ≠ 401。**与修复轮无关**（main.py 未改、缺包是环境基线），但暴露：
   ①这两个用例缺"mcp 未安装即 skip"守卫，环境性红会被误读为回归；②R1 P2-15
   的"静默下线 + 日志误导"面属实存在。建议：用例加 `pytest.importorskip` 或
   mount 状态探测。
3. 开发日志"全量回归 **1496 passed / 0 failed**"在本环境不可复现：我实测
   unit+api+integration = **1335 passed / 2 failed（上述 mcp 环境性）/ 4 skipped /
   slow deselect**。差额部分应在 tests/ 根目录用例与 slow 项，但"0 failed"与
   mcp 缺包事实矛盾——请补记当时的运行环境/命令（是否装有 mcp、是否 deselect
   auth_wall），避免留痕失真。
4. dispatcher 瞬时异常丢项不重排队（§2.1 P2-9 残留，P3）。
5. live ma_stop 参考线用 T-1 MA（回测 tail 口径含当日收盘）——同一"回测实盘
   同路径"口径在 ma_stop 上仍有微小差异（P3，provisional bar 语义下的合理
   近似，建议注释注明）。

---

## 4. 实测结果

| 套件 | 结果 |
|---|---|
| tests/unit/test_review_r3_unit.py + tests/integration/test_review_r3.py（新钉子 31 项） | **31 passed**（21.6s） |
| tests/unit + tests/api（-m "not slow"） | 1120 passed / **2 failed**（auth_wall MCP，mcp 缺包环境性，非本轮回归）/ 4 skipped（264s） |
| tests/integration（-m "not slow"） | **215 passed** / 1 deselected（238s） |

失败归因证据：`python -c "import mcp"` → ModuleNotFoundError（基线即如此）；
main.py mtime 早于修复轮起点、其 ImportError 回退逻辑未变。钉子质量抽查：
31 项均为真实调用形态（无参 instruments 桩、真 worker 池、真 job 函数、
live 面板含 provisional 尖峰），符合本项目"钉子复刻真实路径"的纪律要求。

---

## 5. PASS 的三个附带条件（文档/口径收口，非代码阻断）

1. **promote 决策文本统一**：代码 docstring（AI 闭环自动晋升，2026-09-24 用户
   决策）与开发日志（仅 human、待确认）二选一对齐；若维持 AI 自助晋升，详设
   §5.5 补会话口径一句。
2. **momentum benchmark 收口**：YAML/详设描述与实际行为（日退出+月进入）
   二选一对齐（R1 P2-21，本轮唯一未动的行为类 P2）。
3. **留痕修正**：开发日志补记全量回归的真实环境与 auth_wall 两用例的环境性
   失败归因；详设 §6.5.1 的"换手增幅成本可解释"硬条件按本轮口径决策（降级为
   人工判读警告）回写注记。

上述三条之外，R1 其余 P3 清单维持"随触碰模块顺手修"的原处置不变。

---

## 6. 复审方法附注

- 每条修复均对照修复后源码本体逐行核验（关键算法手算复核：CF t 临界 ν=29、
  棘轮重建与回测 max 链的数学等价性、canonical_verdict 与 conclusion 内联
  选择器的语义一致性），未采信日志与注释的自述。
- 专设"修复引入新问题"检查：P1-3 的同日买入边界（保留入场根，符合决策 6）、
  P1-1 的扩展轴重构（前收 ffill 逻辑与修复前逐位等价）、哨兵线程幂等与二次
  冻结竞态、daemon 池回退路径——均核过，结论见 §2/§3。
- 本轮未重读 review/ 下其他模型的报告，维持盲审独立。
