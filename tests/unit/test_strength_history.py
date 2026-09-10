"""逐日强度（``assign_strength_history``）：趋势 mini 图悬停数据源。

核心约束是与最新值强度 ``assign_strength`` / ``strength`` 同口径——同一
scope 内当日 MA5 的 ``<=`` 计数占比、四舍五入到整数。实现改用 ``rank``
以求 O(n log n)，本文件用逐值计数版 ``strength()`` 做等价性守门。
"""

from __future__ import annotations

import random

from services.dashboard_common import assign_strength, assign_strength_history, strength


def _item(symbol: str, l1: str, dates: list[str], ma5: list[float | None]) -> dict:
    return {
        "symbol": symbol,
        "category_l1": l1,
        "trend_ma5": ma5[-1] if ma5 else None,
        "trend_dates": dates,
        "trend_history": ma5,
    }


def test_matches_latest_value_strength_on_last_day() -> None:
    """逐日强度的最后一天必须与「强度」列（最新值口径）完全相等。"""
    dates = [f"2026-08-{day:02d}" for day in range(11, 21)]
    items = [
        _item("A", "ETF", dates, [float(i) for i in range(10)]),
        _item("B", "ETF", dates, [float(2 * i) for i in range(10)]),
        _item("C", "ETF", dates, [float(10 - i) for i in range(10)]),
        _item("D", "ETF", dates, [None] * 4 + [float(i) for i in range(6)]),
    ]

    assign_strength_history(items, ("category_l1",))
    assign_strength(items, ("category_l1",))

    for item in items:
        assert item["strength_history"][-1] == item["strength"]
        assert len(item["strength_history"]) == len(item["trend_dates"]) == 10


def test_percentile_matches_value_counting_reference() -> None:
    """逐日逐值等价于 strength() 的逐值计数实现（含并列值与半整数舍入）。"""
    rng = random.Random(20260910)
    dates = [f"d{day:02d}" for day in range(12)]
    series = {
        symbol: [rng.choice([None, *[rng.randint(-3, 3) / 2 for _ in range(20)]]) for _ in dates]
        for symbol in ("A", "B", "C", "D", "E")
    }
    items = [_item(symbol, "ETF", dates, values) for symbol, values in series.items()]

    assign_strength_history(items, ("category_l1",))

    for day_index, date in enumerate(dates):
        day_values = [
            values[day_index] for values in series.values() if values[day_index] is not None
        ]
        for item in items:
            value = series[item["symbol"]][day_index]
            expected = strength(day_values, value) if value is not None else None
            assert item["strength_history"][day_index] == expected, (
                f"{date} {item['symbol']}: {item['strength_history'][day_index]} != {expected}"
            )


def test_scopes_are_ranked_independently() -> None:
    """不同一级类目各自排名：类目内最高分恒为 100、最低分恒为 1/成员数。"""
    dates = ["2026-09-09"]
    items = [
        _item("A", "ETF", dates, [5.0]),
        _item("B", "ETF", dates, [1.0]),
        _item("C", "股票", dates, [9.0]),
        _item("D", "股票", dates, [8.0]),
        _item("E", "股票", dates, [7.0]),
    ]

    assign_strength_history(items, ("category_l1",))

    by_symbol = {item["symbol"]: item["strength_history"] for item in items}
    assert by_symbol["A"] == [100]
    assert by_symbol["B"] == [50]
    assert by_symbol["C"] == [100]
    assert by_symbol["D"] == [67]
    assert by_symbol["E"] == [33]


def test_latest_point_matches_column_when_members_differ_in_last_day() -> None:
    """部分标的缺当日数据时（停牌/盘中报价失败），最新点仍须等于「强度」列。

    「强度」列按各标的**最新可得值**排名，不区分这些值的日期；逐日强度若对
    最新点用严格的当日成员口径，就会与行内显示的数字差 1~2 个百分点。
    """
    items = [
        _item("A", "ETF", ["d1", "d2"], [1.0, 4.0]),
        _item("B", "ETF", ["d1", "d2"], [2.0, 3.0]),
        _item("C", "ETF", ["d1", "d2"], [3.0, 2.0]),
        # D 当日缺数据：最新只到 d1
        _item("D", "ETF", ["d0", "d1"], [8.0, 5.0]),
    ]

    assign_strength_history(items, ("category_l1",))
    assign_strength(items, ("category_l1",))

    by_symbol = {item["symbol"]: item for item in items}
    for item in items:
        assert item["strength_history"][-1] == item["strength"], (
            f"{item['symbol']}: 最新点 {item['strength_history'][-1]} != 强度列 {item['strength']}"
        )
    # 最新点按最新可得值排名：[A=4, B=3, C=2, D=5] → D 最高、C 最低。
    assert [by_symbol[s]["strength_history"][-1] for s in "ABCD"] == [75, 50, 25, 100]
    # 历史点仍是严格的当日成员口径：d1 只有 A/B/C 三只（D 的 d1 值 5.0 归入
    # 最新点，不参与 d1 的横截面）。
    assert [by_symbol[s]["strength_history"][0] for s in "ABC"] == [33, 67, 100]


def test_missing_days_yield_none_and_keep_alignment() -> None:
    """MA5 不足 5 根的头部为 None：该日强度为 None，且长度与 trend_dates 对齐。"""
    dates = ["d1", "d2", "d3"]
    items = [
        _item("A", "ETF", dates, [None, None, 3.0]),
        _item("B", "ETF", dates, [None, None, 1.0]),
    ]

    assign_strength_history(items, ("category_l1",))

    assert items[0]["strength_history"] == [None, None, 100]
    assert items[1]["strength_history"] == [None, None, 50]


def test_empty_history_produces_empty_list() -> None:
    """无序列的类目行：产出空列表而非缺字段，前端 index 取值恒为 undefined。"""
    items = [{"symbol": None, "category_l1": "ETF", "trend_dates": [], "trend_history": []}]

    assign_strength_history(items, ("category_l1",))

    assert items[0]["strength_history"] == []
