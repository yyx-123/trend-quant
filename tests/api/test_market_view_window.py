"""P1-4 回归测试：Web 日K指标在日期区间全量历史上计算，输出再 tail。

旧实现先 ``tail(limit)`` 再算指标——EMA 族指标无限记忆，显式传小 limit 时
同一标的同一日期的 MACD/EMA/趋势值随请求窗口漂移，与 MCP 口径不一致。
本测试锁定「不同 limit 下，同一日期的指标值完全一致」。
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd


def _seed_bars(test_db, symbol: str = "510300.SS", rows: int = 120) -> None:
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


def test_daily_indicators_do_not_depend_on_limit(client, test_db):
    _seed_bars(test_db)

    wide = client.get("/market-view/api/daily", params={"symbol": "510300.SS", "limit": 120})
    narrow = client.get("/market-view/api/daily", params={"symbol": "510300.SS", "limit": 30})
    assert wide.status_code == 200
    assert narrow.status_code == 200
    wide, narrow = wide.json(), narrow.json()

    assert len(narrow["dates"]) == 30
    assert narrow["dates"] == wide["dates"][-30:]
    assert narrow["meta"]["rows"] == 30
    assert narrow["meta"]["start"] == narrow["dates"][0]

    # 同一日期的指标值必须与窗口无关（对齐到窄窗口逐点比对）
    for group in ("ma", "atr", "boll", "macd", "bias", "volume_ma"):
        for key, series in wide["indicators"][group].items():
            assert narrow["indicators"][group][key] == series[-30:], f"{group}.{key}"
    assert narrow["indicators"]["rsi"]["series"] == wide["indicators"]["rsi"]["series"][-30:]
    assert narrow["indicators"]["trend"]["score"] == wide["indicators"]["trend"]["score"][-30:]
    for key, series in wide["indicators"]["trend"]["ma"].items():
        assert narrow["indicators"]["trend"]["ma"][key] == series[-30:]


def test_daily_dates_candles_tailed_to_limit(client, test_db):
    _seed_bars(test_db)
    resp = client.get("/market-view/api/daily", params={"symbol": "510300.SS", "limit": 10})
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload["dates"]) == 10
    assert len(payload["candles"]) == 10
    assert len(payload["volumes"]) == 10
    assert len(payload["amounts"]) == 10
    for group in ("ma", "atr", "boll", "macd", "bias", "volume_ma"):
        for key, series in payload["indicators"][group].items():
            assert len(series) == 10, f"{group}.{key}"
    assert len(payload["indicators"]["trend"]["score"]) == 10
