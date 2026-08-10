"""一致性守护：盘中趋势值的两个实现必须产出相同结果。

``compute_intraday_trend_cached``（O(1) 锚点递推，实时看板 600+ 标的的
主路径）与 ``compute_intraday_trend_score``（全量重算，intraday overlay
与看板兜底路径）是刻意的性能分层双实现。任何一边的公式/参数改动若
漏同步另一边，本测试立即失败 —— 「exact-equivalent」是测试保障，
不是注释承诺。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.strategy_config import get_strategy_config
from data.indicator_store import compute_indicator_frame
from data.intraday_service import (
    compute_intraday_trend_cached,
    compute_intraday_trend_score,
)


def _make_bars(seed: int = 7, n: int = 300) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    closes = 100 + np.cumsum(rng.normal(0.05, 1.5, n))
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {
            "time": dates,
            "open": closes * (1 + rng.normal(0, 0.002, n)),
            "high": closes + np.abs(rng.normal(0, 0.5, n)),
            "low": closes - np.abs(rng.normal(0, 0.5, n)),
            "close": closes,
            "volume": np.abs(rng.normal(1e6, 2e5, n)),
            "amount": np.abs(rng.normal(2e6, 4e5, n)),
        }
    )


def _quote(bars: pd.DataFrame, move_pct: float) -> dict:
    prev_close = float(bars["close"].iloc[-1])
    price = prev_close * (1 + move_pct)
    return {
        "symbol": "T.SS",
        "price": price,
        "open": prev_close * (1 + move_pct / 2),
        "high": max(price, prev_close) * 1.003,
        "low": min(price, prev_close) * 0.997,
        "volume": 8e5,
        "amount": 1.6e6,
        "ts": pd.Timestamp(bars["time"].iloc[-1]).date().isoformat() + "T14:30:00",
    }


@pytest.mark.parametrize("seed", [7, 42])
@pytest.mark.parametrize("move_pct", [0.03, -0.025, 0.0])
def test_cached_matches_full_history(seed: int, move_pct: float) -> None:
    bars = _make_bars(seed=seed)
    quote = _quote(bars, move_pct)
    cfg = get_strategy_config()

    full = compute_intraday_trend_score(bars.copy(), quote, cfg)
    assert full.get("ok"), f"full-history path failed: {full}"

    # 模拟生产环境：cache_row = 指标缓存最后一行（与全量历史同一份K线算出），
    # tail = 1 年尾部K线（此处全量即尾部）。
    cache_row = dict(compute_indicator_frame(bars).iloc[-1])
    cached = compute_intraday_trend_cached("T.SS", quote, bars.copy(), cache_row, cfg)
    assert cached.get("ok"), f"cached path failed: {cached}"

    assert cached["trend_score"] == pytest.approx(full["trend_score"], rel=1e-9, abs=1e-9), (
        f"trend_score drift: cached={cached['trend_score']} vs full={full['trend_score']}"
    )
    assert cached["price_direction"] == pytest.approx(full["price_direction"], abs=1e-9)
    assert cached["confidence"] == pytest.approx(full["confidence"], rel=1e-9, abs=1e-9)


def test_stale_anchor_falls_back() -> None:
    """锚点日期落后于尾部K线（行情已更新、指标缓存未重建）时必须拒绝，
    由调用方回退全量计算 —— 不允许旧锚点 × 新收盘价的混算结果流出。"""
    bars = _make_bars()
    quote = _quote(bars, 0.01)
    cfg = get_strategy_config()

    cache_row = dict(compute_indicator_frame(bars).iloc[-1])
    # 人为把锚点行改到 5 天前，模拟指标缓存滞后。
    cache_row["time"] = pd.Timestamp(bars["time"].iloc[-1]) - pd.Timedelta(days=5)

    cached = compute_intraday_trend_cached("T.SS", quote, bars.copy(), cache_row, cfg)
    assert cached.get("ok") is False
    assert cached.get("reason") == "stale_anchor"
