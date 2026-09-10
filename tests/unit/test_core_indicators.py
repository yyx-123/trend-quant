"""Golden-master parity tests for core/indicators.py.

The ``ref_*`` functions are verbatim copies of the legacy implementations
(market_view inline, strategy/indicators, rule_backtest/indicators) frozen
at the unification point. They pin the current behavior so the unified
library can never silently drift from what the system computes today.

Approved semantic changes (master plan v1.1) are asserted as relationships
rather than equality:
- rule_backtest RSI: Cutler (rolling mean) -> Wilder (documented difference)
- rule_backtest MACD histogram: x1 -> x2 (exact 2x relationship)
- BIAS display: decimal -> percent (exact 100x relationship)
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.indicators import (
    atr,
    bias,
    bollinger,
    e_bias,
    efficiency_ratio,
    ema,
    macd,
    momentum_return,
    rsi,
    sma,
)

# ---------------------------------------------------------------------------
# Verbatim reference copies of legacy implementations
# ---------------------------------------------------------------------------


def ref_atr_strategy(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Copy of strategy/indicators.atr."""
    if df.empty:
        return pd.Series(dtype=float)
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift(1)).abs()
    low_close = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=1).mean()


def ref_er_strategy(series: pd.Series, period: int = 10) -> pd.Series:
    """Copy of strategy/indicators.efficiency_ratio."""
    if series.empty:
        return pd.Series(dtype=float)
    change = (series - series.shift(period)).abs()
    volatility = series.diff().abs().rolling(period, min_periods=1).sum()
    er = change / volatility.replace(0, np.nan)
    return er.fillna(0.0)


def ref_market_ema(series: pd.Series, span: int) -> pd.Series:
    """Copy of market_view._ema."""
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def ref_market_rsi(close: pd.Series, period: int) -> pd.Series:
    """Copy of market_view._rsi (Wilder)."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    out = out.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    out = out.mask((avg_loss == 0) & (avg_gain == 0), 50.0)
    return out


def ref_market_macd(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Copy of market_view MACD block (bar = (DIF-DEA) * 2)."""
    ema_short = ref_market_ema(close, 12)
    ema_long = ref_market_ema(close, 26)
    dif = ema_short - ema_long
    dea = ref_market_ema(dif, 9)
    bar = (dif - dea) * 2
    return dif, dea, bar


