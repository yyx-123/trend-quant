"""实盘运行器（薄版，决策 3；详设 §5.7）。

**与组合回测器的关系：同一个策略对象、同一条决策代码路径**
（evaluate_exits / select_entries 共享）。差异只有四点：定时触发
（交易日 14:00）、live 模式取数（含盘中合成 bar）、只出清单不撮合、
账户状态用 manual_trades 真实记账重建。

它不是执行系统（不碰券商接口）——出清单，人执行，系统对账。执行差
（实际成交价 vs 清单基准价、未执行）反过来标定引擎的 slippage_tail
参数（架构稿 §3.2）。

现金重建口径（记账近似，已记开发日志）：cash = initial_capital − Σ 未平仓
买入成本 + Σ 已平仓卖出净额；不含出入金与实盘费用微差——对账模块每日
校准的是"执行差"，账户绝对水位以券商端为准。
"""

from __future__ import annotations

import json
from datetime import date, datetime

import numpy as np
import pandas as pd

from engine.models import Account, Position
from gateway.service import Gateway
from portfolio import library
from portfolio.backtester import (
    _build_tradability_cards,
    _needed_atr_periods,
    _precompute_atr,
    evaluate_exits,
    instantiate_modules,
    select_entries,
)
from portfolio.context import AccountView, DayContext, PanelView
from portfolio.strategy import parse_strategy_yaml

from audit.app_logger import get_logger

logger = get_logger(__name__)

LIVE_AS_OF_HOUR = 14  # 每日 14:00 前后产出清单（用户实盘习惯，架构稿 §3.2）


def rebuild_account_from_manual_trades(
    db, *, user_id: int, initial_capital: float, panel, position_risk_module, ctx_builder=None
) -> Account:
    """manual_trades → Account（真实记账重建）。

    持仓止损状态按模块公式用面板数据重建（highest_since_buy 取买入以来
    最高、ATR 取买入日含当根——与回测口径一致的可重建部分全部重建）。
    """
    trades = db.list_manual_trades(user_id)
    open_trades = [t for t in trades if t["status"] == "open"]
    closed = [t for t in trades if t["status"] == "closed"]

    cash = float(initial_capital)
    cash -= sum(float(t["buy_price"]) * float(t["shares"]) for t in open_trades)
    for t in closed:
        cash += float(t["sell_price"] or 0.0) * float(t["shares"])  # 卖出总额
        cash -= float(t["buy_price"]) * float(t["shares"])           # 对应买入成本

    account = Account(cash=cash)
    for t in open_trades:
        symbol = str(t["symbol"]).upper()
        qty = int(float(t["shares"]))
        buy_date = pd.Timestamp(t["buy_date"]).date()
        entry_price = float(t["buy_price"])
        stop_state = None
        if position_risk_module is not None:
            stop_state = _rebuild_stop_state(
                position_risk_module, panel, symbol, buy_date, entry_price
            )
        # T+1（评审 DS-P2-6）：buy_date == 当日 → sellable=0（当日买入不可卖，
        # 否则止损评估会把当日买入列进"应卖"——与 §4.2/§5.7 同路径不一致）
        sellable = 0 if buy_date >= panel.dates[-1] else qty
        account.positions[symbol] = Position(
            symbol=symbol, quantity=qty, sellable_quantity=sellable,
            avg_cost=entry_price, entry_date=buy_date, entry_price=entry_price,
            stop=stop_state,
            position_risk_ref=getattr(position_risk_module, "_registered_key", ""),
        )
    return account


