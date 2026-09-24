# C 盲审报告：测试完备性与有效性

审查对象：13 个新增测试文件（unit 7 + integration 5 + api 1），被测实现
`src/gateway/`、`src/engine/`、`src/portfolio/`、`src/research/`、`src/core/run_freeze.py`。
审查方式：只读代码审查 + 独立验算（statistics.NormalDist / Decimal / 手写 BH）+
实际运行。审查人对开发过程零先验，仅依据代码与测试文本判断。

## 总体结论

**有条件通过。** 测试整体质量明显高于平均水平：engine golden 全部手算值经
独立复算正确；PSR worked example 有绝对数值锚（0.94<Φ(1.5804)<0.95）；
stops 对拍确为两套独立实现（`services/stop_loss.py` 不 import `engine.stops`）；
parity 对拍是新引擎薄驱动 vs 旧 `rule_backtest` 引擎两条独立代码路径，且用
`len(trades) >= 4` 钉住非空前提；append-only 触发器用原生 SQL 直测，无循环论证。
未发现 P0 级虚假断言（assert True / 自比 / 短路）。

但存在 3 项 P1：(1) 一个空体测试恒过；(2) 一个测试名不副实——声称测
单调性却无任何单调性断言，且多个 verdict 断言退化为枚举重言式；(3) 两处
裸 `pytest.raises(Exception)` 无任何消息/类型校验，实现抛错类型换错也照过；
另有一处单元层测试经 `get_strategy_config()` 隐式读写生产库，属环境耦合/
污染隐患（P1）。以上任一修复前不建议视为完备。

运行结果：全部 135 个用例通过（unit 98 / integration 37 分批 25+12 /
api 含于第二批，命令见下）。

```
.venv/Scripts/python.exe -m pytest tests/unit/test_research_stage0.py \
  tests/unit/test_gateway.py tests/unit/test_engine_fees_matcher.py \
  tests/unit/test_engine_golden.py tests/unit/test_engine_stops_parity.py \
  tests/unit/test_portfolio_config.py tests/unit/test_research_stats.py -q
# 98 passed in 16.45s
.venv/Scripts/python.exe -m pytest tests/integration/test_engine_parity.py \
  tests/integration/test_portfolio_backtester.py \
  tests/integration/test_research_evaluations.py \
  tests/integration/test_live_runner.py tests/api/test_research_ledger_api.py -q
# 25 passed in 38.23s
.venv/Scripts/python.exe -m pytest tests/integration/test_research_stage5.py -q
# 12 passed in 23.48s
```

## 可疑断言清单

### P0 虚假断言（恒真/自比/循环论证）

无。

特别核查过的对拍独立性（均排除循环论证嫌疑）：

- `test_research_stats.py` 的 PSR/DSR 参考实现是测试内**独立书写**的公式
  （未 import 被测模块内部件），Φ 路径声明为 NormalDist vs 实现的 math.erf。
  我独立复算：PSR worked example z=1.580352、Φ(z)=0.942987，测试的
  0.94<got<0.95 锚正确；DSR 参考值=0.574009（测试未锚此绝对值，见 P2）。
  注：statistics.NormalDist.cdf 内部同样走 math.erf，"独立"主要体现在
  公式代数独立书写 + PSR 绝对锚；DSR 的 Φ⁻¹ 路径两侧共享 stdlib
  `NormalDist.inv_cdf`，独立性打折扣但可接受（stdlib 可信，测的是公式装配）。
- `test_engine_stops_parity.py` 对拍双方：`engine/stops.py`（新栈）vs
  `services/stop_loss.py:compute_stop_loss`（实盘侧）——确认后者不 import
  前者，是两套独立公式装配。注意两侧的 ATR 都最终委托给
  `core.indicators.atr`，即**对拍锁定的是止损公式装配与 T-1 截断语义，
  不是 ATR 数学本身**；`live["atr_at_buy"] == atr_incl` 这条断言因两侧
  同序列而接近重言（仅验证截断取点一致），ATR 正确性依赖
  `test_core_indicators.py` 另行覆盖。
- `test_engine_parity.py`：`engine/parity.py:run_macd_parity`（新引擎薄驱动）
  vs `rule_backtest.SingleSymbolAllInBacktestEngine`（旧引擎），两条独立路径；
  差异白名单（滑点比例 1.003/1.002、0.997/0.998）为手算比例，正确。
