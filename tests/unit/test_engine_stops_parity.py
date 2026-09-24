"""止损收编口径对拍（决策 6）：新栈 stops 公式 vs 实盘侧 services/stop_loss.py
同参数序列——回测侧向实盘侧对齐的回归锁。

对拍点：
- 硬止损 = 实际买入价 − mul × ATR(20, 含当根)——两侧同值；
- 吊灯初始化 = max(买入日 high, 买入价) − mul × ATR(含当根)——两侧同值；
- 吊灯逐日判定用 T-1 状态（日内路径口径，详设 §4.3 写死）：
  新栈 day-T 判定值 == 实盘侧以 end_date=T-1 截断计算的值。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.indicators import atr as core_atr
from engine import stops
from services.stop_loss import compute_stop_loss

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _default_strategy_config(monkeypatch):
    """与生产库解耦（评审 C-P1-4）：stop_loss 的策略配置不读全局 DB——
    打桩为代码默认值（硬止损 1.5 / 吊灯 2.5），干净机器上也不会创建生产库。"""
    monkeypatch.setattr(
        "services.stop_loss.get_strategy_config",
        lambda: {"hard_stop_atr_mul_default": 1.5, "chandelier_stop_atr_mul": 2.5},
    )


def _make_df(n: int = 80, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    closes = 10 + np.cumsum(rng.normal(0.03, 0.15, n))
    highs = closes + np.abs(rng.normal(0.05, 0.05, n))
    lows = closes - np.abs(rng.normal(0.05, 0.05, n))
    dates = pd.bdate_range("2024-01-01", periods=n)
    return pd.DataFrame(
        {"time": dates, "open": closes, "high": highs, "low": lows, "close": closes,
         "volume": np.full(n, 1e6), "amount": closes * 1e6}
    )


def _live_side(df: pd.DataFrame, buy_date: str, buy_price: float, end_date: str | None, db):
    atr_series = pd.Series(core_atr(df, 20).to_numpy(), index=pd.to_datetime(df["time"]))
    return compute_stop_loss(
        "510300.SS", buy_date, buy_price,
        db=db, df=df, atr_series=atr_series, intraday=False, end_date=end_date,
        intraday_bar=None, validate_price=False,
    )


def test_hard_stop_matches_live_side(test_db):
    df = _make_df()
    buy_date = str(df["time"].iloc[40].date())
    buy_price = float(df["close"].iloc[40])

    live = _live_side(df, buy_date, buy_price, None, test_db)
    # 新栈：入场日 ATR 含当根
    through_entry = df[df["time"] <= pd.Timestamp(buy_date)]
    atr_incl = stops.atr_at(through_entry, period=20)
    state = stops.init_hard_stop(
        entry_price=buy_price, atr_including_entry=atr_incl, atr_mul=1.5
    )
    assert live["atr_at_buy"] == pytest.approx(atr_incl, abs=1e-4)
    assert state.stop_price == pytest.approx(live["hard_stop_price"], rel=1e-6, abs=1e-4)


def test_chandelier_init_matches_live_side(test_db):
    df = _make_df()
    buy_date = str(df["time"].iloc[40].date())
    buy_price = float(df["close"].iloc[40])
    entry_high = float(df["high"].iloc[40])

    live = _live_side(df, buy_date, buy_price, buy_date, test_db)  # end_date=buy_date = 入场当日口径
    through_entry = df[df["time"] <= pd.Timestamp(buy_date)]
    atr_incl = stops.atr_at(through_entry, period=20)
    state = stops.init_chandelier(
        entry_price=buy_price, entry_day_high=entry_high,
        atr_including_entry=atr_incl, atr_mul=2.5,
    )
    assert state.highest_since_buy == pytest.approx(max(entry_high, buy_price))
    assert state.stop_price == pytest.approx(live["chandelier_stop_price"], rel=1e-6, abs=1e-4)


def test_chandelier_daily_uses_t_minus_1_state(test_db):
    """日内路径口径：T 日判定价 == 实盘侧截断到 T-1 的计算值。"""
    df = _make_df()
    buy_idx = 40
    judge_idx = 50  # T 日
    buy_date = str(df["time"].iloc[buy_idx].date())
    buy_price = float(df["close"].iloc[buy_idx])

    through_entry = df.iloc[: buy_idx + 1]
    state = stops.init_chandelier(
        entry_price=buy_price, entry_day_high=float(df["high"].iloc[buy_idx]),
        atr_including_entry=stops.atr_at(through_entry, 20), atr_mul=2.5,
    )
    # 逐日并入 high（buy_idx+1 .. judge_idx-1），T-1 状态就位
    for i in range(buy_idx + 1, judge_idx):
        stops.post_day_update(state, day_high=float(df["high"].iloc[i]))

    through_yesterday = df.iloc[:judge_idx]  # 截至 T-1
    new_side = stops.daily_chandelier_stop(
        state, atr_through_yesterday=stops.atr_at(through_yesterday, 20), atr_mul=2.5
    )
    live = _live_side(df, buy_date, buy_price, str(df["time"].iloc[judge_idx - 1].date()), test_db)
    assert new_side == pytest.approx(live["chandelier_stop_price"], rel=1e-6, abs=1e-4)


def test_ratchet_only_moves_up():
    state = stops.StopState(stop_price=9.0, highest_since_buy=10.0, atr_at_entry=0.5,
                            module_state={"ratchet": True})
    # 候选更低 → 保持 9.0
    assert stops.daily_chandelier_stop(state, atr_through_yesterday=0.8, atr_mul=2.5) == 9.0
    # 候选更高 → 上移
    state.highest_since_buy = 12.0
    assert stops.daily_chandelier_stop(state, atr_through_yesterday=0.8, atr_mul=2.5) == 10.0


def test_breakeven_activation():
    state = stops.init_breakeven(entry_price=10.0, entry_day_high=10.0,
                                 atr_including_entry=0.5, trigger_atr=1.0)
    # 浮盈未达 1×ATR → 无触发价
    state.highest_since_buy = 10.4
    assert stops.daily_breakeven_stop(state, entry_price=10.0) is None
    # 浮盈 > 1×ATR → 激活，触发价 = 买入价
    state.highest_since_buy = 10.6
    assert stops.daily_breakeven_stop(state, entry_price=10.0) == 10.0
    assert state.module_state["activated"] is True


def test_ma_and_donchian_helpers():
    df = _make_df()
    ma = stops.ma_value(df, 20)
    assert ma == pytest.approx(float(df["close"].tail(20).mean()))
    dl = stops.donchian_low(df, 10)
    assert dl == pytest.approx(float(df["low"].tail(10).min()))
