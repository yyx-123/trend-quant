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
from app.routers import market_view


@pytest.fixture(autouse=True)
def _no_network_period_fetch(monkeypatch):
    """本文件的 API 测试一律不联网。

    查看接口现在会为周/月做「按需补当期 bar」自愈，若不封住，种子数据的末根
    停在往期就会真的去打 vendor（慢且不确定）。需要断言自愈行为的用例，在用例
    体内再次 monkeypatch 覆盖本 fixture 即可（后写生效）。
    """

    class _Noop:
        def ensure_period_history(self, *args, **kwargs):
            return {"status": "ready"}

    monkeypatch.setattr(market_view, "get_data_service", lambda: _Noop())


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
        assert meta["last_bar_provisional"] is False

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
        # 当期（未收盘）bar 会入库：此处种子数据的末根是 2026-01 的往期 bar，
        # 故不算进行中；日K恒 False
        assert meta["last_bar_provisional"] is False
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


class TestRollingTrendSeries:
    """TREND 副图的滚动周/月趋势值（trend_rolling_daily）：日K视图带出并对齐日期轴。

    周/月视图不带出——它锚定在交易日上，而周/月视图的日期轴是周期 bar 标注日，
    副图趋势值本身已按该周期 K 线重算（见 routers/market_view.get_market_daily）。
    """

    def test_daily_payload_aligns_rolling_series_to_dates(self, client, test_db) -> None:
        _seed_daily(test_db, rows=30)
        rows = [
            {"time": pd.Timestamp(day), "w_trend": w, "m_trend": m}
            for day, w, m in (
                ("2026-01-20", 12.5, -3.5),
                ("2026-01-21", pd.NA, -4.0),  # 单侧预热：w 为 NULL
                ("2026-01-22", 15.0, 2.0),
            )
        ]
        test_db.save_rolling_trend_many([("510300.SS", pd.DataFrame(rows))])

        resp = client.get("/market-view/api/daily", params={"symbol": "510300.SS"})
        assert resp.status_code == 200
        payload = resp.json()
        node = payload["indicators"]["trend_rolling"]
        dates = payload["dates"]
        assert len(node["weekly"]) == len(dates)
        assert len(node["monthly"]) == len(dates)

        by_day = dict(zip(dates, zip(node["weekly"], node["monthly"])))
        assert by_day["2026-01-20"] == (12.5, -3.5)
        assert by_day["2026-01-21"] == (None, -4.0)
        assert by_day["2026-01-22"] == (15.0, 2.0)
        # 表里没有的日期（如 2026-01-19 之前）补 None 而不是错位
        assert by_day["2026-01-19"] == (None, None)

    def test_weekly_view_has_no_rolling_series(self, client, test_db) -> None:
        _seed_daily(test_db, rows=30)
        _seed_period(test_db, "1w", [("2026-01-09", 4.0), ("2026-01-16", 4.1)])
        test_db.save_rolling_trend_many(
            [
                (
                    "510300.SS",
                    pd.DataFrame(
                        [{"time": pd.Timestamp("2026-01-16"), "w_trend": 9.0, "m_trend": 8.0}]
                    ),
                )
            ]
        )

        resp = client.get(
            "/market-view/api/daily", params={"symbol": "510300.SS", "period": "1w"}
        )
        assert resp.status_code == 200
        assert "trend_rolling" not in resp.json()["indicators"]

    def test_rolling_series_truncated_with_limit(self, client, test_db) -> None:
        """按 limit 截尾展示窗口时，滚动序列跟着截尾（否则与 dates 错位）。"""
        _seed_daily(test_db, rows=30)
        # _seed_daily 从 2026-01-05 起 30 个自然日 → 末三天
        tail_days = [
            (date(2026, 1, 5) + timedelta(days=idx)).isoformat() for idx in (27, 28, 29)
        ]
        test_db.save_rolling_trend_many(
            [
                (
                    "510300.SS",
                    pd.DataFrame(
                        [
                            {"time": pd.Timestamp(day), "w_trend": 1.0, "m_trend": 2.0}
                            for day in tail_days
                        ]
                    ),
                )
            ]
        )

        resp = client.get(
            "/market-view/api/daily", params={"symbol": "510300.SS", "limit": 3}
        )
        assert resp.status_code == 200
        payload = resp.json()
        node = payload["indicators"]["trend_rolling"]
        assert payload["dates"] == tail_days
        assert node["weekly"] == [1.0, 1.0, 1.0]
        assert node["monthly"] == [2.0, 2.0, 2.0]


