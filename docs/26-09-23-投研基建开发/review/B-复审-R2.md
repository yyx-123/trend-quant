# B 复审 R2：工程质量与安全评审（第 2 轮）

评审人：评审子代理 B（只读复审，未改任何源文件）
日期：2026-09-24
基线：R1 报告（docs/26-09-23-投研基建开发/review/B-工程质量与安全评审.md）判 FAIL，4 项 P1 + 17 项 P2
方法：逐项代码复读 + 临时库实证注入 + 全量测试回归

---

## 总体结论

**通过（PASS）**。4 项 P1 全部修复到可验收标准（其中 2 项留有降级为 P2 的残余，见下）；择要复核的 4 项 P2 修复全部落实；全量测试零回归（新栈 135、unit+api 1127、integration 165 全过，比 R1 多 7 个测试）。无 P0/P1 未决。

---

## 原 P1 逐项复核

### B-P1-1 DSL ref(close, 0-1) 算术绕过 → 已修复
- 代码：`src/research/dsl.py:84-93`——`ref(x, k)` 的 k 必须是 `ast.Constant` 且 `int` 且 `>= 0`，任何表达式/一元负号/浮点字面量一律 `DslError`。
- 实测矩阵（临时脚本直调 `compile_expression`）：

| 表达式 | 结果 |
|---|---|
| `ref(close, 0-1)` / `ref(close, -1)` / `ref(close, -(1))` / `ref(close, 1-1)` / `ref(close, 1*0)` / `ref(close, 1.0)` | 全部 REJECTED（"requires a non-negative integer literal"） |
| `ref(close, 0)` | 放行，row1=当日值（语义正确） |
| `ref(close, 1)` | 放行，row1=前一日值（语义正确） |
| `sma(close, 20) > ref(close, 5)` | 放行，求值正常 |

- 附带验证：`tests/integration/test_research_stage5.py:226 test_dsl_rejects_lookahead_ref` 已入套件。残留边角：`ref(close, True)` 可编译（`True` 是 int 1 → shift(1)，无前视，无害怪癖）；`ref(close, 1.0)` 被拒属可接受的偏严。不构成问题。

### B-P1-2 回测崩溃孤儿 running 行 → 主路径已修复，残留降 P2
- 代码：`src/portfolio/backtester.py:214-293`——日循环整体 try/except，异常时 `store_obj.finish_run(status="failed", error=...)` + `gateway.flush_audit()` + 重抛。
- 实测（临时库 + 注入异常模块，两个注入点分别验证）：
  - **scan 崩溃（日循环内）**：`engine_runs` 落 `status='failed', error='boom-in-scan'`——已修复，无 running 残留。
  - **prepare 崩溃（begin_run 之后、日循环 try 起点之前）**：仍留 `status='running'` 孤儿行。
- 残留评估：try 起点（backtester.py:214）在 `begin_run`（~160 行）之后，但未覆盖 `prepare(panel)`（183 行）/`prepare_with_gateway`（187 行）/ATR 预计算/start_idx 检查这一小段。该段同样执行模块代码（含 AI 提的 DSL/python 模块 prepare），崩溃场景同质但窗口窄得多。**降 P2**：建议把 try 起点上移到 `begin_run` 调用之后立即开始（一行结构调整）。

### B-P1-3 live 清单未并入持仓 → 已修复
- 代码：`src/portfolio/live.py:156-162` 先读 `manual_trades(status='open')` 得 `held_symbols`，`_live_universe_symbols(db, config, held_symbols)` 在 302 行做并集 `sorted(set(symbols) | held)`，然后才取面板。
- 实测（临时库：universe=static_list[U1]，持仓 H1 且 H1 在 metadata 中 disabled）：`generate_daily_list` 产出 `target_holdings=['H1.SS','U1.SS']`——disabled 持仓标的已进入取数集与清单，R1 的「静默失管」链路（无面板→无止损评估→ref_price=None）断开。
- 附带实测：`est_stop` 已改读影子 ctx（live.py:261），buys 输出含真实数值（`est_stop: 15.366`），不再是恒 None。

### B-P1-4 exec 无沙箱 → 已修复（验收三项实测通过），残留一项声明不实降 P2
- 代码：`src/research/modules.py:103-190`——`_prescreen_python_source`（AST：白名单 import 限 numpy/pandas/math/本栈协议类型；dunder 属性访问禁；eval/exec/open/compile/getattr/setattr/globals/locals/vars/input/breakpoint 名禁）+ `_RESTRICTED_BUILTINS`（无 open/eval/exec，保留 `__import__`/`__build_class__` 供 import 语句与 class 定义）+ 信任模型写入 docstring（"防误伤，不是对抗恶意代码的真沙箱"）。
- 实测：

| 用例 | 结果 |
|---|---|
| `import os` 的模块 | REJECTED：`prescreen failed: import not allowed: os` |
| `().__class__.__bases__[0].__subclasses__()` | REJECTED：dunder attribute access not allowed |
| 干净模块（numpy/pandas/math + SignalEvent 协议实现，含 prepare/scan） | LOADED 正常 |
| `str.format` 字符串内 dunder 路径（`"{0.__class__.__bases__[0].__subclasses__}".format(())`） | 实测不成立——`str.format` 只返回 str，拿不到活对象（R1 理论推演有误，予以更正） |

