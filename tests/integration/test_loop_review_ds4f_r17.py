"""Round 17 修复钉子。

- R17A-F1：`match_buy` 的 quantity 路径在现金递减后未复检最小申报（已提交态存在）
  → 收紧断言并在 r16 钉子里补精确用例。
- R17A-F2：旧栈 `rule_backtest` 是**第二个下单入口**且买入定量 symbol-blind
  → 生产库 336 笔 100 股科创板"成交"（5 只标的）。本文件钉住旧栈的最小申报判定。
"""

from __future__ import annotations


def test_legacy_engine_respects_star_min_order():
    """R17A-F2：旧栈的买入定量也必须分品种（科创板 200 股）。

    旧实现 `_max_buy_qty(cash, reference_price, execution)` 看不到标的代码，
    只按 lot_size（100）对齐 → 产出必被拒的 100 股科创板委托并记账成交。
    """
    from rule_backtest import BacktestExecutionConfig
    from rule_backtest.engine import SKIP_BELOW_MIN_ORDER, SingleSymbolAllInBacktestEngine

    eng = SingleSymbolAllInBacktestEngine()
    cfg = BacktestExecutionConfig(
        initial_capital=100_000.0, slippage=0.002, instrument_type="stock",
    )
    # 现金只够 ~100 股：科创板（688）→ 不可下；主板（600）→ 照常 100 股
    qty, skip = eng._resolve_buy_qty(
        cash=1_008.0, reference_price=10.0, day_str="2026-06-16", execution=cfg,
        symbol="688802.SS", asset_type="stock",
    )
    assert qty == 0, "科创板现金只够 100 股时不得下单"
    assert skip and skip["reason"] == SKIP_BELOW_MIN_ORDER, skip
    qty_lot, skip_lot = eng._resolve_buy_qty(
        cash=1_008.0, reference_price=10.0, day_str="2026-06-16", execution=cfg,
        symbol="600519.SS", asset_type="stock",
    )
    assert qty_lot == 100 and skip_lot is None, (qty_lot, skip_lot)
    # 科创板现金够 200 股以上 → 正常下单且数量 ≥200
    qty_ok, skip_ok = eng._resolve_buy_qty(
        cash=3_000.0, reference_price=10.0, day_str="2026-06-16", execution=cfg,
        symbol="688802.SS", asset_type="stock",
    )
    assert qty_ok >= 200 and skip_ok is None, (qty_ok, skip_ok)
    # 元数据缺失（symbol=None）时按代码兜底：仍按 200 判定
    qty_na, skip_na = eng._resolve_buy_qty(
        cash=1_008.0, reference_price=10.0, day_str="2026-06-16", execution=cfg,
        symbol="688802.SS", asset_type=None,
    )
    assert qty_na == 0 and skip_na and skip_na["reason"] == SKIP_BELOW_MIN_ORDER
