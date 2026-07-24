# 仓位管理（Position Sizing）功能实施方案 v3

> 日期：2026-07-22（v3 修订：2026-07-24，吸收外部 code review 意见）
> 分支：feature/cangwei-strategy
> 状态：方案已获用户认可并经两轮外部 code review（v2：统一降级机制；v3：
> Kelly 净收益率口径、atr_at 签名与回溯上限、数据链路细节；v3.1：SELL trade
> 新增 avg_cost 字段、skip reason 枚举补全），审核结论为批准开发

## 1. 背景与目标

当前规则回测（`src/rule_backtest/`）支持多交易策略批量回测，但所有策略的买卖都是
当前可用资金的全仓买入、全仓卖出（`_max_buy_qty` 总是尽可能买满）。本功能引入
「仓位策略」抽象，在买入信号触发时由仓位策略决定投入多少资金，实现仓位控制。

### 1.1 需求要点（用户原始诉求）

1. 支持按可用资金的固定百分比买入（如 70%；现状 = 固定 100%）。
2. 支持按风险预算买入：风险预算可设为绝对金额（如 1 万元）或总资金百分比
   （如 1%/2%，可调）；基于风险预算与理论硬止损金额算出最大可买数量；
   若全买也不超风险预算则全买，否则只投入风险预算允许的金额。
3. 支持最优凯利比买入：利用本 run 内该策略在该标的下的历史交易统计
   （胜率、盈亏比）计算最优投入比例；本笔交易结果成为下一次计算的依据。
   默认依赖前 10 次交易，不足则取已有数据；第一次买入直接全仓。
4. 仓位管理仅限买入时；一旦买入即视为持仓，有剩余资金也不再加仓（无分批
   买入）；卖出时全部卖出（无分批卖出）。
5. 回测支持交易策略 × 仓位策略多选，按笛卡尔积产出结果（3 交易策略 ×
   2 仓位策略 = 6 条结果）。
6. 未投入的资金不产生收益和亏损，直接留存在现金中计入净值。
7. 仓位策略不要求像交易策略那样的通用条件配置化，但三种内置策略的参数必须
   可配置；架构上要为未来新增仓位策略预留扩展空间。

### 1.2 已确认的关键决策

- **统一降级机制（v2 修订）**：所有仓位策略在「算不出理论值」的降级场景下，
  统一用 `fallback_pct`（默认 10%，每个仓位策略实例可调）买入 + 降级 flag，
  不再按策略区分降级行为：
  - 凯利 f\* ≤ 0 → `fallback_pct` 买入（flag `kelly_floor_applied`）
  - 风险预算完全无可用 ATR → `fallback_pct` 买入（flag `atr_unavailable_fallback`）
  - 风险预算当日 ATR 缺失但向前回溯可取到 → 正常路径，仅 info flag
    `atr_fallback_prev_day`，不算降级
  - 凯利无任何亏损样本（盈亏比 b 无法计算）→ 正常路径（f\* = p，封顶
    `max_pct`，不标记）
- **降级必须用户可感知**：warn 日志 + 交易记录 `sizing.flags` + 前端交易行
  橙色徽标 + K 线买点异色标记。不能只打日志。
- **skip（不买入）仅保留机械性失败**，reason 枚举两种：`insufficient_cash`
  （连一手都买不起）与 `sizer_target_below_lot`（现金够买一手但仓位策略
  target 不足一手），记录 `skipped_buys`，不视为策略降级。
- **凯利历史口径**：同一回测 run 内、该 交易策略 × 标的 × 仓位策略 组合下
  的先前已平仓交易（回测结果不持久化，跨 run 统计不可行也无意义）。
- **凯利首次买入全仓是刻意的**（需求 1.1 第 3 条原文确认）：虽然「信息最少时
  下注最大」与统一降级哲学相反，但这是用户明确指定的行为，后续 review 不必
  再质疑。
- **仓位策略管理 UI**：独立新页面 `/position-strategies` + 导航栏新项。
- **引擎天然满足的约束**：有持仓时不再买入（无分批买入）、卖出全量
  （无分批卖出）；闲置资金留在 cash 计入每日 equity、不产生收益
  （`daily_nav` 已有 cash / market_value 分列）。

## 2. 现状架构要点（集成点）

