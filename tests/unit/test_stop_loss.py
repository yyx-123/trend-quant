"""Unit tests for services.stop_loss (止损价计算的单一实现来源).

覆盖：
- 硬止损以「买入价 − 1.5×ATR(买入日)」计算，而非买入当日收盘价
- 吊灯止损公式与 per-instrument stop_atr_mul 覆盖
- 边界：无数据、非交易日、无效输入
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from core.strategy_config import DEFAULT_STRATEGY_CONFIG
from data.indicator_store import compute_live_series
from services import stop_loss as sl


@pytest.fixture(autouse=True)
def _default_strategy_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin strategy config to code defaults (global DB may be uninitialized)."""
    monkeypatch.setattr(sl, "get_strategy_config", lambda: dict(DEFAULT_STRATEGY_CONFIG))
    # 默认走纯 EOD 路径，避免测试在交易时段访问实时行情；
    # 盘中行为由 TestComputeStopLossIntraday 显式注入合成K线验证。
    monkeypatch.setattr(sl, "_fetch_intraday_bar", lambda symbol, df: None)


@pytest.fixture
def bull_db(test_db):
    from conftest import make_bull_bars

    bars = make_bull_bars(40)
    test_db.save_market_data("510300.SS", bars, price_mode="qfq")
    return test_db, bars


def _buy_inputs(bars: pd.DataFrame, idx: int = -3) -> tuple[str, float]:
    row = bars.iloc[idx]
    buy_date = str(row["time"])[:10]
    # 买入价刻意偏离当日收盘价 —— 手工买入通常不是收盘价成交；
    # 但必须落在当日 [low, high] 区间内（买入价合理性校验）。
    buy_price = round((float(row["low"]) + float(row["close"])) / 2, 4)
    return buy_date, buy_price


def make_atr_expansion_bars() -> pd.DataFrame:
    """25 个平静上涨日后接 5 个振幅剧增但不创新高的日子。

    ATR 急剧扩张而最高价不变 —— 普通吊灯止损会被拉低，
    棘轮版应保持历史最高候选值不变。
    """
    base = date(2025, 1, 6)  # Monday
    records: list[dict] = []
    price = 10.0
    offset = 0
    made = 0
    while made < 30:
        day = base + timedelta(days=offset)
        offset += 1
        if day.weekday() >= 5:
            continue
        made += 1
        if made <= 25:
            close = price * 1.01
            high = close * 1.002
            low = close * 0.998
        else:
            close = price * 0.95
            high = price * 1.01  # 不创新高
            low = price * 0.85
        records.append(
            {
                "time": day.isoformat(),
                "open": round(price, 4),
                "high": round(float(high), 4),
                "low": round(float(low), 4),
                "close": round(float(close), 4),
                "volume": 1_000_000,
            }
        )
        price = close
    return pd.DataFrame(records)


def expected_ratchet(bars: pd.DataFrame, buy_date: str, atr_mul: float = 2.5) -> float:
    """独立重算棘轮价：逐日 候选=截至当日最高价 − mul×当日ATR 的历史最大值。"""
    atr_series = compute_live_series(bars, "atr")
    atr_daily = atr_series.copy()
    atr_daily.index = pd.DatetimeIndex(pd.to_datetime(atr_daily.index))
    buy_ts = pd.Timestamp(buy_date)
    since = bars[pd.to_datetime(bars["time"]) >= buy_ts]
    dates = pd.DatetimeIndex(pd.to_datetime(since["time"]))
    atr_aligned = atr_daily.reindex(dates).ffill()
    running_high = pd.to_numeric(since["high"], errors="coerce").cummax()
    candidates = running_high.to_numpy(dtype=float) - atr_mul * atr_aligned.to_numpy(dtype=float)
    candidates = candidates[pd.notna(candidates)]
    return round(float(candidates.max()), 4)


