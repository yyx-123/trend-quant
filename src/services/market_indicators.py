"""Single-symbol indicator suite for charting and detail views (service layer).

Moved out of app.routers.market_view: the router now only handles HTTP;
all computation delegates to core.indicators / core.trend.
"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from core import indicators as core_ind
from core.numfmt import number6_or_none
from core.strategy_config import get_strategy_config
from core.trend import calculate_trend_score_series

MA_PERIODS = (5, 10, 20, 30, 40, 60, 120, 200)
ATR_PERIODS = (20,)
BIAS_PERIODS = (6, 12, 24)
VOL_MA_PERIODS = (5, 10)
TREND_MA_PERIODS = (5, 10)
DEFAULT_RSI_PERIOD = 14

# E-BIAS（均线偏离度·减法版）：周期锁定 20 日，与广发策略原文及通达信
# 复刻口径一致（见 core.indicators.e_bias）。
E_BIAS_PERIOD = 20
# 参考线取自广发策略《如何区分主线是调整还是终结？》与《6大指标看居民入市》
# 的减法版阈值。**该阈值来自 2012 年以来 A 股行业指数的分组统计，未在本
# 项目 ETF 池上标定** —— 仅作看图辅助，不是已验证的交易信号，前端必须
# 如实标注来源（改阈值前请先做回测标定）。
E_BIAS_LINES: tuple[tuple[float, str], ...] = (
    (15.0, "过热"),
    (5.0, "失速"),
    (-5.0, "止损参考"),
    (0.0, "零轴"),
)
E_BIAS_LINE_NOTE = "参考线取自广发策略行业指数口径，未在本 ETF 池标定"


def _num(value: object) -> float | None:
    return number6_or_none(value)


def _series(values: Iterable[object]) -> list[float | None]:
    return [_num(v) for v in values]


def trend_config(overrides: dict | None = None) -> dict:
    cfg = get_strategy_config()
    cfg.update(overrides or {})
    return cfg


def compute_trend_indicator(df: pd.DataFrame, cfg: dict) -> dict:
    """Trend score series for charting — thin wrapper over core.trend."""
    series = calculate_trend_score_series(df, cfg)
    score_series = series["trend_score"].astype("float64")
    ma = {
        str(period): _series(series[f"trend_ma{period}"])
        for period in TREND_MA_PERIODS
    }
    return {
        "score": _series(score_series),
        "ma": ma,
        "price_direction": _series(series["price_direction"]),
        "confidence": _series(series["confidence"]),
        "config": {
            "n_short": int(cfg.get("n_short", 3)),
            "n_mid": int(cfg.get("n_mid", 5)),
            "n_long": int(cfg.get("n_long", 8)),
            "atr_period": int(cfg.get("atr_period", 20)),
        },
    }


def compute_market_indicators(
    df: pd.DataFrame,
    trend_cfg: dict | None = None,
    rsi_period: int = DEFAULT_RSI_PERIOD,
) -> dict:
    """Full indicator suite for one symbol's K-line history."""
    close = pd.to_numeric(df["close"], errors="coerce")
    volume = pd.to_numeric(df.get("volume", pd.Series(index=df.index)), errors="coerce")

    ma = {
        str(period): _series(core_ind.sma(close, period))
        for period in MA_PERIODS
    }

    boll_out = core_ind.bollinger(close)
    boll = {
        "mid": _series(boll_out["mid"]),
        "upper": _series(boll_out["up"]),
        "lower": _series(boll_out["dn"]),
    }

    macd_out = core_ind.macd(close, warmup=False)
    macd = {
        "dif": _series(macd_out["dif"]),
        "dea": _series(macd_out["dea"]),
        "bar": _series(macd_out["hist"]),
    }

    bias: dict[str, list[float | None]] = {}
    for period in BIAS_PERIODS:
        bias[str(period)] = _series(core_ind.bias(close, period) * 100)

    # E-BIAS：core 出 decimal，展示层 ×100 转百分比（与上面 bias 同约定）。
    # 盘中场景 df 已含当日合成K线（routers/market_view、symbol_detail 的
    # intraday overlay），此处无需特判即得盘中实时值。
    e_bias = {
        "series": _series(core_ind.e_bias(close, E_BIAS_PERIOD) * 100),
        "period": E_BIAS_PERIOD,
        "lines": [{"value": value, "label": label} for value, label in E_BIAS_LINES],
        "note": E_BIAS_LINE_NOTE,
    }

    volume_ma = {
        str(period): _series(core_ind.sma(volume, period))
        for period in VOL_MA_PERIODS
    }
    rsi = {
        "series": _series(core_ind.rsi(close, rsi_period)),
        "period": rsi_period,
    }
    atr_values = {
        str(period): _series(core_ind.atr(df, period=period))
        for period in ATR_PERIODS
    }

    return {
        "ma": ma,
        "atr": atr_values,
        "boll": boll,
        "macd": macd,
        "bias": bias,
        "e_bias": e_bias,
        "volume_ma": volume_ma,
        "rsi": rsi,
        "trend": compute_trend_indicator(df, trend_config(trend_cfg)),
    }
