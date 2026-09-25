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

import math
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
            # 与旧引擎 _prepare_bars 同口径的明确报错（此前裸 KeyError）
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
    # 形状异常（None/NaN/非数值日期）不得让归因器抛异常穿出（实证：
    # 旧侧此前 int(qty)/Timestamp(date) 无保护，而"新侧 trade_shape_invalid"
    # 的守卫对旧侧不可达）——统一转成 sentinel，由返回的差异清单判负。
    old_trades = []
    for t in legacy_result["trades"]:
        try:
            date_v = pd.Timestamp(t["date"]).date()
        except (KeyError, TypeError, ValueError):
            date_v = None
        try:
            qty_v: int | None = int(t["qty"])
        except (KeyError, TypeError, ValueError):
            qty_v = None
        try:
            price_v: float | None = float(t["exec_price"])
        except (KeyError, TypeError, ValueError):
            price_v = None
        old_trades.append({
            "date": date_v, "side": t.get("side"), "qty": qty_v, "price": price_v,
        })
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
            and new_t["price"] is not None and old_t["price"] is not None
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

# 方向判定的价格容差：半个最小变动单位（覆盖价格舍入噪声）。合法尾滑点只能让
# 成交**更差**（买价抬升、卖价压低），"更有利"的系统性价差不可能来自任何合法
# 参数（滑点符号写反/取错参考价的典型形态）——此前用 `abs(价差)` 判带对方向
# 盲，这类真实错误会在 ≤1.1% 带内被静默归入白名单。真实 run 实测（510300.SS
# 2015-2024，191 笔）：不利 191 / 有利 0。
_PRICE_TICK_EPS = 5e-4


