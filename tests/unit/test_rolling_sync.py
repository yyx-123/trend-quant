"""滚动周/月趋势值（trend_rolling_daily）的 service 维护路径。

- full 或无存量 → 整段重建；落库值与 core.rolling_bars 全量重算逐点一致；
- 增量（since）→ 只 upsert 新行，窗口截断后仍与全量重算逐点一致（PIT 确定性）；
- 预热期双 NaN 行不落库；
- 除权（qfq 分段变价）后 full refresh 值更新为新水位；
- ensure_daily_history 钩子：日K 增量触发维护，刷新失败不拖垮日更主结果。
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import numpy as np
import pandas as pd

import data.service as data_service
from core.rolling_bars import rolling_period_trend_series, rolling_trend_cfg
from data.storage.market_store import MarketStore


def _make_daily(rows: int, seed: int = 7) -> pd.DataFrame:
    """确定性随机游走的日K（time 统一 pd.Timestamp，与 vendor 帧格式一致——
    混用 'YYYY-MM-DD' 字符串会让同一日期产生两个主键）。business-day 频率，
    行数需 ≥221 才有非 NaN 的月趋势值（min_bars=10 × 22 交易日）。"""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2024-01-02", periods=rows)
    records = []
    price = 10.0
    for day in days:
        close = price * (1.0 + rng.normal(0.0005, 0.012))
        open_ = price * (1.0 + rng.normal(0.0, 0.004))
        high = max(open_, close) * (1.0 + abs(rng.normal(0.0, 0.003)))
        low = min(open_, close) * (1.0 - abs(rng.normal(0.0, 0.003)))
        volume = float(rng.integers(500_000, 2_000_000))
        records.append(
            {"time": pd.Timestamp(day), "open": open_, "high": high, "low": low,
             "close": close, "volume": volume, "amount": volume * close}
        )
        price = close
    return pd.DataFrame(records)


def _expected_frame(daily: pd.DataFrame) -> pd.DataFrame:
    """全量重算的期望帧（预热期双 NaN 行已剔除，与落库口径一致）。"""
    w = rolling_period_trend_series(daily, rolling_trend_cfg("1w"), period="1w")
    m = rolling_period_trend_series(daily, rolling_trend_cfg("1M"), period="1M")
    frame = pd.DataFrame({"w_trend": w, "m_trend": m}).reset_index()
    return frame[frame["w_trend"].notna() | frame["m_trend"].notna()].reset_index(drop=True)


def _assert_table_matches(test_db, symbol: str, daily: pd.DataFrame) -> None:
    """落库值与全量重算逐点一致（容差 1e-12）。"""
    expected = _expected_frame(daily)
    back = test_db.load_rolling_trend(symbol)
    assert len(back) == len(expected)
    merged = back.merge(expected, on="time", suffixes=("", "_exp"))
    assert len(merged) == len(expected)
    assert np.allclose(merged["w_trend"], merged["w_trend_exp"], atol=1e-12, equal_nan=True)
    assert np.allclose(merged["m_trend"], merged["m_trend_exp"], atol=1e-12, equal_nan=True)


class _FakeProvider:
    def __init__(self, frames: dict | None = None):
        self.frames = frames or {}

    def fetch_daily_history(self, symbol, start, end, adjust="none"):
        return self.frames.get((symbol, adjust), pd.DataFrame())

    def close(self) -> None:  # pragma: no cover - 接口占位
        pass


def _make_service(monkeypatch, provider, test_db) -> data_service.DataService:
    import data.storage.db as db_module

    monkeypatch.setattr(db_module, "get_db", lambda: test_db)
    monkeypatch.setattr(db_module, "_db_instance", test_db)
    service = data_service.DataService.__new__(data_service.DataService)
    service.providers = {"tickflow": provider}
    service.provider_priority = ["tickflow"]
    service.tickflow_settings = None
    service.market_store = MarketStore()
    service.raw_store = MarketStore(price_mode="raw")
    return service


def _seed_symbol(service, test_db, symbol: str, daily: pd.DataFrame) -> None:
    """把 raw/qfq 双侧都种下日K。"""
    service.raw_store.save_history(symbol, daily)
    test_db.save_market_data(symbol, daily, price_mode="qfq")


class TestRefreshRollingTrend:
    def test_full_refresh_matches_recompute(self, monkeypatch, test_db) -> None:
        service = _make_service(monkeypatch, _FakeProvider(), test_db)
        daily = _make_daily(300)
        _seed_symbol(service, test_db, "AAA.SS", daily)
        out = service.refresh_rolling_trend("AAA.SS", full=True)
        assert out == {"symbol": "AAA.SS", "rows": len(_expected_frame(daily))}
        _assert_table_matches(test_db, "AAA.SS", daily)

    def test_warmup_nan_rows_not_stored(self, monkeypatch, test_db) -> None:
        service = _make_service(monkeypatch, _FakeProvider(), test_db)
        daily = _make_daily(300)
        _seed_symbol(service, test_db, "AAA.SS", daily)
        service.refresh_rolling_trend("AAA.SS", full=True)
        w = rolling_period_trend_series(daily, rolling_trend_cfg("1w"), period="1w")
        m = rolling_period_trend_series(daily, rolling_trend_cfg("1M"), period="1M")
        back = test_db.load_rolling_trend("AAA.SS")
        # 首行即周口径首个非 NaN 日；此前的预热期行不落库
        assert back["time"].min() == w.first_valid_index()
        # 周预热完成、月仍在预热的区间内，m_trend 为 NULL → 读出 NaN
        m_warmup = back[back["time"] < m.first_valid_index()]
        assert not m_warmup.empty
        assert m_warmup["m_trend"].isna().all()
        assert m_warmup["w_trend"].notna().all()

    def test_incremental_matches_full_recompute(self, monkeypatch, test_db) -> None:
        service = _make_service(monkeypatch, _FakeProvider(), test_db)
        daily = _make_daily(550)
        old, appended = daily.iloc[:545], daily.iloc[545:]
        _seed_symbol(service, test_db, "AAA.SS", old)
        service.refresh_rolling_trend("AAA.SS", full=True)
        before = test_db.load_rolling_trend("AAA.SS")

        # 追加 5 个交易日的日K，按 since 增量维护。存量 545 行 >
        # ROLLING_TREND_LOOKBACK_DAYS，窗口被真正截断（截断后仍须逐点一致）。
        assert len(old) > data_service.ROLLING_TREND_LOOKBACK_DAYS
        test_db.save_market_data("AAA.SS", daily, price_mode="qfq")
        since = appended["time"].iloc[0].date()
        out = service.refresh_rolling_trend("AAA.SS", since=since)
        assert out["rows"] == len(appended)

        # 新行 + 既有行整体与全量重算逐点一致（容差 1e-12）
        _assert_table_matches(test_db, "AAA.SS", daily)
        # since 之前的既有行一个字节都没动
        after_old = test_db.load_rolling_trend("AAA.SS", end=old["time"].iloc[-1])
        pd.testing.assert_frame_equal(
            before.reset_index(drop=True), after_old.reset_index(drop=True)
        )

    def test_no_existing_rows_falls_back_to_full(self, monkeypatch, test_db) -> None:
        service = _make_service(monkeypatch, _FakeProvider(), test_db)
        daily = _make_daily(300)
        _seed_symbol(service, test_db, "AAA.SS", daily)
        # 表内无存量：即使给了 since 也整段重建（否则历史永远缺段）
        out = service.refresh_rolling_trend("AAA.SS", since=date(2025, 3, 3))
        assert out["rows"] == len(_expected_frame(daily))
        _assert_table_matches(test_db, "AAA.SS", daily)

    def test_factor_change_full_refresh_updates_values(self, monkeypatch, test_db) -> None:
        service = _make_service(monkeypatch, _FakeProvider(), test_db)
        daily = _make_daily(300)
        _seed_symbol(service, test_db, "AAA.SS", daily)
        service.refresh_rolling_trend("AAA.SS", full=True)
        before = test_db.load_rolling_trend("AAA.SS")

        # 模拟除权后的 qfq 整段重写：除权日（第 200 个交易日）之前的历史段
        # 价格乘 0.5（真实除权是分段因子——全历史同乘一个系数不会改变趋势值，
        # 公式全是比值）。
        adjusted = daily.copy()
        boundary = 200
        adjusted.loc[: boundary - 1, ["open", "high", "low", "close"]] *= 0.5
        test_db.save_market_data("AAA.SS", adjusted, price_mode="qfq")
        service.refresh_rolling_trend("AAA.SS", full=True)

        # 落库值 = 新水位的全量重算
        _assert_table_matches(test_db, "AAA.SS", adjusted)
        # 且确实有值变了（跨界窗口含跳变）
        after = test_db.load_rolling_trend("AAA.SS")
        merged = after.merge(before, on="time", suffixes=("_new", "_old"))
        changed = ~np.isclose(
            merged["w_trend_new"].fillna(0.0), merged["w_trend_old"].fillna(0.0), atol=1e-12
        ) | ~np.isclose(
            merged["m_trend_new"].fillna(0.0), merged["m_trend_old"].fillna(0.0), atol=1e-12
        )
        assert changed.any()

    def test_empty_qfq_is_noop(self, monkeypatch, test_db) -> None:
        service = _make_service(monkeypatch, _FakeProvider(), test_db)
        out = service.refresh_rolling_trend("NOPE.SS", full=True)
        assert out == {"symbol": "NOPE.SS", "rows": 0}


class TestDailyUpdateHook:
    def test_raw_update_triggers_refresh(self, monkeypatch, test_db) -> None:
        # 存量日K 300 天；provider 给出第 301 天的新 bar
        daily = _make_daily(301)
        old, new_bar = daily.iloc[:300], daily.iloc[300:]
        provider = _FakeProvider({("AAA.SS", "none"): new_bar})
        service = _make_service(monkeypatch, provider, test_db)
        _seed_symbol(service, test_db, "AAA.SS", old)
        monkeypatch.setattr(service, "sync_ex_factors", lambda symbols, db=None: ({}, []))

        result = service.ensure_daily_history(
            "AAA.SS", date(2024, 1, 1), new_bar["time"].iloc[0].date()
        )
        assert result["status"] == "updated"
        # 表内无存量 → 首次维护即整段建满，与全量重算逐点一致
        _assert_table_matches(test_db, "AAA.SS", daily)

    def test_refresh_failure_does_not_break_daily_update(self, monkeypatch, test_db) -> None:
        daily = _make_daily(301)
        old, new_bar = daily.iloc[:300], daily.iloc[300:]
        provider = _FakeProvider({("AAA.SS", "none"): new_bar})
        service = _make_service(monkeypatch, provider, test_db)
        _seed_symbol(service, test_db, "AAA.SS", old)
        monkeypatch.setattr(service, "sync_ex_factors", lambda symbols, db=None: ({}, []))
        monkeypatch.setattr(
            service, "refresh_rolling_trend",
            MagicMock(side_effect=RuntimeError("boom")),
        )
        result = service.ensure_daily_history(
            "AAA.SS", date(2024, 1, 1), new_bar["time"].iloc[0].date()
        )
        assert result["status"] == "updated"  # 趋势值刷新失败不影响日更结果
        service.refresh_rolling_trend.assert_called()
