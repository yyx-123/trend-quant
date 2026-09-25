"""撮合语义（详设 §4.2，尾盘执行模型，决策 1）。

首版只实现 `tail_market`（尾盘市价等价物）与止损单；`order_type` 字段
预留，加限价单时扩展本模块即可。

买入生命周期（决策日 t 生成）：
1. 卡控（可交易性标注）：suspended → unfilled(suspended)；
   is_limit_up → unfilled(limit_up)（尾盘封板 ≈ 买不到）；
2. 定量：整手对齐（卖出全清例外）；现金校验（含预估费用）不足 →
   逐手递减，递减到 0 → unfilled(insufficient_cash / lot_rounding)；
3. 成交：fill_price = close(t) × (1 + 基础滑点 + 尾盘滑点)（买正卖负）；
4. T+1：当日买入计入 quantity、不计入 sellable_quantity。

止损触发（盘中语义；条件单成交可行性 2026-09-23 用户确认）：
- 跳空穿透：open(t) < stop_price → 按 open(t) 成交（stop_gap_fill 语义）；
- 盘中触及：low(t) ≤ stop_price → 按 stop_price 成交；
- 成交阻塞记 unfilled 并次日按同口径重试（止损价由持仓风控模块按最新
  数据重算——回测器每日重评估天然承担重试）：跌停 limit_down /
  T+1 t1_block / 停牌 suspended。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from engine import fees
from engine.models import Account, Fill, Position, Unfilled
from engine.profiles import MarketProfile, min_buy_qty


@dataclass(frozen=True, slots=True)
class TradabilityCard:
    """某标的当日的可交易性（由 L1.5 标注转供）。"""

    suspended: bool = False
    is_limit_up: bool = False
    is_limit_down: bool = False


@dataclass(slots=True)
class MatchResult:
    status: Literal["filled", "unfilled", "not_triggered"]
    fill: Fill | None = None
    unfilled: Unfilled | None = None
    quantity_change: int = 0  # 成交引起的持仓变化（买正卖负）
    cash_delta: float = 0.0


def match_buy(
    *,
    order_id: str,
    symbol: str,
    day,
    card: TradabilityCard,
    bar_close: float,
    intent_type: str,
    intent_value: float,
    account: Account,
    profile: MarketProfile,
    asset_type: str,
    slippage_base: float,
    slippage_tail: float,
    intent_snapshot: dict | None = None,
) -> MatchResult:
    """尾盘买入撮合。intent: quantity（定量股数）| target_value（目标金额）。"""
    snapshot = dict(intent_snapshot or {})
    snapshot.setdefault("intent_type", intent_type)
    snapshot.setdefault("intent_value", intent_value)

    if card.suspended:
        return _unfilled(order_id, symbol, day, "suspended", snapshot)
    if card.is_limit_up:
        return _unfilled(order_id, symbol, day, "limit_up", snapshot)
    if bar_close <= 0:
        return _unfilled(order_id, symbol, day, "suspended", snapshot)

    lot = max(int(profile.lot_size), 1)
    # 分品种最小申报数量（R16-D-1）：科创板股票单笔不小于 200 股——低于该数的
    # 委托在现实中必被拒单，不得记账成交（旧实现按 lot=100 对齐 → 会产出 100 股
    # 的科创板"成交"）。注意不能把数量自动抬到 200：target_value 语义下那会超预算。
    min_qty = min_buy_qty(symbol, asset_type=asset_type, lot_size=lot)
    exec_price_est = bar_close * (1.0 + slippage_base + slippage_tail)

    if intent_type == "quantity":
        intended = int(intent_value)
        if intended < lot:
            return _unfilled(order_id, symbol, day, "lot_rounding", snapshot)
        qty = (intended // lot) * lot
        if qty <= 0:
            return _unfilled(order_id, symbol, day, "lot_rounding", snapshot)
        # 整手对齐后仍低于该品种最小申报数量（科创板 200）→ 现实中不可下
        # （保留 `lot_rounding` 给"不足一手"的原语义）
        if qty < min_qty:
            return _unfilled(order_id, symbol, day, "below_min_order", snapshot)
        # 现金校验（含预估费用）不足 → 逐手递减（解析式直达可负担上限，
        # 语义与逐手递减完全一致：max_affordable_quantity 内部同口径校验）
        affordable = fees.max_affordable_quantity(
            cash=account.cash, exec_price_est=exec_price_est, profile=profile
        )
        qty = min(qty, affordable)
        if qty <= 0:
            return _unfilled(order_id, symbol, day, "insufficient_cash", snapshot)
        # 现金递减后可能又落到最小申报数量之下（如科创板现金只够 199 股 →
        # 递减到 100 股）：同样不可下，不得记账成交（R17A 预警的漏点，本轮自查修掉）
        if qty < min_qty:
            return _unfilled(order_id, symbol, day, "below_min_order", snapshot)
    elif intent_type == "target_value":
        # target_weight 由 L3 sizing 槽换算为金额后再下达（引擎不估组合净值）。
        budget = float(intent_value)
        if budget <= 0:
            return _unfilled(order_id, symbol, day, "insufficient_cash", snapshot)
        qty = int(budget // exec_price_est // lot) * lot
        affordable = fees.max_affordable_quantity(
            cash=account.cash, exec_price_est=exec_price_est, profile=profile
        )
        qty = min(qty, affordable)
        if qty <= 0:
            reason = (
                "lot_rounding"
                if budget < exec_price_est * lot
                else "insufficient_cash"
            )
            return _unfilled(order_id, symbol, day, reason, snapshot)
        if qty < min_qty:
            # 预算只够最小申报数量以下（科创板 200 股）→ 该单在现实中不可下
            return _unfilled(order_id, symbol, day, "below_min_order", snapshot)
    else:
        raise ValueError(f"unknown intent_type: {intent_type}")

    cost = fees.buy_cost(
        base_price=bar_close, slippage_base=slippage_base,
        slippage_tail=slippage_tail, quantity=qty,
        profile=profile, asset_type=asset_type,
    )
    cash_after = account.cash - cost["total_cost"]
    fill = Fill(
        order_id=order_id, symbol=symbol, fill_date=day,
        base_price=bar_close, slippage_base=slippage_base,
        slippage_tail=slippage_tail, fill_price=cost["fill_price"],
        quantity=qty, commission=cost["commission"], stamp_tax=cost["stamp_tax"],
        fee_total=cost["fee_total"], cash_after=cash_after,
    )
    return MatchResult(
        status="filled", fill=fill, quantity_change=qty, cash_delta=-cost["total_cost"]
    )


def match_tail_sell(
    *,
    order_id: str,
    symbol: str,
    day,
    card: TradabilityCard,
    bar_close: float,
    position: Position,
    account: Account,
    profile: MarketProfile,
    asset_type: str,
    slippage_base: float,
    slippage_tail: float,
    intent_snapshot: dict | None = None,
) -> MatchResult:
    """尾盘卖出撮合（信号退出/收盘确认型离场；整仓全清、无部分卖出）。"""
    snapshot = dict(intent_snapshot or {})
    if card.suspended:
        return _unfilled(order_id, symbol, day, "suspended", snapshot)
    if card.is_limit_down:
        return _unfilled(order_id, symbol, day, "limit_down", snapshot)
    if position.sellable_quantity <= 0:
        return _unfilled(order_id, symbol, day, "t1_block", snapshot)
    qty = position.sellable_quantity  # 整仓全清
    proceeds = fees.sell_proceeds(
        reference_price=bar_close, slippage_base=slippage_base,
        slippage_tail=slippage_tail, quantity=qty,
        profile=profile, asset_type=asset_type, apply_slippage=True,
    )
    cash_after = account.cash + proceeds["net_proceeds"]
    fill = Fill(
        order_id=order_id, symbol=symbol, fill_date=day,
        base_price=bar_close, slippage_base=slippage_base,
        slippage_tail=slippage_tail, fill_price=proceeds["fill_price"],
        quantity=qty, commission=proceeds["commission"],
        stamp_tax=proceeds["stamp_tax"], fee_total=proceeds["fee_total"],
        cash_after=cash_after,
    )
    return MatchResult(
        status="filled", fill=fill, quantity_change=-qty, cash_delta=proceeds["net_proceeds"]
    )


def match_intraday_stop(
    *,
    order_id: str,
    symbol: str,
    day,
    card: TradabilityCard,
    bar_open: float | None,
    bar_low: float | None,
    stop_price: float,
    position: Position,
    account: Account,
    profile: MarketProfile,
    asset_type: str,
    intent_snapshot: dict | None = None,
) -> MatchResult:
    """止损单盘中撮合：先判触发，再判阻塞。

    未触发 → status="not_triggered"（不落 unfilled——没发生的交易不是
    "错过的交易"）；触发但被阻塞 → unfilled（次日重试由回测器每日重评估承担）。
    """
    snapshot = dict(intent_snapshot or {})
    snapshot.setdefault("stop_price", stop_price)

    if card.suspended or bar_low is None:
        # 停牌/无 bar：当日无法判定也无法成交（详设 §4.2 阻塞原因③）
        return _unfilled(order_id, symbol, day, "suspended", snapshot)

    gap_through = bar_open is not None and 0 < bar_open < stop_price
    touched = bar_low <= stop_price
    if not (gap_through or touched):
        return MatchResult(status="not_triggered")
    # 触发先于阻塞判定（评审 C：跌停但未触发不记 unfilled——
    # 详设 §4.2 的 limit_down 是"触发日跌停卖不掉"，不是"跌停日本来就没人卖"）
    if card.is_limit_down:
        return _unfilled(order_id, symbol, day, "limit_down", snapshot)
    if position.sellable_quantity <= 0:
        return _unfilled(order_id, symbol, day, "t1_block", snapshot)

    reference = float(bar_open) if gap_through else float(stop_price)
    qty = position.sellable_quantity
    proceeds = fees.sell_proceeds(
        reference_price=reference, slippage_base=0.0, slippage_tail=0.0,
        quantity=qty, profile=profile, asset_type=asset_type,
        apply_slippage=False,  # 条件单触发价即成交假设（§4.2 成交可行性确认）
    )
    cash_after = account.cash + proceeds["net_proceeds"]
    fill = Fill(
        order_id=order_id, symbol=symbol, fill_date=day,
        base_price=reference, slippage_base=0.0, slippage_tail=0.0,
        fill_price=proceeds["fill_price"], quantity=qty,
        commission=proceeds["commission"], stamp_tax=proceeds["stamp_tax"],
        fee_total=proceeds["fee_total"], cash_after=cash_after,
    )
    return MatchResult(
        status="filled", fill=fill, quantity_change=-qty, cash_delta=proceeds["net_proceeds"]
    )


def _unfilled(order_id: str, symbol: str, day, reason: str, snapshot: dict) -> MatchResult:
    return MatchResult(
        status="unfilled",
        unfilled=Unfilled(
            order_id=order_id, symbol=symbol, decision_date=day,
            reason=reason, intent_snapshot=snapshot,
        ),
    )
