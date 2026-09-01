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

    def test_total_return_includes_buy_day_gain(self, bull_db) -> None:
        """口径回归：累计收益率以买入价为净值 1.0 起算（含买入日收益），
        而非从买入日收盘价起算（旧口径漏算买入日，2026-08 用户反馈）。"""
        db, bars = bull_db
        row = bars.iloc[-3]
        buy_date = str(row["time"])[:10]
        # 买入价刻意低于当日收盘 → 买入日即有浮盈，新旧口径必然不同
        buy_price = round((float(row["low"]) + float(row["close"])) / 2, 4)

        out = mt.compute_manual_trade("510300.SS", buy_date, buy_price, db=db)

        latest_close = float(bars.iloc[-1]["close"])
        first_close = float(row["close"])
        holding = out["holding"]
        # 新口径：最新收盘 / 买入价 − 1（含买入日收益）
        assert holding["total_return"] == pytest.approx(
            round((latest_close / buy_price - 1) * 100, 2)
        )
        # 旧口径（最新收盘 / 买入日收盘 − 1）应不再成立
        assert holding["total_return"] != pytest.approx(
            round((latest_close / first_close - 1) * 100, 2)
        )
        # 日收益样本数 = 持有交易日数（买入日收益 + 之后每日）
        assert holding["n_returns"] == holding["hold_days"]
        # 悬停明细字段
        assert holding["highest_since_buy"] > 0
        assert holding["highest_since_buy_date"] is not None
        assert isinstance(holding["mean_daily_return"], float)
        assert isinstance(holding["std_daily_return"], float)
        assert isinstance(holding["downside_std"], float)

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
        # 首次击穿日价格明细：最低价 ≤ 硬止损价，且与当日K线一致
        assert stops["hard_stop_trigger_low"] is not None
        assert stops["hard_stop_trigger_low"] <= stops["hard_stop_price"]
        trigger_day = bars[pd.to_datetime(bars["time"]).dt.date.astype(str) == stops["hard_stop_trigger_date"]]
        assert stops["hard_stop_trigger_low"] == pytest.approx(round(float(trigger_day.iloc[0]["low"]), 4))
        assert stops["hard_stop_trigger_close"] == pytest.approx(round(float(trigger_day.iloc[0]["close"]), 4))
        assert out["holding"]["pnl_pct"] < 0
        # 最大回撤峰/谷明细：买入即净值峰值 1.0，之后持续回撤
        holding = out["holding"]
        assert holding["max_drawdown"] < 0
        assert holding["max_dd_peak_date"] == buy_date
        assert holding["max_dd_peak_equity"] == pytest.approx(1.0)
        assert holding["max_dd_trough_date"] is not None
        assert holding["max_dd_trough_equity"] < holding["max_dd_peak_equity"]

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
        # 盘中击穿：明细取自合成K线
        assert out["stops"]["hard_stop_trigger_low"] == pytest.approx(round(synth["low"], 4))
        assert out["stops"]["hard_stop_trigger_close"] == pytest.approx(round(synth["close"], 4))
        # EOD 口径下尚未触发
        assert eod["stops"]["hard_stop_triggered"] is False

    def test_daily_change_pct_eod(self, bull_db) -> None:
        """当日涨跌幅（EOD 口径）= 最新收盘 / 前一交易日收盘 − 1。"""
        db, bars = bull_db
        row = bars.iloc[-3]
        buy_price = round((float(row["low"]) + float(row["close"])) / 2, 4)

        out = mt.compute_manual_trade("510300.SS", str(row["time"])[:10], buy_price, db=db)

        last_close = float(bars.iloc[-1]["close"])
        prev_close = float(bars.iloc[-2]["close"])
        assert out["prev_close"] == pytest.approx(round(prev_close, 4))
        assert out["daily_change_pct"] == pytest.approx(
            round((last_close / prev_close - 1) * 100, 2)
        )

    def test_daily_change_pct_intraday_uses_realtime(self, bull_db, monkeypatch) -> None:
        """盘中口径：最新价为实时价，前一交易日收盘 = 最后一根 EOD 收盘。"""
        db, bars = bull_db
        row = bars.iloc[-3]
        buy_price = round((float(row["low"]) + float(row["close"])) / 2, 4)
        last_eod_close = float(bars.iloc[-1]["close"])
        synth_close = last_eod_close * 1.05
        monkeypatch.setattr(
            sl,
            "_fetch_intraday_bar",
            lambda symbol, df: {
                "time": pd.Timestamp("2025-03-03 10:30:00"),  # 晚于 bull bars 末日
                "open": last_eod_close,
                "high": synth_close,
                "low": last_eod_close * 0.99,
                "close": synth_close,
                "volume": 0.0,
                "amount": 0.0,
            },
        )

        out = mt.compute_manual_trade("510300.SS", str(row["time"])[:10], buy_price, db=db)

        assert out["is_intraday"] is True
        assert out["prev_close"] == pytest.approx(round(last_eod_close, 4))
        assert out["daily_change_pct"] == pytest.approx(
            round((synth_close / last_eod_close - 1) * 100, 2)
        )
        assert out["daily_change_pct"] > 0

    def test_buy_date_today_before_daily_bar_persisted(self, bull_db, monkeypatch) -> None:
        """当日试算（日K未落库）：DB 无当日K线时用实时报价合成的当日K线补齐，
        买入价按当日最新最高/最低价校验，ATR 取前一交易日口径。"""
        db, bars = bull_db
        last_close = float(bars.iloc[-1]["close"])
        synth = {
            "time": pd.Timestamp("2025-03-03 10:30:00"),  # 晚于 bull bars 末日
            "open": last_close,
            "high": last_close * 1.02,
            "low": last_close * 0.98,
            "close": last_close * 1.01,
            "volume": 0.0,
            "amount": 0.0,
        }
        monkeypatch.setattr(sl, "_fetch_intraday_bar", lambda symbol, df: synth)
        buy_price = round(last_close * 1.005, 4)  # 落在当日 [low, high] 内

        out = mt.compute_manual_trade("510300.SS", "2025-03-03", buy_price, db=db)

        assert out["is_intraday"] is True
        assert out["start_date"] == "2025-03-03"
        assert out["latest_date"] == "2025-03-03"
        assert out["holding"]["hold_days"] == 1
        assert out["holding"]["pnl_points"] == pytest.approx(round(synth["close"] - buy_price, 4))
        # ATR 取前一交易日（当日 20 日 ATR 未定型，不含当日不完整K线）
        assert out["stops"]["atr_at_buy"] == pytest.approx(out["stops"]["current_atr"])

    def test_buy_date_today_price_out_of_intraday_range(self, bull_db, monkeypatch) -> None:
        """当日试算：买入价超出当日实时最高/最低价区间 → 报错。"""
        db, bars = bull_db
        last_close = float(bars.iloc[-1]["close"])
        monkeypatch.setattr(
            sl,
            "_fetch_intraday_bar",
            lambda symbol, df: {
                "time": pd.Timestamp("2025-03-03 10:30:00"),
                "open": last_close,
                "high": last_close * 1.02,
                "low": last_close * 0.98,
                "close": last_close * 1.01,
                "volume": 0.0,
                "amount": 0.0,
            },
        )
        with pytest.raises(sl.StopLossError, match="当日价格区间"):
            mt.compute_manual_trade(
                "510300.SS", "2025-03-03", round(last_close * 1.05, 4), db=db
            )


