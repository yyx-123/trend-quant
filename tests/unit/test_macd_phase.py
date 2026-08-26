"""Unit tests for MACD cross-phase detection and the kline mini payload.

 detect_macd_phase 的口径（与看板展示一致）：
 - hist = DIF - DEA；昨天负、今天正 → 今天是金叉第 1 天（死叉反之）
 - days 按K线根数计，翻转当日为第 1 天
 - change_pct = 最新收盘 / 金叉（死叉）首日收盘 - 1
"""

from __future__ import annotations

import pandas as pd
import pytest

from core.indicators import detect_macd_phase, kline_mini, macd


def _signs(closes: list[float]) -> list[float]:
    result = macd(pd.Series(closes, dtype="float64"), warmup=True)
    return list((result["dif"] - result["dea"]).to_numpy(dtype=float))


def _dates(n: int) -> list[str]:
    return [d.date().isoformat() for d in pd.date_range("2026-01-01", periods=n, freq="B")]


class TestDetectMacdPhase:
    def test_golden_cross_run_counted_from_flip_bar(self) -> None:
        # 60 根阴跌（hist 转负）后 12 根强阳 → 金叉；天数 = 尾部 hist>0 的连续根数。
        closes = [100.0 - 0.5 * i for i in range(60)]
        price = closes[-1]
        for _ in range(12):
            price *= 1.04
            closes.append(price)
        dates = _dates(len(closes))

        phase = detect_macd_phase(closes, dates)

        assert phase["phase"] == "golden"
        signs = _signs(closes)
        expected_days = 0
        for value in reversed(signs):
            if value > 0:
                expected_days += 1
            else:
                break
        assert expected_days >= 1
        assert phase["days"] == expected_days
        signal_idx = len(closes) - expected_days
        assert phase["signal_date"] == dates[signal_idx]
        expected_change = round((closes[-1] / closes[signal_idx] - 1.0) * 100.0, 2)
        assert phase["change_pct"] == expected_change

    def test_intraday_flip_is_day_one_with_zero_change(self) -> None:
        # 盘中场景：昨天 hist 为负，今天实时价暴涨使 hist 转正 → 金叉第 1 天，
        # 且首日收盘即现价 → 涨跌幅 0。
        closes = [100.0 - 0.4 * i for i in range(70)]
        spike = None
        for factor in (1.10, 1.15, 1.20, 1.30, 1.50, 2.00):
            candidate = closes[-1] * factor
            signs = _signs([*closes, candidate])
            if signs[-2] < 0 < signs[-1]:
                spike = candidate
                break
        assert spike is not None, "未能构造出单日翻转的价格序列"
        live_closes = [*closes, spike]
        dates = _dates(len(live_closes))

        phase = detect_macd_phase(live_closes, dates)

        assert phase["phase"] == "golden"
        assert phase["days"] == 1
        assert phase["signal_date"] == dates[-1]
        assert phase["change_pct"] == 0.0

    def test_dead_cross(self) -> None:
        # 60 根上涨后 12 根急跌 → 死叉。
        closes = [100.0 + 0.5 * i for i in range(60)]
        price = closes[-1]
        for _ in range(12):
            price *= 0.96
            closes.append(price)

        phase = detect_macd_phase(closes, _dates(len(closes)))

        assert phase["phase"] == "dead"
        assert phase["days"] is not None and phase["days"] >= 1
        assert phase["change_pct"] is not None and phase["change_pct"] < 0

    def test_insufficient_history_returns_default(self) -> None:
        phase = detect_macd_phase([100.0 + i for i in range(30)], _dates(30))
        assert phase == {"phase": None, "days": None, "change_pct": None, "signal_date": None}

    def test_none_closes_dropped_with_aligned_dates(self) -> None:
        closes = [100.0 - 0.5 * i for i in range(60)]
        price = closes[-1]
        for _ in range(12):
            price *= 1.04
            closes.append(price)
        dates = _dates(len(closes))
        # 中段挖两个空洞，日期同步丢弃，相位结果应与无洞序列一致。
        holed_closes = closes[:10] + [None, None] + closes[10:]
        holed_dates = dates[:10] + ["2020-01-01", "2020-01-02"] + dates[10:]

        phase = detect_macd_phase(holed_closes, holed_dates)
        baseline = detect_macd_phase(closes, dates)

        assert phase["phase"] == "golden"
        # 挖洞（None 行同步丢日期）后相位与无洞基准一致
        assert phase["phase"] == baseline["phase"]
        assert phase["days"] == baseline["days"]
        assert phase["signal_date"] == baseline["signal_date"]
        assert phase["change_pct"] == baseline["change_pct"]


class TestKlineMini:
    def _bars(self, n: int) -> pd.DataFrame:
        rows = []
        for i in range(n):
            close = 100.0 + i
            rows.append({
                "time": pd.Timestamp("2026-01-01") + pd.Timedelta(days=i),
                "open": close - 0.5,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
            })
        return pd.DataFrame(rows)

    def test_returns_last_thirty_with_aligned_ma5(self) -> None:
        bars = self._bars(34)
        payload = kline_mini(bars)
        assert len(payload["kline"]) == 30
        assert len(payload["kline_ma5"]) == 30
        # 窗口从第 5 根（index 4）开始：close = 104 … 133
        assert payload["kline"][0]["c"] == 104.0
        assert payload["kline"][-1]["c"] == 133.0
        # 前 4 根 MA5 不足窗口 → None；第 5 根起有值。
        assert payload["kline_ma5"][:4] == [None, None, None, None]
        assert payload["kline_ma5"][4] == pytest.approx((104 + 105 + 106 + 107 + 108) / 5)
        assert payload["kline_ma5"][-1] == pytest.approx((129 + 130 + 131 + 132 + 133) / 5)

    def test_fewer_than_count_bars_returned_as_is(self) -> None:
        payload = kline_mini(self._bars(6))
        assert len(payload["kline"]) == 6

    def test_custom_count(self) -> None:
        payload = kline_mini(self._bars(12), count=10)
        assert len(payload["kline"]) == 10
        assert payload["kline"][0]["c"] == 102.0

    def test_empty_bars(self) -> None:
        assert kline_mini(pd.DataFrame()) == {"kline": [], "kline_ma5": []}
