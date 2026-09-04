"""services.instrument_catalog.search_instruments：筛选与数据范围组装。"""

from __future__ import annotations

from services.instrument_catalog import search_instruments

_INSTRUMENTS = [
    {
        "symbol": "510300.SS",
        "name": "沪深300ETF",
        "category_l1": "ETF",
        "category_l2": "宽基",
        "category_l3": "沪深300",
        "enabled": True,
    },
    {
        "symbol": "513100.SS",
        "name": "纳指ETF",
        "category_l1": "ETF",
        "category_l2": "跨境",
        "category_l3": "纳指",
        "enabled": True,
    },
    {
        "symbol": "600519.SS",
        "name": "贵州茅台",
        "category_l1": "股票",
        "category_l2": "白酒",
        "category_l3": "高端白酒",
        "enabled": False,
    },
]


class _FakeDb:
    def list_instrument_metadata(self) -> list[dict]:
        return _INSTRUMENTS

    def get_market_data_summary_many(self, symbols, price_mode: str = "qfq") -> dict:
        return {
            s: {"rows": 100, "start": "2026-01-01", "end": "2026-08-27"} for s in symbols
        }


class _BrokenDb:
    def list_instrument_metadata(self) -> list[dict]:
        raise RuntimeError("db down")

    def get_market_data_summary_many(self, symbols, price_mode: str = "qfq") -> dict:
        return {}


def test_lists_enabled_with_data_range() -> None:
    result = search_instruments(db=_FakeDb())
    assert result["ok"] is True
    assert result["count"] == 2  # 默认过滤未启用标的
    first = result["instruments"][0]
    assert first["symbol"] == "510300.SS"
    assert first["data_rows"] == 100
    assert first["data_start"] == "2026-01-01"
    assert first["data_end"] == "2026-08-27"


def test_enabled_only_false_includes_disabled() -> None:
    result = search_instruments(enabled_only=False, db=_FakeDb())
    assert result["count"] == 3


def test_category_filter_matches_any_level_case_insensitive() -> None:
    assert search_instruments(category="跨境", db=_FakeDb())["count"] == 1
    assert search_instruments(category="etf", db=_FakeDb())["count"] == 2  # 命中 L1
    assert search_instruments(category="纳指", db=_FakeDb())["count"] == 1  # 命中 L3
    assert search_instruments(category="不存在", db=_FakeDb())["count"] == 0


def test_keyword_filter_matches_symbol_or_name() -> None:
    assert search_instruments(keyword="510300", db=_FakeDb())["count"] == 1
    assert search_instruments(keyword="etf", db=_FakeDb())["count"] == 2  # 名称含 ETF
    assert search_instruments(keyword="茅台", db=_FakeDb())["count"] == 0  # 未启用被先过滤


def test_db_failure_returns_empty_list() -> None:
    result = search_instruments(db=_BrokenDb())
    assert result == {"ok": True, "count": 0, "instruments": []}