class TestPositionSizing:
    def test_floor_to_hundreds(self) -> None:
        """可买份数下取整到百位：12345.67 份 → 12300 份。"""
        ps = mt.compute_position_sizing(buy_price=2.0, hard_stop_price=1.0, risk_budget=12345.67)
        assert ps["risk_per_share"] == pytest.approx(1.0)
        assert ps["max_qty"] == 12300
        assert ps["max_loss"] == pytest.approx(12300.0)
        assert ps["position_value"] == pytest.approx(24600.0)
        # 下取整保证硬止损触发损失不超过预算
        assert ps["max_loss"] <= 12345.67

    def test_fractional_shares_dropped(self) -> None:
        ps = mt.compute_position_sizing(buy_price=4.025, hard_stop_price=3.95, risk_budget=10000)
        assert ps["risk_per_share"] == pytest.approx(0.075)
        assert ps["max_qty"] == 133300  # 10000 / 0.075 = 133333.33 → 133300
        assert ps["max_qty"] % 100 == 0

    def test_tiny_budget_rounds_to_zero(self) -> None:
        ps = mt.compute_position_sizing(buy_price=10.0, hard_stop_price=9.0, risk_budget=50)
        assert ps["max_qty"] == 0
        assert ps["max_loss"] == 0

    def test_sizing_in_compute_manual_trade(self, bull_db) -> None:
        db, bars = bull_db
        row = bars.iloc[-3]
        buy_date = str(row["time"])[:10]
        buy_price = round((float(row["low"]) + float(row["close"])) / 2, 4)

        out = mt.compute_manual_trade("510300.SS", buy_date, buy_price, db=db, risk_budget=10000)

        ps = out["position_sizing"]
        hard_stop = out["stops"]["hard_stop_price"]
        assert ps["risk_per_share"] == pytest.approx(round(buy_price - hard_stop, 4))
        assert ps["max_qty"] % 100 == 0
        assert ps["max_qty"] * ps["risk_per_share"] <= 10000
        assert ps["max_loss"] == pytest.approx(round(ps["max_qty"] * ps["risk_per_share"], 2))
        assert ps["position_value"] == pytest.approx(round(ps["max_qty"] * buy_price, 2))

    def test_no_risk_budget_no_sizing(self, bull_db) -> None:
        db, bars = bull_db
        row = bars.iloc[-3]
        buy_price = round((float(row["low"]) + float(row["close"])) / 2, 4)
        out = mt.compute_manual_trade("510300.SS", str(row["time"])[:10], buy_price, db=db)
        assert "position_sizing" not in out


