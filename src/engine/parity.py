"""单标的回测薄适配（详设 §8 阶段 1）：用新引擎重跑单标的策略，与旧
引擎（rule_backtest）在相同策略/窗口下对比，产出口径差异报告。

预期差异来源（阶段 1 验收只允许的三类）：
1. 涨跌停卡控（新引擎限板日拒买/拒卖，旧引擎照成交）；
2. T+1 显式（新引擎 sellable 一等表达；单标的同日无卖买场景，预期零差异）；
3. 尾盘滑点（新引擎 slippage_tail 参数；置 0 时两侧应位级一致）。

止损口径差异不在本对比范围：决策 6 已定新栈 ATR 含当根（回测侧向实盘
侧对齐），旧引擎 T-1 口径不动；止损公式对拍走
tests/unit/test_engine_stops_parity.py（与实盘侧 services/stop_loss.py
同参数序列对拍）。故本驱动的对比策略为 **MACD 金叉进 / 死叉出、
无止损、全进全出**——让引擎间差异只剩上述三类。
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from core.indicators import macd as core_macd
from engine.engine import Engine, new_run_id
from engine.matcher import TradabilityCard
from engine.models import ExitOrderIntent, OrderIntent
from engine.profiles import MarketProfile, get_profile
from engine.store import EngineStore


def _normalize_bars(bars: pd.DataFrame) -> pd.DataFrame:
    df = pd.DataFrame(bars).copy()
    if "date" not in df.columns:
        if "time" not in df.columns:
            # R2-P3-9：与旧引擎 _prepare_bars 同口径的明确报错（此前裸 KeyError）
            raise ValueError("bars must have a 'date' or 'time' column")
        df["date"] = pd.to_datetime(df["time"], errors="coerce").dt.date
    else:
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["date", "open", "high", "low", "close"]).sort_values("date").reset_index(drop=True)


def run_macd_parity(
    bars: pd.DataFrame,
    *,
    initial_capital: float = 100_000.0,
    slippage_base: float = 0.002,
    slippage_tail: float = 0.0,
    asset_type: str = "etf",
    profile: MarketProfile | str = "cn_stock",
    cards: dict[date, TradabilityCard] | None = None,
    store: EngineStore | None = None,
    run_id: str | None = None,
) -> dict:
    """MACD 金叉全进 / 死叉全出（无止损）在新引擎上的单标的薄驱动。

    与旧引擎 ``single_symbol_all_in`` + macd_cross 策略同语义：
    信号当日收盘计算、当日收盘价 + 滑点成交、整手对齐、现金约束。
    """
    df = _normalize_bars(bars)
    if df.empty:
        raise ValueError("empty bars")

    engine = Engine(
        profile=get_profile(profile) if isinstance(profile, str) else profile,
        initial_cash=initial_capital,
        store=store,
    )
    cards = cards or {}

    macd_df = core_macd(df["close"], 12, 26, 9, warmup=True)
    dif = macd_df["dif"].to_numpy(dtype=float)
    dea = macd_df["dea"].to_numpy(dtype=float)
    # 预热保护（与旧引擎 rule_backtest/indicators.macd 同语义）：
    # 不足 max(fast,slow)+signal 根 bar 不出信号。
    # 对齐旧引擎的最早信号日（i=35）：当日需满 35 根（指标非 None），
    # 且昨日值需 prev 段满 35 根 → i≥35。
    warmup_bars = 26 + 9

    trades: list[dict] = []
    nav_rows: list[dict] = []
    symbol = "PARITY"

    for i, row in enumerate(df.itertuples()):
        day = row.date
        close = float(row.close)
        card = cards.get(day, TradabilityCard())
        engine.begin_day(day=day)
        position = engine.account.positions.get(symbol)

        golden = i >= 1 and i >= warmup_bars and dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]
        dead = i >= 1 and i >= warmup_bars and dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]

        if position is not None and position.is_open and dead:
            result = engine.sell(
                intent=ExitOrderIntent(
                    symbol=symbol, decision_date=day, fill_mode="tail", source="signal"
                ),
                day=day, card=card, asset_type=asset_type, bar_close=close,
                slippage_base=slippage_base, slippage_tail=slippage_tail,
            )
            if result.status == "filled" and result.fill is not None:
                trades.append({"date": day, "side": "SELL", "qty": result.fill.quantity,
                               "price": result.fill.fill_price})
        elif position is None and golden:
            from engine.fees import max_affordable_quantity

            exec_est = close * (1.0 + slippage_base + slippage_tail)
            qty = max_affordable_quantity(
                cash=engine.account.cash, exec_price_est=exec_est, profile=engine.profile
            )
            if qty > 0:
                result = engine.buy(
                    intent=OrderIntent(
                        symbol=symbol, decision_date=day,
                        intent_type="quantity", value=qty, source="signal",
                    ),
                    day=day, bar_close=close, card=card, asset_type=asset_type,
                    slippage_base=slippage_base, slippage_tail=slippage_tail,
                )
                if result.status == "filled" and result.fill is not None:
                    trades.append({"date": day, "side": "BUY", "qty": result.fill.quantity,
                                   "price": result.fill.fill_price})

        nav_row = engine.settle(day=day, close_prices={symbol: close})
        nav_rows.append(nav_row)

    return {
        "run_id": run_id or new_run_id(),
        "trades": trades,
        "daily_nav": nav_rows,
        "final_equity": nav_rows[-1]["equity"] if nav_rows else initial_capital,
    }


def diff_against_legacy(new_result: dict, legacy_result: dict) -> dict:
    """新旧引擎结果对比 → 差异清单（每条带可归因类别）。

    归因类别即阶段 1 验收白名单：limit_card / t_plus / tail_slippage /
    cash_interest（空仓计息是详设 §4.4 新引入的口径——属"尾部滑点外"的
    第四类显式差异，parity 报告里单列）。
    """
    new_trades = new_result["trades"]
    old_trades = [
        {
            "date": pd.Timestamp(t["date"]).date(),
            "side": t["side"],
            "qty": int(t["qty"]),
            "price": float(t["exec_price"]),
        }
        for t in legacy_result["trades"]
    ]
    diffs: list[dict] = []
    n = max(len(new_trades), len(old_trades))
    for i in range(n):
        new_t = new_trades[i] if i < len(new_trades) else None
        old_t = old_trades[i] if i < len(old_trades) else None
        if new_t is None or old_t is None:
            diffs.append({"index": i, "kind": "count_mismatch", "new": new_t, "old": old_t})
            continue
        if (
            new_t["date"] == old_t["date"] and new_t["side"] == old_t["side"]
            and new_t["qty"] == old_t["qty"]
            and abs(new_t["price"] - old_t["price"]) < 1e-9
        ):
            continue
        diffs.append({"index": i, "kind": "trade_mismatch", "new": new_t, "old": old_t})

    new_nav = [float(r["equity"]) for r in new_result["daily_nav"]]
    old_nav = [float(r["equity"]) for r in legacy_result["daily_nav"]]
    nav_divergence = []
    # DS-复审-R2 §4-4：zip 截断静默丢尾部——长度不齐先断言留痕
    if len(new_nav) != len(old_nav):
        nav_divergence.append({
            "kind": "length_mismatch",
            "new_len": len(new_nav), "old_len": len(old_nav),
        })
    for i, (a, b) in enumerate(zip(new_nav, old_nav)):
        if abs(a - b) > 1e-6:
            nav_divergence.append({"index": i, "new": a, "old": b})
    return {"trade_diffs": diffs, "nav_divergence": nav_divergence,
            "trades_new": len(new_trades), "trades_old": len(old_trades)}


# 阶段 1 验收白名单（详设 §8 人类介入点）：差异只允许来自这三类 + 空仓计息。
ATTRIBUTION_WHITELIST = ("limit_card", "t_plus", "tail_slippage", "cash_interest")


def attribute_diffs(
    new_result: dict, legacy_result: dict, cards: dict | None = None,
    *, max_tail_slippage: float = 0.011,
    cash_interest_rate: float = 0.01,
    lot_size: int = 100,
) -> dict:
    """新旧引擎差异的白名单归因（详设 §8：差异只允许来自涨跌停卡控/T+1/
    尾盘滑点/空仓计息——超纲即测试失败）。

    判据（loop-review R2-P1-1 修正后如实声明，两条腿）：

    1. **零卡控场景**：trade/NAV 位级一致——``trade_diffs==[]`` 且
       ``unexplained==[]``，任何真实差异都必须被归入白名单类；
    2. **卡控场景**：路径级联错位使逐笔位置对齐失效（拒买一笔改变后续
       全部持仓路径），位置级归因不可用——机器判据退到
       ``violations==[]``（新引擎不得在任何卡控日成交对应方向：涨停日
       不买/跌停日不卖）。``unexplained`` 在卡控场景**必然非空**（级联
       错位的 trade/NAV 差异），不得作为该场景的验收断言。

    **适用区间（R2A 复核校准，务必按此读结果）**：归因界是**物理量**——
    "新引擎相对旧引擎累计多付/少收的现金"能买多少股、能解释多少净值偏离，
    而不是滑点上界的复合乘积（后者在数千笔时发散，会把荒谬错误吸收掉）。
    代价是：**长窗口**下尾滑点成本本身会复合，真实引擎实测合法净值偏离
    2500 日 47% / 4000 日 63% / 6000 日 74%（零引擎逻辑差异、零卡控）。
    故本函数的判别力只在短/中窗口（≲200 笔差异）成立；超出时结果里
    ``saturated=True`` 且 ``attribution_note`` 非空——**不得**再把
    ``unexplained == []`` 当"引擎力学一致"的验收断言，改用 ``violations``。

    白名单归类规则：
    - trade_mismatch：同日同向且价差幅度在尾盘滑点界限内 → tail_slippage。
      数量差在一手以内也归此类——更高的成交价降低购买力，整手取整传导为
      ±一手数量漂移（滑点参数的合法下游，R2VB B-2 集成断言的实证形态）；
    - count_mismatch：落点日带涨跌停卡 → limit_card；
    - NAV 逐点差异：相对差 ≤ 日计息界限（年化/252，2 倍容差）→
      cash_interest；超界 → unexplained（此前 NAV 逐点差异不参与判负，
      "超纲即失败"在 NAV 轴未接线——R2-P1-1 修复）；
    - length_mismatch → unexplained。
    - ``t_plus`` 为结构性零差异键（单标的买卖不同日流程下 T+1 不产生
      任何差异，详见详设 §8 阶段 1 预期差异表），保留占位不计。
    """
    diff = diff_against_legacy(new_result, legacy_result)
    cards = cards or {}
    violations: list[dict] = []
    for t in new_result["trades"]:
        card = cards.get(t["date"])
        if card is None:
            continue
        if t["side"] == "BUY" and card.is_limit_up:
            violations.append({"kind": "limit_up_buy", "trade": t})
        if t["side"] == "SELL" and card.is_limit_down:
            violations.append({"kind": "limit_down_sell", "trade": t})

    classified = {k: 0 for k in ATTRIBUTION_WHITELIST}
    unexplained: list[dict] = []
    # 累计"额外成本"（**物理量**，不是上界）：新引擎相对旧引擎因尾盘滑点
    # 多付/少收的现金。它是数量漂移与 NAV 漂移唯一的合法来源——
    #   * 买入：新价更高 → 多付 (P_new − P_old) × qty_old；
    #   * 卖出：新价更低 → 少收 (P_old − P_new) × qty_old。
    # loop-review-ds4f R2A-P1-1/P2-1：R1 用"滑点上界的乘积 Π(1+slip)"当界限，
    # 在生产尺度（数千笔）上 Π 发散到 25%+ → NAV 上限被推到 75%，+100% 的净值
    # 错误又被吸收；同时"该笔数量的 1/2"硬帽会把**合法**的长 run 漂移判成超纲
    # （4000~6000 日、零卡控场景下 76~229 笔假报警，而 docstring 要求该场景
    # `unexplained == []`）。物理量两头都对：能买多少股，取决于已经多花了多少钱。
    cum_extra_cost = 0.0
    for i, d in enumerate(diff["trade_diffs"]):
        if d["kind"] == "trade_mismatch":
            nt, ot = d["new"], d["old"]
            if (
                nt["date"] == ot["date"] and nt["side"] == ot["side"]
                and ot["price"] > 0
            ):
                slip_ratio = abs(nt["price"] / ot["price"] - 1.0)
                ref_price = float(nt["price"]) or float(ot["price"])
                # 本笔**之前**累计的额外成本所能解释的股数（+一手取整噪声）
                qty_bound = (
                    int(cum_extra_cost / ref_price) + int(lot_size)
                    if ref_price > 0 else int(lot_size)
                )
                qty_drift = abs(int(nt["qty"]) - int(ot["qty"]))
                if 0 < slip_ratio <= max_tail_slippage and qty_drift <= qty_bound:
                    classified["tail_slippage"] += 1
                    cum_extra_cost += abs(float(nt["price"]) - float(ot["price"])) * abs(
                        int(ot["qty"])
                    )
                    continue
            unexplained.append(d)
        elif d["kind"] == "count_mismatch":
            row = d["new"] or d["old"] or {}
            card = cards.get(row.get("date"))
            if card is not None and (card.is_limit_up or card.is_limit_down):
                classified["limit_card"] += 1
            else:
                unexplained.append(d)
        else:
            unexplained.append(d)

    daily_interest_bound = cash_interest_rate / 252.0
    # NAV 逐点差异的归类语境：若已有 trade 级白名单归类（如 tail_slippage），
    # NAV 路径漂移是其复利下游——同归该类；**零差异语境**（无任何 trade 级
    # 白名单命中）下超界 NAV 差异=超纲（如计息误加进持仓市值），必须判负。
    downstream_kind = "tail_slippage" if classified["tail_slippage"] > 0 else None
    # 级联上限（R1-P2-7 + R2A-P1-1）：净值偏离 ≤ 累计额外成本占权益的比例
    # （放宽 5× 余量，覆盖持仓规模差带来的市值差），并叠加**绝对天花板 50%**
    # ——任何 ≥50% 的净值错误无条件判超纲，避免"长 run 上限发散"再次把
    # 荒谬错误吸收掉。物理量 + 天花板：两头都堵。
    _eqs = [float(r["equity"]) for r in new_result.get("daily_nav") or []
            if r.get("equity") is not None]
    avg_equity = (sum(_eqs) / len(_eqs)) if _eqs else 0.0
    drag_ratio = (cum_extra_cost / avg_equity) if avg_equity > 0 else 0.0
    # R2A-P1-1 复核后的最终口径：**只用物理量**，不加绝对天花板。
    # 真实引擎实测（纯尾滑点差异、零卡控、零引擎逻辑差异）：合法净值偏离
    # 随窗口增长到 2500 日 47%、4000 日 63%、6000 日 74%——任何"≥X% 一律
    # 判超纲"的绝对阈值都会把**合法**长 run 判成失败。反过来物理量界在
    # 短/中窗口上是紧的：300 笔前缀时把 +100% 净值错误与 +100% 数量错误
    # 都判超纲（钉子在案）。判别力边界由 saturated 显式标注。
    nav_cascade_bound = max(daily_interest_bound * 2.0, drag_ratio * 5.0)
    for nd in diff["nav_divergence"]:
        if nd.get("kind") == "length_mismatch":
            unexplained.append({**nd, "kind": "nav_length_mismatch"})
            continue
        old_eq = float(nd.get("old") or 0.0)
        rel = abs(float(nd["new"]) - old_eq) / old_eq if old_eq > 0 else float("inf")
        # 计息界限放宽 2 倍容差（复利/计提顺序的 1 日误差量级）
        if rel <= daily_interest_bound * 2.0:
            classified["cash_interest"] += 1
        elif downstream_kind is not None and rel <= nav_cascade_bound:
            classified[downstream_kind] += 1
        else:
            unexplained.append({**nd, "kind": "nav_point_diff_beyond_interest"})
    # 判别力饱和标记（R2A-P1-1/P2-2 口径收口）：笔数一多，逐笔位置对齐退化、
    # 漂移本身可以很大（实测 4000 日合法净值偏离 63%）——此时
    # `unexplained == []` **不再等价于"引擎力学一致"**。凡是越出判别区间的
    # 归因结果都显式标注，避免把"解释不了"与"判别不了"混为一谈。
    n_trade_diffs = len(diff["trade_diffs"])
    # 阈值取 25%：实测 stage-1 验收尺度（260 日 / 22 笔 / 尾滑点 0.001~0.003）
    # 的合法净值偏离为 2.2%~6.3% → 不饱和、判据照常有效；1200 日已到 26%
    # → 饱和。这样"未饱和"才真正等价于"归因有判别力"。
    saturated = bool(
        n_trade_diffs > 200
        or nav_cascade_bound > 0.25
        or any(
            (
                float(nd.get("old") or 0.0) > 0
                and abs(float(nd["new"]) - float(nd["old"])) / float(nd["old"]) > 0.25
            )
            for nd in diff["nav_divergence"]
            if nd.get("kind") != "length_mismatch"
        )
    )
    return {
        "violations": violations,
        "trade_diffs": diff["trade_diffs"],
        "nav_divergence": diff["nav_divergence"],
        "classified": classified,
        "unexplained": unexplained,
        "n_trade_diffs": n_trade_diffs,
        "nav_cascade_bound": nav_cascade_bound,
        "drag_ratio": drag_ratio,
        "saturated": saturated,
        "attribution_note": (
            "判别力饱和：差异笔数/漂移幅度超出逐笔归因的判别区间，"
            "unexplained 为空**不代表**引擎力学一致；该场景请改用 violations "
            "作为验收判据（见 attribute_diffs.__doc__ 的适用区间）"
            if saturated else ""
        ),
    }