def _rebuild_stop_state(module, panel, symbol: str, buy_date: date, entry_price: float):
    """用面板数据重建止损状态（实盘持仓的入场信息来自 manual_trades）。

    T-1 日内路径口径（GLM53F-P1-3）：live 面板末根是当日 14:00 provisional
    bar——当日盘中虚高不得参与 highest/止损价计算（回测侧 evaluate 用 T-1
    状态判定，§5.7"同一策略对象、同一条决策代码路径"）。故状态重建截至
    T-1：末根为 provisional 时排除；post-close 重跑（末根 = 最近真实收盘）
    不排除。棘轮止损按持仓期逐日累计 max 重建（只上移语义无法由端点值推出）。
    """
    from engine.models import StopState

    col = panel._symbol_index.get(symbol)
    if col is None:
        return None
    idx = panel.date_pos(buy_date)
    upto = len(panel.dates) - 1
    if idx is None:
        return None
    provisional = getattr(panel, "provisional", None)
    last_is_provisional = bool(provisional is not None and provisional[upto, col])
    state_end = upto - 1 if last_is_provisional else upto
    state_end = max(state_end, idx)  # 当日买入（T+1 不可卖，状态仅需自洽）：用入场日
    highs = panel.data["high"][idx: state_end + 1, col]
    closes = panel.data["close"][idx: state_end + 1, col]
    highest = float(np.nanmax(highs)) if len(highs) and np.isfinite(highs).any() else entry_price
    key = getattr(module, "_registered_key", "")

    from core.indicators import atr as core_atr

    period = int(getattr(module, "atr_period", 20) or 20)

    def _atr_through(row_end: int) -> float:
        df = pd.DataFrame({
            "high": panel.data["high"][: row_end + 1, col],
            "low": panel.data["low"][: row_end + 1, col],
            "close": panel.data["close"][: row_end + 1, col],
        })
        atr_series = core_atr(df, period)
        if len(atr_series):
            v = float(atr_series.iloc[-1])
            if np.isfinite(v):
                return v
        return 0.0

    atr_entry = _atr_through(idx)   # 入场日含当根（决策 6：hard_stop/breakeven 口径）
    atr_t1 = _atr_through(state_end)  # T-1（吊灯/棘轮逐日判定口径）

    if key.startswith("hard_stop"):
        mul = float(module.atr_mul)
        return StopState(stop_price=entry_price - mul * atr_entry if atr_entry > 0 else None,
                         highest_since_buy=highest, atr_at_entry=atr_entry)
    if key.startswith(("chandelier", "ratchet")):
        mul = float(module.atr_mul)
        stop = None
        if key.startswith("ratchet"):
            # 棘轮只上移：逐日候选（highest(d) − mul×ATR(d)）的累计 max
            running_high = 0.0
            for d in range(idx, state_end + 1):
                h = panel.data["high"][d, col]
                if np.isfinite(h):
                    running_high = max(running_high, float(h))
                atr_d = _atr_through(d)
                if atr_d > 0 and running_high > 0:
                    cand = running_high - mul * atr_d
                    stop = cand if stop is None else max(stop, cand)
        else:
            stop = highest - mul * atr_t1 if atr_t1 > 0 else None
        return StopState(stop_price=stop, highest_since_buy=highest, atr_at_entry=atr_entry,
                         module_state={"ratchet": key.startswith("ratchet")})
    if key.startswith("ma_stop"):
        n = int(module.n)
        valid = closes[np.isfinite(closes)]
        ma = float(valid[-n:].mean()) if len(valid) >= n else None
        return StopState(stop_price=ma, highest_since_buy=highest, atr_at_entry=atr_entry,
                         fill_mode="tail", heat_approximate=True)
    # 其余模块（none/time_stop/…）：保守重建公共字段
    return StopState(stop_price=None, highest_since_buy=highest, atr_at_entry=atr_entry,
                     fill_mode=getattr(module, "_fill_mode", "intraday_stop"))


