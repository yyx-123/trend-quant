"""新旧引擎 parity 对拍（详设 §8 阶段 1 验收 + §9 parity 测试）。

同策略（MACD 金叉进 / 死叉出、无止损、全进全出）、同窗口、同费用参数，
两侧各跑一遍：

- 无涨跌停干预时：成交逐笔一致（日期/方向/数量/价格位级一致）、NAV 一致
  ——预期差异三类（涨跌停卡控/T+1/尾盘滑点）全部置零后，两侧必须重合；
- 尾盘滑点 slippage_tail > 0 时：买入价按 (1+base+tail)/(1+base) 比例抬升；
- 金叉日涨停卡控时：新引擎拒买（unfilled），旧引擎照成交——差异必须
  恰好落在卡控日。
"""

from __future__ import annotations

import pandas as pd
import pytest

from engine.matcher import TradabilityCard
from engine.parity import attribute_diffs, diff_against_legacy, run_macd_parity
from engine.profiles import MarketProfile
from rule_backtest import (
    BacktestExecutionConfig,
    RuleBacktestRequest,
    SingleSymbolAllInBacktestEngine,
)

pytestmark = pytest.mark.integration

PARITY_PROFILE = MarketProfile(
    name="parity_no_interest",     # 空仓计息是详设 §4.4 新引入口径（验收
    commission_rate=0.0000854,     # 白名单外的第四类显式差异），parity 对拍
    commission_min=5.0,            # 置零隔离引擎力学；计息正确性由
    stamp_tax_sell_stock=0.0005,   # test_engine_golden 单独锁定。
    stamp_tax_sell_etf=0.0,
    lot_size=100,
    t_plus=1,
    cash_interest_rate=0.0,
)

STRATEGY = {
    "id": "parity_macd",
    "trade_mode": "single_symbol_all_in",
    "entry": {
        "type": "group", "combinator": "all",
        "children": [{
            "id": "e1", "type": "condition",
            "left": {"type": "indicator", "name": "macd_line", "params": {}},
            "operator": "cross_above",
            "right": {"type": "indicator", "name": "macd_signal", "params": {}},
        }],
    },
    "exit": {
        "type": "group", "combinator": "any",
        "children": [{
            "id": "x1", "type": "condition",
            "left": {"type": "indicator", "name": "macd_line", "params": {}},
            "operator": "cross_below",
            "right": {"type": "indicator", "name": "macd_signal", "params": {}},
        }],
    },
}


def _bars(n_days: int = 260, seed: int = 11) -> pd.DataFrame:
    import numpy as np

    rng = np.random.default_rng(seed)
    closes = 10 + np.cumsum(rng.normal(0.015, 0.25, n_days))
    closes = np.maximum(closes, 1.0)
    dates = pd.bdate_range("2023-01-02", periods=n_days)
    return pd.DataFrame({
        "time": dates,
        "open": closes * (1 + rng.normal(0, 0.002, n_days)),
        "high": closes * 1.01,
        "low": closes * 0.99,
        "close": closes,
        "volume": np.full(n_days, 1e6),
        "amount": closes * 1e6,
    })


def _run_legacy(bars, slippage=0.002):
    engine = SingleSymbolAllInBacktestEngine()
    request = RuleBacktestRequest(
        strategy=STRATEGY, symbol="PARITY.SS", bars=bars,
        execution=BacktestExecutionConfig(
            initial_capital=100_000.0, slippage=slippage, instrument_type="etf",
        ),
    )
    return engine.run(request)


def _run_new(bars, slippage_tail=0.0, cards=None):
    return run_macd_parity(
        bars, initial_capital=100_000.0, slippage_base=0.002,
        slippage_tail=slippage_tail, asset_type="etf",
        profile=PARITY_PROFILE, cards=cards,
    )


def test_parity_identical_when_all_deltas_off():
    bars = _bars()
    legacy = _run_legacy(bars)
    new = _run_new(bars)
    assert len(new["trades"]) >= 4, "合成数据应产生若干往返，否则对拍无意义"
    diff = diff_against_legacy(new, legacy)
    assert diff["trades_new"] == diff["trades_old"]
    assert diff["trade_diffs"] == []
    assert diff["nav_divergence"] == []
    # 阶段 1 验收机器判据接线（DS-P2-9）：差异归因白名单——无差异时白名单自然为空
    report = attribute_diffs(new, legacy)
    assert report["violations"] == []


def test_parity_tail_slippage_is_the_only_delta():
    bars = _bars()
    legacy = _run_legacy(bars)
    new = _run_new(bars, slippage_tail=0.001)
    diff = diff_against_legacy(new, legacy)
    assert diff["trades_new"] == diff["trades_old"]
    # 日期/方向序列逐笔一致（价格按固定比例偏移；后续买入数量可差一手——
    # 更高的成交价改变购买力，这是滑点参数的合法传导，不是口径差异）
    new_ts = new["trades"]
    old_ts = legacy["trades"]
    for new_t, old_t in zip(new_ts, old_ts):
        assert new_t["date"] == pd.Timestamp(old_t["date"]).date()
        assert new_t["side"] == old_t["side"]
        if new_t["side"] == "BUY":
            assert new_t["price"] == pytest.approx(
                float(old_t["exec_price"]) * 1.003 / 1.002, rel=1e-9
            )
        else:
            assert new_t["price"] == pytest.approx(
                float(old_t["exec_price"]) * 0.997 / 0.998, rel=1e-9
            )


def test_parity_limit_up_card_blocks_buy():
    bars = _bars()
    legacy = _run_legacy(bars)
    first_buy_day = pd.Timestamp(legacy["trades"][0]["date"]).date()
    cards = {first_buy_day: TradabilityCard(is_limit_up=True)}
    new = _run_new(bars, cards=cards)

    new_buy_days = [t["date"] for t in new["trades"] if t["side"] == "BUY"]
    old_buy_days = [pd.Timestamp(t["date"]).date() for t in legacy["trades"] if t["side"] == "BUY"]
    # 涨停日拒买：新引擎的首笔买入不在被卡控的那一天（顺延到下一个金叉）
    assert first_buy_day not in new_buy_days
    assert first_buy_day in old_buy_days
    # 归因白名单：所有差异必须落在 limit_card 类，超纲即失败
    # 机器判据：卡控日零违规成交（涨停日无买/跌停日无卖）
    report = attribute_diffs(new, legacy, cards=cards)
    assert report["violations"] == []