- `test_portfolio_backtester.py` / `test_engine_fees_matcher.py` /
  `test_engine_golden.py` 的全部数值我逐一复算：10.03=10×1.003、
  10035=10030+5、50428.5=39965+10468.5−5、10.4685=10.5×0.997、
  利息 252000×0.01/252=10、heat=(10−9.4)×1000=600、
  target_value 20000/10.03=1994→1900、印花税 9.97×1000×0.0005=4.985、
  递减 500×10.03+5>5000→400 股——全部正确，非实现誊抄。
- `test_gateway.py` 涨跌停推导：10.005×1.1→11.01（Decimal ROUND_HALF_UP
  与实现的向量化 floor(x·100+0.5+1e-9)/100 双路复算一致）、除权基准
  10/1.5×1.1→7.33、×0.9→6.0，均正确。
- `test_research_stats.py` BH 手算 adjusted=[0.01, 0.04, 0.1025, 0.1025,
  1.0…] 与我手写 BH 单调化结果一致；PBO 构造性用例（主导策略 λ=1→PBO=0、
  n_combinations=C(8,4)=70）推理复核成立；MinTRL≈745.55、round 后
  PSR≈0.9501 在 abs=0.01 内，正确。

### P1 无效或过宽断言

1. **空体测试恒过** — `tests/unit/test_portfolio_config.py:129-130`
   `test_meta_nesting_rejected_by_strategy_layer` 只有 docstring、零语句，
   pytest 收集即绿。嵌套拒绝实际由上一用例（:119-126）覆盖，此用例是纯
   占位，给人"已测"的错觉。删之或移入真实断言。
2. **名不副实 + 枚举重言** — `tests/integration/test_research_evaluations.py`
   - `test_bucket_analysis_monotonicity`（:122-145）名称承诺"单调性"，
     断言只有 `len(bucket_table)==5` 与
     `random_band_abs95 is None or >= 0`（后者近乎重言——任何合法值都过），
     **没有任何单调性/排序力断言**。构造数据明知一半标的有趋势，却不钉
     verdict 方向。
   - :85、:142 与 `test_research_stage5.py:453` 的
     `assert v["suggested_verdict"] in ("confirmed","rejected","inconclusive")`
     是枚举重言——实现返回任何合法值必过，只有发明第四个枚举值才会挂。
     作为端到端 smoke 可留，但须配合方向性断言（合成数据漂移方向已知，
     至少 event_study 一例可钉 `confirmed` 或证据符号）。
3. **裸 `pytest.raises(Exception)` 无补偿校验** — 实现有具体异常类型
   （PermissionDenied / LifecycleError / DuplicateFound 等），换成任何
   意外异常（TypeError、sqlite 错误、拼写错误）测试照过：
   - `tests/unit/test_research_stage0.py:576`（AI 越权发 token；实现抛
     `PermissionDenied`，见 `src/research/sessions.py:66`）——应断言类型。
   - `tests/integration/test_research_stage5.py:306`（AI retire 模块，
     同上）——应断言类型。
   - `tests/integration/test_research_stage5.py:410`（grade 升级拦截）、
     `tests/integration/test_live_runner.py:126`（ghost 策略版本）——
     至少校验消息子串。
   - （有消息校验、降级为 P2 的同类：`test_research_evaluations.py:111`
     校验了 "event module not registered"、`:163` 未校验消息；
     `test_research_stage5.py:188` 校验了 "duplicate_of" + 首个实验 id。）
4. **单元层测试隐式耦合生产库** — `tests/unit/test_engine_stops_parity.py`
   走 `compute_stop_loss → get_strategy_config() → get_db()`
   （`src/core/strategy_config.py:83-94`），而 `tests/unit/conftest.py`
   **没有** isolate_get_db 夹具（integration/api 层有）：在被测机上实际
   读写 `data/trend_quant.db`（已证实该库存在且当前存默认值 1.5/2.5，
   故测试今日通过）。后果：(a) 用户改掉生产库的 stop 倍数配置，测试即
   莫名变红——flaky 于环境；(b) 在干净机器上运行会**创建**生产库文件并
   播种配置——测试污染环境。测试本身写法无错，缺的是单元层隔离夹具或
   该用例显式 monkeypatch。

### P2 可加强

1. `tests/unit/test_gateway.py:96`：
   `assert panel.provisional[-1, 0] is True or bool(panel.provisional[-1, 0])`
   ——`X is True or bool(X)` 恒等于 `bool(X)`，冗余写法（为兼容 numpy.bool_
   只需后者）。非恒假、非恒真，但形式像"和稀泥断言"，建议直写
   `assert bool(panel.provisional[-1, 0]) is True`。
2. `tests/unit/test_research_stats.py` DSR 只有往返对拍、无绝对数值锚
   （PSR 有 0.94<·<0.95，DSR 没有对应锚）。独立复算参考值 0.574009，
   建议加 `assert got == pytest.approx(0.5740, abs=1e-4)`。