def attribute_diffs(
    new_result: dict, legacy_result: dict, cards: dict | None = None,
    *, max_tail_slippage: float = 0.011,
    cash_interest_rate: float = 0.01,
    lot_size: int = 100,
) -> dict:
    """新旧引擎差异的白名单归因（详设 §8：差异只允许来自涨跌停卡控/T+1/
    尾盘滑点/空仓计息——超纲即测试失败）。

    判据（修正后如实声明，两条腿）：

    1. **零卡控场景**：trade/NAV 位级一致——``trade_diffs==[]`` 且
       ``unexplained==[]``，任何真实差异都必须被归入白名单类；
    2. **卡控场景**：路径级联错位使逐笔位置对齐失效（拒买一笔改变后续
       全部持仓路径），位置级归因不可用——机器判据退到
       ``violations==[]``（新引擎不得在任何卡控日成交对应方向：涨停日
       不买/跌停日不卖）。``unexplained`` 在卡控场景**必然非空**（级联
       错位的 trade/NAV 差异），不得作为该场景的验收断言。

    判据分三层（四轮复核收敛后的最终口径）：

    1. **白名单归类**（差异必须可归因）：tail_slippage / limit_card /
       t_plus / cash_interest。数量界是**物理量**——"累计多付/少收的现金
       能买多少股" ＋ "整手取整的量化随机游走"，而不是滑点上界的复合乘积
       （后者在数千笔时发散，会把荒谬错误吸收掉）。
    2. **精确恒等式**（不依赖任何启发式界，长窗口同样有效）：
       - 每侧的 ``equity == cash + 持仓市值``——两个引擎都是这么算的，合法
         run 残差是浮点级（实测 0.0）；**内部不一致的伪造净值**（5% / +100%
         / 任意窗口长度）都会立刻破坏它（kind: nav_identity_broken；NaN/inf
         另判 nav_identity_nonfinite）。
       - 新侧 ``positions_value == Σ(成交清单推出来的持仓量) × 当日收盘价``
         （收盘价取自旧侧 nav，两侧同一市场价）——任何伪造/错配的成交数量
         都会立刻破坏它（kind: nav_position_identity_broken）。
       这两条使"长窗口下伪造净值与合法偏离不可区分"的旧论断失效：合法偏离
       **满足**恒等式，伪造净值**破坏**恒等式。
    3. **饱和标注**：白名单归类在长窗口仍会放宽（合法净值偏离实证 260 日
       2.0% / 1200 日 26% / 2500 日 47% / 4000 日 63% / 6000 日 74%），
       此时 ``saturated=True`` 且 ``attribution_note`` 非空——``unexplained``
       的**空白**不能再当作"引擎力学一致"的验收断言，须以恒等式与
       ``violations`` 为准。

    前置条件（务必如实，后的准确边界）：本函数假定两侧**除尾盘
    滑点外配置相同**（同一 profile/费率/计息），且成交清单与 nav 呈**逐笔
    对齐**形态（极端滑点下新侧权益衰减到买不起一手时会结构性错位——此时
    差异如实计入 `unexplained` 并标 `saturated=True`）。两类差异**不在**判别
    范围内：① 费率/计息等配置差异（恒等式两侧各自成立、白名单不覆盖）——
    应由 run_params 对账；② **自洽地重写** nav（equity 与 cash 同改）在长
    窗口下也会被算术吸收——长窗口的 `unexplained == []` 本来就只表示"归类
    没有超纲项"，恒等式负责抓"内部不一致的伪造"。

    白名单归类规则：
    - trade_mismatch：同日同向且价差幅度在尾盘滑点界限内 → tail_slippage。
      数量差在一手以内也归此类——更高的成交价降低购买力，整手取整传导为
      ±一手数量漂移（滑点参数的合法下游，R2VB B-2 集成断言的实证形态）；
    - count_mismatch：落点日带涨跌停卡 → limit_card；
    - NAV 逐点差异：相对差 ≤ 日计息界限（年化/252，2 倍容差）→
      cash_interest；超界 → unexplained（此前 NAV 逐点差异不参与判负，
      "超纲即失败"在 NAV 轴未接线——修复）；
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
    # 累计"额外成本"（**物理量**，不是上界）：新引擎相对旧引擎因尾盘滑点多付
    # /少收的现金 = Σ |P_new − P_old| × qty_old（买入多付、卖出少收同号）。
    #
    # 界的量纲（三轮审查收敛后的最终口径，实测驱动）：
    #   1. **现金项** `cum_extra_cost / price`：多花的钱能买多少股；
    #   2. **量化项** `lot_size × n_in_band`：整手取整的**随机游走**——真实的
    #      合法漂移不是连续量，而是"每轮最多差一手"的量化跳变（实测 260 日
    #      19 笔差异里 drift 恒为 1~2 手的整数倍、与已发生轮数同阶）。只算
    #      现金项会低估约 3×（每跳一手值 1 手 × 价，驱动它的现金差小得多）
    #      → R1 的"数量 1/2 硬帽"与 R2 的"纯现金项"都会把合法 run 判超纲
    #      （实测 260 日 7 笔 / 4000 日 294 笔假报警）。
    #   3. 现金项对**所有**同日同向、价差在带内的差异累积（不因数量检验失败
    #      而冻结——冻结会造成"一笔不归类 → 界的增速停摆 → 连环误杀"）。
    cum_extra_cost = 0.0
    n_in_band = 0
    for d in diff["trade_diffs"]:
        if d["kind"] == "trade_mismatch":
            nt, ot = d["new"], d["old"]
            try:
                ot_price = float(ot["price"])
                nt_price = float(nt["price"])
                ot_qty = int(ot["qty"])
                nt_qty = int(nt["qty"])
            except (TypeError, ValueError):
                # 形状异常（None/NaN/非数值）→ 显式判超纲，而不是抛异常穿出
                unexplained.append({**d, "kind": "trade_shape_invalid"})
                continue
            if nt["date"] == ot["date"] and nt["side"] == ot["side"] and ot_price > 0:
                slip_ratio = abs(nt_price / ot_price - 1.0)
                # 方向敏感：合法尾滑点只能更差（买价 ≥、卖价 ≤）；系统性"更有利"
                # 的价差判超纲（见 _PRICE_TICK_EPS 注）
                _drift = nt_price - ot_price
                _favorable = (
                    (nt["side"] == "BUY" and _drift < -_PRICE_TICK_EPS)
                    or (nt["side"] == "SELL" and _drift > _PRICE_TICK_EPS)
                )
                if not _favorable and 0 < slip_ratio <= max_tail_slippage:
                    ref_price = nt_price if nt_price > 0 else ot_price
                    lot = int(lot_size)
                    qty_bound = (
                        (int(cum_extra_cost / ref_price) + lot * max(1, n_in_band))
                        if ref_price > 0 else lot
                    )
                    n_in_band += 1
                    cum_extra_cost += abs(nt_price - ot_price) * abs(ot_qty)
                    if abs(nt_qty - ot_qty) <= qty_bound:
                        classified["tail_slippage"] += 1
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

    # ---- 精确恒等式校验（后的收口；不依赖任何启发式界）----
    # (a) 内部一致：每侧的 equity 必须等于 cash + 持仓市值。两个引擎都是这么算
    #     的，故合法 run 的残差是浮点级（~1e-16）；任何**伪造的净值**（无论
    #     窗口多长、偏离多大）都会立刻破坏它——这是"长窗口下无法判别"论断的
    #     反例：伪造净值与合法偏离在**恒等式**下完全不同。
    # (b) 仓位一致：新引擎的 positions_value 必须等于"新成交清单推出来的持仓
    #     量 × 当日收盘价（取自旧侧 nav，两侧同一市场价）"。伪造/错配的成交
    #     数量会立刻破坏它。
    # 两条都**只做判定不吸收**：破坏即 unexplained（同族判定见 tests）。
    identity_tol = 1e-9
    identity_equity = 0.0
    identity_checked_days = 0
    for side, rows, mv_key in (
        ("new", new_result.get("daily_nav") or [], "positions_value"),
        ("legacy", legacy_result.get("daily_nav") or [], "market_value"),
    ):
        for row in rows:
            try:
                eq_v = float(row["equity"])
                cash_v = float(row["cash"])
                mv_v = float(row[mv_key])
            except (KeyError, TypeError, ValueError):
                # 字段缺失（外部自制的精简 nav）→ **跳过**而不是判负：恒等式
                # 是"可用时的精确校验"，不是 schema 强制；可用性由
                # nav_identity_checked_days 如实报告。
                continue
            identity_checked_days += 1
            # NaN/inf 不可比较（`nan > tol` 恒为 False → 静默通过）：显式判负，
            # 否则"往 cash 里塞 NaN"可以绕过恒等式（实证）
            if not (math.isfinite(eq_v) and math.isfinite(cash_v) and math.isfinite(mv_v)):
                unexplained.append({
                    "kind": "nav_identity_nonfinite", "side": side,
                    "date": row.get("date"),
                })
                continue
            scale = max(abs(eq_v), abs(cash_v) + abs(mv_v), 1.0)
            resid = abs(eq_v - (cash_v + mv_v)) / scale
            identity_equity = max(identity_equity, resid)
            if resid > identity_tol:
                unexplained.append({
                    "kind": "nav_identity_broken", "side": side,
                    "date": row.get("date"), "residual": resid,
                })
    # (b) 新侧仓位一致（旧侧 nav 提供市场价；新侧持仓量由成交清单累加）
    qty_from_trades = 0
    trade_cursor = 0
    position_checked_days = 0
    _new_trades = new_result.get("trades") or []
    _old_nav = legacy_result.get("daily_nav") or []
    if _new_trades and _old_nav:
        close_by_day = {}
        for row in _old_nav:
            try:
                close_by_day[str(row.get("date"))[:10]] = float(row["close"])
            except (KeyError, TypeError, ValueError):
                continue
        for row in new_result.get("daily_nav") or []:
            day = row.get("date")
            while (
                trade_cursor < len(_new_trades)
                and str(_new_trades[trade_cursor].get("date"))[:10] == str(day)[:10]
            ):
                t = _new_trades[trade_cursor]
                try:
                    qty = int(t["qty"])
                except (KeyError, TypeError, ValueError):
                    qty = 0
                qty_from_trades += qty if t.get("side") == "BUY" else -qty
                trade_cursor += 1
            close_v = close_by_day.get(str(day)[:10])
            if close_v is None or not math.isfinite(close_v) or close_v <= 0:
                continue
            try:
                pv = float(row["positions_value"])
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(pv):
                continue
            position_checked_days += 1
            expected_pv = qty_from_trades * close_v
            scale = max(abs(pv), abs(expected_pv), 1.0)
            if abs(pv - expected_pv) / scale > identity_tol:
                unexplained.append({
                    "kind": "nav_position_identity_broken", "date": day,
                    "positions_value": pv, "expected": expected_pv,
                })
    daily_interest_bound = cash_interest_rate / 252.0
    # NAV 逐点差异的归类语境：若已有 trade 级白名单归类（如 tail_slippage），
    # NAV 路径漂移是其复利下游——同归该类；**零差异语境**（无任何 trade 级
    # 白名单命中）下超界 NAV 差异=超纲（如计息误加进持仓市值），必须判负。
    downstream_kind = "tail_slippage" if classified["tail_slippage"] > 0 else None
    _eqs = [float(r["equity"]) for r in new_result.get("daily_nav") or []
            if r.get("equity") is not None]
    avg_equity = (sum(_eqs) / len(_eqs)) if _eqs else 0.0
    drag_ratio = (cum_extra_cost / avg_equity) if avg_equity > 0 else 0.0
    # NAV 界 = 累计额外成本占权益的比例（放宽 2× 余量：持仓规模差的市值差与
    # 现金差同量级，实测合法偏离 ≤ drag_ratio）。**不加绝对天花板**——真实
    # 引擎实测（纯尾滑点、零卡控、零引擎逻辑差异）合法净值偏离随窗口增长到
    # 260 日 2.2% / 1200 日 26% / 2500 日 47% / 4000 日 63% / 6000 日 74%，
    # 任何绝对阈值都会把合法长 run 判成失败。代价：长窗口下归因不再有判别力
    # ——由 saturated 显式标注，并规定该情形改用 violations 验收。
    nav_cascade_bound = max(daily_interest_bound * 2.0, drag_ratio * 2.0)
    absorbed_max_rel = 0.0
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
            absorbed_max_rel = max(absorbed_max_rel, rel)
        else:
            unexplained.append({**nd, "kind": "nav_point_diff_beyond_interest"})
    # 判别力饱和标记（口径收口）：笔数一多，逐笔位置对齐退化、
    # 漂移本身可以很大（实测 4000 日合法净值偏离 63%）——此时
    # `unexplained == []` **不再等价于"引擎力学一致"**。凡是越出判别区间的
    # 归因结果都显式标注，避免把"解释不了"与"判别不了"混为一谈。
    n_trade_diffs = len(diff["trade_diffs"])
    # 判别力饱和的判据（R2 复核后**收紧**）：只要"归因还能吸收显著偏离"或
    # "差异笔数已超出 stage-1 验收尺度"，就判饱和——而不是等笔数上千。
    # 实测锚点：260 日 / 尾滑点 0.001（stage-1 验收尺度）合法净值偏离 2.0%、
    # 差异 19~28 笔（随种子波动）→ 不饱和，`unexplained == []` 可作验收断言。
    # 笔数阈值取 40（而非 20）以给 stage-1 尺度留出随种子波动的余量（
    # 20 的阈值在 24 个种子里有 4 个因 21~28 笔而误判饱和）。判定"不饱和"
    # 必须同时满足三条：笔数少、上界紧、且从未吸收过 >5% 的偏离。
    saturated = bool(
        n_trade_diffs > 40
        or nav_cascade_bound > 0.05
        or absorbed_max_rel > 0.05
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
        "nav_identity_residual": identity_equity,
        "nav_identity_checked_days": identity_checked_days,
        "nav_position_identity_checked_days": position_checked_days,
        "saturated": saturated,
        "attribution_note": (
            "判别力饱和：差异笔数/漂移幅度超出逐笔归因的判别区间，"
            "unexplained 为空**不代表**引擎力学一致；该场景请改用 violations "
            "作为验收判据（见 attribute_diffs.__doc__ 的适用区间）"
            if saturated else ""
        ),
    }