- `engine.py` `SingleSymbolAllInBacktestEngine.run`：逐日循环，卖出优先、
  当日卖出后不再买入；买入点调用 `_max_buy_qty(cash, reference_price,
  execution)` 计算最大可买数量（含滑点、费率、最低佣金、lot 对齐的循环校验），
  `_execute_buy` / `_execute_sell` 处理成交与费用；SELL trade 记录 `pnl`。
- `models.py`：`RuleBacktestRequest`（strategy/symbol/bars/起止日期/
  execution/run_id/progress_callback）、`BacktestExecutionConfig`
  （initial_capital/fee_rate/fee_min/slippage/lot_size 等）、`PositionState`。
- `service.py` `RuleBacktestService.run`：接受 `strategy_ids` 列表，逐策略跑
  引擎，返回 `results[]` + 首条结果平铺（向后兼容）；进度 total =
  天数 × 策略数。
- 策略存储：DB 表 `rule_strategies`（payload_json，软删除）+ YAML 兜底
  （loader.py）；仓位策略镜像此模式但只走 DB。
- ATR 取值：`ValueResolver.atr_value_at`（P1.3 全序列 memoize 热路径）。
- 回测 API：`src/app/routers/rule_backtest.py`，`POST /api/run` 异步 +
  `GET /api/progress/{run_id}` 轮询；`/api/meta` 是前端表单默认值唯一来源。
- 前端：回测发起在 `market_view.html`（标的查看页，策略多选 checkbox 面板）；
  策略 CRUD 在 `rule_backtest.html`（策略管理页）。
- Golden 测试：`tests/unit/test_p13_memoized_golden.py` 要求 memoized 与
  legacy 路径结果 bit-identical——sizer 为 None 时必须走完全相同的旧路径。

## 3. 方案详设

### 3.1 新包 `src/rule_backtest/sizing/`

**`base.py`** — 抽象基类与数据模型：

```python
@dataclass
class SizingContext:        # 引擎在买入点喂给 sizer 的全部上下文
    cash: float
    equity: float           # 买入点必无持仓，故此刻 equity == cash；
                            # 两字段保留是为未来加仓类策略留口，sizer 可任取
    reference_price: float  # 当日收盘价
    exec_price: float       # 含滑点估算成交价
    atr_at: Callable[[int, int], float | None]
                            # atr_at(period, lookback=0)；由引擎闭包绑定当日
                            # 在 all_bars 中的原始位置 idx（过滤后未 reset index，
                            # 与 ValueResolver.atr_value_at(idx, period) 对齐），
                            # lookback>0 表示向前回溯 N 根；越界/预热期返回 None
                            # （_value_at 对 idx<0 返回 None，天然安全）。
                            # 注：Callable 标注表达不了 lookback=0 的默认值，
                            # 实现时用 Protocol 精确声明（含默认参）
    closed_trades: list[dict]  # 本 run 内已平仓 SELL 交易（含 pnl/qty/avg_cost；
                            # avg_cost 为本次配套新增的 SELL trade 字段，见 3.3）
    execution: BacktestExecutionConfig

@dataclass
class SizingDecision:
    action: Literal["buy", "skip"]
    target_qty: int          # 期望数量（浮点结果须显式 int() 向下取整；
                             # 此处不做 lot 对齐，lot 对齐与费用校验由引擎最终裁决）
    position_pct: float      # 目标投入占权益比例（展示用）
    flags: list[str]         # 降级/信息标记
    note: str                # 人类可读说明

class PositionSizer(ABC):
    sizer_type: ClassVar[str]
    def __init__(self, fallback_pct: float = 0.10, **params): ...
                            # fallback_pct 是显式实例参数（统一降级仓位），
                            # 不用类属性，保证每个仓位策略实例独立可调
    def decide(self, ctx: SizingContext) -> SizingDecision: ...
    def _fallback_decision(self, ctx, flag, note) -> SizingDecision: ...
    @classmethod
    def param_specs(cls) -> list[dict]  # 供 /api/meta 驱动前端表单
```

**ATR 回溯语义（risk_budget 专用）**：`atr_at(period, lookback)` 从 lookback=0
开始递增直至取到非 None 值；**回溯上限为序列起点**——一直回溯到 all_bars
第一根仍为 None（上市初期预热不足）→ 走统一降级 `fallback_pct` 买入
（flag `atr_unavailable_fallback`）。sizer 侧循环条件必须写清上限，不得依赖
越界异常。回溯走 memoized 全序列，纯索引查找，无性能问题。

