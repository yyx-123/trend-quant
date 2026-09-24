"""引擎主体：账户 + 撮合 + 落库的编排面（纯执行，不懂策略/选股/研究）。

数据需求由调用方（L3 回测器 / 实盘运行器）经 L1.5 门面取数后转供
（同一 as_of 锚定的面板 + 可交易性标注）；引擎不独立直连 L1。
回测与实盘同一条代码路径：两者驱动同一个引擎实例族，差异只在数据源
是历史面板还是当日数据（架构稿 §2.4）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from engine import account as acct
from engine import matcher
from engine.models import (
    Account,
    ExitOrderIntent,
    OrderIntent,
    StopState,
)
from engine.profiles import MarketProfile, get_profile
from engine.store import EngineStore


class EngineError(RuntimeError):
    """引擎规则违例（如 MVP 不加仓被违反）。"""


class Engine:
    def __init__(
        self,
        *,
        profile: MarketProfile | str = "cn_stock",
        initial_cash: float = 1_000_000.0,
        store: EngineStore | None = None,
        order_prefix: str = "O",
    ) -> None:
        self.profile = get_profile(profile) if isinstance(profile, str) else profile
        self.account = Account(cash=float(initial_cash))
        self.store = store
        self._order_seq = 0
        self._order_prefix = order_prefix

    def _next_order_id(self) -> str:
        self._order_seq += 1
        return f"{self._order_prefix}-{self._order_seq:05d}"

    # ------------------------------------------------------------------
    # 买入（尾盘市价等价物）
    # ------------------------------------------------------------------
    def buy(
        self,
        *,
        intent: OrderIntent,
        day: date,
        bar_close: float,
        card: matcher.TradabilityCard,
        asset_type: str,
        slippage_base: float,
        slippage_tail: float,
        on_filled: Callable[[object], tuple[StopState | None, str]] | None = None,
        intent_snapshot: dict | None = None,
    ) -> matcher.MatchResult:
        order_id = self._next_order_id()
        # §5.4.2 不加仓：同标的已有持仓时 fail-loud（评审 DS-P2-1——静默覆盖
        # 会让现金少记、净值失真；加仓是二期项，现在必须是"报错"）
        if intent.symbol in self.account.positions:
            raise EngineError(
                f"buy intent for already-held symbol {intent.symbol} "
                "(no pyramiding in MVP; §5.4.2)"
            )
        result = matcher.match_buy(
            order_id=order_id, symbol=intent.symbol, day=day, card=card,
            bar_close=bar_close, intent_type=intent.intent_type,
            intent_value=intent.value, account=self.account,
            profile=self.profile, asset_type=asset_type,
            slippage_base=slippage_base, slippage_tail=slippage_tail,
            intent_snapshot=intent_snapshot,
        )
        self._record_order(
            order_id=order_id, decision_date=intent.decision_date, fill_day=day,
            symbol=intent.symbol, side="buy", intent_type=intent.intent_type,
            intent_value=intent.value, source=intent.source, result=result,
        )
        if result.status == "filled" and result.fill is not None:
            stop_state, module_ref = (None, "")
            if on_filled is not None:
                stop_state, module_ref = on_filled(result.fill)
            acct.apply_buy_fill(
                self.account, result.fill,
                entry_state=stop_state, position_risk_ref=module_ref,
            )
            if self.store:
                self.store.record_fill(result.fill)
        elif result.status == "unfilled" and result.unfilled is not None and self.store:
            self.store.record_unfilled(result.unfilled)
        return result

    # ------------------------------------------------------------------
    # 卖出（信号退出/收盘确认型：尾盘；止损：盘中）
    # ------------------------------------------------------------------
    def sell(
        self,
        *,
        intent: ExitOrderIntent,
        day: date,
        card: matcher.TradabilityCard,
        asset_type: str,
        bar_close: float | None = None,
        bar_open: float | None = None,
        bar_low: float | None = None,
        slippage_base: float = 0.0,
        slippage_tail: float = 0.0,
        intent_snapshot: dict | None = None,
    ) -> matcher.MatchResult:
        position = self.account.positions.get(intent.symbol)
        order_id = self._next_order_id()
        if position is None or position.quantity <= 0:
            raise ValueError(f"sell intent for symbol with no open position: {intent.symbol}")

        if intent.fill_mode == "intraday_stop":
            if intent.stop_price is None or intent.stop_price <= 0:
                raise ValueError("intraday_stop intent requires stop_price")
            result = matcher.match_intraday_stop(
                order_id=order_id, symbol=intent.symbol, day=day, card=card,
                bar_open=bar_open, bar_low=bar_low, stop_price=float(intent.stop_price),
                position=position, account=self.account, profile=self.profile,
                asset_type=asset_type, intent_snapshot=intent_snapshot,
            )
        elif intent.fill_mode == "tail":
            if bar_close is None:
                # 停牌/无 bar：不是"传参错误"而是"当日不可成交"——走 §4.2
                # unfilled(suspended) 语义（评审 K3-P1-1/DS-P2-2：time_stop/
                # ma_cross exit 等模块在停牌日的 tail 离场不得炸掉整个 run）
                result = matcher.MatchResult(
                    status="unfilled",
                    unfilled=matcher.Unfilled(
                        order_id=order_id, symbol=intent.symbol, decision_date=day,
                        reason="suspended",
                        intent_snapshot={"note": "no bar (suspended)"},
                    ),
                )
            else:
                result = matcher.match_tail_sell(
                    order_id=order_id, symbol=intent.symbol, day=day, card=card,
                    bar_close=bar_close, position=position, account=self.account,
                    profile=self.profile, asset_type=asset_type,
                    slippage_base=slippage_base, slippage_tail=slippage_tail,
                    intent_snapshot=intent_snapshot,
                )
        else:
            raise ValueError(f"unknown fill_mode: {intent.fill_mode}")

        if result.status != "not_triggered":
            self._record_order(
                order_id=order_id, decision_date=intent.decision_date, fill_day=day,
                symbol=intent.symbol, side="sell",
                intent_type="quantity",
                intent_value=position.sellable_quantity, source=intent.source,
                result=result,
            )
        if result.status == "filled" and result.fill is not None:
            acct.apply_sell_fill(self.account, result.fill)
            if self.store:
                self.store.record_fill(result.fill)
        elif result.status == "unfilled" and result.unfilled is not None and self.store:
            self.store.record_unfilled(result.unfilled)
        return result

    # ------------------------------------------------------------------
    # 日循环钩子：begin_day（T+1 滚动）→ 交易 → settle（日结）
    # ------------------------------------------------------------------
    def begin_day(self, *, day: date) -> None:
        """新交易日开始：昨日持仓全部转为可卖（T+1 一等表达）。"""
        acct.rollover_t1(self.account)

    def settle(self, *, day: date, close_prices: dict[str, float]) -> dict:
        nav_row = acct.settle_day(
            self.account, day=day, close_prices=close_prices,
            cash_interest_rate=self.profile.cash_interest_rate,
        )
        if self.store:
            self.store.record_nav(nav_row)
            self.store.record_positions(day=day, account=self.account)
        return nav_row

    def _record_order(self, *, order_id, decision_date, fill_day, symbol, side,
                      intent_type, intent_value, source, result) -> None:
        if not self.store:
            return
        status = {"filled": "filled", "unfilled": "unfilled"}.get(result.status, "pending")
        if result.status == "not_triggered":
            return  # 止损未触发不产生订单记录（没发生的交易不是"错过的交易"）
        self.store.record_order(
            order_id=order_id,
            decision_date=decision_date.isoformat() if hasattr(decision_date, "isoformat") else str(decision_date),
            target_fill_date=fill_day.isoformat() if hasattr(fill_day, "isoformat") else str(fill_day),
            symbol=symbol, side=side, order_type="tail_market",
            intent_type=intent_type, intent_value=intent_value,
            source=source, status=status,
        )


def new_run_id(prefix: str = "R") -> str:
    from datetime import datetime

    return f"{prefix}{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
