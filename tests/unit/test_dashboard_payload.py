"""services.dashboard 读取口径：dashboard_lite 瘦身变换 + trend_dashboard_payload 缓存。"""

from __future__ import annotations

import pytest

import services.dashboard as dashboard_module
from services.dashboard import RevisionCache, dashboard_lite, trend_dashboard_payload


def _fake_board_payload() -> dict:
    """含全部序列字段的最小看板结构（groups → L2 → L3 → 标的）。"""
    inst = {
        "symbol": "510300.SS",
        "name": "沪深300ETF",
        "trend_ma5": 1.23456789123,
        "kline": [{"d": f"2026-08-{i + 1:02d}", "c": 4.0 + i * 0.01} for i in range(30)],
        "kline_ma5": [4.0] * 30,
        "macd_dif": [0.001 * i for i in range(30)],
        "macd_dea": [0.002 * i for i in range(30)],
        "macd_hist": [0.0] * 30,
        "macd_dates": [f"2026-08-{i + 1:02d}" for i in range(30)],
        "trend_history": [1.0] * 61,
        "trend_dates": [f"d{i}" for i in range(61)],
    }
    l3 = {
        "category_l3": "沪深300",
        "trend_history": [1.0] * 61,
        "trend_dates": ["d"] * 61,
        "children": [inst],
    }
    l2 = {"category_l2": "宽基", "children": [l3]}
    return {"ok": True, "as_of": "2026-08-27", "groups": [{"category_l1": "ETF", "items": [l2]}]}


def _lite_instrument(lite: dict) -> dict:
    return lite["groups"][0]["items"][0]["children"][0]["children"][0]


class TestDashboardLite:
    def test_lite_truncates_and_drops_series(self) -> None:
        lite = dashboard_lite(_fake_board_payload())
        inst = _lite_instrument(lite)
        assert len(inst["kline"]) == 2  # 只留末尾 2 根
        assert inst["kline"][-1]["d"] == "2026-08-30"
        assert len(inst["macd_dif"]) == 2
        assert len(inst["macd_dea"]) == 2
        assert len(inst["macd_dates"]) == 2
        for dropped in ("kline_ma5", "macd_hist", "trend_history", "trend_dates"):
            assert dropped not in inst
        # 类目聚合行的长序列同样删除
        l3 = lite["groups"][0]["items"][0]["children"][0]
        assert "trend_history" not in l3
        assert "trend_dates" not in l3

    def test_lite_rounds_floats(self) -> None:
        lite = dashboard_lite(_fake_board_payload())
        assert _lite_instrument(lite)["trend_ma5"] == round(1.23456789123, 6)

    def test_lite_does_not_mutate_source(self) -> None:
        payload = _fake_board_payload()
        dashboard_lite(payload)
        inst = payload["groups"][0]["items"][0]["children"][0]["children"][0]
        assert len(inst["kline"]) == 30  # 共享缓存里的原对象保持不变
        assert "trend_history" in inst


class TestTrendDashboardPayload:
    def _stub(self, monkeypatch: pytest.MonkeyPatch) -> list[int]:
        class FakeDb:
            def get_market_dashboard_revision(self):
                return ("2026-08-24 00:00:00", "", 1)

        monkeypatch.setattr(dashboard_module, "get_db", lambda: FakeDb())
        calls: list[int] = []
        monkeypatch.setattr(
            dashboard_module,
            "build_subject_dashboard_payload",
            lambda db: calls.append(1) or _fake_board_payload(),
        )
        # 单例缓存可能已被其他测试填充——换一个独立缓存验证
        monkeypatch.setattr(dashboard_module, "dashboard_revision_cache", RevisionCache())
        return calls

    def test_second_call_hits_revision_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._stub(monkeypatch)
        first = trend_dashboard_payload()
        second = trend_dashboard_payload()
        assert first is second
        assert calls == [1]

    def test_lite_does_not_pollute_full_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._stub(monkeypatch)
        lite = trend_dashboard_payload(detail="lite")
        assert lite["detail"] == "lite"
        assert "trend_history" not in _lite_instrument(lite)

        full = trend_dashboard_payload()  # 命中同一份缓存，不再计算
        assert calls == [1]
        assert "detail" not in full
        assert len(_lite_instrument(full)["kline"]) == 30
