"""Round-trip 交易日志 + ATR T-1 口径 + 止损跳空成交修正（方案 2026-08-30 §2/§7）。

引擎层单测：构造已知走势，断言 MAE/MFE/R 倍数/post-exit 漂移、
入场 ATR 不含当根 K 线、跳空日按开盘价成交。
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from rule_backtest import (
    BacktestExecutionConfig,
    RuleBacktestRequest,
    SingleSymbolAllInBacktestEngine,
)

START = date(2026, 1, 1)


def make_bars(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """rows = (open, high, low, close) 逐日。"""
    return pd.DataFrame(
        {
            "date": [START + timedelta(days=i) for i in range(len(rows))],
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": [1000 + i for i in range(len(rows))],
            "amount": [r[3] * (1000 + i) for i, r in enumerate(rows)],
        }
    )


def hard_stop_strategy(atr_period: int = 1, atr_mul: float = 1.0) -> dict:
    return {
        "id": "rt_test",
        "entry": {
            "type": "group",
            "combinator": "all",
            "children": [
                {
                    "left": {"type": "literal", "value": 1},
                    "operator": ">=",
                    "right": {"type": "literal", "value": 0},
                }
            ],
        },
        "exit": {
            "type": "group",
            "combinator": "all",
            "children": [
                {
                    "left": {"type": "price", "field": "close"},
                    "operator": "<=",
                    "right": {
                        "type": "state_value",
                        "name": "hard_stop",
                        "params": {"atr_period": atr_period, "atr_mul": atr_mul},
                    },
                }
            ],
        },
    }


def run_engine(bars: pd.DataFrame, **kwargs) -> dict:
    kwargs.setdefault("execution", BacktestExecutionConfig())
    return SingleSymbolAllInBacktestEngine().run(
        RuleBacktestRequest(
            strategy=hard_stop_strategy(),
            symbol="TEST",
            bars=bars,
            **kwargs,
        )
    )


# ── 场景 A：round-trip 全字段 ─────────────────────────────
# d0 预热（TR=2）；d1 入场 close=100 exec=100.2，止损=100.2-2=98.2；
# d2 未触发（low 96 → MAE 候选）；d3 close 97.5 ≤ 98.2 止损卖出（open 98.6 无跳空，low 95）；
# d4~d8 连涨用于 post-exit 漂移（5 根可见，10/20 根不足 → None）。
SCENARIO_A = [
    (100.0, 101.0, 99.0, 100.0),
    (100.0, 102.0, 98.0, 100.0),
    (99.0, 100.0, 96.0, 99.0),
    (98.6, 98.8, 95.0, 97.5),
    (99.0, 99.5, 98.5, 99.0),
    (101.0, 101.5, 100.5, 101.0),
    (102.0, 102.5, 101.5, 102.0),
    (103.0, 103.5, 102.5, 103.0),
    (104.0, 104.5, 103.5, 104.0),
]


def test_round_trip_fields() -> None:
    result = run_engine(make_bars(SCENARIO_A), start_date=date(2026, 1, 2))
    trips = result["round_trips"]
    # d4 起入场条件再次通过会买回，但持有到末尾未平仓 → 只有一笔 round-trip。
    assert len(trips) == 1
    rt = trips[0]

    sell = result["trades"][1]
    assert rt["exit_reason"] == "hard_stop"
    assert rt["entry_date"] == "2026-01-02"
    assert rt["exit_date"] == sell["date"]
    assert rt["entry_price"] == pytest.approx(100.2)
    assert rt["exit_price"] == pytest.approx(sell["exec_price"])
    assert rt["qty"] == sell["qty"]
    assert rt["pnl"] == pytest.approx(sell["pnl"])
    # 入场 ATR 为 T-1（d0 TR=2），不含入场日当根（d1 TR=20）
    assert rt["entry_atr"] == pytest.approx(2.0)
    assert rt["entry_atr_pct"] == pytest.approx(2.0 / 100.2)
    assert rt["hard_stop_atr_mul"] == pytest.approx(1.0)
    # MAE/MFE：持有期（含出场日）最低 95 / 最高 102
    assert rt["mae_pct"] == pytest.approx((95.0 / 100.2 - 1.0) * 100.0)
    assert rt["mfe_pct"] == pytest.approx((102.0 / 100.2 - 1.0) * 100.0)
    assert rt["mae_atr"] == pytest.approx((95.0 - 100.2) / 2.0)
    assert rt["mfe_atr"] == pytest.approx((102.0 - 100.2) / 2.0)
    assert rt["holding_days"] == 2
    assert rt["days_to_trigger"] == 2
    assert rt["asset_type"] == "etf"
    # R 倍数 = pnl / (qty × atr_mul × atr_at_entry)
    assert rt["r_multiple"] == pytest.approx(sell["pnl"] / (sell["qty"] * 1.0 * 2.0))
    # post-exit：d4~d8 五根可见，末根收盘 104
    assert rt["post_exit_ret_5d"] == pytest.approx(104.0 / sell["exec_price"] - 1.0)
    assert rt["reentry_above_entry_5d"] is True
    assert rt["post_exit_ret_10d"] is None
    assert rt["reentry_above_entry_10d"] is None
    assert rt["post_exit_ret_20d"] is None


def test_round_trip_reentry_false_when_kept_below_entry() -> None:
    rows = SCENARIO_A[:4] + [
        (97.0, 98.0, 96.5, 97.0),
        (96.5, 97.5, 96.0, 96.5),
        (96.0, 97.0, 95.5, 96.0),
        (95.5, 96.5, 95.0, 95.5),
        (95.0, 96.0, 94.5, 95.0),
    ]
    result = run_engine(make_bars(rows), start_date=date(2026, 1, 2))
    rt = result["round_trips"][0]
    assert rt["reentry_above_entry_5d"] is False
    assert rt["post_exit_ret_5d"] == pytest.approx(95.0 / rt["exit_price"] - 1.0)


# ── 场景 B：止损跳空成交修正（§7.1）────────────────────────
# d2 open=97 < 止损价 98.2 → 按开盘价成交；stop_gap_fill=False 时仍在止损价成交。
GAP_BARS = [
    (100.0, 101.0, 99.0, 100.0),
    (100.0, 101.0, 99.0, 100.0),
    (97.0, 97.5, 96.0, 96.5),
]


def test_stop_gap_fill_uses_open_price() -> None:
    result = run_engine(make_bars(GAP_BARS), start_date=date(2026, 1, 2))
    sell = result["trades"][1]
    assert sell["reason"] == "hard_stop"
    assert sell["reference_price"] == pytest.approx(97.0)
    assert sell["exec_price"] == pytest.approx(97.0 * 0.998)


def test_stop_gap_fill_disabled_fills_at_stop_price() -> None:
    result = run_engine(
        make_bars(GAP_BARS),
        start_date=date(2026, 1, 2),
        execution=BacktestExecutionConfig(stop_gap_fill=False),
    )
    sell = result["trades"][1]
    assert sell["reason"] == "hard_stop"
    assert sell["reference_price"] == pytest.approx(98.2)


# ── 场景 C：入场 ATR 为 T-1 收盘口径（§7.3）─────────────────
# d1 入场日振幅极大（TR=40）：旧口径止损会被拉到 60.2 永远不触发；
# T-1 口径止损=98.2，d2 即触发。
def test_entry_atr_uses_prev_close_basis() -> None:
    bars = make_bars(
        [
            (100.0, 101.0, 99.0, 100.0),
            (100.0, 120.0, 80.0, 100.0),
            (99.0, 99.5, 96.5, 97.0),
        ]
    )
    result = run_engine(bars, start_date=date(2026, 1, 2))
    trips = result["round_trips"]
    assert len(trips) == 1
    assert trips[0]["exit_reason"] == "hard_stop"
    assert trips[0]["entry_atr"] == pytest.approx(2.0)


def test_first_bar_entry_has_no_hard_stop() -> None:
    # 入场日无前一根 K 线 → T-1 ATR 缺失 → hard_stop=0（与旧版 ATR 周期不足行为一致）。
    bars = make_bars(
        [
            (100.0, 101.0, 99.0, 100.0),
            (90.0, 91.0, 89.0, 90.0),
        ]
    )
    result = run_engine(bars)
    assert len(result["trades"]) == 1  # 只有买入，止损未生效
    assert result["round_trips"] == []