**`fixed_pct.py`**：参数 `pct`（默认 1.0 = 现状全仓）。
`target_qty = cash × pct / 每股成本`，其中**每股成本口径与 `_max_buy_qty`
一致**：`exec_price + exec_price × fee_rate`（不预扣最低佣金 fee_min——即使
略高估也会被引擎 `min(affordable, ...)` 截断，安全性无虞，仅影响展示用的
`position_pct` 精度，口径在此注明）。

**`risk_budget.py`**：参数 `mode`（`absolute` | `equity_pct`）、`value`
（如 10000 元 或 0.01）、`atr_period`（默认 20）、`atr_mul`（默认 1.5）、
`fallback_pct`。

- 理论硬止损 = 买入估算价 − atr_mul × ATR（仅用于仓位计算，不影响实际卖出
  条件）；每股风险 = atr_mul × ATR。
- 风险预算 = value（absolute）或 equity × value（equity_pct）。
- `target_qty = 风险预算 / 每股风险`，与最大可买数量取小：
  可买量 ≤ 目标 → 全买（info flag `risk_budget_unconstrained`，非降级）；
  反之部分买入。
- ATR 缺失处理见上文「ATR 回溯语义」：回溯取到历史 ATR → 正常路径（info
  flag `atr_fallback_prev_day`）；回溯至序列起点仍无 → 统一降级
  `fallback_pct` 买入（flag `atr_unavailable_fallback`）。

**`kelly.py`**：参数 `lookback`（默认 10）、`fraction`（凯利乘数，默认 1.0，
可设 0.5 半凯利）、`max_pct`（默认 1.0）、`fallback_pct`。

- 取本 run 内最近 `lookback` 笔已平仓交易，**统计口径为每笔净收益率而非绝对
  金额**（P0 修订）：`ret_i = pnl_i / (qty_i × avg_cost_i)`。引擎里
  `avg_cost = total_cost / qty` 已含买入费用、`pnl` 是净额，故该比率正好是
  净收益率。胜率 p 与盈亏比 b = 平均盈利收益率 / 平均亏损收益率（绝对值）都
  基于 `ret_i` 序列计算——仓位策略会让各笔交易规模不同，若用金额口径，后期
  大仓位盈亏会主导 b，使 Kelly 公式失真。`f* = p − (1−p)/b`，再乘
  `fraction`。
- 无历史（第一次买）→ 全仓（正常路径，不标记）。
- f\* ≤ 0 → 统一降级 `fallback_pct` 买入（flag `kelly_floor_applied`）。
- 无亏损样本 → b 视为无穷大，f\* = p（正常路径）。
- f\* > max_pct → 封顶 max_pct。

**`registry.py`**：`SIZER_REGISTRY = {"fixed_pct": ..., "risk_budget": ...,
"kelly": ...}`，含参数 spec、校验、从存储 dict 实例化。新增仓位策略 =
加一个类 + 注册一行（仿 indicator registry 模式）。

**`loader.py`**：`PositionStrategyLoader`，DB 读写仓位策略定义。

### 3.2 存储

- `db.py` 新表 `position_strategies`：`id TEXT PK, name, sizer_type,
  params_json, is_active, created_at, updated_at`（软删除，镜像
  `rule_strategies` 模式）+ CRUD 方法。
- **被软删除的仓位策略仍被回测引用时**：loader load 失败即报错，行为与交易
  策略一致，不做静默降级。
- 回测未选仓位策略 → 内置全仓（fixed_pct 1.0），无需 seed 数据。

### 3.3 引擎集成（`models.py` / `engine.py`）

- `RuleBacktestRequest` 新增可选字段 `sizer: PositionSizer | None = None`；
  **为 None 时走现有全仓路径，逐字节保护 P1.3 golden 测试**。
- **SELL trade dict 新增 `avg_cost` 字段**（卖出时的持仓成本价，现有 trade
  dict 只有 qty/pnl/net_proceeds 等，无此字段——Kelly 的
  `ret_i = pnl_i / (qty_i × avg_cost_i)` 依赖它）：`_execute_sell` 已接收
  `avg_cost` 参数，仅需透传出队。纯增量改动，golden 测试比较的是 memoized
  vs legacy 两条路径、两边同时变，bit-identical 不受影响；前端交易明细还可
  顺带展示成本价。