class TestViewPathSelfHealsCurrentBar:
    """查看页契约：任意标的切到周/月都能看到**当期** K 线及按它算的指标。

    全池任务覆盖不到「不在标的池 / 刚加入 / 缺席补数」的标的，查看接口必须自己
    兜住 —— 否则用户在页面上只会看到 404 或一根停在往期的 bar。
    """

    def test_endpoint_fetches_missing_period_on_demand(self, client, test_db, monkeypatch) -> None:
        """库里完全没有该周期数据时，查看接口按需补这一只，而不是 404。"""
        _seed_daily(test_db)
        weekly = pd.DataFrame(
            [
                {
                    "time": "2026-05-08",
                    "open": 5.0, "high": 5.1, "low": 4.9, "close": 5.05,
                    "volume": 1000.0, "amount": 5000.0,
                }
            ]
        )
        calls: list[tuple] = []

        class _Service:
            def ensure_period_history(self, symbol, period, *, db=None, now=None):
                calls.append((symbol, period))
                test_db.save_market_data(symbol, weekly, price_mode="qfq", period=period)
                test_db.save_market_data(symbol, weekly, price_mode="raw", period=period)
                return {"status": "fetched"}

        monkeypatch.setattr(market_view, "get_data_service", lambda: _Service())
        resp = client.get(
            "/market-view/api/daily", params={"symbol": "510300.SS", "period": "1w"}
        )
        assert resp.status_code == 200, resp.json()
        payload = resp.json()
        assert calls == [("510300.SS", "1w")]
        assert payload["dates"] == ["2026-05-08"]
        # 指标按补回来的当期 K 线现算（不是空图）
        assert len(payload["indicators"]["ma"]["20"]) == 1
        assert len(payload["indicators"]["trend"]["score"]) == 1

    def test_endpoint_still_404s_when_symbol_truly_has_no_data(
        self, client, test_db, monkeypatch
    ) -> None:
        """补不到就如实 404（例如代码写错/退市），不能把错误伪装成空图。"""
        _seed_daily(test_db)

        class _Service:
            def ensure_period_history(self, symbol, period, *, db=None, now=None):
                return {"status": "fetched"}  # 声称抓了但库里仍没有

        monkeypatch.setattr(market_view, "get_data_service", lambda: _Service())
        resp = client.get(
            "/market-view/api/daily", params={"symbol": "510300.SS", "period": "1M"}
        )
        assert resp.status_code == 404
        assert "月" in resp.json()["detail"]

    def test_daily_view_does_not_trigger_on_demand_fetch(
        self, client, test_db, monkeypatch
    ) -> None:
        """日K不走自愈：日 bar 只由 16:30 写入，别拿查看流量去打接口。"""
        _seed_daily(test_db)
        calls: list[tuple] = []

        class _Service:
            def ensure_period_history(self, *a, **k):
                calls.append((a, k))
                return {"status": "not_applicable"}

        monkeypatch.setattr(market_view, "get_data_service", lambda: _Service())
        resp = client.get("/market-view/api/daily", params={"symbol": "510300.SS"})
        assert resp.status_code == 200
        assert resp.json()["meta"]["period"] == "1d"
        assert calls == []

    def test_self_heal_failure_does_not_break_the_view(
        self, client, test_db, monkeypatch
    ) -> None:
        """自愈过程抛错时仍按库内数据出图（不能因为补数失败而白屏）。"""
        _seed_daily(test_db)
        _seed_period(test_db, "1w", [("2026-05-08", 5.0)])

        class _Service:
            def ensure_period_history(self, *a, **k):
                raise RuntimeError("vendor down")

        monkeypatch.setattr(market_view, "get_data_service", lambda: _Service())
        resp = client.get(
            "/market-view/api/daily", params={"symbol": "510300.SS", "period": "1w"}
        )
        assert resp.status_code == 200
        assert resp.json()["dates"] == ["2026-05-08"]