class TestComputeStopLoss:
    def test_hard_stop_uses_buy_price_not_close(self, bull_db) -> None:
        db, bars = bull_db
        buy_date, buy_price = _buy_inputs(bars)

        out = sl.compute_stop_loss("510300", buy_date, buy_price, db=db)

        atr_series = compute_live_series(bars, "atr")
        atr_at_buy = float(atr_series[atr_series.index <= pd.Timestamp(buy_date)].iloc[-1])
        expected = round(buy_price - 1.5 * atr_at_buy, 4)
        assert out["hard_stop_price"] == pytest.approx(expected)
        assert out["hard_stop_atr_mul"] == 1.5
        assert out["atr_at_buy"] == pytest.approx(round(atr_at_buy, 4))
        # 若以收盘价为基准，结果会不同 —— 确保没有回退到收盘价口径
        close_based = round(float(bars.iloc[-3]["close"]) - 1.5 * atr_at_buy, 4)
        assert out["hard_stop_price"] != pytest.approx(close_based)

    def test_chandelier_stop(self, bull_db) -> None:
        db, bars = bull_db
        buy_date, buy_price = _buy_inputs(bars)

        out = sl.compute_stop_loss("510300.SS", buy_date, buy_price, db=db)

        atr_series = compute_live_series(bars, "atr")
        current_atr = float(atr_series.iloc[-1])
        buy_ts = pd.Timestamp(buy_date)
        highest = float(bars[pd.to_datetime(bars["time"]) >= buy_ts]["high"].max())
        expected = round(highest - 2.5 * current_atr, 4)
        assert out["chandelier_stop_price"] == pytest.approx(expected)
        assert out["chandelier_stop_atr_mul"] == 2.5
        assert out["highest_since_buy"] == pytest.approx(round(highest, 4))
        # 最高价出现日期（首次触及）
        since = bars[pd.to_datetime(bars["time"]) >= buy_ts]
        expected_date = str(pd.Timestamp(since.loc[since["high"].idxmax(), "time"]).date())
        assert out["highest_since_buy_date"] == expected_date

    def test_chandelier_stop_ratchet_matches_independent_recompute(self, bull_db) -> None:
        """棘轮价 = 逐日候选值（截至当日最高价 − 2.5×当日ATR）的历史最大值，
        且恒 ≥ 普通吊灯止损价。"""
        db, bars = bull_db
        buy_date, buy_price = _buy_inputs(bars)

        out = sl.compute_stop_loss("510300.SS", buy_date, buy_price, db=db)

        assert out["chandelier_stop_ratchet_price"] == pytest.approx(
            expected_ratchet(bars, buy_date)
        )
        assert out["chandelier_stop_ratchet_price"] >= out["chandelier_stop_price"]

    def test_chandelier_stop_ratchet_holds_when_atr_expands(self, test_db) -> None:
        """ATR 急剧扩张而最高价不变：普通吊灯止损被拉低，棘轮版保持不动。"""
        bars = make_atr_expansion_bars()
        test_db.save_market_data("510300.SS", bars, price_mode="qfq")
        buy_date, buy_price = _buy_inputs(bars, idx=4)

        out = sl.compute_stop_loss("510300.SS", buy_date, buy_price, db=test_db)

        assert out["chandelier_stop_ratchet_price"] == pytest.approx(
            expected_ratchet(bars, buy_date)
        )
        # 判别性断言：两版本必须真的分叉（普通版被 ATR 扩张拉低）
        assert out["chandelier_stop_ratchet_price"] > out["chandelier_stop_price"]

    def test_per_instrument_stop_atr_mul_override(self, bull_db) -> None:
        db, bars = bull_db
        db.save_instrument_metadata([{"symbol": "510300.SS", "name": "沪深300ETF", "stop_atr_mul": 2.0}])
        buy_date, buy_price = _buy_inputs(bars)

        out = sl.compute_stop_loss("510300.SS", buy_date, buy_price, db=db)

        atr_series = compute_live_series(bars, "atr")
        atr_at_buy = float(atr_series[atr_series.index <= pd.Timestamp(buy_date)].iloc[-1])
        assert out["hard_stop_atr_mul"] == 2.0
        assert out["hard_stop_price"] == pytest.approx(round(buy_price - 2.0 * atr_at_buy, 4))

    def test_chandelier_first_trigger_none_in_uptrend(self, bull_db) -> None:
        """持续上涨：收盘价从未跌破当日口径吊灯止损，历史触发字段为空。"""
        db, bars = bull_db
        buy_date, buy_price = _buy_inputs(bars)

        out = sl.compute_stop_loss("510300.SS", buy_date, buy_price, db=db)

        assert out["chandelier_first_trigger_date"] is None
        assert out["chandelier_first_trigger_close"] is None
        assert out["chandelier_first_trigger_stop"] is None

    def test_chandelier_first_trigger_matches_independent_recompute(self, test_db) -> None:
        """持续阴跌：历史首次跌破日与独立逐日重算（收盘 ≤ 当日最高价−2.5×当日ATR）一致。"""
        from conftest import make_bear_bars

        bars = make_bear_bars(40)
        test_db.save_market_data("510500.SS", bars, price_mode="qfq")
        buy_date = str(bars.iloc[0]["time"])[:10]
        buy_price = float(bars.iloc[0]["close"])

        out = sl.compute_stop_loss("510500.SS", buy_date, buy_price, db=test_db)

        date = out["chandelier_first_trigger_date"]
        assert date is not None
        # 明细与当日K线一致，且收盘价确实 ≤ 当日口径吊灯止损价
        day = bars[pd.to_datetime(bars["time"]).dt.date.astype(str) == date].iloc[0]
        assert out["chandelier_first_trigger_close"] == pytest.approx(round(float(day["close"]), 4))
        assert out["chandelier_first_trigger_close"] <= out["chandelier_first_trigger_stop"]

        # 独立重算首次触发日
        atr_daily = compute_live_series(bars, "atr")
        atr_daily.index = pd.DatetimeIndex(pd.to_datetime(atr_daily.index))
        since = bars[pd.to_datetime(bars["time"]) >= pd.Timestamp(buy_date)]
        dates = pd.DatetimeIndex(pd.to_datetime(since["time"]))
        atr_aligned = atr_daily.reindex(dates).ffill()
        running_high = pd.to_numeric(since["high"], errors="coerce").cummax()
        daily_ch = running_high.to_numpy(dtype=float) - 2.5 * atr_aligned.to_numpy(dtype=float)
        closes = pd.to_numeric(since["close"], errors="coerce").to_numpy(dtype=float)
        below = closes <= daily_ch
        assert below.any()
        assert date == str(dates[int(below.argmax())].date())
        assert out["chandelier_first_trigger_stop"] == pytest.approx(
            round(float(daily_ch[int(below.argmax())]), 4)
        )

    def test_non_trading_day_buy_date_uses_lookback_atr(self, bull_db) -> None:
        db, bars = bull_db
        # 取一个交易日，顺延到周日（非交易日）买入
        row = bars.iloc[-4]
        friday_or_later = pd.Timestamp(str(row["time"])[:10])
        sunday = friday_or_later + pd.Timedelta(days=(6 - friday_or_later.weekday()) % 7 or 7)
        buy_date = str(sunday.date())

        out = sl.compute_stop_loss("510300.SS", buy_date, 1.0, db=db)

        atr_series = compute_live_series(bars, "atr")
        atr_at_buy = float(atr_series[atr_series.index <= sunday].iloc[-1])
        assert out["hard_stop_price"] == pytest.approx(round(1.0 - 1.5 * atr_at_buy, 4))

    def test_no_data_raises(self, test_db) -> None:
        with pytest.raises(sl.StopLossError, match="未找到"):
            sl.compute_stop_loss("999999.SS", "2025-01-10", 1.0, db=test_db)

    def test_invalid_symbol_raises(self, test_db) -> None:
        with pytest.raises(sl.StopLossError, match="无效"):
            sl.compute_stop_loss("   ", "2025-01-10", 1.0, db=test_db)

    def test_invalid_price_raises(self, bull_db) -> None:
        db, bars = bull_db
        buy_date, _ = _buy_inputs(bars)
        with pytest.raises(sl.StopLossError, match="大于 0"):
            sl.compute_stop_loss("510300.SS", buy_date, 0.0, db=db)

    def test_price_below_day_low_raises(self, bull_db) -> None:
        """买入价低于买入日最低价 → 拒绝。"""
        db, bars = bull_db
        row = bars.iloc[-3]
        buy_date = str(row["time"])[:10]
        too_low = round(float(row["low"]) - 0.01, 4)
        with pytest.raises(sl.StopLossError, match="当日价格区间"):
            sl.compute_stop_loss("510300.SS", buy_date, too_low, db=db)

    def test_price_above_day_high_raises(self, bull_db) -> None:
        """买入价高于买入日最高价 → 拒绝，报错信息含区间。"""
        db, bars = bull_db
        row = bars.iloc[-3]
        buy_date = str(row["time"])[:10]
        too_high = round(float(row["high"]) + 0.01, 4)
        with pytest.raises(sl.StopLossError, match="当日价格区间"):
            sl.compute_stop_loss("510300.SS", buy_date, too_high, db=db)

    def test_price_at_day_bounds_accepted(self, bull_db) -> None:
        """买入价恰为当日最高/最低价（边界）→ 接受。"""
        db, bars = bull_db
        row = bars.iloc[-3]
        buy_date = str(row["time"])[:10]
        for price in (float(row["low"]), float(row["high"])):
            out = sl.compute_stop_loss("510300.SS", buy_date, round(price, 4), db=db)
            assert out["buy_price"] == pytest.approx(round(price, 4))