- 买入点流程：`affordable_qty = _max_buy_qty(...)`（现有逻辑不变）→
  `decision = sizer.decide(ctx)` → `qty = min(affordable, lot 对齐后的
  target)`：
  - `action="skip"` 或 qty = 0 → 记入结果新字段 `skipped_buys:
    [{date, reason, note, close}]`，不成交。**skip 的 reason 枚举**：
    `insufficient_cash`（连一手都买不起）与 `sizer_target_below_lot`
    （现金够买一手，但仓位策略算出的 target 不足一手，如风险预算只够
    50 股 < lot 100）——两种成因分开标注，前端灰色行的原因列才能准确；
  - 成交 → trade dict 新增 `sizing: {sizer_id, sizer_type, position_pct
    （实际投入/权益）, flags, note}`。
- 凯利上下文：`ctx.closed_trades` 传引擎内 trades 中的 SELL 记录（天然满足
  「本笔结果成为下一次依据」）。
- 结果新增 `sizer_id / sizer_name / skipped_buys`。
- **charts 数据链路（两处 buy_points 都要透传 flags）**：engine 的 charts 有
  两套买卖点——`charts.buy_points`（`_trade_point` 生成）与
  `charts.kline.buy_points`（`_build_kline_payload` 生成，service 的
  `multi_kline` 用的是后者）——sizing flags 必须**两处都带上**；另在 charts
  新增 `skipped_buy_points`（日期/价格/原因），供 K 线空心标记使用，数据链
  路不得断在 `skipped_buys` 顶层字段上。

### 3.4 Service / API

- `RuleBacktestService.run` 接受 `position_strategy_ids: list[str]`；
  交易策略 × 仓位策略**笛卡尔积**逐组合跑引擎，每条结果带
  `strategy_name × sizer_name` 标签；进度 total = 天数 × 组合数。
  `position_strategy_ids` 缺省 → 单倍全仓，完全向后兼容。
- `POST /rule-backtest/api/run` 请求模型加 `position_strategy_ids`；
  `GET /rule-backtest/api/meta` 增加 `position_strategies`（列表）与
  `sizer_types`（参数 spec，前端表单唯一来源）。
- 新 router `src/app/routers/position_strategy.py`：`GET
  /position-strategies` 页面 + CRUD api，注册进 `main.py`。

### 3.5 前端

- **新页 `web/templates/position_strategies.html`**（导航「仓位策略」）：
  仓位策略卡片列表 + 新建/编辑/复制/删除弹窗；类型下拉切换后按
  `sizer_types` 的 param spec 动态渲染参数表单；`base.html` 加导航项。
- **`market_view.html` 回测区改造**：
  - 新增「仓位策略」多选下拉（复用现有 checkbox 面板组件模式）；
  - 汇总表每行标签 = 「交易策略名 × 仓位策略名」，点击行切换图表买卖点
    （现有按 index 切换逻辑不变）；
  - 交易明细表：BUY 行新增「仓位%」列；带降级 flag 的买入行橙色徽标；
    `skipped_buys` 以灰色行插入（数量 0、原因列示）；
  - K 线：降级买点异色标记、跳过买点空心标记。

### 3.6 测试

- `tests/unit/test_position_sizers.py`：三个 sizer 数学正确性 + 全部边界
  （统一 fallback_pct 各触发路径、ATR 回溯与缺失、风险预算封顶与全买分支、
  凯利首次全仓 / f\*≤0 兜底 / 封顶 / 无亏损样本）。
- **Kelly 收益率口径单测**：构造仓位逐笔变化的交易序列，断言 b 不受交易
  规模影响（同一收益率序列、不同金额规模 → 相同 f\*）。
- 引擎测试：部分买入后现金留存且 equity 正确；skip 记入 skipped_buys；
  凯利逐笔消费本 run 历史；**两套 buy_points（charts.buy_points 与
  charts.kline.buy_points）均带 sizing flags**；charts 含
  skipped_buy_points；ATR 回溯至序列起点仍缺失 → fallback 的边界用例。
- service / API 测试：3 × 2 组合返回 6 条结果；缺省仓位策略结果与现状一致；
  **`position_strategies` CRUD router 测试**（含软删除后 load 报错）。
- 现有 golden 测试（`test_p13_memoized_golden.py`）不改动、必须通过。

## 4. 实施顺序

1. sizing 包（base + 3 sizer + registry）+ 单测
2. DB 表 + loader
3. 引擎/模型集成（skipped_buys、sizing 注解）+ 引擎测试
4. service 笛卡尔积 + API（run / meta / CRUD router）
5. 前端：新管理页 → market_view 改造
6. 全量测试 + 文档更新
