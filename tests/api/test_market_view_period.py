"""标的查看页 K 线周期切换（日/周/月）接口回归。

锁定三件事：
1. ``period`` 参数按 core.bars 的规范周期取值，非法值（尤其小写 '1m' 分钟线）
   必须 400，不会静默落到月K；
2. 每个周期读各自的行情表，副图指标按该周期 K 线现算（不是读 indicator_daily）；
3. 周/月只呈现已收盘周期，且 meta 里的日K跨度（daily_start/daily_end）始终
   来自日K表 —— 回测面板的日期边界锚定日K，不能被周期 bar 标注日污染。
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest


def _seed_daily(test_db, symbol: str = "510300.SS", rows: int = 120) -> None:
    start = date(2026, 1, 5)
    items = []
    price = 4.0
    for idx in range(rows):
        day = start + timedelta(days=idx)
        price += 0.01 * (1 if idx % 3 else -1)
        items.append(
            {
                "time": day.isoformat(),
                "open": price,
                "high": price + 0.05,
                "low": price - 0.05,
                "close": price + 0.02,
                "volume": 100000 + idx * 100,
                "amount": 400000 + idx * 400,
            }
        )
    test_db.save_market_data(symbol, pd.DataFrame(items), price_mode="qfq")


def _seed_period(
    test_db, period: str, rows: list[tuple[str, float]], symbol: str = "510300.SS"
) -> None:
    frame = pd.DataFrame(
        [
            {
                "time": day,
                "open": price,
                "high": price + 0.1,
                "low": price - 0.1,
                "close": price + 0.05,
                "volume": 500000.0,
                "amount": 2000000.0,
            }
            for day, price in rows
        ]
    )
    test_db.save_market_data(symbol, frame, price_mode="qfq", period=period)


class TestPeriodParameter:
    def test_default_is_daily(self, client, test_db) -> None:
        _seed_daily(test_db)
        resp = client.get("/market-view/api/daily", params={"symbol": "510300.SS"})
        assert resp.status_code == 200
        meta = resp.json()["meta"]
        assert meta["period"] == "1d"
        assert meta["period_label"] == "日"
        assert meta["only_closed_bars"] is False

    @pytest.mark.parametrize("period,label", [("1d", "日"), ("1w", "周"), ("1M", "月")])
    def test_returns_requested_period_bars(self, client, test_db, period, label) -> None:
        _seed_daily(test_db)
        _seed_period(test_db, "1w", [("2026-01-09", 4.0), ("2026-01-16", 4.1)])
        _seed_period(test_db, "1M", [("2026-01-30", 4.0)])
        # _seed_daily 从 2026-01-05 起 120 个自然日（不过滤周末）
        daily_end = (date(2026, 1, 5) + timedelta(days=119)).isoformat()

        resp = client.get(
            "/market-view/api/daily", params={"symbol": "510300.SS", "period": period}
        )
        assert resp.status_code == 200
        payload = resp.json()
        meta = payload["meta"]
        assert meta["period"] == period
        assert meta["period_label"] == label
        # 周/月视图只呈现已收盘周期（当期不在库内，由落库环节保证）
        assert meta["only_closed_bars"] is (period != "1d")
        # 回测面板锚点始终是日K跨度，与所选周期无关
        assert meta["daily_start"] == "2026-01-05"
        assert meta["daily_end"] == daily_end
        # 图上的日期是所选周期 bar 的标注日（1w 两根、1M 一根、1d 用日K末日）
        expected_last = {"1d": daily_end, "1w": "2026-01-16", "1M": "2026-01-30"}[period]
        assert payload["dates"][-1] == expected_last
        assert meta["end"] == expected_last

    def test_period_bars_are_scoped_to_their_table(self, client, test_db) -> None:
        _seed_daily(test_db, rows=40)
        _seed_period(test_db, "1w", [("2026-01-09", 9.99), ("2026-01-16", 9.99)])
        _seed_period(test_db, "1M", [("2026-01-30", 8.88)])

        weekly = client.get(
            "/market-view/api/daily", params={"symbol": "510300.SS", "period": "1w"}
        ).json()
        monthly = client.get(
            "/market-view/api/daily", params={"symbol": "510300.SS", "period": "1M"}
        ).json()
        assert len(weekly["dates"]) == 2
        assert weekly["candles"][0][1] == pytest.approx(10.04)  # close = price + 0.05
        assert len(monthly["dates"]) == 1
        assert monthly["candles"][0][1] == pytest.approx(8.93)

    def test_alias_period_values(self, client, test_db) -> None:
        _seed_daily(test_db)
        _seed_period(test_db, "1w", [("2026-01-09", 4.0)])
        for alias in ("weekly", "w", "1w"):
            resp = client.get(
                "/market-view/api/daily", params={"symbol": "510300.SS", "period": alias}
            )
            assert resp.status_code == 200
            assert resp.json()["meta"]["period"] == "1w"

    @pytest.mark.parametrize("period", ["1m", "5m", "60m", "hour", "1y"])
    def test_invalid_period_rejected(self, client, test_db, period) -> None:
        """小写 '1m' 是分钟线 —— 必须报错而非落到月K（'1M'）。"""
        _seed_daily(test_db)
        resp = client.get(
            "/market-view/api/daily", params={"symbol": "510300.SS", "period": period}
        )
        assert resp.status_code == 400
        assert "period" in resp.json()["detail"]

    def test_missing_period_data_404_names_the_period(self, client, test_db) -> None:
        _seed_daily(test_db)  # 只有日K，没有周/月
        resp = client.get(
            "/market-view/api/daily", params={"symbol": "510300.SS", "period": "1w"}
        )
        assert resp.status_code == 404
        assert "周" in resp.json()["detail"]


class TestPeriodIndicators:
    def test_indicators_recomputed_from_period_bars(self, client, test_db) -> None:
        """副图指标按所选周期的 K 线现算：同一标的周K的 MA20 与日K的 MA20 不同。

        这是「切周期副图跟着切」的机制证据 —— 指标不读 indicator_daily 缓存，
        而是吃 build_market_payload 传入的 OHLCV，故周期一换即重算。
        """
        _seed_daily(test_db, rows=120)
        _seed_period(
            test_db,
            "1w",
            [
                ((date(2026, 1, 9) + timedelta(days=7 * i)).isoformat(), 4.0 + 0.05 * i)
                for i in range(30)
            ],
        )
        daily = client.get("/market-view/api/daily", params={"symbol": "510300.SS"}).json()
        weekly = client.get(
            "/market-view/api/daily", params={"symbol": "510300.SS", "period": "1w"}
        ).json()

        assert len(daily["indicators"]["ma"]["20"]) == len(daily["dates"])
        assert len(weekly["indicators"]["ma"]["20"]) == len(weekly["dates"])
        assert weekly["indicators"]["ma"]["20"] != daily["indicators"]["ma"]["20"][-len(weekly["dates"]):]
        # 每个副图分组都在（趋势/MACD/RSI/BIAS/E-BIAS/BOLL/VOL-MA/ATR）
        for group in ("ma", "atr", "boll", "macd", "bias", "e_bias", "volume_ma", "rsi", "trend"):
            assert group in weekly["indicators"], group
        assert len(weekly["indicators"]["trend"]["score"]) == len(weekly["dates"])

    def test_trend_params_pass_through_for_weekly(self, client, test_db) -> None:
        """趋势参数（根数口径）在周K上原样生效、不被改写。"""
        _seed_daily(test_db)
        _seed_period(
            test_db,
            "1w",
            [
                ((date(2026, 1, 9) + timedelta(days=7 * i)).isoformat(), 4.0 + 0.02 * i)
                for i in range(40)
            ],
        )
        resp = client.get(
            "/market-view/api/daily",
            params={
                "symbol": "510300.SS",
                "period": "1w",
                "trend_n_short": 4,
                "trend_n_mid": 8,
                "trend_n_long": 12,
                "trend_atr_period": 10,
            },
        )
        assert resp.status_code == 200
        cfg = resp.json()["indicators"]["trend"]["config"]
        assert cfg == {"n_short": 4, "n_mid": 8, "n_long": 12, "atr_period": 10}

    def test_intraday_overlay_ignored_for_non_daily(self, client, test_db) -> None:
        """周/月视图不合成盘中 bar：intraday=true 也只返回已收盘周期。"""
        _seed_daily(test_db)
        _seed_period(test_db, "1w", [("2026-01-09", 4.0), ("2026-01-16", 4.1)])
        resp = client.get(
            "/market-view/api/daily",
            params={"symbol": "510300.SS", "period": "1w", "intraday": "true"},
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["meta"]["is_intraday"] is False
        assert len(payload["dates"]) == 2