class TestStopMode:
    """止损松紧档位：tight 紧止损固定 1×ATR / 2×ATR，默认 loose 沿用既有口径。"""

    def test_default_is_loose(self, bull_db) -> None:
        db, bars = bull_db
        buy_date, buy_price = _buy_inputs(bars)

        out = sl.compute_stop_loss("510300.SS", buy_date, buy_price, db=db)

        assert out["stop_mode"] == "loose"
        assert out["hard_stop_atr_mul"] == 1.5
        assert out["chandelier_stop_atr_mul"] == 2.5

    def test_tight_mode_multipliers(self, bull_db) -> None:
        db, bars = bull_db
        buy_date, buy_price = _buy_inputs(bars)

        out = sl.compute_stop_loss("510300.SS", buy_date, buy_price, db=db, stop_mode="tight")

        atr_series = compute_live_series(bars, "atr")
        atr_at_buy = float(atr_series[atr_series.index <= pd.Timestamp(buy_date)].iloc[-1])
        current_atr = float(atr_series.iloc[-1])
        highest = float(bars[pd.to_datetime(bars["time"]) >= pd.Timestamp(buy_date)]["high"].max())
        assert out["stop_mode"] == "tight"
        assert out["hard_stop_atr_mul"] == 1.0
        assert out["chandelier_stop_atr_mul"] == 2.0
        assert out["hard_stop_price"] == pytest.approx(round(buy_price - 1.0 * atr_at_buy, 4))
        assert out["chandelier_stop_price"] == pytest.approx(round(highest - 2.0 * current_atr, 4))

    def test_tight_mode_ignores_per_instrument_override(self, bull_db) -> None:
        """紧止损为固定 1×ATR，标的级 stop_atr_mul 覆盖只在松止损下生效。"""
        db, bars = bull_db
        db.save_instrument_metadata([{"symbol": "510300.SS", "name": "沪深300ETF", "stop_atr_mul": 2.0}])
        buy_date, buy_price = _buy_inputs(bars)

        loose = sl.compute_stop_loss("510300.SS", buy_date, buy_price, db=db)
        tight = sl.compute_stop_loss("510300.SS", buy_date, buy_price, db=db, stop_mode="tight")

        assert loose["hard_stop_atr_mul"] == 2.0
        assert tight["hard_stop_atr_mul"] == 1.0

    def test_tight_stop_is_higher_than_loose(self, bull_db) -> None:
        """紧止损的两个止损价都应高于松止损（更贴近现价、更易触发）。"""
        db, bars = bull_db
        buy_date, buy_price = _buy_inputs(bars)

        loose = sl.compute_stop_loss("510300.SS", buy_date, buy_price, db=db)
        tight = sl.compute_stop_loss("510300.SS", buy_date, buy_price, db=db, stop_mode="tight")

        assert tight["hard_stop_price"] > loose["hard_stop_price"]
        assert tight["chandelier_stop_price"] > loose["chandelier_stop_price"]


