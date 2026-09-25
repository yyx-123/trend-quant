"""Round 20 修复钉子。

- R20B-F1（P1）：上游因子响应缺键/为空时，本地因子表被整体清空 → qfq 全历史回落到
  不复权（实测造出 −66.94% 单日断裂）并把真实除权日判成假跌停（卖不出/止损顺延）。
- R20A-F1（P2）：heat_cap 事后告警读错参数名（`cap` vs 声明的 `max_heat_pct`）。
"""

from __future__ import annotations

from datetime import date


def _service_with_local_factors(test_db, monkeypatch):
    """构造：本地有因子、上游返回空（缺键/整批异常）的 sync 场景。"""
    from data.service import DataService

    test_db.replace_ex_factors(
        "X.SS", [(date(2024, 5, 20), 1.5)], provider="tickflow"
    )

    class _Svc(DataService):
        def __init__(self):  # 跳过真实 provider 构造
            pass

    svc = _Svc()
    monkeypatch.setattr(svc, "fetch_ex_factors", lambda symbols: ({"X.SS": []}, {}))
    return svc


def test_empty_upstream_does_not_wipe_local_factors(test_db, monkeypatch):
    """R20B-F1（P1）：因子"消失"必须拒绝覆盖本地表。

    因子被清空会让 qfq 回落不复权（实测 −66.94% 单日断裂）→ 真实除权日判成假跌停。
    """
    svc = _service_with_local_factors(test_db, monkeypatch)
    _, changed = svc.sync_ex_factors(["X.SS"], db=test_db)
    assert changed == [], "空响应不得被当作\"因子变更\""
    kept = test_db.load_all_ex_factors().get("X.SS") or []
    assert len(kept) == 1 and kept[0][1] == 1.5, "本地因子必须原样保留"


def test_legit_factor_update_still_applies(test_db, monkeypatch):
    """反向：真实因子变更（含新增/修改）必须照常落库——守卫不能挡住合法更新。"""
    svc = _service_with_local_factors(test_db, monkeypatch)
    monkeypatch.setattr(
        svc, "fetch_ex_factors",
        lambda symbols: ({"X.SS": [(date(2024, 5, 20), 1.5), (date(2024, 11, 1), 2.0)]}, {}),
    )
    _, changed = svc.sync_ex_factors(["X.SS"], db=test_db)
    assert changed == ["X.SS"], "新增因子必须落库"
    kept = test_db.load_all_ex_factors().get("X.SS") or []
    assert len(kept) == 2 and kept[1][1] == 2.0