3. `test_research_stats.py:110` `assert a["low"] <= a["point"] <= a["high"]`
   区间含点检查偏弱（low=-∞/high=+∞ 的实现也过）；已有全正序列用例
   （:113-116）兜底，可接受，建议再钉一个已知种子下的数值区间。
4. `tests/integration/test_live_runner.py:92-104`：价差对账断言整体包在
   `if target["buys"]:` 里——若清单为空，整块静默跳过、测试照样绿。
   已实测当前夹具 buys=1（非空），但前提未钉死；建议开头加
   `assert target["buys"]`。
5. `tests/integration/test_portfolio_backtester.py:204`：注释写"最多 1 只
   成交"，断言却是 `len(buys) <= 2`——注释与断言不一致，按弱断言放行；
   另有 :118 `positions = EngineStore` 死赋值、:181 死变量 `first_close`。
6. `tests/integration/test_research_evaluations.py:145`
   `random_band_abs95 is None or >= 0` 重言式形状检查（见 P1-2，单列因其
   本身断言强度≈0）。
7. `tests/integration/test_research_stage5.py:356-401` 课题结论用例的断言
   多为形状级（verdict_counts 非空、suggested_grade 存在、note 含固定
   文案），`chosen` 又自适应 suggested——未钉 verdict_counts 的具体分布，
   统计摘要内容错误（如计数错位）测不出来。
8. `test_research_stage0.py:447-476` 与 `test_research_stage5.py` 多处直接
   改写全局 `evaluations.base._MODULES` / `set_enforced`：均有 finally
   恢复，串行运行安全；若将来上 pytest-xdist 并发会互相踩踏，建议在
   conftest 层做注册表快照恢复。

## 覆盖缺口清单（按模块）

### engine（matcher/engine/stores）

- 已覆盖良好：T+1（golden :83-101 + matcher t1_block）、涨跌停买卖双侧、
  跳空穿透按开盘、盘中触及按止损价、停牌、sellable 滚动、五表落库、
  计息、heat、整手/现金递减、target_value。
- **缺口 1**：`matcher.match_intraday_stop` 先判 `is_limit_down` 后判触发
  （`src/engine/matcher.py:204-209`）——"跌停日但止损价未触及"时实现会
  记 `unfilled(limit_down)`，与其自身 docstring"未触发→not_triggered
  不落 unfilled"矛盾。**无任何测试钉这条边界**（现有
  `test_stop_blocked_at_limit_down_records_unfilled` 恰好是既跌停又触发）。
  这是测试漏检的潜在实现 bug，建议补用例并对齐语义。
- **缺口 2**：T+1 阻塞后**次日重试**（止损顺延）只有 matcher 层的 unfilled
  记录断言，回测层"次日按同口径重评估重试"无端到端用例（现有
  test_t1_blocks_same_day_sell 钉了次日可卖，可视为部分覆盖；止损-specific
  的顺延重试未测）。
- **缺口 3**：`match_buy` 的 `bar_close <= 0 → suspended`（matcher.py:75-76）
  与 target_value 下 lot_rounding/insufficient_cash 的 reason 判别
  （:107-112）未测。
- **缺口 4**：`fees` 的 ETF/股票佣金边界已有；`apply_slippage=False` 仅
  单断言，卖出滑点符号方向已有；`profiles.get_profile` 字符串路径未测（小）。

### gateway（panel/service/tradability/audit）

- 已覆盖良好：as_of 必填、historical 严格 ≤ as_of、live provisional、
  字段校验、对齐与 pre_close、受限句柄 get_panel 越权+留痕、
  data_version 锚定、audit 字段、主板/科创/创业幅度、分位舍入、除权基准、
  停牌日前收沿用、新股无涨跌幅、非交易日跳过。
- **缺口 1**：新股 5 日窗口的**边界**（第 5 个 vs 第 6 个交易日）未测——
  现有用例只测窗口内 3 天，`elapsed < 5` 的 off-by-one 测不出来
  （`src/gateway/tradability.py:147-151`）。
- **缺口 2**：BoundGateway 的 `get_tradability` / `get_production_indicator`
  越权路径（service.py:232-249）未测，只测了 get_panel。
- **缺口 3**：`get_production_indicator` 的 as_of 裁剪（service.py:104-148）
  整体未测。
- **缺口 4**：20% 板块（688/300）在 `compute_tradability` 端到端的
  涨停价推导未测（board_limit_pct 单测了幅度表，但没测它接入推导）。
