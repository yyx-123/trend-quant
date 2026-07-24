from __future__ import annotations

import random

import numpy as np
import pandas as pd

from core import indicators as core_ind
from core.trend import calculate_trend_score_series
from rule_backtest import indicators
from rule_backtest.models import PositionState


class ValueResolver:
    def __init__(self, strategy_cfg: dict | None = None) -> None:
        self.strategy_cfg = strategy_cfg or {}
        # RNG 实例随 resolver 生命周期（= 单次回测运行）存活，保证跨天序列独立；
        # seed 为 None 时 random.Random 从 OS 熵源播种，每次运行结果不同。
        self._rngs: dict[int | None, random.Random] = {}
        # Hot-path memoization (P1.3): full indicator series computed once
        # per (name, params) over the run's complete history; per-day
        # resolution is an indexed lookup. Debug resolutions bypass the
        # cache to keep exact trace fidelity.
        self._context_bars: pd.DataFrame | None = None
        self._series_cache: dict[tuple, pd.Series] = {}

    def set_context_bars(self, bars: pd.DataFrame) -> None:
        self._context_bars = bars
        self._series_cache = {}

    def resolve(
        self,
        spec: dict,
        bars: pd.DataFrame,
        position: PositionState,
        debug: bool = False,
    ) -> tuple[float | None, dict]:
        spec_type = str(spec.get("type", "")).strip()
        if spec_type == "price":
            field = str(spec.get("field", "close")).strip()
            value = indicators.latest_field(bars, field)
            return value, {"type": "price", "field": field, "value": value} if debug else {}

        if spec_type == "literal":
            try:
                value = float(spec.get("value"))
            except Exception:
                value = None
            return value, {"type": "literal", "value": value} if debug else {}

        if spec_type == "state_value":
            return self._resolve_state_value(
                spec=spec, position=position, bar_idx=len(bars) - 1, debug=debug
            )

        if spec_type == "indicator":
            return self._resolve_indicator(spec=spec, bars=bars, debug=debug)

        return None, {"reason": "unsupported_value_spec", "spec": spec} if debug else {}

    def _resolve_state_value(
        self,
        spec: dict,
        position: PositionState,
        bar_idx: int,
        debug: bool,
    ) -> tuple[float | None, dict]:
        name = str(spec.get("name", "")).strip()
        value = None
        if name == "entry_price":
            value = position.entry_price if position.is_open else None
        elif name == "hard_stop":
            value = position.hard_stop if position.is_open else None
        elif name == "highest_high_since_entry":
            value = position.highest_high_since_entry if position.is_open else None
        elif name == "chandelier_stop":
            value = position.chandelier_stop if position.is_open else None
        elif name == "days_since_last_exit":
            # 离场冷却期：空仓也可解析（区别于上面四个持仓期状态值）。
            # 从未卖出 → None；condition_engine 对 `>=` 特判为通过
            # （首次入场不受冷却限制）。bar_idx 与 last_exit_bar_idx 同
            # 为 all_bars 坐标系，卖出当天差值为 0。
            if position.last_exit_bar_idx is not None:
                value = float(bar_idx - position.last_exit_bar_idx)
        trace = {"type": "state_value", "name": name, "value": value}
        return value, trace if debug else {}

    def _resolve_indicator(self, spec: dict, bars: pd.DataFrame, debug: bool) -> tuple[float | None, dict]:
        name = str(spec.get("name", "")).strip()
        params = spec.get("params", {}) if isinstance(spec.get("params", {}), dict) else {}
        field = str(params.get("field", "close")).strip()

        # Hot path: memoized full-series lookup (debug keeps legacy per-day
        # computation for exact trace fidelity; random_uniform is per-day
        # by design and must never be cached).
        if not debug and name in _MEMOIZABLE_INDICATORS and self._context_bars is not None:
            series = self._series_for(name, params, field)
            idx = len(bars) - 1
            return self._value_at(name, series, idx, params), {}

        return self._resolve_indicator_legacy(name, params, field, bars, debug)

    def atr_value_at(self, idx: int, period: int) -> float | None:
        """Memoized ATR value at a day index, for stop-state bookkeeping."""
        if self._context_bars is None:
            return None
        series = self._series_for("atr", {"period": period}, "close")
        return self._value_at("atr", series, idx, {"period": period})

    # ------------------------------------------------------------------
    # Memoized full-series computation
    # ------------------------------------------------------------------
    def _series_for(self, name: str, params: dict, field: str) -> pd.Series:
        key = (name, field, tuple(sorted(params.items())))
        if key in self._series_cache:
            return self._series_cache[key]

        bars = self._context_bars
        field_series = indicators.field_series(bars, field) if field != "volume" else indicators.field_series(bars, "volume")

        if name == "sma" or name == "volume_sma":
            target = indicators.field_series(bars, "volume") if name == "volume_sma" else field_series
            series = core_ind.sma(target, int(params.get("period", 20)))
        elif name == "ema":
            series = core_ind.ema(field_series, int(params.get("period", 20)), min_periods=0)
        elif name == "bias":
            series = core_ind.bias(field_series, int(params.get("period", 20)))
        elif name == "bias_atr_normed":
            ma = core_ind.sma(field_series, int(params.get("period", 20)))
            atr_series = core_ind.atr(bars, int(params.get("atr_period", 20)))
            series = (field_series - ma) / atr_series.replace(0, np.nan)
        elif name == "atr":
            series = core_ind.atr(bars, int(params.get("period", 20)))
        elif name == "rsi":
            series = core_ind.rsi(field_series, int(params.get("period", 14)))
        elif name in {"macd_line", "macd_signal", "macd_histogram"}:
            out = core_ind.macd(
                field_series,
                fast_period=int(params.get("fast_period", 12)),
                slow_period=int(params.get("slow_period", 26)),
                signal_period=int(params.get("signal_period", 9)),
                warmup=True,
            )
            series = out[{"macd_line": "dif", "macd_signal": "dea", "macd_histogram": "hist"}[name]]
        elif name in {"bollinger_upper", "bollinger_middle", "bollinger_lower"}:
            out = core_ind.bollinger(
                field_series,
                period=int(params.get("period", 20)),
                std_mul=float(params.get("std_mul", 2.0)),
            )
            series = out[{"bollinger_upper": "up", "bollinger_middle": "mid", "bollinger_lower": "dn"}[name]]
        elif name == "volume_ratio":
            volume = indicators.field_series(bars, "volume")
            series = volume / core_ind.sma(volume, int(params.get("period", 20))).replace(0, np.nan)
        elif name == "momentum_return":
            series = core_ind.momentum_return(field_series, int(params.get("period", 20)))
        elif name == "trend_score":
            series = calculate_trend_score_series(bars, self.strategy_cfg)["trend_score"]
        elif name in {"trend_score_sma", "trend_score_ema"}:
            trend = self._series_for("trend_score", params={}, field="close")
            period = int(params.get("period", 5))
            if name == "trend_score_sma":
                series = trend.rolling(period, min_periods=period).mean()
            else:
                series = _rolling_ewm_last(trend, period)
        else:  # pragma: no cover - guarded by _MEMOIZABLE_INDICATORS
            raise ValueError(f"unsupported memoized indicator: {name}")

        self._series_cache[key] = series
        return series

    @staticmethod
    def _value_at(name: str, series: pd.Series, idx: int, params: dict) -> float | None:
        if idx < 0 or idx >= len(series):
            return None
        # Warmup masks replicating the legacy per-day "insufficient_bars"
        # behavior exactly (legacy returns None, not a warmup value).
        if name == "ema" and idx + 1 < int(params.get("period", 20)):
            return None
        if name == "rsi" and idx + 1 < int(params.get("period", 14)) + 1:
            return None
        if name in {"macd_line", "macd_signal", "macd_histogram"}:
            min_rows = max(int(params.get("fast_period", 12)), int(params.get("slow_period", 26))) + int(
                params.get("signal_period", 9)
            )
            if idx + 1 < min_rows:
                return None
        return indicators.safe_float(series.iloc[idx])

    # ------------------------------------------------------------------
    # Legacy per-day computation (exact trace fidelity)
    # ------------------------------------------------------------------
    def _resolve_indicator_legacy(self, name: str, params: dict, field: str, bars: pd.DataFrame, debug: bool) -> tuple[float | None, dict]:

        if name == "sma":
            value, trace = indicators.sma(bars, field=field, period=int(params.get("period", 20)))
        elif name == "ema":
            value, trace = indicators.ema(bars, field=field, period=int(params.get("period", 20)))
        elif name == "bias":
            value, trace = indicators.bias(bars, field=field, period=int(params.get("period", 20)))
        elif name == "bias_atr_normed":
            value, trace = indicators.bias_atr_normed(
                bars,
                field=field,
                period=int(params.get("period", 20)),
                atr_period=int(params.get("atr_period", 20)),
            )
        elif name == "atr":
            value, trace = indicators.atr(bars, period=int(params.get("period", 20)))
        elif name == "rsi":
            value, trace = indicators.rsi(bars, field=field, period=int(params.get("period", 14)))
        elif name in {"macd_line", "macd_signal", "macd_histogram"}:
            values, trace = indicators.macd(
                bars,
                field=field,
                fast_period=int(params.get("fast_period", 12)),
                slow_period=int(params.get("slow_period", 26)),
                signal_period=int(params.get("signal_period", 9)),
            )
            key = name.replace("macd_", "")
            value = values.get(key)
        elif name in {"bollinger_upper", "bollinger_middle", "bollinger_lower"}:
            values, trace = indicators.bollinger(
                bars,
                field=field,
                period=int(params.get("period", 20)),
                std_mul=float(params.get("std_mul", 2.0)),
            )
            key = name.replace("bollinger_", "")
            value = values.get(key)
        elif name == "volume_sma":
            value, trace = indicators.sma(bars, field="volume", period=int(params.get("period", 20)))
        elif name == "volume_ratio":
            volume = indicators.latest_field(bars, "volume")
            ma_value, ma_trace = indicators.sma(bars, field="volume", period=int(params.get("period", 20)))
            value = (volume / ma_value) if volume is not None and ma_value not in (None, 0) else None
            trace = {"volume": volume, "volume_sma": ma_value, "sma": ma_trace, "value": value}
        elif name == "momentum_return":
            value, trace = indicators.momentum_return(bars, field=field, period=int(params.get("period", 20)))
        elif name == "trend_score":
            value, trace = indicators.trend_score(bars, cfg=self.strategy_cfg)
        elif name == "trend_score_sma":
            value, trace = indicators.trend_score_series(
                bars, period=int(params.get("period", 5)), mode="sma", cfg=self.strategy_cfg
            )
        elif name == "trend_score_ema":
            value, trace = indicators.trend_score_series(
                bars, period=int(params.get("period", 5)), mode="ema", cfg=self.strategy_cfg
            )
        elif name == "random_uniform":
            value, trace = self._random_uniform(params)
        else:
            value, trace = None, {"reason": "unsupported_indicator", "name": name}

        if debug:
            return value, {"type": "indicator", "name": name, "params": dict(params), "trace": trace, "value": value}
        return value, {}

    def _random_uniform(self, params: dict) -> tuple[float, dict]:
        seed = params.get("seed")
        if seed is not None:
            seed = int(seed)
        if seed not in self._rngs:
            self._rngs[seed] = random.Random(seed)
        value = self._rngs[seed].random()
        return value, {"seed": seed, "value": value}