class TestComputeStopLossIntraday:
    """盘中实时叠加：当日合成K线计入最高价/最新价，ATR 保持历史完整K线口径。"""

    @staticmethod
    def _synth_bar(high: float, close: float, low: float | None = None) -> dict:
        return {
            "time": pd.Timestamp("2025-03-03 10:30:00"),  # 晚于 bull bars 末日
            "open": close,
            "high": high,
            "low": low if low is not None else close * 0.99,
            "close": close,
            "volume": 0.0,
            "amount": 0.0,
        }

    def test_intraday_bar_updates_high_and_latest(self, bull_db, monkeypatch) -> None:
        db, bars = bull_db
        buy_date, buy_price = _buy_inputs(bars)
        eod = sl.compute_stop_loss("510300.SS", buy_date, buy_price, db=db)
        assert eod["is_intraday"] is False

        synth_high = eod["highest_since_buy"] * 1.05
        synth_close = eod["highest_since_buy"] * 1.04
        monkeypatch.setattr(
            sl, "_fetch_intraday_bar",
            lambda symbol, df: self._synth_bar(synth_high, synth_close),
        )

        out = sl.compute_stop_loss("510300.SS", buy_date, buy_price, db=db)

        assert out["is_intraday"] is True
        assert out["intraday_bar"]["date"] == "2025-03-03"
        assert out["highest_since_buy"] == pytest.approx(round(synth_high, 4))
        # 盘中创出新高：最高价日期为盘中当日
        assert out["highest_since_buy_date"] == "2025-03-03"
        assert out["latest_price"] == pytest.approx(round(synth_close, 4))
        # ATR 不被当日不完整K线污染 → 硬止损不变
        assert out["current_atr"] == eod["current_atr"]
        assert out["hard_stop_price"] == eod["hard_stop_price"]
        # 吊灯止损随盘中新高实时上移
        current_atr = float(compute_live_series(bars, "atr").iloc[-1])
        expected = round(synth_high - 2.5 * current_atr, 4)
        assert out["chandelier_stop_price"] == pytest.approx(expected)
        assert out["chandelier_stop_price"] > eod["chandelier_stop_price"]

    def test_intraday_unavailable_falls_back_to_eod(self, bull_db, monkeypatch) -> None:
        db, bars = bull_db
        buy_date, buy_price = _buy_inputs(bars)
        eod = sl.compute_stop_loss("510300.SS", buy_date, buy_price, db=db)

        monkeypatch.setattr(sl, "_fetch_intraday_bar", lambda symbol, df: None)
        out = sl.compute_stop_loss("510300.SS", buy_date, buy_price, db=db)

        assert out["is_intraday"] is False
        assert "intraday_bar" not in out
        assert out["chandelier_stop_price"] == eod["chandelier_stop_price"]


