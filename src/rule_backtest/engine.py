from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from audit.app_logger import get_logger
from rule_backtest.condition_engine import ConditionEngine
from rule_backtest.metrics import (
    compute_annual_returns,
    compute_drawdown,
    compute_monthly_heatmap,
    compute_summary,
    monthly_returns,
)
from rule_backtest.models import BacktestExecutionConfig, PositionState, RuleBacktestRequest
from rule_backtest.sizing.base import (
    DEGRADED_FLAGS,
    SKIP_INSUFFICIENT_CASH,
    SKIP_SIZER,
    SKIP_TARGET_BELOW_LOT,
    SizingContext,
)
from rule_backtest.state_values import initialize_stop_state, update_position_state_for_day
from rule_backtest.value_resolver import ValueResolver

logger = get_logger(__name__)


class SingleSymbolAllInBacktestEngine:
    def run(self, request: RuleBacktestRequest) -> dict:
        all_bars = self._prepare_bars(request.bars)
        bars = all_bars
        if request.start_date is not None:
            bars = bars[bars["date"] >= request.start_date]
        if request.end_date is not None:
            bars = bars[bars["date"] <= request.end_date]
        if bars.empty:
            raise ValueError(f"symbol has no market data in range: {request.symbol}")

        run_id = request.run_id or datetime.now().strftime("%Y%m%d%H%M%S%f")
        strategy = request.strategy
        # 零值费用参数统一归一为默认值（全系统不提供零成本回测，与
        # service 层同口径；显式传 0 与未传参同等处理）。
        execution = request.execution.normalized()
        debug_enabled = self._debug_enabled(execution=execution, bars=bars)
        total_days = len(bars)
        progress_callback = request.progress_callback

        resolver = ValueResolver(strategy_cfg=strategy.get("indicator_config", {}) if isinstance(strategy, dict) else {})
        # Hot-path memoization: full indicator series are computed once over
        # the complete history; per-day resolution is an indexed lookup.
        resolver.set_context_bars(all_bars)
        condition_engine = ConditionEngine(resolver)

        cash = float(execution.initial_capital)
        position = PositionState()
        trades: list[dict] = []
        skipped_buys: list[dict] = []
        daily_nav: list[dict] = []
        condition_trace: list[dict] = []
        debug_log: list[dict] = []
        turnover_total = 0.0

        # 热路径用 itertuples（比 iterrows 快约一个数量级，P2-15）；
        # _prepare_bars 已 reset_index(drop=True)，row.Index 即位置坐标。
        for day_no, row in enumerate(bars.itertuples(), 1):
            idx = row.Index
            day = row.date
            day_str = day.isoformat()
            # Slice view only — nothing downstream mutates it; the previous
            # per-day .copy() was O(n^2) memory churn.
            day_bars = all_bars.iloc[: idx + 1]
            close_price = float(row.close)
            debug_day: dict = {
                "date": day_str,
                "raw_bar": self._row_to_dict(all_bars.iloc[idx]),
                "history_window": {
                    "start": all_bars.iloc[0]["date"].isoformat(),
                    "end": day_str,
                    "rows": len(day_bars),
                },
            } if debug_enabled else {}

            state_before = self._position_snapshot(position)
            state_trace = update_position_state_for_day(
                position=position, bars=day_bars, strategy=strategy, atr_at=resolver.atr_value_at
            )
            sold_today = False
            if debug_enabled:
                debug_day["state_before"] = state_before
                debug_day["state_values"] = state_trace

            exit_passed = False
            exit_traces: list[dict] = []
            if position.is_open:
                exit_passed, exit_traces = condition_engine.evaluate_group(
                    strategy.get("exit", {}),
                    bars=day_bars,
                    position=position,
                    debug=debug_enabled,
                    combinator="any",
                )
                condition_trace.extend(self._format_condition_trace(day_str, "EXIT", exit_traces))
                if debug_enabled:
                    debug_day["exit_condition_trace"] = exit_traces

            if position.is_open and exit_passed:
                reason, reference_price = self._resolve_sell_reference_price(
                    exit_traces=exit_traces,
                    close_price=close_price,
                    position=position,
                )
                trade, cash_delta = self._execute_sell(
                    symbol=request.symbol,
                    day=day_str,
                    qty=position.qty,
                    reference_price=reference_price,
                    cash_before=cash,
                    avg_cost=position.avg_cost,
                    reason=reason,
                    execution=execution,
                )
                cash += cash_delta
                turnover_total += float(trade["gross_amount"])
                trades.append(trade)
                if debug_enabled:
                    debug_day["decision"] = {"side": "SELL", "reason": reason}
                    debug_day["execution_trace"] = trade
                position.reset()
                # 冷却期记账：last_exit_bar_idx 用 all_bars 坐标系（与
                # resolver 逐日切片的 len(bars)-1 一致），reset() 不清除它。
                position.last_exit_bar_idx = len(day_bars) - 1
                sold_today = True

            entry_passed = False
            entry_traces: list[dict] = []
            if (not position.is_open) and (not sold_today):
                entry_passed, entry_traces = condition_engine.evaluate_group(
                    strategy.get("entry", {}),
                    bars=day_bars,
                    position=position,
                    debug=debug_enabled,
                    combinator="all",
                )
                condition_trace.extend(self._format_condition_trace(day_str, "ENTRY", entry_traces))
                if debug_enabled:
                    debug_day["entry_condition_trace"] = entry_traces

            if (not position.is_open) and entry_passed:
                reference_price = close_price
                qty, sizing_info, skip_info = self._resolve_buy_qty(
                    sizer=request.sizer,
                    resolver=resolver,
                    idx=idx,
                    day_bars=day_bars,
                    day_str=day_str,
                    cash=cash,
                    reference_price=reference_price,
                    trades=trades,
                    execution=execution,
                )
                if qty <= 0:
                    if skip_info is not None:
                        skipped_buys.append(skip_info)
                    if debug_enabled:
                        debug_day["decision"] = {
                            "side": "BUY_SKIPPED",
                            "reason": (skip_info or {}).get("reason", ""),
                        }
                        debug_day["sizing_decision"] = sizing_info
                if qty > 0:
                    trade, cash_delta = self._execute_buy(
                        symbol=request.symbol,
                        day=day_str,
                        qty=qty,
                        reference_price=reference_price,
                        cash_before=cash,
                        reason="entry_conditions_passed",
                        execution=execution,
                    )
                    cash += cash_delta
                    turnover_total += float(trade["gross_amount"])
                    if sizing_info is not None:
                        cash_before = float(trade["cash_before"])
                        sizing_info["position_pct"] = (
                            float(trade["total_cost"]) / cash_before if cash_before > 0 else 0.0
                        )
                        trade["sizing"] = sizing_info
                    trades.append(trade)
                    position.qty = qty
                    position.avg_cost = float(trade["total_cost"]) / qty
                    initialize_trace = initialize_stop_state(
                        position=position,
                        bars=day_bars,
                        strategy=strategy,
                        entry_price=float(trade["exec_price"]),
                        entry_date=day_str,
                        atr_at=resolver.atr_value_at,
                    )
                    if debug_enabled:
                        debug_day["decision"] = {"side": "BUY", "reason": "entry_conditions_passed"}
                        debug_day["execution_trace"] = trade
                        debug_day["sizing_decision"] = sizing_info
                        debug_day["state_initialization"] = initialize_trace

            market_value = position.qty * close_price if position.is_open else 0.0
            equity = cash + market_value
            nav_row = {
                "date": day_str,
                "cash": float(cash),
                "market_value": float(market_value),
                "equity": float(equity),
                "qty": int(position.qty),
                "close": float(close_price),
            }
            daily_nav.append(nav_row)
            if debug_enabled:
                debug_day["state_after"] = self._position_snapshot(position)
                debug_day["daily_nav"] = nav_row
                debug_log.append(debug_day)
            if progress_callback is not None:
                progress_callback(day_no, total_days)

        drawdown = compute_drawdown(daily_nav)
        summary = compute_summary(daily_nav=daily_nav, trades=trades, turnover_total=turnover_total)
        benchmark = self._buy_and_hold_benchmark(
            bars=bars,
            initial_capital=execution.initial_capital,
            lot_size=execution.lot_size,
        )
        benchmark_nav = (benchmark or {}).get("series", [])
        benchmark_summary = compute_summary(daily_nav=benchmark_nav, trades=[], turnover_total=0.0) if benchmark_nav else {}
        kline_payload = self._build_kline_payload(bars=bars, trades=trades, skipped_buys=skipped_buys)

        return {
            "run_id": run_id,
            "status": "ok",
            "strategy_id": strategy.get("id", ""),
            "sizer_id": getattr(request.sizer, "strategy_id", "") if request.sizer is not None else "",
            "sizer_name": getattr(request.sizer, "strategy_name", "") if request.sizer is not None else "",
            "symbol": request.symbol,
            "start_date": bars["date"].iloc[0].isoformat() if not bars.empty else None,
            "end_date": bars["date"].iloc[-1].isoformat() if not bars.empty else None,
            "initial_capital": float(execution.initial_capital),
            "final_equity": float(daily_nav[-1]["equity"]) if daily_nav else float(execution.initial_capital),
            "summary": summary,
            "trades": trades,
            "skipped_buys": skipped_buys,
            "daily_nav": daily_nav,
            "condition_trace": condition_trace,
            "debug_log": debug_log if debug_enabled else [],
            "drawdown": drawdown,
            "annual_returns": compute_annual_returns(
                daily_nav,
                trades=trades,
                benchmark_daily_nav=benchmark_nav,
            ),
            "monthly_returns": monthly_returns(daily_nav),
            "monthly_heatmap": compute_monthly_heatmap(daily_nav),
            "benchmark": benchmark,
            "benchmark_summary": benchmark_summary,
            "charts": {
                "dates": [row["date"] for row in daily_nav],
                "nav": [row["equity"] for row in daily_nav],
                "drawdown": [row["drawdown"] for row in drawdown],
                "buy_points": [self._trade_point(trade) for trade in trades if trade["side"] == "BUY"],
                "sell_points": [self._trade_point(trade) for trade in trades if trade["side"] == "SELL"],
                "skipped_buy_points": [
                    {
                        "date": skip["date"],
                        "price": skip["close"],
                        "reason": skip["reason"],
                        "note": skip.get("note", ""),
                    }
                    for skip in skipped_buys
                ],
                "kline": kline_payload,
            },
        }

    @staticmethod
    def _prepare_bars(raw_bars: object) -> pd.DataFrame:
        df = pd.DataFrame(raw_bars).copy()
        if df.empty:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
        if "date" not in df.columns:
            if "time" in df.columns:
                df["date"] = pd.to_datetime(df["time"], errors="coerce").dt.date
            else:
                raise ValueError("行情数据缺少时间列（需要 date 或 time 列）")
        else:
            df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
        for col in ("open", "high", "low", "close", "volume", "amount"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["date", "open", "high", "low", "close"]).sort_values("date").reset_index(drop=True)
        return df

    @staticmethod
    def _debug_enabled(execution: BacktestExecutionConfig, bars: pd.DataFrame) -> bool:
        if execution.debug_log_enabled is not None:
            return bool(execution.debug_log_enabled)
        if bars.empty:
            return False
        days = (bars["date"].iloc[-1] - bars["date"].iloc[0]).days
        return days < int(execution.debug_auto_enable_max_days)

    def _resolve_buy_qty(
        self,
        *,
        sizer: object | None,
        resolver: ValueResolver,
        idx: int,
        day_bars: pd.DataFrame,
        day_str: str,
        cash: float,
        reference_price: float,
        trades: list[dict],
        execution: BacktestExecutionConfig,
    ) -> tuple[int, dict | None, dict | None]:
        """Decide the buy quantity: affordability first (legacy logic), then
        the position sizer's target, clamped and lot-aligned.

        Returns (qty, sizing_annotation, skip_record). qty=0 with a
        skip_record means the entry signal produced no trade.
        """
        affordable_qty = self._max_buy_qty(cash=cash, reference_price=reference_price, execution=execution)
        if affordable_qty <= 0:
            return 0, None, {
                "date": day_str,
                "reason": SKIP_INSUFFICIENT_CASH,
                "note": "现金不足，买不起一手",
                "close": float(reference_price),
            }
        if sizer is None:
            return affordable_qty, None, None

        ctx = SizingContext(
            cash=float(cash),
            equity=float(cash),  # no open position at a buy point
            reference_price=float(reference_price),
            exec_price=float(reference_price) * (1.0 + execution.slippage),
            atr_at=self._make_atr_source(resolver=resolver, idx=idx),
            closed_trades=[t for t in trades if t.get("side") == "SELL"],
            execution=execution,
            history_bars=len(day_bars),
            affordable_qty=int(affordable_qty),
        )
        decision = sizer.decide(ctx)
        annotation = {
            "sizer_id": getattr(sizer, "strategy_id", ""),
            "sizer_type": getattr(sizer, "sizer_type", ""),
            "target_pct": float(decision.position_pct),
            "flags": list(decision.flags),
            "note": decision.note,
        }
        degraded = sorted(set(decision.flags) & DEGRADED_FLAGS)
        if degraded:
            logger.warning(
                "Degraded position sizing on %s (sizer=%s, flags=%s): %s",
                day_str, annotation["sizer_id"], ",".join(degraded), decision.note,
            )

        lot_size = max(int(execution.lot_size), 1)
        target_qty = (max(int(decision.target_qty), 0) // lot_size) * lot_size
        if decision.action == "skip":
            # No built-in sizer returns "skip"; honor it with its own reason
            # so future sizers are not mislabeled as below-lot.
            return 0, annotation, {
                "date": day_str,
                "reason": SKIP_SIZER,
                "note": decision.note or "仓位策略主动跳过本次买入",
                "close": float(reference_price),
            }
        if target_qty <= 0:
            return 0, annotation, {
                "date": day_str,
                "reason": SKIP_TARGET_BELOW_LOT,
                "note": decision.note or "仓位策略目标数量不足一手",
                "close": float(reference_price),
            }
        # No re-validation of fees needed after clamping: commission is
        # max(gross * rate, fee_min), so a smaller qty never costs more
        # than the affordable quantity the engine already validated.
        return min(affordable_qty, target_qty), annotation, None

    @staticmethod
    def _make_atr_source(resolver: ValueResolver, idx: int):
        """Bind the resolver's memoized ATR lookup to the current day index
        (position within all_bars; identical to the idx state_values uses)."""

        def atr_at(period: int, lookback: int = 0) -> float | None:
            return resolver.atr_value_at(idx - int(lookback), int(period))

        return atr_at

    @staticmethod
    def _max_buy_qty(cash: float, reference_price: float, execution: BacktestExecutionConfig) -> int:
        if cash <= 0 or reference_price <= 0:
            return 0
        exec_price = reference_price * (1.0 + execution.slippage)
        lot_size = max(int(execution.lot_size), 1)
        estimated_fee_per_share = exec_price * execution.fee_rate
        estimated_per_share = exec_price + estimated_fee_per_share
        raw_qty = int(cash // estimated_per_share)
        qty = (raw_qty // lot_size) * lot_size
        while qty > 0:
            gross = qty * exec_price
            commission = max(gross * execution.fee_rate, execution.fee_min)
            if gross + commission <= cash:
                return qty
            qty -= lot_size
        return 0

    @staticmethod
    def _execute_buy(
        symbol: str,
        day: str,
        qty: int,
        reference_price: float,
        cash_before: float,
        reason: str,
        execution: BacktestExecutionConfig,
    ) -> tuple[dict, float]:
        exec_price = reference_price * (1.0 + execution.slippage)
        gross = qty * exec_price
        commission = max(gross * execution.fee_rate, execution.fee_min)
        stamp_tax = 0.0
        total_cost = gross + commission + stamp_tax
        trade = {
            "date": day,
            "symbol": symbol,
            "side": "BUY",
            "qty": int(qty),
            "reason": reason,
            "reference_price_source": "close",
            "reference_price": float(reference_price),
            "slippage_rate": float(execution.slippage),
            "exec_price": float(exec_price),
            "price": float(exec_price),
            "gross_amount": float(gross),
            "commission_rate": float(execution.fee_rate),
            "commission_min": float(execution.fee_min),
            "commission": float(commission),
            "fee": float(commission),
            "stamp_tax_rate": 0.0,
            "stamp_tax": float(stamp_tax),
            "total_cost": float(total_cost),
            "net_proceeds": 0.0,
            "cash_before": float(cash_before),
            "cash_after": float(cash_before - total_cost),
        }
        return trade, -total_cost

    @staticmethod
    def _execute_sell(
        symbol: str,
        day: str,
        qty: int,
        reference_price: float,
        cash_before: float,
        avg_cost: float,
        reason: str,
        execution: BacktestExecutionConfig,
    ) -> tuple[dict, float]:
        exec_price = reference_price * (1.0 - execution.slippage)
        gross = qty * exec_price
        commission = max(gross * execution.fee_rate, execution.fee_min)
        stamp_tax_rate = execution.stock_stamp_tax_rate if execution.instrument_type == "stock" else 0.0
        stamp_tax = gross * stamp_tax_rate
        net = gross - commission - stamp_tax
        pnl = net - qty * avg_cost
        trade = {
            "date": day,
            "symbol": symbol,
            "side": "SELL",
            "qty": int(qty),
            "reason": reason,
            "reference_price_source": "stop_price" if reason in {"hard_stop", "chandelier_stop", "chandelier_stop_ratchet"} else "close",
            "reference_price": float(reference_price),
            "slippage_rate": float(execution.slippage),
            "exec_price": float(exec_price),
            "price": float(exec_price),
            "gross_amount": float(gross),
            "avg_cost": float(avg_cost),
            "commission_rate": float(execution.fee_rate),
            "commission_min": float(execution.fee_min),
            "commission": float(commission),
            "fee": float(commission),
            "stamp_tax_rate": float(stamp_tax_rate),
            "stamp_tax": float(stamp_tax),
            "total_cost": 0.0,
            "net_proceeds": float(net),
            "pnl": float(pnl),
            "cash_before": float(cash_before),
            "cash_after": float(cash_before + net),
        }
        return trade, net

    @staticmethod
    def _resolve_sell_reference_price(
        exit_traces: list[dict],
        close_price: float,
        position: PositionState,
    ) -> tuple[str, float]:
        for trace in exit_traces:
            if not bool(trace.get("passed", False)):
                continue
            for key in ("left_trace", "right_trace"):
                value_trace = trace.get(key, {})
                if not isinstance(value_trace, dict):
                    continue
                if value_trace.get("type") == "state_value" and value_trace.get("name") in {"hard_stop", "chandelier_stop", "chandelier_stop_ratchet"}:
                    name = str(value_trace.get("name"))
                    value = value_trace.get("value")
                    if value is not None and float(value) > 0:
                        return name, float(value)
        if position.hard_stop > 0 and close_price <= position.hard_stop:
            return "hard_stop", position.hard_stop
        if position.chandelier_stop > 0 and close_price <= position.chandelier_stop:
            return "chandelier_stop", position.chandelier_stop
        if position.chandelier_stop_ratchet > 0 and close_price <= position.chandelier_stop_ratchet:
            return "chandelier_stop_ratchet", position.chandelier_stop_ratchet
        return "exit_conditions_passed", close_price

    @staticmethod
    def _format_condition_trace(day: str, side: str, traces: list[dict]) -> list[dict]:
        out: list[dict] = []
        for trace in traces:
            row = {
                "date": day,
                "side": side,
                "condition_id": trace.get("condition_id"),
                "condition_index": trace.get("condition_index"),
                "left_value": trace.get("left_value"),
                "operator": trace.get("operator"),
                "right_value": trace.get("right_value"),
                "passed": bool(trace.get("passed", False)),
            }
            if trace.get("operator") in {"cross_above", "cross_below"}:
                row["left_prev_value"] = trace.get("left_prev_value")
                row["right_prev_value"] = trace.get("right_prev_value")
            out.append(row)
        return out

    @staticmethod
    def _position_snapshot(position: PositionState) -> dict:
        return {
            "qty": int(position.qty),
            "entry_price": float(position.entry_price),
            "avg_cost": float(position.avg_cost),
            "entry_date": position.entry_date,
            "atr_at_entry": float(position.atr_at_entry),
            "hard_stop": float(position.hard_stop),
            "highest_high_since_entry": float(position.highest_high_since_entry),
            "chandelier_stop": float(position.chandelier_stop),
            "chandelier_stop_ratchet": float(position.chandelier_stop_ratchet),
            "last_exit_bar_idx": position.last_exit_bar_idx,
        }

    @staticmethod
    def _row_to_dict(row: pd.Series) -> dict:
        out = {}
        for key, value in row.items():
            if isinstance(value, date):
                out[str(key)] = value.isoformat()
            elif hasattr(value, "item"):
                out[str(key)] = value.item()
            else:
                out[str(key)] = value
        return out

    @staticmethod
    def _trade_point(trade: dict) -> dict:
        return {
            "date": trade.get("date"),
            "symbol": trade.get("symbol"),
            "qty": trade.get("qty"),
            "price": trade.get("exec_price"),
            "amount": trade.get("total_cost") if trade.get("side") == "BUY" else trade.get("net_proceeds"),
            "reason": trade.get("reason"),
            "flags": list(trade.get("sizing", {}).get("flags", [])),
        }

    @staticmethod
    def _buy_and_hold_benchmark(bars: pd.DataFrame, initial_capital: float, lot_size: int) -> dict | None:
        if bars.empty:
            return {"name": "buy_and_hold", "series": []}
        first_close = float(bars.iloc[0]["close"])
        if first_close <= 0:
            # 非正价格 = 行情数据异常（历史上的等差复权事故）。返回 None 让上层
            # 把基准/超额记为缺失，而不是静默退化为「全程现金、基准年化 0%」。
            logger.warning(
                "buy-and-hold benchmark skipped: non-positive first close %.6f", first_close
            )
            return None
        qty = int((initial_capital // first_close) // lot_size) * lot_size
        cash = initial_capital - qty * first_close
        series = [
            {"date": day.isoformat(), "equity": float(cash + qty * float(close_))}
            for day, close_ in zip(bars["date"], bars["close"])
        ]
        return {"name": "buy_and_hold", "qty": int(qty), "series": series}

    @staticmethod
    def _build_kline_payload(bars: pd.DataFrame, trades: list[dict], skipped_buys: list[dict] | None = None) -> dict:
        if bars.empty:
            return {"dates": [], "candles": [], "ma": {}, "buy_points": [], "sell_points": [], "skipped_buy_points": []}

        data = bars.copy()
        dates = [day.isoformat() for day in data["date"]]
        candles = [
            [float(o), float(c), float(lo), float(hi)]
            for o, c, lo, hi in zip(data["open"], data["close"], data["low"], data["high"])
        ]
        close = pd.to_numeric(data["close"], errors="coerce")
        ma: dict[str, list[float | None]] = {}
        for period in (5, 10, 20, 30, 60, 120, 200):
            series = close.rolling(period, min_periods=period).mean()
            ma[str(period)] = [float(v) if pd.notna(v) else None for v in series.tolist()]

        buy_points = []
        sell_points = []
        for trade in trades:
            point = {
                "date": str(trade.get("date", "")),
                "price": float(trade.get("exec_price", 0.0) or 0.0),
                "reference_price": float(trade.get("reference_price", 0.0) or 0.0),
                "qty": int(trade.get("qty", 0) or 0),
                "amount": trade.get("total_cost") if str(trade.get("side", "")).upper() == "BUY" else trade.get("net_proceeds"),
                "reason": trade.get("reason", ""),
                "flags": list(trade.get("sizing", {}).get("flags", [])),
            }
            if str(trade.get("side", "")).upper() == "BUY":
                buy_points.append(point)
            elif str(trade.get("side", "")).upper() == "SELL":
                sell_points.append(point)

        skipped_buy_points = [
            {
                "date": str(skip.get("date", "")),
                "price": float(skip.get("close", 0.0) or 0.0),
                "reason": str(skip.get("reason", "")),
                "note": str(skip.get("note", "")),
            }
            for skip in (skipped_buys or [])
        ]

        return {
            "dates": dates,
            "candles": candles,
            "ma": ma,
            "buy_points": buy_points,
            "sell_points": sell_points,
            "skipped_buy_points": skipped_buy_points,
        }