- **缺口 5**：除权日恰逢前一日停牌（base 用 ffill 前收再 ÷f_t）的组合
  边界未测；ST ±5% 为实现明示的已知简化（st_status=unknown 按主板），
  属阶段 7 前豁免，不计缺口但应在报告里挂账。

### portfolio（backtester/live/registry/strategy/library）

- §5.4.2 八条写死语义逐条核对：
  1. 同日先卖后买（跨标的）——**已测**（test_same_day_sell_buy…）；
  2. 边卖边买同标的禁止——**已测**（同用例反向断言）；
  3. 买入失败不递补——**已测**（涨停头名+次名不递补）；
  4. 止损优先于信号——**未直测**：`backtester.py:231` 的执行序
     `[*stop_intents, *signal_exits, *rotation_intents]` + exited_today
     去重没有任何用例构造"同一持仓同日既有止损又有信号退出"来钉优先级；
  5. 卖出全部整仓——**仅隐式**（engine golden 的卖出恰好全仓；无
     "部分持仓不可卖"的反向断言）；
  6. 不加仓——**已测**（always_entry 买入持有仅 1 笔）；
  7. 空仓计息 1%——**已测**（engine golden 手算 10 元/日）；
  8. 窗口默认 2015-01-01 起——**未测**（backtester 要求显式起止，默认
     窗口在上层 runner，低价值，可豁免）。
- 其他：位级确定性、槽位上限、heat_cap、月度行动门、落库血缘均已测。
- **缺口**：`live.py` 对账的卖出侧（missing_sells / 已卖未卖）与
  reconcile 的价格差符号方向（卖高卖低）未测，只测了买入侧。
- registry/strategy/library：解析、哈希稳定/敏感、diff 追加/替换、元模块
  槽位消歧、种子幂等、不可变触发器均覆盖到位。

### research（状态机/台账/holdout/模块门/recompute/stats/API）

- 状态机：跳步、终态不可逆、重复确认、可降不可升、allowed_finals、
  烂尾扫描、archive 门、课题关题前后约束——覆盖良好。
- 骨架校验：七槽、假设长度、评估模块注册、不可变基准、基准入库、
  模块注册、多槽 compound 三态（拒/标/单槽）、attempt_index 跳过
  rejected——覆盖良好。
- holdout：窗口判定、enforced 开关、token 一次性、人审门槛——已测。
  **缺口**：token 绑定 experiment_id 不匹配分支（holdout.py:106-109）
  与非实验路径"触碰必留痕"（决策 C3）未测。
- 模块门：DSL 免测、DSL 前视拒、python 前缀稳定性拒/放、retire 人审——
  覆盖良好（含 accept 例，不是只测拒绝）。
- 重复检测、recompute 旧 verdict 一行未改、并发池崩溃隔离——已测且
  注释记录了一次"试图 SQL 直改 spec_json 被触发器正确拦截"的测试演化，
  说明测试真的在咬实现。
- stats：见 P2-2/3，DSR 缺绝对锚、bootstrap 缺数值锚。
- API：页面渲染含 id/标题/verdict、confirm/conclude 直读库验证（不依赖
  HTML）、404——质量合格。

## 修复建议（按优先级）

1. 删除或实化 `test_portfolio_config.py:129` 的空体测试。
2. `test_bucket_analysis_monotonicity` 增加真正的单调性/方向断言（合成
   数据漂移已知），或改名 `..._shape`；三个 `in (confirmed, rejected,
   inconclusive)` 枚举重言至少一处换成钉死方向。
3. 四处裸 `pytest.raises(Exception)` 改为具体异常类型
   （PermissionDenied / LifecycleError / DuplicateFound / LibraryError），
   无类型的至少 `match=` 关键子串。
4. 给 `tests/unit/conftest.py` 补 isolate_get_db 夹具（对齐 integration
   层），或在 stops 对拍用例内 monkeypatch `get_strategy_config` 返回固定
   1.5/2.5，斩断单元测试对 `data/trend_quant.db` 的隐式读写。
5. matcher 的"跌停未触发"边界：补用例并与实现对齐（先判触发再判阻塞，
   或接受现状并改 docstring+钉测试）。
6. 补：新股第 5/6 日边界、止损优先于信号的执行序、卖出整仓反向断言、
   BoundGateway 另两个方法的越权、holdout token 错绑 experiment_id、
   live 对账卖出侧。
7. 小项：DSR 绝对锚 0.5740；live_runner 钉 `assert target["buys"]`；
   清理三处死代码（test_gateway.py:196 未用变量、backtester 测试 :118/:181）；
   `heat_cap` 注释与断言对齐。

C_VERDICT: FAIL