class TestComputeStopLossBatch:
    """compute_stop_loss_batch：与逐次 compute_stop_loss 同一计算口径，

    IO 走批量路径（load_market_data_many / get_series_bulk / metadata map）。
    盘中行情预取在单测中 stub 掉（网络无关）。
    """

    @pytest.fixture(autouse=True)
    def _no_intraday_prefetch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            sl, "fetch_intraday_bars", lambda dfs: dict.fromkeys(dfs, None)
        )

    @pytest.fixture
    def two_symbol_db(self, test_db):
        from conftest import make_bull_bars

        bars_a = make_bull_bars(40)
        bars_b = make_bull_bars(40)
        test_db.save_market_data("510300.SS", bars_a, price_mode="qfq")
        test_db.save_market_data("510500.SS", bars_b, price_mode="qfq")
        return test_db, bars_a, bars_b

    def test_empty_items(self, test_db) -> None:
        assert sl.compute_stop_loss_batch([], db=test_db) == []

    def test_matches_single_calls(self, two_symbol_db) -> None:
        db, bars_a, bars_b = two_symbol_db
        buy_date, buy_price = _buy_inputs(bars_a)

        items = [
            {"symbol": "510300", "buy_date": buy_date, "buy_price": buy_price},
            {"symbol": "510500.SS", "buy_date": buy_date, "buy_price": buy_price},
        ]
        results = sl.compute_stop_loss_batch(items, db=db)

        assert [r["ok"] for r in results] == [True, True]
        for item_result, symbol in zip(results, ("510300.SS", "510500.SS"), strict=True):
            single = sl.compute_stop_loss(symbol, buy_date, buy_price, db=db)
            assert item_result["symbol"] == symbol
            for key in (
                "hard_stop_price",
                "chandelier_stop_price",
                "chandelier_stop_ratchet_price",
                "atr_at_buy",
                "current_atr",
                "highest_since_buy",
                "latest_price",
            ):
                assert item_result[key] == pytest.approx(single[key]), key
            assert item_result["is_intraday"] is False

    def test_order_preserved_and_item_isolation(self, two_symbol_db) -> None:
        db, bars_a, _ = two_symbol_db
        buy_date, buy_price = _buy_inputs(bars_a)

        items = [
            {"symbol": "510300.SS", "buy_date": buy_date, "buy_price": buy_price},
            {"symbol": "999999.SS", "buy_date": buy_date, "buy_price": buy_price},  # 无数据
            {"symbol": "510300.SS", "buy_date": "not-a-date", "buy_price": buy_price},
            {"symbol": "", "buy_date": buy_date, "buy_price": buy_price},
            {"symbol": "510500.SS", "buy_date": buy_date, "buy_price": -1.0},  # 非法价格
        ]
        results = sl.compute_stop_loss_batch(items, db=db)

        assert len(results) == 5
        assert results[0]["ok"] is True
        assert results[1]["ok"] is False and "未找到" in results[1]["error"]
        assert results[2]["ok"] is False and "买入日期" in results[2]["error"]
        assert results[3]["ok"] is False and "无效" in results[3]["error"]
        assert results[4]["ok"] is False and "大于 0" in results[4]["error"]
        # 失败项携带原始 symbol 便于调用方定位
        assert results[1]["symbol"] == "999999.SS"

    def test_batch_default_and_per_item_stop_mode(self, two_symbol_db) -> None:
        db, bars_a, _ = two_symbol_db
        buy_date, buy_price = _buy_inputs(bars_a)

        items = [
            {"symbol": "510300.SS", "buy_date": buy_date, "buy_price": buy_price},
            {
                "symbol": "510500.SS",
                "buy_date": buy_date,
                "buy_price": buy_price,
                "stop_mode": "loose",  # 单项覆盖批次默认
            },
        ]
        results = sl.compute_stop_loss_batch(items, db=db, stop_mode="tight")

        assert results[0]["stop_mode"] == "tight"
        assert results[0]["hard_stop_atr_mul"] == 1.0
        assert results[1]["stop_mode"] == "loose"
        assert results[1]["hard_stop_atr_mul"] == 1.5

    def test_intraday_prefetched_bar_reused(self, two_symbol_db, monkeypatch) -> None:
        """批量路径的盘中K线来自统一预取，逐项不再单独拉行情。"""
        db, bars_a, _ = two_symbol_db
        buy_date, buy_price = _buy_inputs(bars_a)
        synth = {
            "time": pd.Timestamp("2026-08-27 14:00:00"),  # 与 build_synthetic_bar 同为 Timestamp
            "open": 99.0,
            "high": 100.0,
            "low": 98.0,
            "close": 99.5,
            "volume": 1.0,
        }
        monkeypatch.setattr(
            sl,
            "fetch_intraday_bars",
            lambda dfs: {s: dict(synth) for s in dfs},
        )

        calls: list[int] = []
        monkeypatch.setattr(
            sl, "_fetch_intraday_bar", lambda symbol, df: calls.append(1) or None
        )
        results = sl.compute_stop_loss_batch(
            [{"symbol": "510300.SS", "buy_date": buy_date, "buy_price": buy_price}],
            db=db,
        )
        assert results[0]["ok"] is True
        assert results[0]["is_intraday"] is True
        assert calls == []  # 逐项零行情请求