# Deterministic, price/volume-derived indicators eligible for full-series
# memoization. random_uniform is deliberately excluded (per-day RNG).
_MEMOIZABLE_INDICATORS = frozenset(
    {
        "sma", "ema", "bias", "bias_atr_normed", "atr", "rsi",
        "macd_line", "macd_signal", "macd_histogram",
        "bollinger_upper", "bollinger_middle", "bollinger_lower",
        "volume_sma", "volume_ratio", "momentum_return",
        "trend_score", "trend_score_sma", "trend_score_ema",
    }
)


def _rolling_ewm_last(series: pd.Series, span: int) -> pd.Series:
    """Value of ewm(span, adjust=False) recomputed within each trailing window.

    Matches rule_backtest's trend_score_series(mode="ema"), which takes the
    last ``span`` trend scores and EMAs just those. For adjust=False the
    windowed EMA's last value is a fixed weighted dot product:
    y_last = (1-a)^(n-1)*x0 + sum_{i=1}^{n-1} a*(1-a)^(n-1-i)*x_i.
    NaN in any window position propagates (per-day path returns None then).
    """
    alpha = 2.0 / (span + 1.0)
    weights = np.array(
        [(1 - alpha) ** (span - 1)]
        + [alpha * (1 - alpha) ** (span - 1 - i) for i in range(1, span)]
    )

    def _dot(window: np.ndarray) -> float:
        if np.isnan(window).any():
            return np.nan
        return float(np.dot(window, weights))

    return series.rolling(span, min_periods=span).apply(_dot, raw=True)
