"""组合回测器（详设 §5.4）：日循环（尾盘口径）。

```
for t in trading_days(start, end):
    1. 持仓风控评估：逐持仓 evaluate → 止损/离场（盘中触及按止损价，跳空按开盘）
    2. 信号扫描：universe 内逐标的算事件（经 L1.5 取截至 t 收盘的数据）
    3. 排序与筛选：rank → portfolio_risk（槽位/热上限/集中度/闸门）→ sizing
    4. 当日尾盘成交：经 L2 下单，涨跌停/停牌/T+1 卡控
    5. 日结：NAV、heat、exposure 落 engine_daily_nav
```

§5.4.2 写死语义（跨标的 vs 同标的，澄清——旧措辞把"同日先卖后买
允许"与"边卖边买不允许"并列，同标的时二者指的是同一件事）：

- **跨标的**：当日卖出释放的现金与槽位当日即可用于新买入（先卖后买）；
- **同标的**：当日卖出后当日不再买回（`exited_symbols` 过滤）；
- 其余：买入失败不递补；止损优先于信号；卖出全部整仓；不加仓；空仓计息
  1%；窗口默认 2015-01-01 起（sample/holdout 由 L4 传入，backtester 不感知）。
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

import numpy as np
import pandas as pd

from core import run_freeze
from core.indicators import atr as core_atr
from engine.engine import Engine, new_run_id
from engine.matcher import TradabilityCard
from engine.models import ExitOrderIntent, OrderIntent
from engine.store import EngineStore
from gateway.service import Gateway
from portfolio.context import AccountView, DayContext, PanelView
from portfolio.strategy import StrategyConfig

# 面板加载的历史垫片：SMA200（breadth/market_gate）+ MACD/ATR 预热，
# 500 个日历日覆盖全部内置模块的预热需求。
LOOKBACK_PAD_DAYS = 500


class BacktestError(Exception):
    pass


def _holdout_start(db) -> str:
    """holdout 段起点（决策 C3 留痕判定用；配置缺省 2025-01-01）。

    经 gateway 层可用的最轻读取：直接读 app_config（元数据配置，非行情，
    不违反数据面铁律——holdout.py 同样直接读 app_config）。"""
    try:
        return str(db.get_config("research.holdout_start", "2025-01-01"))
    except Exception:
        return "2025-01-01"


def _build_tradability_cards(frame) -> dict[tuple[date, str], TradabilityCard]:
    cards: dict[tuple[date, str], TradabilityCard] = {}
    if frame is None or frame.empty:
        return cards
    for r in frame.itertuples():
        cards[(r.date, r.symbol)] = TradabilityCard(
            suspended=bool(r.suspended),
            is_limit_up=bool(r.is_limit_up),
            is_limit_down=bool(r.is_limit_down),
        )
    return cards


def _precompute_atr(panel, periods: set[int]) -> dict[int, np.ndarray]:
    """ATR 面板（T,N）：逐标的因果 rolling（含当根口径即当日行含当日 bar）。

    停牌缺口口径：close 先列内前向填充再算 TR——
    复牌日的 TR 用最近可得前收计跳空波幅（否则 close.shift 在 NaN 行后
    为 NaN，TR 退化为 high−low，跨停牌缺口被系统性低估）。连续序列
    （golden/parity 用例）ffill 前后逐值相同。"""
    out: dict[int, np.ndarray] = {p: np.full(panel.shape, np.nan) for p in periods}
    close = panel.data["close"]
    for col, symbol in enumerate(panel.symbols):
        closes = close[:, col]
        valid = np.isfinite(closes)
        if valid.sum() < 3:
            continue
        closes_ffill = pd.Series(closes).ffill().to_numpy(dtype=float)
        df = pd.DataFrame({
            "high": panel.data["high"][:, col],
            "low": panel.data["low"][:, col],
            "close": closes_ffill,
        })
        for p in periods:
            series = core_atr(df, period=p)
            # 无 bar 日不产 ATR 行（保持 NaN——rolling 窗口按有效行收缩）
            series = series.where(np.isfinite(panel.data["close"][:, col]))
            out[p][:, col] = series.to_numpy(dtype=float)
    return out


def _needed_atr_periods(config: StrategyConfig) -> set[int]:
    """从配置里收集 ATR 周期（position_risk 槽 + 任何声明 atr_period 的模块）。"""
    periods = {20}
    bindings = list(config.slots.values()) + list(config.gates)
    for binding in bindings:
        params = binding.params or {}
        if "atr_period" in params:
            periods.add(int(params["atr_period"]))
        for item in params.get("members") or []:  # 元模块成员
            if isinstance(item, dict) and "atr_period" in (item.get("params") or {}):
                periods.add(int(item["params"]["atr_period"]))
    return periods


def instantiate_modules(config: StrategyConfig, registry) -> dict[str, Any]:
    """七槽模块实例化（配置 → 对象）。空槽 → None（不产生交易）。"""
    modules: dict[str, Any] = {}
    for slot, binding in config.slots.items():
        if binding.module is None:
            modules[slot] = None
            continue
        spec = registry.require(binding.module, slot=slot)
        instance = spec.factory(binding.params)
        instance._registered_key = spec.key
        modules[slot] = instance
    modules["portfolio_risk"] = []
    for binding in config.gates:
        spec = registry.require(binding.module, slot="portfolio_risk")
        instance = spec.factory(binding.params)
        instance._registered_key = spec.key
        modules["portfolio_risk"].append((spec, instance))
    return modules


def heat_cap_of(config) -> float:
    """读 heat_cap 门的声明参数（`max_heat_pct`，默认 0.06）。

    R20A-F1：此处曾读 `params.get("cap")`——而 `cap` 不是该门的声明参数
    （hot_cap 的参数域见 `portfolio/slots/portfolio_risk.py` 的 schema），
    配置里写 `cap` 会被载入期拒绝，故该分支不可达、cap 恒为硬编码 6%，
    导致非默认 cap 的 run 拿到"与配置矛盾"的告警数字（实测 cap=12% 时
    仍写 6%、111/242 天与 5.35×，真值为 50/242 与 2.68×）。
    """
    caps = [
        float((getattr(g, "params", None) or {}).get("max_heat_pct", 0.06))
        for g in (getattr(config, "gates", ()) or ())
        if (getattr(g, "module", "") or "").startswith("heat_cap")
    ]
    # 多门时取**约束最紧**的那条（R21A-F3）：实际生效的是 min（准入时每个门都卡控），
    # 取第一个会把告警整条抑制（实测两门 0.25/0.06 时按 0.25 判 → 一条告警都没有，
    # 而真值 110/243 天越线）。
    return min(caps) if caps else 0.06


def heat_cap_ex_post_warning(nav_rows: list[dict], cap: float) -> str | None:
    """事后 heat 越线告警（纯函数，便于直接断言；R19B-F1）。

    `heat_cap` 只在**准入时刻**校验组合热；已持仓的 heat 随收盘价上涨自然增长
    （止损价固定、MVP 无部分卖出）→ 日结 heat 可长期高于 cap。实测（12 只真实
    标的 · cap 6% · 一年）264/365 天越线、峰值 4.15×cap，而旧实现无任何留痕。
    返回 None 表示未越线。
    """
    comparable = [
        r for r in nav_rows
        if r.get("heat") is not None and r.get("equity") and float(r["equity"]) > 0
    ]
    over = [
        float(r["heat"]) / float(r["equity"])
        for r in comparable
        if float(r["heat"]) > float(r["equity"]) * float(cap)
    ]
    if not over:
        return None
    peak = max(over)
    return (
        f"heat_cap_exceeded(事后口径): {len(over)}/{len(comparable)} 个可比日结 heat 超"
        f"cap={float(cap):.2%}，峰值 {peak:.2%}（{peak / float(cap):.2f}×cap）——"
        "cap 仅在准入时刻卡控，已持仓 heat 随价格增长不受约束；"
        "不要把该 run 的敞口读作受 cap 约束"
    )


def run_backtest(
    db,
    *,
    config: StrategyConfig,
    registry,
    start: date,
    end: date,
    initial_capital: float = 1_000_000.0,
    market_profile: str = "cn_stock",
    strategy_ref: str = "",
    run_params: dict | None = None,
    window_kind: str = "sample",
    run_id: str | None = None,
    store: bool = True,
) -> dict:
    """跑一次组合回测。返回 {run_id, nav, trades, unfilled, gate_log, ...}。

    数据面：一次性经 L1.5 取全窗口面板 + 可交易性（as_of = end 收盘，
    血缘锚定 data_version），逐日以 PanelView 限窗转供模块，且受限句柄
    逐日重绑定 as_of=当日收盘（PIT 双保险：面板限窗 + 句柄当日锚定）。
    运行期间冻结数据写入（决策 A3）；崩溃时 run 落 failed（不留 running
    孤儿行）。
    """
    from portfolio import slots  # noqa: F401  确保内置模块已注册

    run_id = run_id or new_run_id()
    gateway = Gateway(db)

    load_start = (pd.Timestamp(start) - pd.Timedelta(days=LOOKBACK_PAD_DAYS)).date()
    as_of = datetime.combine(end, time(15, 0))

    universe_symbols = _universe_symbols(gateway.metadata, config, registry)
    if not universe_symbols:
        # 全 none 的配置（如种子里的 blank-base@1）此前会一路走到
        # 面板层，以 `PanelRequestError("symbols must be non-empty")` 顶层报错
        # ——不是领域错误、无 run 行、原因不可读。这里显式给出可行动的报错。
        raise BacktestError(
            "universe slot is empty (no symbols): 该配置没有标的池，"
            "无法回测——请为 universe 槽绑定 static_list/category_filter/"
            "liquidity_filter 等模块"
        )
    with run_freeze.frozen_writes():
        panel = gateway.get_panel(
            symbols=universe_symbols,
            start=load_start, end=end,
            fields=["open", "high", "low", "close", "volume", "amount"],
            adjust="qfq", as_of=as_of, mode="historical",
            caller_layer="portfolio", run_id=run_id,
        )
        if not panel.dates:
            raise BacktestError(f"no market data in window [{start}, {end}]")
        run_dates = [d for d in panel.dates if d >= start]
        tradability = gateway.get_tradability(
            symbols=list(panel.symbols), dates=run_dates,
            as_of=as_of, caller_layer="portfolio", run_id=run_id,
        )
        cards = _build_tradability_cards(tradability)
        data_version = gateway.data_version("qfq")

        modules = instantiate_modules(config, registry)

        # 组合告警（DS-R2 P2）：heat_cap × 按设计无止损价的持仓风控模块——
        # heat_cap 将退化为"不卡控"，运行级警告随结果与 verdict 聚合。
        # 元模块（any_of）成员同样下钻——只查顶层名字会漏掉
        # any_of[time_stop,...] 这类组合。
        run_warnings: list[str] = []
        prisk_binding = config.slots.get("position_risk")
        prisk_names: list[str] = []
        if prisk_binding is not None and prisk_binding.module:
            prisk_names.append(prisk_binding.module)
            for member in (prisk_binding.params or {}).get("members") or []:
                m = member.get("module") if isinstance(member, dict) else member
                if m:
                    prisk_names.append(f"{m} (any_of member)")
        uses_heat_cap = any(
            (g.module or "").startswith("heat_cap") for g in config.gates
        )
        _stop_less = ("time_stop", "breakeven", "none")
        for name in prisk_names:
            if uses_heat_cap and str(name).split("@")[0] in _stop_less:
                # 措辞与实现严格对齐——该模块的
                # estimate_stop 恒为 None，故**候选自身没有止损估计**、
                # 增量的组合热算不出来；卡控对这些候选不生效但持仓照常建立
                # （heat_cap.admit 的放行分支 + gate_log 逐候选留痕）。
                # 旧措辞说"组合热不可知"，而空仓时 heat 已知为 0，
                # 与实现的 `continue`（拒绝全部）一起构成双重失真。
                run_warnings.append(
                    f"heat_cap×{name}: 该持仓风控模块不提供止损价（estimate_stop=None），"
                    "新增仓位的风险增量不可算，heat_cap 对该 run 不生效（放行并落 "
                    "gate_log）——不是零成交，也不要把成交结果当作受 cap 约束的产物"
                )

        store_obj = EngineStore(db, run_id) if store else None
        if store_obj:
            # 决策 C3：非实验路径触碰 holdout 不拦截但必留痕——
            # 触碰标记显式进 run_params_json（不依赖从 window 间接推断）
            store_obj.begin_run(
                kind="backtest", strategy_ref=strategy_ref or config.name,
                config_hash=config.config_hash(),
                resolved_config_yaml=config.canonical_yaml(),
                run_params={
                    **(run_params or {}),
                    "initial_capital": initial_capital,
                    "market_profile": market_profile,
                    "window_kind": window_kind,
                    "window": [start.isoformat(), end.isoformat()],
                    "holdout_touched": str(end)[:10] >= _holdout_start(db),
                },
                data_version=data_version,
            )
        engine = Engine(profile=market_profile, initial_cash=initial_capital, store=store_obj)

        # 装配段（prepare/元数据/起算日）：失败同样落 failed（B-R2-P2）
        try:
            atr_periods = _needed_atr_periods(config)
            atr_panels = _precompute_atr(panel, atr_periods)

            signal_mod = modules.get("signal")
            if signal_mod is not None and hasattr(signal_mod, "prepare"):
                signal_mod.prepare(panel)
            # trend_score_cross 等需要面板外数据的模块：经受限句柄在 run 级
            # as_of 下取生产指标（模块实例级缓存，日内不重取；评审 A-P1-4 接线）
            bound_run = gateway.bind(as_of=as_of, caller_layer="portfolio", run_id=run_id)
            if signal_mod is not None and hasattr(signal_mod, "prepare_with_gateway"):
                signal_mod.prepare_with_gateway(bound_run, list(panel.symbols), load_start)

            prisk_mod = modules.get("position_risk")
            exec_mod = modules.get("execution")
            fill_policy = (
                exec_mod.fill_policy()
                if exec_mod
                else {"slippage_base": 0.002, "slippage_tail": 0.001}
            )

            meta_map = gateway.metadata.instruments(list(panel.symbols))
            start_idx = panel.date_pos(start)
            if start_idx is None:
                # start 落在非交易日：取其后第一个有数据的交易日
                later = [i for i, d in enumerate(panel.dates) if d >= start]
                start_idx = later[0] if later else None
            if start_idx is None:
                raise BacktestError(f"no trading day >= {start} in panel")

            nav_rows: list[dict] = []
            trades: list[dict] = []
            unfilled_log: list[dict] = []
            gate_log: list[dict] = []
            ffilled_close = panel.data["close"].copy()  # 前向填充供停牌标的估值
            _ffill_inplace(ffilled_close)
        except Exception as exc:
            if store_obj:
                store_obj.finish_run(status="failed", error=str(exc))
            gateway.flush_audit()
            raise

        try:
            for t_idx in range(start_idx, len(panel.dates)):
                day = panel.dates[t_idx]
                day_as_of = datetime.combine(day, time(15, 0))
                # PIT 逐日绑定：受限句柄 as_of = 当日收盘（不是 run 末日）——
                # 模块经 ctx.gateway 也物理上拿不到未来数据（评审 A-P1-1）。
                bound = gateway.bind(as_of=day_as_of, caller_layer="portfolio", run_id=run_id)
                panel_view = PanelView(panel, t_idx)
                engine.begin_day(day=day)
                close_today = {
                    s: float(ffilled_close[t_idx, j])
                    for j, s in enumerate(panel.symbols)
                    if np.isfinite(ffilled_close[t_idx, j])
                }
                ctx = DayContext(
                    date=day, as_of=day_as_of, gateway=bound, panel=panel_view,
                    account=AccountView(engine.account, close_today),
                    data_version=data_version,
                    history=nav_rows,
                    gate_log=gate_log,
                    extras={"atr": dict(atr_panels)},
                )

                # 共享决策路径（与实盘运行器同一条代码路径，详设 §5.7）：
                # 阶段 A 卖出意图 → 引擎执行 → 阶段 B 买入意图 → 引擎执行。
                ctx.params["_meta_map"] = meta_map
                stop_intents, signal_exits, rotation_intents, events = evaluate_exits(
                    ctx, modules=modules, panel=panel, t_idx=t_idx
                )

                exited_today: set[str] = set()
                for intent in [*stop_intents, *signal_exits, *rotation_intents]:
                    symbol = intent.symbol
                    if symbol in exited_today or symbol not in engine.account.positions:
                        continue
                    result = engine.sell(
                        intent=intent, day=day,
                        card=cards.get((day, symbol), TradabilityCard()),
                        asset_type=_asset_type(meta_map, symbol),
                        bar_close=_field_at(panel, t_idx, symbol, "close"),
                        bar_open=_field_at(panel, t_idx, symbol, "open"),
                        bar_low=_field_at(panel, t_idx, symbol, "low"),
                        slippage_base=fill_policy["slippage_base"],
                        slippage_tail=fill_policy["slippage_tail"],
                        intent_snapshot={"reason": intent.reason},
                    )
                    _record_result(result, trades, unfilled_log, day, "sell")
                    if result.status == "filled":
                        exited_today.add(symbol)

                _ranked, admitted = select_entries(
                    ctx, modules=modules, events=events, exited_symbols=exited_today
                )
                for intent in admitted:

                    def _on_filled(fill, _ctx=ctx, _prisk=prisk_mod):
                        if _prisk is None:
                            return None, ""
                        return _prisk.init_stop(_ctx, fill), _module_ref(_prisk)

                    result = engine.buy(
                        intent=intent, day=day,
                        bar_close=_field_at(panel, t_idx, intent.symbol, "close") or 0.0,
                        card=cards.get((day, intent.symbol), TradabilityCard()),
                        asset_type=_asset_type(meta_map, intent.symbol),
                        slippage_base=fill_policy["slippage_base"],
                        slippage_tail=fill_policy["slippage_tail"],
                        on_filled=_on_filled,
                        intent_snapshot={"source": intent.source},
                    )
                    _record_result(result, trades, unfilled_log, day, "buy")

                # 5. 日结
                nav_rows.append(engine.settle(day=day, close_prices=close_today))
        except Exception as exc:
            # 崩溃不留 running 孤儿行（评审 B-P1-2）
            if store_obj:
                store_obj.finish_run(status="failed", error=str(exc))
            gateway.flush_audit()
            raise

        if store_obj:
            store_obj.finish_run(status="finished")
        gateway.flush_audit()

    round_trips = _round_trips_enriched(trades, panel)

    # heat_cap 的**事后**口径如实可见（R19B-F1）：cap 只在准入时刻卡控，已持仓的
    # heat 会随收盘价上涨自然增长（stop 固定，MVP 无部分卖出）→ 日结 heat 可长期
    # 高于 cap，而旧实现无任何留痕。实测（12 只真实标的 · heat_cap 6% · 一年）
    # 264/365 天越线、峰值 4.15×cap。这里只**告警**不改行为（硬上限需要部分卖出）。
    if uses_heat_cap:
        _cap = heat_cap_of(config)
        _msg = heat_cap_ex_post_warning(nav_rows, _cap)
        if _msg:
            run_warnings.append(_msg)

    return {
        "run_id": run_id,
        "round_trips": round_trips,
        "strategy": config.name,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "data_version": data_version,
        "daily_nav": nav_rows,
        "trades": trades,
        # 去掉去重用的内部键：对外/落库的记录保持原字段形状
        "unfilled": [{k: v for k, v in u.items() if k != "_key"} for u in unfilled_log],
        "gate_log": gate_log,
        "warnings": run_warnings,
        "final_equity": nav_rows[-1]["equity"] if nav_rows else initial_capital,
    }


def _universe_symbols(metadata_svc, config: StrategyConfig, registry) -> list[str]:
    """回测窗口内可能进池的标的全集：静态清单直接取；类目/流动性过滤取
    当前 enabled 全池（时点动态池是数据线二期的事；幸存者偏差警告由
    verdict 流水线承担，详设 §6.6.3）。元数据经 L1.5 门面（评审 A-P1-2）。
    """
    binding = config.slots.get("universe")
    if binding is None or binding.module is None:
        return []
    if binding.module.startswith("static_list@"):
        return [str(s).strip().upper() for s in (binding.params.get("symbols") or [])]
    asset_type = str((binding.params or {}).get("asset_type") or "all")
    return metadata_svc.enabled_symbols(asset_type=asset_type)


def _ffill_inplace(matrix: np.ndarray) -> None:
    """列向前填充 NaN（停牌标的估值用最近可得价）。"""
    for j in range(matrix.shape[1]):
        col = matrix[:, j]
        mask = np.isfinite(col)
        if not mask.any():
            continue
        idx = np.where(mask, np.arange(len(col)), 0)
        np.maximum.accumulate(idx, out=idx)
        matrix[:, j] = col[idx]


def _field_at(panel, row: int, symbol: str, field: str) -> float | None:
    col = panel._symbol_index.get(symbol)
    if col is None:
        return None
    v = panel.data[field][row, col]
    return float(v) if np.isfinite(v) else None


def _asset_type(meta_map: dict, symbol: str) -> str:
    return str((meta_map.get(symbol) or {}).get("asset_type") or "stock").lower()


def _member(symbol: str, meta_map: dict):
    from portfolio.slots.universe import UniverseMember

    row = meta_map.get(symbol) or {}
    return UniverseMember(
        symbol=symbol,
        category_l1=str(row.get("category_l1") or ""),
        category_l2=str(row.get("category_l2") or ""),
        category_l3=str(row.get("category_l3") or ""),
        asset_type=str(row.get("asset_type") or ""),
    )


def _module_ref(module) -> str:
    return getattr(module, "_registered_key", module.__class__.__name__)


# ----------------------------------------------------------------------
# 共享决策路径（回测器与实盘运行器同一条代码路径，详设 §5.7）
# ----------------------------------------------------------------------


def evaluate_exits(ctx, *, modules, panel, t_idx):
    """阶段 A：持仓风控 + 信号退出 + 轮换 → 卖出意图清单（不执行）。

    返回 (stop_intents, signal_exit_intents, rotation_intents, events)。
    调用方负责执行（回测器：引擎撮合；实盘运行器：只出清单）。
    """
    universe_mod = modules.get("universe")
    signal_mod = modules.get("signal")
    prisk = modules.get("position_risk")
    exec_mod = modules.get("execution")

    stop_intents = []
    if prisk is not None:
        for symbol, position in list(ctx.account.positions.items()):
            intent = prisk.evaluate(ctx, position)
            if intent is not None:
                stop_intents.append(intent)

    if universe_mod is None or signal_mod is None:
        members = []
    else:
        members = universe_mod.members(ctx)
    ctx.params["_members_count"] = len(members)
    held = set(ctx.account.positions)
    member_symbols = {m.symbol for m in members}
    # `held - member_symbols` 是 set——迭代顺序随 PYTHONHASHSEED 变化，
    # 退出/成交记录的落库顺序因此不可复现（净值与数量不受影响）。排序固定。
    extra_held = [
        _member(symbol, ctx.params["_meta_map"])
        for symbol in sorted(held - member_symbols)
    ]
    events = signal_mod.scan(ctx, members + extra_held) if signal_mod else []

    signal_exits = [
        ExitOrderIntent(symbol=e.symbol, decision_date=ctx.date, fill_mode="tail",
                        source="signal", reason="signal_exit")
        for e in events if e.kind == "exit" and e.symbol in held
    ]
    rotation = []
    if exec_mod is not None:
        candidates = [e for e in events if e.kind == "entry" and e.symbol not in ctx.account.positions]
        rotation = list(exec_mod.rotation_policy(ctx, candidates, list(ctx.account.positions)) or [])
    return stop_intents, signal_exits, rotation, events


def select_entries(ctx, *, modules, events, exited_symbols):
    """阶段 B：候选竞争（rank → sizing → 组合风控 gates）→ 买入意图清单。

    调用方在"卖出已执行/已假定执行"的账户状态下传入刷新后的 ctx。
    """
    rank_mod = modules.get("rank")
    sizing_mod = modules.get("sizing")
    prisk = modules.get("position_risk")
    exec_mod = modules.get("execution")
    gates = modules.get("portfolio_risk") or []

    if exec_mod is not None and not exec_mod.allows_action(ctx):
        return [], []
    if rank_mod is None or sizing_mod is None:
        return [], []

    entries = [
        e for e in events
        if e.kind == "entry"
        and e.symbol not in ctx.account.positions
        and e.symbol not in exited_symbols
    ]
    ranked = rank_mod.rank(ctx, entries)
    est_stops: dict[str, float | None] = {}
    intents: list[OrderIntent] = []
    for candidate in ranked:
        stop_est = prisk.estimate_stop(ctx, candidate.symbol) if prisk else None
        est_stops[candidate.symbol] = stop_est
        intent = sizing_mod.size(ctx, candidate, stop_est)
        if intent is not None:
            intents.append(intent)
    ctx.params["_est_stops"] = est_stops
    pending_exits = list(exited_symbols)
    admitted = intents
    for _spec, gate in gates:
        admitted = gate.admit(ctx, admitted, pending_exits)
    return ranked, admitted


def _record_result(result, trades, unfilled_log, day, side) -> None:
    if result.status == "filled" and result.fill is not None:
        trades.append({
            "date": day, "side": side, "symbol": result.fill.symbol,
            "qty": result.fill.quantity, "price": result.fill.fill_price,
        })
    elif result.status == "unfilled" and result.unfilled is not None:
        # 同一 (日, 标的, 方向) 可能被两条独立路径各拒一次
        # （止损被阻塞 + 同日信号退出被阻塞），两条记录字段完全相同 →
        # unfilled_by_reason 重复计数。只保留首条。
        key = (day.isoformat(), result.unfilled.symbol, side)
        if any(u.get("_key") == key for u in unfilled_log):
            return
        unfilled_log.append({
            "date": key[0], "symbol": key[1],
            "reason": result.unfilled.reason, "side": side, "_key": key,
        })


def _round_trips_enriched(trades: list[dict], panel) -> list[dict]:
    """成交配对 → round trips（R 倍数/MAE/MFE，详设 §5.4.3 复用 RoundTrip 诊断）。

    R 倍数 = pnl ÷ (qty × 1.5×ATR(入场日))——止损距离的通用分母；
    MAE/MFE = 持仓期最低/最高价相对入场价。
    """
    from core.indicators import atr as core_atr

    open_lots: dict[str, dict] = {}
    out: list[dict] = []
    close = panel.data["close"]
    low = panel.data["low"]
    high = panel.data["high"]
    date_idx = {d: i for i, d in enumerate(panel.dates)}
    for t in trades:
        symbol = t["symbol"]
        col = panel._symbol_index.get(symbol)
        if col is None:
            continue
        if t["side"] == "buy":
            open_lots[symbol] = t
            continue
        entry = open_lots.pop(symbol, None)
        if entry is None:
            continue
        e_idx = date_idx.get(entry["date"])
        x_idx = date_idx.get(t["date"])
        if e_idx is None or x_idx is None:
            continue
        qty = int(entry["qty"])
        entry_price = float(entry["price"])
        exit_price = float(t["price"])
        pnl = qty * (exit_price - entry_price)
        atr_e = 0.0
        # close 先 ffill（与 _precompute_atr 同口径——跨停牌入场
        # 窗时 TR 含复牌跳空，r_multiple 分母不再被低估）
        closes_ffill = pd.Series(close[: e_idx + 1, col]).ffill().to_numpy(dtype=float)
        df = pd.DataFrame({"high": high[: e_idx + 1, col], "low": low[: e_idx + 1, col],
                           "close": closes_ffill})
        a = core_atr(df, 20)
        if len(a) and np.isfinite(a.iloc[-1]):
            atr_e = float(a.iloc[-1])
        window_low = low[e_idx: x_idx + 1, col]
        window_high = high[e_idx: x_idx + 1, col]
        mae = float(np.nanmin(window_low) / entry_price - 1.0) if np.isfinite(window_low).any() else None
        mfe = float(np.nanmax(window_high) / entry_price - 1.0) if np.isfinite(window_high).any() else None
        # 通用分母 = 1.5×ATR（与持仓实际配置的止损倍数无关——首个课题
        # "止损选型"下各臂的 R 因此不可比）。字段名带分母，避免跨策略比较时
        # 被误读为"按各自止损距离的 R"（口径变更属运行期决定）。
        risk = qty * 1.5 * atr_e
        out.append({
            "symbol": symbol,
            "entry_date": entry["date"].isoformat(),
            "exit_date": t["date"].isoformat(),
            "entry_price": entry_price,
            "exit_price": exit_price,
            "qty": qty,
            "pnl": pnl,
            "r_multiple": (pnl / risk) if risk > 0 else None,
            "r_multiple_denominator": "1.5xATR20",
            "mae_pct": mae * 100 if mae is not None else None,
            "mfe_pct": mfe * 100 if mfe is not None else None,
            "holding_days": x_idx - e_idx,
        })
    return out