class TestChandelierFirstTriggerExcludesBuyDay:
    """吊灯止损「首次跌破」排除买入当天（与 manual_trade 硬止损曾跌穿同口径）。"""

    @staticmethod
    def _bars_with_buy_day_spike() -> pd.DataFrame:
        """25 个平静上涨日 + 买入日长上影大振幅 + 次日高开企稳。

        买入日：high 远高于 close，使 当日close ≤ 当日吊灯候选值
        （= 当日 high − 2.5×ATR）——旧逻辑会把首次跌破记到买入当天；
        次日 close 回到吊灯候选值之上，之后不再跌破。
        """
        base = date(2025, 1, 6)
        records: list[dict] = []
        price = 10.0
        offset = 0
        made = 0
        while made < 28:
            day = base + timedelta(days=offset)
            offset += 1
            if day.weekday() >= 5:
                continue
            made += 1
            if made == 26:  # 买入日：长上影（high +10%），close −2%
                row = dict(open=price, high=price * 1.10, low=price * 0.97,
                           close=price * 0.98)
            elif made == 27:  # 次日高开企稳：close 回到吊灯候选值之上
                row = dict(open=price * 1.06, high=price * 1.10, low=price * 1.055,
                           close=price * 1.09)
            else:
                row = dict(open=price, high=price * 1.002, low=price * 0.998,
                           close=price * 1.01)
            records.append(
                {
                    "time": day.isoformat(),
                    "open": round(float(row["open"]), 4),
                    "high": round(float(row["high"]), 4),
                    "low": round(float(row["low"]), 4),
                    "close": round(float(row["close"]), 4),
                    "volume": 1_000_000,
                }
            )
            price = float(row["close"])
        return pd.DataFrame(records)

    def test_buy_day_close_below_chandelier_not_counted(self, test_db) -> None:
        bars = self._bars_with_buy_day_spike()
        test_db.save_market_data("510300.SS", bars, price_mode="qfq")
        buy_row = bars.iloc[25]
        buy_date = str(buy_row["time"])[:10]
        buy_price = round(float(buy_row["open"]), 4)  # 落在买入日 [low, high] 内

        out = sl.compute_stop_loss("510300.SS", buy_date, buy_price, db=test_db)

        # 前置条件：买入日收盘确实低于买入日吊灯候选值（旧逻辑会记为首次跌破日）
        atr_series = compute_live_series(bars, "atr")
        atr_at_buy = float(atr_series[atr_series.index <= pd.Timestamp(buy_date)].iloc[-1])
        buy_day_chandelier = float(buy_row["high"]) - 2.5 * atr_at_buy
        assert float(buy_row["close"]) <= buy_day_chandelier

        # 新逻辑：买入日不参与 → 之后无跌破 → 无首次跌破记录
        assert out["chandelier_first_trigger_date"] is None
        assert out["chandelier_first_trigger_close"] is None