- **残留（新 P2）**：动态 `__import__("os")` 实测 **LOADED 成功拿到 os 模块**。`_RESTRICTED_BUILTINS` 注释声称"动态调用 `__import__("os")` 由 AST 预筛的 Name 白名单拦截"，但 `_prescreen_python_source` 的 Name 黑名单（modules.py:153-155）不含 `__import__`（`_BLOCKED_DUNDERS` 常量定义了却未在 walk 中使用）。声明与实际不符，一行可修：把 `"__import__"` 加入 Name 黑名单即可（不影响白名单 `import` 语句——`ast.Import` 不经 Name 检查）。定 P2 而非 P1 的理由：验收三项全部通过、信任模型已显式声明非对抗定位、其余逃逸路径实测封闭。

---

## 择要 P2 复核（任务指定的 4 项）

| 项 | 状态 | 证据 |
|---|---|---|
| est_stop 改读影子 ctx | 已修复 | live.py:261 读 `shadow_ctx.params["_est_stops"]`；实测 buys 输出 `est_stop=15.366`（R1 恒 None） |
| vol_target/drawdown_throttle 实盘 caveats | 已修复 | `_live_caveats`（live.py:376-385）按 gate.module 前缀生成注记；实测 payload `caveats=['vol_target@1 需要组合净值历史，实盘清单 MVP 不驱动它（仅回测生效）']` |
| holdout token 原子消费 | 已修复 | holdout.py:111-120 `UPDATE ... WHERE id=? AND consumed_at IS NULL` + rowcount 校验；实测：首次使用消费成功、二次使用 HoldoutError、无 token 触碰被拒 |
| regime 段指标全序列差分按日筛 | 已修复 | `_regime_segment_metrics`（evaluations/backtest.py:144-163）：收益在全序列相邻日差分（归属后一日）后按 regime 日筛，len<5 段放弃——R1 的非连续净值差分失真消除（残留 cosmetic：`_regime_split` 里 `exp_seg`/`base_seg` 成了死变量） |

---

## R1 其余 P2 状态速览（不阻断）

- 已顺带修复：`instantiate_modules` slot 泄漏（backtester.py:104 改 `slot="portfolio_risk"`）；`trend_score_cross@1` 的 `prepare_with_gateway` 双侧接线（backtester.py:186-188 用 run 级 as_of 的受限句柄、live.py:178-180）；bucket `_ScanCtx.account=None` 与 event 桩不一致（未复核到改动，保持 R1 意见）。
- 仍未修（维持 R1 P2，不阻断）：worker dispatcher 会话达 cap 时热自旋无退避；lifespan 收尾不停止 research worker；attempt_index 并发撞号；library.add_version MAX+1 竞态；backtester docstring「同一标的同日先卖后买允许」与代码排除 exited_symbols 的语义出入；DSL 大常量字符串乘法；ST/新股涨跌停近似；live 现金重建口径近似；`src/**/__pycache__` 残留已删模块 pyc；生产库孤儿表 `portfolio_backtest_runs`。

## 新引入问题

1. **P2**：动态 `__import__("os")` 未被 AST 预筛拦截（见 B-P1-4 残留；注释声称已拦截，实测未拦截）。
2. **P2**：`_fix_layering.py`（一次性分层修复脚本）遗留在仓库根目录，属开发杂物，建议删除或移入 scripts/temp/。
3. **P2**：`backtester.run_backtest` 的 try 保护未覆盖 begin_run→日循环前的 prepare 段（见 B-P1-2 残留）。
4. 提示（非问题）：`_regime_split` 内 `exp_seg`/`base_seg` 为修复后的死代码，可在下次触碰时清理。

## 验证留痕

| 验证 | 方法 | 结果 |
|---|---|---|
| DSL ref 拒绝/放行矩阵 | 临时脚本直调 compile_expression，10 条用例 | 全符合预期 |
| backtester 崩溃收口 | 临时库 + boom_prepare/boom_scan 两个注入点 | scan→failed 收口；prepare→残留 running（降 P2） |
| live 持仓并入/est_stop/caveats | 临时库 + disabled 持仓 + vol_target gate | 三项全部符合预期 |
| python 沙箱 | 5 个模块用例（import os / dunder / 动态 __import__ / format 逃逸 / 干净模块） | 前两项拒、干净模块过；动态 __import__ 漏（P2）；format 逃逸不成立 |
| holdout 原子消费 | 临时库 enforced 下 token 首用/复用/无 token | 首用过、复用拒、无 token 拒 |
| 新栈测试 | pytest 13 个新测试文件 | 135 passed |
| 存量回归 | pytest tests/unit+tests/api（1127）、tests/integration（165） | 全过，零回归（比 R1 多 7 个测试） |

B_VERDICT_R2: PASS
