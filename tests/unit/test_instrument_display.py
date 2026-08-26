"""Unit tests for app.instrument_display — display-name helpers."""

from __future__ import annotations

from app.instrument_display import (
    build_symbol_display,
    format_symbol_display,
    strip_etf_suffix,
)


class TestStripEtfSuffix:
    def test_strips_etf(self) -> None:
        assert strip_etf_suffix("沪深300ETF") == "沪深300"

    def test_strips_case_insensitive(self) -> None:
        assert strip_etf_suffix("黄金 etf") == "黄金"

    def test_no_etf_passes_through(self) -> None:
        assert strip_etf_suffix("中国平安") == "中国平安"

    def test_none_and_empty(self) -> None:
        assert strip_etf_suffix(None) == ""
        assert strip_etf_suffix("") == ""



class TestFormatSymbolDisplay:
    def test_uses_name_when_available(self) -> None:
        assert format_symbol_display("510300.SS", "沪深300ETF") == "沪深300"

    def test_falls_back_to_code(self) -> None:
        assert format_symbol_display("510300.SS", None) == "510300"


class TestBuildSymbolDisplay:
    def test_looks_up_name_from_map(self) -> None:
        name_map = {"510300.SS": "沪深300"}
        assert build_symbol_display("510300.SS", name_map) == "沪深300"

    def test_falls_back_to_code(self) -> None:
        assert build_symbol_display("510300.SS", {}) == "510300"