def generate_daily_list(
    db,
    *,
    strategy_version_id: str,
    user_id: int,
    as_of: datetime | None = None,
    initial_capital: float = 1_000_000.0,
    live_overlay=None,
    registry=None,
) -> dict:
    """产出当日目标持仓 + 应买应卖清单（只出清单，不撮合）。"""
    from portfolio.slots import REGISTRY, ensure_builtins

    registry = registry or REGISTRY
    ensure_builtins()

    row = library.require_version(db, strategy_version_id)
    config = parse_strategy_yaml(row["config_yaml"], registry)

    as_of = as_of or datetime.now().replace(hour=LIVE_AS_OF_HOUR, minute=0, second=0)
    day = as_of.date()
    gateway = Gateway(db, live_overlay=live_overlay)

    # 先读持仓（manual_trades）再取面板：持仓标的必须并入取数集（B-P1-3）
    held_symbols = {
        str(t["symbol"]).upper()
        for t in db.list_manual_trades(user_id)
        if t["status"] == "open"
    }
    symbols = _live_universe_symbols(db, config, held_symbols)
    pad_start = (pd.Timestamp(day) - pd.Timedelta(days=400)).date()
    panel = gateway.get_panel(
        symbols=symbols, start=pad_start, end=day,
        fields=["open", "high", "low", "close", "volume", "amount"],
        adjust="qfq", as_of=as_of, mode="live",
        caller_layer="live", run_id=None,
    )
    if not panel.dates:
        raise RuntimeError(f"no market data up to {day}")
    # 数据新鲜度闸门（评审 DS-P2-5）：live 模式当日 bar 缺席（报价失败/非交易时段
    # 重跑）时，t_idx 指向昨日——清单会静默基于昨日收盘生成。交易时段内直接拒绝；
    # 盘后可接受（昨收即最新），清单带 caveat 明示。
    from core.calendar import is_trading_day as _is_td

    freshness_caveats: list[str] = []
    if panel.dates[-1] < day:
        if _is_td(day) and as_of.hour < 15:
            raise RuntimeError(
                f"live panel stale: last bar {panel.dates[-1]} < {day} "
                f"(intraday quotes unavailable)"
            )
        freshness_caveats.append(
            f"面板末日 {panel.dates[-1]} 早于决策日 {day}——清单基于最近可得收盘"
        )
    t_idx = len(panel.dates) - 1

    modules = instantiate_modules(config, registry)
    signal_mod = modules.get("signal")
    if signal_mod is not None and hasattr(signal_mod, "prepare"):
        signal_mod.prepare(panel)
    if signal_mod is not None and hasattr(signal_mod, "prepare_with_gateway"):
        bound_run = gateway.bind(as_of=as_of, caller_layer="live")
        signal_mod.prepare_with_gateway(bound_run, list(panel.symbols), pad_start)
    prisk = modules.get("position_risk")
    account = rebuild_account_from_manual_trades(
        db, user_id=user_id, initial_capital=initial_capital,
        panel=panel, position_risk_module=prisk,
    )

    atr_panels = _precompute_atr(panel, _needed_atr_periods(config))
    close_today = {
        s: float(panel.data["close"][t_idx, j])
        for j, s in enumerate(panel.symbols)
        if np.isfinite(panel.data["close"][t_idx, j])
    }
    meta_map = gateway.metadata.instruments(list(panel.symbols))
    bound = gateway.bind(as_of=as_of, caller_layer="live")
    ctx = DayContext(
        date=day, as_of=as_of, gateway=bound, panel=PanelView(panel, t_idx),
        account=AccountView(account, close_today),
        data_version=gateway.data_version("qfq"),
        gate_log=[],
        extras={"atr": atr_panels},
    )
    ctx.params["_meta_map"] = meta_map

    stop_intents, signal_exits, rotation_intents, events = evaluate_exits(
        ctx, modules=modules, panel=panel, t_idx=t_idx
    )
    sells = [*stop_intents, *signal_exits, *rotation_intents]
    exited = {i.symbol for i in sells}

    # 假定卖出全部执行后的影子账户（清单口径：目标持仓 = 现有持仓 − 应卖 + 应买）
    shadow = Account(cash=account.cash)
    for symbol, pos in account.positions.items():
        if symbol in exited:
            price = close_today.get(symbol)
            shadow.cash += pos.quantity * (price if price else pos.avg_cost)
            continue
        shadow.positions[symbol] = pos
    shadow_view = AccountView(shadow, close_today)
    shadow_ctx = DayContext(
        date=day, as_of=as_of, gateway=bound, panel=PanelView(panel, t_idx),
        account=shadow_view, data_version=ctx.data_version,
        gate_log=ctx.gate_log, extras=ctx.extras,
    )
    shadow_ctx.params.update(ctx.params)

    _ranked, admitted = select_entries(
        shadow_ctx, modules=modules, events=events, exited_symbols=exited
    )

    fill_policy = (
        modules["execution"].fill_policy() if modules.get("execution")
        else {"slippage_base": 0.002, "slippage_tail": 0.001}
    )
    slip = fill_policy["slippage_base"] + fill_policy["slippage_tail"]

    cards = _build_tradability_cards(
        gateway.get_tradability(symbols=list(panel.symbols), dates=[panel.dates[t_idx]],
                                as_of=as_of, caller_layer="live")
    )
    sell_list = []
    for intent in sells:
        price = close_today.get(intent.symbol)
        pos = account.positions.get(intent.symbol)
        card = cards.get((panel.dates[t_idx], intent.symbol))
        sell_list.append({
            "symbol": intent.symbol,
            "qty": int(pos.quantity) if pos else None,
            "reason": intent.reason,
            "ref_price": price,
            "est_price": round(price * (1 - slip), 4) if price else None,
            "tradability": _card_dict(card),
        })
    buy_list = []
    for intent in admitted:
        price = close_today.get(intent.symbol)
        qty_est = (
            int(intent.value) if intent.intent_type == "quantity"
            else int(intent.value / price // 100 * 100) if price else 0
        )
        if qty_est <= 0:
            continue
        card = cards.get((panel.dates[t_idx], intent.symbol))
        buy_list.append({
            "symbol": intent.symbol,
            "qty": qty_est,
            "ref_price": price,
            "est_price": round(price * (1 + slip), 4) if price else None,
            "est_stop": (shadow_ctx.params.get("_est_stops") or {}).get(intent.symbol),
            "tradability": _card_dict(card),
        })

    target = {
        "as_of": as_of.isoformat(sep=" "),
        "strategy_version_id": strategy_version_id,
        "sells": sell_list,
        "buys": buy_list,
        "target_holdings": sorted(
            set(shadow.positions) | {b["symbol"] for b in buy_list}
        ),
        "gate_log": ctx.gate_log,
        "cash_est": round(shadow.cash, 2),
        "heat": None if shadow_view.heat() is None else round(shadow_view.heat(), 2),
        "caveats": [*freshness_caveats, *_live_caveats(config)],
    }
    # 实盘运行档案（§4.1 + 评审 DS-P3-4）：清单挂 run 级血缘锚点
    from engine.engine import new_run_id as _new_run_id
    from engine.store import EngineStore as _EngineStore

    live_run_id = _new_run_id("L")
    store = _EngineStore(db, live_run_id)
    store.begin_run(
        kind="live", strategy_ref=strategy_version_id,
        config_hash=config.config_hash(), resolved_config_yaml=config.canonical_yaml(),
        run_params={"list_date": day.isoformat(), "as_of": as_of.isoformat()},
        data_version=gateway.data_version("qfq"),
    )
    store.finish_run(status="finished")
    target["engine_run_id"] = live_run_id

    with db.connect() as conn:
        conn.execute(
            """INSERT INTO portfolio_live_lists
               (list_date, strategy_version_id, as_of, engine_run_id, target_json)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(list_date, strategy_version_id)
               DO UPDATE SET as_of = excluded.as_of, target_json = excluded.target_json,
                            engine_run_id = excluded.engine_run_id""",
            (day.isoformat(), strategy_version_id, as_of.isoformat(sep=" "),
             live_run_id, json.dumps(target, ensure_ascii=False, sort_keys=True)),
        )
    gateway.flush_audit()
    return target


def _live_universe_symbols(db, config, held_symbols: set[str] | None = None) -> list[str]:
    """实盘清单的取数标的集：universe 池 ∪ 当前持仓（持仓掉出 universe 也必须
    在面板里——否则止损评估静默失管，评审 B-P1-3）。元数据经 L1.5 门面。"""
    from gateway.metadata import MetadataService

    binding = config.slots.get("universe")
    if binding is not None and binding.module and binding.module.startswith("static_list@"):
        symbols = [str(s).strip().upper() for s in (binding.params.get("symbols") or [])]
    else:
        asset_type = str((binding.params or {}).get("asset_type") or "all") if binding else "all"
        symbols = MetadataService(db).enabled_symbols(asset_type=asset_type)
    return sorted(set(symbols) | {str(s).upper() for s in (held_symbols or set())})


def reconcile_daily_list(db, *, list_date: str, strategy_version_id: str, user_id: int) -> dict | None:
    """对账（详设 §5.7 第 4 步）：清单 vs manual_trades 实际执行。

    应买未买 / 应卖未卖 / 成交价 vs 收盘基准价偏差 → 执行差统计
    （标定引擎 slippage_tail 的样本来源）。
    """
    with db.connect() as conn:
        row = conn.execute(
            """SELECT * FROM portfolio_live_lists
               WHERE list_date = ? AND strategy_version_id = ?""",
            (list_date, strategy_version_id),
        ).fetchone()
    if row is None:
        return None
    target = json.loads(row["target_json"])

    trades = db.list_manual_trades(user_id)
    day_buys = [t for t in trades if str(t["buy_date"])[:10] == list_date]
    day_sells = [t for t in trades if t["status"] == "closed" and str(t.get("sell_date") or "")[:10] == list_date]

    buy_by_symbol = {str(t["symbol"]).upper(): t for t in day_buys}
    sell_by_symbol = {str(t["symbol"]).upper(): t for t in day_sells}

    missing_buys, missing_sells = [], []
    qty_mismatches: list[dict] = []
    price_diffs: list[dict] = []
    for item in target.get("buys", []):
        symbol = item["symbol"]
        actual = buy_by_symbol.get(symbol)
        if actual is None:
            missing_buys.append(symbol)
            continue
        if item.get("qty") and int(float(actual["shares"])) != int(item["qty"]):
            qty_mismatches.append({"symbol": symbol, "side": "buy",
                                   "target_qty": int(item["qty"]),
                                   "actual_qty": int(float(actual["shares"]))})
        # GLM53F-P2-18：对账标定对象是"实际成交价 vs **收盘基准价**"
        # （架构稿 §2.1/详设 §5.7——这是标定 slippage_tail 的样本）；
        # 用 est_price（收盘×(1±模型滑点)）当分母度量的是"实际 vs 模型预估"
        # 的残差，滑点参数自身的偏差会被吸掉
        bench = item.get("ref_price")
        if bench and bench > 0:
            diff = float(actual["buy_price"]) / bench - 1.0
            price_diffs.append({"symbol": symbol, "side": "buy", "bench_price": bench,
                                "actual_price": float(actual["buy_price"]),
                                "diff_pct": round(diff, 5)})
    for item in target.get("sells", []):
        symbol = item["symbol"]
        actual = sell_by_symbol.get(symbol)
        if actual is None:
            missing_sells.append(symbol)
            continue
        if item.get("qty") and int(float(actual["shares"])) != int(item["qty"]):
            qty_mismatches.append({"symbol": symbol, "side": "sell",
                                   "target_qty": int(item["qty"]),
                                   "actual_qty": int(float(actual["shares"]))})
        bench = item.get("ref_price")  # 收盘基准价（同上，GLM53F-P2-18）
        if bench and bench > 0 and actual.get("sell_price"):
            diff = float(actual["sell_price"]) / bench - 1.0
            price_diffs.append({"symbol": symbol, "side": "sell", "bench_price": bench,
                                "actual_price": float(actual["sell_price"]),
                                "diff_pct": round(diff, 5)})

    result = {
        "list_date": list_date,
        "missing_buys": missing_buys,
        "missing_sells": missing_sells,
        "qty_mismatches": qty_mismatches,
        "price_diffs": price_diffs,
        "mean_abs_diff_pct": (
            round(float(np.mean([abs(d["diff_pct"]) for d in price_diffs])), 5)
            if price_diffs else None
        ),
    }
    with db.connect() as conn:
        conn.execute(
            """UPDATE portfolio_live_lists
               SET reconcile_json = ?, reconciled_at = datetime('now','localtime')
               WHERE list_date = ? AND strategy_version_id = ?""",
            (json.dumps(result, ensure_ascii=False, sort_keys=True), list_date, strategy_version_id),
        )
    return result



def _live_caveats(config) -> list[str]:
    """清单口径注记：依赖净值历史的组合风控门（vol_target/drawdown_throttle）
    在实盘 MVP 无净值序列可循，静默失效——清单上必须显式标注。"""
    caveats: list[str] = []
    for gate in config.gates:
        if gate.module and gate.module.startswith(("vol_target@", "drawdown_throttle@")):
            caveats.append(
                f"{gate.module} 需要组合净值历史，实盘清单 MVP 不驱动它（仅回测生效）"
            )
    return caveats


def _card_dict(card) -> dict | None:
    if card is None:
        return None
    return {
        "suspended": bool(card.suspended),
        "is_limit_up": bool(card.is_limit_up),
        "is_limit_down": bool(card.is_limit_down),
    }
