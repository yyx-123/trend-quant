"""Smoke test — pytest 基础设施连通性 + safe_float 统一签名契约（P1-14）。

原 ATR/ER/趋势快照用例已并入 test_core_indicators / test_core_trend（P2-25 重复测试合并）。
"""

from __future__ import annotations

from core.trend import safe_float


class TestSafeFloat:
    def test_none_returns_default(self):
        # P1-14 统一签名：缺省 default=None，调用方按需显式传 0.0
        assert safe_float(None) is None
        assert safe_float(None, default=0.0) == 0.0
        assert safe_float(None, default=-1.0) == -1.0

    def test_nan_returns_default(self):
        assert safe_float(float("nan")) is None
        assert safe_float(float("nan"), 0.0) == 0.0

    def test_vendor_string_cleaning(self):
        # 原 provider_utils 版语义：千分位逗号与占位符
        assert safe_float("1,234.5") == 1234.5
        assert safe_float("-") is None
        assert safe_float("nan") is None
        assert safe_float("") is None

    def test_normal_number(self):
        assert safe_float(42.5) == 42.5
        assert safe_float(0) == 0.0
        assert safe_float(-3.14) == -3.14

    def test_string_coercion(self):
        assert safe_float("12.34") == 12.34

    def test_bool(self):
        assert safe_float(True) == 1.0
        assert safe_float(False) == 0.0