class TestTriggerExcludesBuyDay:
    """止损触发检测排除买入当天（2026-09 用户反馈：8.27 临收盘买入伊利，
    当天盘中低点 25.72 ≤ 硬止损 25.7533 被误报「曾跌穿」——买入前的价格
    行为不可能触发止损）。日K 粒度无法区分当天低点在买入前/后，与回测
    「入场次日才评估止损」口径一致，买入日一律不参与触发判定。"""

    def test_buy_day_low_below_hard_stop_not_triggered(self, test_db) -> None:
        from conftest import make_bull_bars

        bars = make_bull_bars(40)
        buy_idx = len(bars) - 3
        buy_date = str(bars.iloc[buy_idx]["time"])[:10]
        buy_price = round(float(bars.iloc[buy_idx]["close"]), 4)
        # 把买入日最低价砸到远低于硬止损（模拟买入前的盘中下探）；
        # 买入日之后的 K 线保持平静上涨，最低价始终高于硬止损。
        bars.loc[buy_idx, "low"] = round(buy_price * 0.90, 4)
        test_db.save_market_data("510300.SS", bars, price_mode="qfq")

        out = mt.compute_manual_trade("510300.SS", buy_date, buy_price, db=test_db)

        stops = out["stops"]
        # 前置条件：买入日最低价确实低于硬止损（旧逻辑会误报触发）
        assert float(bars.iloc[buy_idx]["low"]) <= stops["hard_stop_price"]
        # 新逻辑：买入日不参与 → 未触发
        assert stops["hard_stop_triggered"] is False
        assert stops["hard_stop_trigger_date"] is None
        # 止损价本身不变（仍含买入日 ATR 口径）
        assert stops["hard_stop_price"] > 0

    def test_next_day_low_below_hard_stop_still_triggered(self, test_db) -> None:
        """对照：买入日**之后**击穿仍然正常报触发。"""
        from conftest import make_bull_bars

        bars = make_bull_bars(40)
        buy_idx = len(bars) - 3
        buy_date = str(bars.iloc[buy_idx]["time"])[:10]
        buy_price = round(float(bars.iloc[buy_idx]["close"]), 4)
        # 买入次日砸穿，买入日保持平静
        bars.loc[buy_idx + 1, "low"] = round(buy_price * 0.90, 4)
        test_db.save_market_data("510300.SS", bars, price_mode="qfq")

        out = mt.compute_manual_trade("510300.SS", buy_date, buy_price, db=test_db)

        stops = out["stops"]
        assert stops["hard_stop_triggered"] is True
        assert stops["hard_stop_trigger_date"] == str(bars.iloc[buy_idx + 1]["time"])[:10]

    def test_intraday_bar_on_buy_day_not_triggered(self, bull_db, monkeypatch) -> None:
        """买入日=今天：实时合成K线的低点（可能发生在买入前）不触发。"""
        db, bars = bull_db
        last_close = float(bars.iloc[-1]["close"])
        buy_price = round(last_close * 1.005, 4)
        # 合成K线低点远低于硬止损（买入前的盘中下探）
        synth = {
            "time": pd.Timestamp("2025-03-03 10:30:00"),
            "open": last_close,
            "high": last_close * 1.02,
            "low": round(last_close * 0.90, 4),
            "close": last_close * 1.01,
            "volume": 0.0,
            "amount": 0.0,
        }
        monkeypatch.setattr(sl, "_fetch_intraday_bar", lambda symbol, df: synth)

        out = mt.compute_manual_trade("510300.SS", "2025-03-03", buy_price, db=db)

        stops = out["stops"]
        assert synth["low"] <= stops["hard_stop_price"]  # 前置：旧逻辑会误报
        assert stops["hard_stop_triggered"] is False
        assert stops["hard_stop_trigger_date"] is None
