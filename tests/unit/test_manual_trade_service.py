"""Unit tests for services.manual_trade (手工交易页面的持仓指标聚合).

止损价本身的测试见 test_stop_loss.py；这里只覆盖聚合层：
持仓指标（持有天数 / 点数 / 回撤 / 夏普等）、止损触发检测与边界。
"""

from __future__ import annotations

import pandas as pd
import pytest

from core.strategy_config import DEFAULT_STRATEGY_CONFIG
from services import manual_trade as mt
from services import stop_loss as sl


@pytest.fixture(autouse=True)
def _default_strategy_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin strategy config to code defaults (global DB may be uninitialized)."""
    monkeypatch.setattr(sl, "get_strategy_config", lambda: dict(DEFAULT_STRATEGY_CONFIG))
    # 默认走纯 EOD 路径，避免测试在交易时段访问实时行情；
    # 盘中行为由 test_intraday_overlay_extends_nav_and_trigger 显式注入。
    monkeypatch.setattr(sl, "_fetch_intraday_bar", lambda symbol, df: None)


@pytest.fixture
def bull_db(test_db):
    from conftest import make_bull_bars

    bars = make_bull_bars(40)
    test_db.save_market_data("510300.SS", bars, price_mode="qfq")
    return test_db, bars


class TestComputeManualTrade:
    def test_holding_metrics(self, bull_db) -> None:
        db, bars = bull_db
        row = bars.iloc[-3]
        buy_date = str(row["time"])[:10]
        # 买入价刻意偏离当日收盘价 —— 手工买入通常不是收盘价成交；
        # 但须落在当日 [low, high] 区间内（买入价合理性校验）
        buy_price = round((float(row["low"]) + float(row["close"])) / 2, 4)

        out = mt.compute_manual_trade("510300", buy_date, buy_price, db=db)

        assert out["symbol"] == "510300.SS"
        buy_ts = pd.Timestamp(buy_date)
        since = bars[pd.to_datetime(bars["time"]) >= buy_ts]
        latest_close = float(bars.iloc[-1]["close"])

        holding = out["holding"]
        assert holding["hold_days"] == len(since)
        assert holding["pnl_points"] == pytest.approx(round(latest_close - buy_price, 4))
        # 牛市数据：收益为正、回撤 ≤ 0、夏普字段存在
        assert holding["pnl_pct"] > 0
        assert holding["max_drawdown"] <= 0
        assert isinstance(holding["sharpe"], float)
        assert isinstance(holding["sortino"], float)
        assert isinstance(holding["calmar"], float)

        stops = out["stops"]
        assert stops["hard_stop_triggered"] is False
        assert stops["chandelier_stop_triggered"] is False
        assert stops["hard_stop_distance_pct"] > 0
        assert out["start_date"] == str(pd.Timestamp(since.iloc[0]["time"]).date())
        assert out["latest_date"] == str(pd.Timestamp(bars.iloc[-1]["time"]).date())

    def test_hard_stop_triggered_in_downtrend(self, test_db) -> None:
        from conftest import make_bear_bars

        bars = make_bear_bars(40)
        test_db.save_market_data("510500.SS", bars, price_mode="qfq")
        buy_date = str(bars.iloc[0]["time"])[:10]
        buy_price = float(bars.iloc[0]["close"])

        out = mt.compute_manual_trade("510500.SS", buy_date, buy_price, db=test_db)

        stops = out["stops"]
        # 持续阴跌：最低价必然击穿硬止损
        assert stops["hard_stop_triggered"] is True
        assert stops["hard_stop_trigger_date"] is not None
        assert out["holding"]["pnl_pct"] < 0

    def test_buy_date_after_latest_raises(self, bull_db) -> None:
        db, _ = bull_db
        with pytest.raises(mt.ManualTradeError, match="晚于最新数据"):
            mt.compute_manual_trade("510300.SS", "2099-01-01", 1.0, db=db)

    def test_no_data_raises_stop_loss_error(self, test_db) -> None:
        # 底层 stop_loss 的 StopLossError 会穿透聚合层（ManualTradeError 是其子类，
        # 路由层统一按 StopLossError 捕获）。
        with pytest.raises(sl.StopLossError, match="未找到"):
            mt.compute_manual_trade("999999.SS", "2025-01-10", 1.0, db=test_db)

    def test_intraday_overlay_extends_nav_and_trigger(self, bull_db, monkeypatch) -> None:
        """盘中叠加：当日合成K线计入净值序列、盈亏与硬止损触发。"""
        db, bars = bull_db
        row = bars.iloc[-3]
        buy_date = str(row["time"])[:10]
        buy_price = round((float(row["low"]) + float(row["close"])) / 2, 4)
        eod = mt.compute_manual_trade("510300.SS", buy_date, buy_price, db=db)
        assert eod["is_intraday"] is False

        synth_close = float(bars.iloc[-1]["close"]) * 1.05
        synth = {
            "time": pd.Timestamp("2025-03-03 10:30:00"),  # 晚于 bull bars 末日
            "open": synth_close,
            "high": synth_close * 1.01,
            "low": eod["stops"]["hard_stop_price"] * 0.99,  # 盘中击穿硬止损
            "close": synth_close,
            "volume": 0.0,
            "amount": 0.0,
        }
        monkeypatch.setattr(sl, "_fetch_intraday_bar", lambda symbol, df: synth)

        out = mt.compute_manual_trade("510300.SS", buy_date, buy_price, db=db)

        assert out["is_intraday"] is True
        assert out["intraday_ts"] is not None
        assert out["latest_date"] == "2025-03-03"
        assert out["holding"]["hold_days"] == eod["holding"]["hold_days"] + 1
        assert out["holding"]["pnl_points"] == pytest.approx(round(synth_close - buy_price, 4))
        assert out["stops"]["hard_stop_triggered"] is True
        assert out["stops"]["hard_stop_trigger_date"] == "2025-03-03"
        # EOD 口径下尚未触发
        assert eod["stops"]["hard_stop_triggered"] is False