def ref_market_boll(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Copy of market_view BOLL block."""
    mid = close.rolling(20, min_periods=20).mean()
    std = close.rolling(20, min_periods=20).std(ddof=0)
    return mid, mid + 2 * std, mid - 2 * std


def ref_market_bias(close: pd.Series, period: int) -> pd.Series:
    """Copy of market_view BIAS block (percent)."""
    ma = close.rolling(period, min_periods=period).mean()
    return (close - ma) / ma * 100


def ref_tdx_logbias(close: pd.Series, period: int = 20) -> pd.Series:
    """Copy of earletf's Tongdaxin replication, decimal (no ×100).

        EMA20 := EMA(LN(CLOSE), 20);
        LOGBIAS: (LN(CLOSE) - EMA20) * 100;

    Pins the EMA-over-log-prices convention: ``EMA(LN(CLOSE))`` is the
    anchor, not ``LN(EMA(CLOSE))``.
    """
    logged = np.log(close)
    return logged - logged.ewm(span=period, adjust=False).mean()


def ref_ratio_bias(close: pd.Series, period: int = 20) -> pd.Series:
    """The division form 广发策略 published first: ln(C)/EMA(C) - 1.

    Kept only to demonstrate *why* the subtraction form is required — this
    is the variant the project deliberately does not ship.
    """
    return np.log(close) / close.ewm(span=period, adjust=False).mean() - 1.0


def ref_rb_rsi_cutler(series: pd.Series, period: int = 14) -> pd.Series:
    """Copy of rule_backtest rsi (Cutler, rolling mean)."""
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).rolling(period, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def ref_rb_macd_last(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """Copy of rule_backtest macd (ewm without min_periods, histogram x1)."""
    f = series.ewm(span=fast, adjust=False).mean()
    s = series.ewm(span=slow, adjust=False).mean()
    line = f - s
    sig = line.ewm(span=signal, adjust=False).mean()
    hist = line - sig
    return {"line": line, "signal": sig, "histogram": hist}


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------


def _make_bars(closes: np.ndarray, seed_noise: float = 0.5) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    high = closes + np.abs(rng.normal(0, seed_noise, len(closes)))
    low = closes - np.abs(rng.normal(0, seed_noise, len(closes)))
    return pd.DataFrame({"close": closes, "high": high, "low": low})


@pytest.fixture
def uptrend() -> pd.Series:
    rng = np.random.default_rng(42)
    steps = rng.normal(0.15, 1.0, 300)
    return pd.Series(100 + np.cumsum(steps), name="close")


@pytest.fixture
def jumpy() -> pd.Series:
    """Volatile series with a dividend-like downward jump in the middle."""
    rng = np.random.default_rng(123)
    steps = rng.normal(0, 2.0, 300)
    prices = 100 + np.cumsum(steps)
    prices[150:] *= 0.85  # ex-dividend style gap
    return pd.Series(prices, name="close")


@pytest.fixture
def with_nans(uptrend: pd.Series) -> pd.Series:
    s = uptrend.copy()
    s.iloc[10] = np.nan
    s.iloc[200] = np.nan
    return s


def _assert_series_equal(a: pd.Series, b: pd.Series, tol: float = 1e-12) -> None:
    assert len(a) == len(b)
    for i in range(len(a)):
        av, bv = a.iloc[i], b.iloc[i]
        if pd.isna(av) and pd.isna(bv):
            continue
        assert pd.notna(av) and pd.notna(bv), f"NaN mismatch at {i}: {av} vs {bv}"
        assert abs(av - bv) <= tol, f"value mismatch at {i}: {av} vs {bv}"


# ---------------------------------------------------------------------------
# Parity: core vs legacy page-side implementations (must be identical)
# ---------------------------------------------------------------------------


class TestParityWithLegacy:
    def test_atr_matches_strategy(self, uptrend, jumpy) -> None:
        for s in (uptrend, jumpy):
            df = _make_bars(s.to_numpy())
            _assert_series_equal(atr(df, 20), ref_atr_strategy(df, 20))

    def test_er_matches_strategy(self, uptrend, jumpy) -> None:
        _assert_series_equal(efficiency_ratio(uptrend, 10), ref_er_strategy(uptrend, 10))
        _assert_series_equal(efficiency_ratio(jumpy, 10), ref_er_strategy(jumpy, 10))

    def test_rsi_matches_market_wilder(self, uptrend, jumpy) -> None:
        _assert_series_equal(rsi(uptrend, 14), ref_market_rsi(uptrend, 14))
        _assert_series_equal(rsi(jumpy, 14), ref_market_rsi(jumpy, 14))

    def test_ema_chart_warmup(self, uptrend) -> None:
        _assert_series_equal(ema(uptrend, 20, min_periods=20), ref_market_ema(uptrend, 20))

    def test_macd_matches_market(self, uptrend, jumpy) -> None:
        for s in (uptrend, jumpy):
            out = macd(s, warmup=False)
            dif, dea, bar = ref_market_macd(s)
            _assert_series_equal(out["dif"], dif)
            _assert_series_equal(out["dea"], dea)
            _assert_series_equal(out["hist"], bar)

    def test_boll_matches_market(self, uptrend, jumpy) -> None:
        for s in (uptrend, jumpy):
            out = bollinger(s)
            mid, up, dn = ref_market_boll(s)
            _assert_series_equal(out["mid"], mid)
            _assert_series_equal(out["up"], up)
            _assert_series_equal(out["dn"], dn)

    def test_sma_matches_market_ma(self, uptrend) -> None:
        for p in (5, 20, 60, 200):
            _assert_series_equal(sma(uptrend, p), uptrend.rolling(p, min_periods=p).mean())

    def test_bias_decimal_vs_market_percent(self, uptrend) -> None:
        _assert_series_equal(bias(uptrend, 6) * 100, ref_market_bias(uptrend, 6))

    def test_e_bias_matches_tongdaxin_logbias(self, uptrend, jumpy) -> None:
        """E-BIAS 逐值对拍通达信复刻公式（decimal 口径，无 ×100）。"""
        for s in (uptrend, jumpy):
            _assert_series_equal(e_bias(s, 20), ref_tdx_logbias(s, 20))

    def test_sma_last_value_matches_rule_backtest(self, uptrend) -> None:
        for p in (5, 20, 60):
            expected = uptrend.dropna().tail(p).mean()
            assert abs(sma(uptrend, p).iloc[-1] - expected) <= 1e-12

    def test_ema_backtest_warmup_last_value(self, uptrend) -> None:
        ref = uptrend.ewm(span=20, adjust=False).mean()
        _assert_series_equal(ema(uptrend, 20, min_periods=0), ref)

    def test_macd_backtest_relationship(self, uptrend) -> None:
        """rule_backtest adapter contract: line/signal unchanged, hist x2."""
        ref = ref_rb_macd_last(uptrend)
        out = macd(uptrend, warmup=True)
        _assert_series_equal(out["dif"], ref["line"])
        _assert_series_equal(out["dea"], ref["signal"])
        _assert_series_equal(out["hist"], ref["histogram"] * 2)

    def test_rsi_wilder_differs_from_cutler(self, uptrend) -> None:
        """Approved semantic change: Wilder != Cutler, both in [0, 100]."""
        wilder = rsi(uptrend, 14).dropna()
        cutler = ref_rb_rsi_cutler(uptrend, 14).dropna()
        assert wilder.between(0, 100).all() and cutler.between(0, 100).all()
        assert not np.allclose(wilder.to_numpy(), cutler.to_numpy(), atol=1e-9)


# ---------------------------------------------------------------------------
# E-BIAS semantics (why the subtraction form is the only usable one)
# ---------------------------------------------------------------------------


class TestEBiasSemantics:
    def test_flat_series_is_zero(self) -> None:
        close = pd.Series([2.5] * 80)
        assert (e_bias(close, 20).abs() < 1e-12).all()

    def test_equals_log_gap_and_approximates_ratio_gap(self) -> None:
        """数值 = ln(C / EMA锚点)，并近似算术偏离 C/EMA − 1。

        「读数可直接当偏离百分比看」的依据是 ln(1+r) ≈ r，但该近似**系统性
        偏低 r²/2**（r≈9% 时约 0.4 个百分点）—— 所以只做量级校验，不做等值。
        """
        close = pd.Series([100.0] * 60 + [110.0])
        anchor = float(np.exp(np.log(close).ewm(span=20, adjust=False).mean().iloc[-1]))
        value = float(e_bias(close, 20).iloc[-1])
        assert value == pytest.approx(np.log(110.0 / anchor), abs=1e-12)
        ratio_gap = 110.0 / anchor - 1.0
        assert value == pytest.approx(ratio_gap, abs=1e-2)
        # 对数读数偏低，差值首位项 = r²/2（余项 ~r³/3，故容差取 1e-3）
        assert value < ratio_gap
        assert (ratio_gap - value) == pytest.approx(ratio_gap**2 / 2, abs=1e-3)

    def test_scale_invariant(self, uptrend) -> None:
        """乘性缩放下逐值不变 —— 低价 ETF 与高价指数可同尺度比较。"""
        _assert_series_equal(e_bias(uptrend * 0.001, 20), e_bias(uptrend, 20), tol=1e-9)

    def test_ratio_form_not_scale_invariant(self, uptrend) -> None:
        """除法版在同一缩放下完全变形（其除数恰是 ln(EMA)）。"""
        assert not np.allclose(
            ref_ratio_bias(uptrend * 0.001).to_numpy(),
            ref_ratio_bias(uptrend).to_numpy(),
            atol=1e-6,
        )

    def test_subtraction_form_survives_prices_below_one(self) -> None:
        """1 元以下的上涨趋势：减法版符号正确，除法版符号颠倒。

        本项目标的全是 ETF，含 1 元附近品种 —— 这是不实现除法版的直接原因。
        """
        close = pd.Series(np.linspace(0.5, 0.8, 60))
        # 首根锚点即自身、偏离为 0，故从第 2 根起断言严格为正。
        assert (e_bias(close, 20).iloc[1:] > 0).all()
        assert (ref_ratio_bias(close, 20) < 0).all()

    def test_ema_is_taken_over_log_prices(self, uptrend) -> None:
        """口径锁定：EMA(ln C) ≠ ln(EMA C)，两者必须是不同的序列。"""
        log_of_ema = np.log(ema(uptrend, 20, min_periods=0))
        assert not np.allclose(
            e_bias(uptrend, 20).to_numpy(),
            (np.log(uptrend) - log_of_ema).to_numpy(),
            atol=1e-9,
        )

    def test_no_warmup_hole(self, uptrend) -> None:
        """min_periods=0：首根起即有值，无 NaN 空洞（与 ema 族其余指标一致）。"""
        assert e_bias(uptrend, 20).notna().all()


# ---------------------------------------------------------------------------
# Independent unit tests (prove the new implementation right, not just equal)
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_inputs(self) -> None:
        empty_s = pd.Series(dtype=float)
        empty_df = pd.DataFrame()
        assert sma(empty_s, 20).empty
        assert ema(empty_s, 20).empty
        assert atr(empty_df).empty
        assert efficiency_ratio(empty_s).empty
        assert rsi(empty_s).empty
        assert macd(empty_s)["dif"].empty
        assert bollinger(empty_s)["mid"].empty
        assert momentum_return(empty_s).empty
        assert e_bias(empty_s, 20).empty

    def test_all_nan_series(self) -> None:
        s = pd.Series([np.nan] * 50)
        assert sma(s, 5).isna().all()
        assert ema(s, 5).isna().all()
        assert rsi(s, 14).isna().all()
        assert e_bias(s, 5).isna().all()

    def test_single_element(self) -> None:
        s = pd.Series([10.0])
        assert pd.isna(sma(s, 5).iloc[-1])
        assert ema(s, 5).iloc[-1] == pytest.approx(10.0)
        assert pd.isna(rsi(s, 14).iloc[-1])
        # 单元素：锚点即自身，偏离度恒为 0（min_periods=0，无 NaN）。
        assert e_bias(s, 5).iloc[-1] == pytest.approx(0.0)

    def test_short_series_below_period(self) -> None:
        s = pd.Series([1.0, 2.0, 3.0])
        assert sma(s, 20).isna().all()
        assert bollinger(s, 20)["mid"].isna().all()
        assert rsi(s, 14).isna().all()
        assert len(e_bias(s, 20)) == 3
        assert e_bias(s, 20).notna().all()

    def test_e_bias_non_positive_prices_do_not_produce_inf(self) -> None:
        """0/负价格不得产生 -inf（会经 EMA 递推污染后续全部值）。"""
        s = pd.Series([10.0, 10.0, 0.0, -1.0, 10.0, 10.0])
        out = e_bias(s, 3)
        assert not np.isinf(out.to_numpy()).any()
        assert pd.isna(out.iloc[2]) and pd.isna(out.iloc[3])

    def test_sma_known_values(self) -> None:
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        assert sma(s, 3).iloc[-1] == pytest.approx(4.0)
        assert pd.isna(sma(s, 3).iloc[1])

    def test_atr_constant_bars(self) -> None:
        df = pd.DataFrame({"high": [11.0] * 30, "low": [9.0] * 30, "close": [10.0] * 30})
        assert atr(df, 20).iloc[-1] == pytest.approx(2.0)

    def test_rsi_all_gains(self) -> None:
        s = pd.Series(np.arange(1.0, 40.0))
        assert rsi(s, 14).iloc[-1] == pytest.approx(100.0)

    def test_rsi_flat(self) -> None:
        s = pd.Series([10.0] * 40)
        assert rsi(s, 14).iloc[-1] == pytest.approx(50.0)

    def test_er_perfect_trend(self) -> None:
        s = pd.Series(np.arange(1.0, 50.0))
        assert efficiency_ratio(s, 10).iloc[-1] == pytest.approx(1.0)

    def test_er_oscillation(self) -> None:
        s = pd.Series([10.0, 11.0] * 20)
        assert efficiency_ratio(s, 10).iloc[-1] == pytest.approx(0.0)

    def test_momentum_return_known_values(self) -> None:
        s = pd.Series([100.0] * 10 + [110.0])
        assert momentum_return(s, 10).iloc[-1] == pytest.approx(0.1)

    def test_dividend_jump_no_exception(self, jumpy) -> None:
        df = _make_bars(jumpy.to_numpy())
        assert pd.notna(atr(df, 20).iloc[-1])
        assert pd.notna(rsi(jumpy, 14).iloc[-1])
        assert pd.notna(macd(jumpy)["hist"].iloc[-1])

    def test_nans_do_not_crash(self, with_nans) -> None:
        df = _make_bars(with_nans.to_numpy())
        sma_out = sma(with_nans, 5)
        rsi_out = rsi(with_nans, 14)
        macd_out = macd(with_nans)
        atr_out = atr(df, 20)
        # 输出长度与输入对齐，且末根 ATR 为有限值（NaN 不传播为崩溃/inf）
        assert len(sma_out) == len(with_nans)
        assert len(rsi_out) == len(with_nans)
        assert len(macd_out["dif"]) == len(with_nans)
        assert len(atr_out) == len(df)
        assert np.isfinite(atr_out.iloc[-1])

    # —— 自 test_indicators.py 合并的独有用例（P2-25 重复测试合并）——
    def test_atr_period_one(self) -> None:
        """period=1：每根 ATR 等于当根 TR。"""
        df = pd.DataFrame({"high": [12.0, 13.0], "low": [8.0, 9.0], "close": [10.0, 11.0]})
        result = atr(df, period=1)
        assert result.iloc[0] == pytest.approx(4.0, abs=0.01)

    def test_atr_zero_true_range(self) -> None:
        """high=low=close 时 TR=0、ATR=0（与 test_atr_constant_bars 的非零变体互补）。"""
        df = pd.DataFrame({"high": [10.0] * 10, "low": [10.0] * 10, "close": [10.0] * 10})
        assert atr(df, period=5).iloc[-1] == 0.0

    def test_atr_single_row(self) -> None:
        df = pd.DataFrame({"high": [11.0], "low": [9.0], "close": [10.0]})
        result = atr(df, period=20)
        assert len(result) == 1
        assert result.iloc[0] == 2.0

    def test_er_noisy_sideways(self) -> None:
        rng = np.random.default_rng(123)
        s = pd.Series(10.0 + rng.normal(0, 0.5, 50), dtype=float)
        assert efficiency_ratio(s, period=10).iloc[-1] < 0.5

    def test_er_period_larger_than_data(self) -> None:
        s = pd.Series([10.0, 11.0, 12.0], dtype=float)
        assert efficiency_ratio(s, period=10).iloc[-1] == 0.0
