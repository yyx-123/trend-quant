"""loop-review-ds4f Round 5 修复钉子（round5-review.md 各项的回归锚）。

Round 5 换面审**存量栈与跨栈原语**：抓到 1 项 P1（生产管理员仍用源码可见的默认
引导密码）+ 1 项 P2（零成交/全现金腿产出 Sharpe ≈ 6e12 的噪声并**决定判定**）+ 3 项 P2/P3。
"""

from __future__ import annotations

import logging

import pytest

pytestmark = pytest.mark.unit


def test_degenerate_flat_leg_does_not_gate_the_verdict():
    """R5-P2-2：全现金/零成交腿（日收益只有计息浮点残差）的 Sharpe 必须记 None
    而不是 ≈6e12 的噪声值——否则一个好实验 × 全现金基准会被判 rejected。"""
    from research.evaluations.backtest import (
        _deltas_from_summaries,
        _nav_summary,
    )
    from research.verdict_rules import suggest_backtest_verdict

    # 全现金 run：只有 1%/252 的计息（std ≈ 浮点噪声）
    equity = 1_000_000.0
    flat = []
    for i in range(40):
        equity *= 1.0 + 0.01 / 252.0
        flat.append({"date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}", "equity": equity})
    flat_summary = _nav_summary(flat)
    assert flat_summary.get("degenerate_leg") is True
    assert flat_summary["sharpe"] is None and flat_summary["sortino"] is None

    # 正常腿：有波动
    normal_equity = 1_000_000.0
    normal = []
    for i in range(40):
        normal_equity *= 1.0 + (0.004 if i % 2 else -0.003)
        normal.append({"date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}",
                       "equity": normal_equity})
    normal_summary = _nav_summary(normal)
    assert normal_summary.get("degenerate_leg") is None

    deltas = _deltas_from_summaries(normal_summary, flat_summary)
    assert deltas["delta_sharpe"] is None, "退化腿不得参与作差（曾是 6e12 级噪声）"
    assert deltas["delta_annual_return"] is not None, "非退化指标照常作差"

    evidence = {
        "deltas_vs_base": {"delta_sharpe": deltas["delta_sharpe"],
                           "delta_annual_return": 0.05,
                           "delta_max_drawdown": 0.0, "delta_turnover": None},
        "stats": {"paired": {"t_stat": 3.0, "dsr_on_diff": 0.1, "n_pairs": 200}},
        "plateau": {"verdict": "plateau"},
    }
    verdict = suggest_backtest_verdict(evidence)
    assert verdict != "rejected", (
        "退化基准不得把实验判成 rejected（ΔSharpe 不可用 → 判定应 inconclusive）"
    )


def test_default_admin_password_in_use_logs_a_warning(test_db, caplog):
    """R5-P1-1：内置管理员仍用源码可见的默认引导密码时，启动必须响亮告警。"""
    import app.main as main_mod
    from data.storage.db import hash_password

    test_db.create_user(main_mod._BUILTIN_ADMIN_USERNAME,
                        main_mod._BUILTIN_ADMIN_DEFAULT_PASSWORD, is_admin=True)
    with caplog.at_level(logging.WARNING):
        main_mod._ensure_builtin_admin(test_db)
    assert any("SECURITY" in r.message and "default password" in r.message
               for r in caplog.records), [r.message for r in caplog.records]

    # 已改密则不再告警
    with test_db.connect() as conn:
        conn.execute("UPDATE users SET password = ? WHERE username = ?",
                     (hash_password("a-strong-rotated-password"),
                      main_mod._BUILTIN_ADMIN_USERNAME))
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        main_mod._ensure_builtin_admin(test_db)
    assert not any("SECURITY" in r.message for r in caplog.records)


def test_cli_test_uses_the_current_interpreter():
    """R5-P2-4：CI（ubuntu）跑不了硬编码 Windows venv 路径的 CLI 用例。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "integration" / "test_critical_paths.py").read_text(encoding="utf-8")
    assert "Scripts" not in src or "sys.executable" in src, \
        "CLI 用例必须用 sys.executable（跨平台/CI 可跑）"


def test_macd_warmup_docstring_states_the_seed_difference():
    """R5-P3-7：docstring 必须说明两种模式不止掩码不同（DEA 种子不同）。"""
    import inspect

    from core import indicators

    doc = inspect.getdoc(indicators.macd) or ""
    assert "DEA" in doc and ("种子" in doc or "seed" in doc.lower()), doc[:200]


# ----------------------------------------------------------------------
# R6-P1-1 / R6-P2-2 / R6-P3-3：退化腿在**所有**消费点都必须 None-safe
# ----------------------------------------------------------------------


def _flat_nav(n=30, equity=1_000_000.0):
    return [{"date": f"2024-01-{i + 1:02d}",
             "equity": equity * (1.0 + 0.01 / 252.0) ** i} for i in range(n)]


def _normal_nav(n=30, equity=1_000_000.0):
    return [{"date": f"2024-01-{i + 1:02d}",
             "equity": equity * (1.004 if i % 2 else 0.997) ** 1} for i in range(n)]


def test_none_safe_delta_helper():
    """R6-P1-1：Δ 助手对退化腿必须返回 None 而不是抛 TypeError。"""
    from research.evaluations.backtest import _delta_or_none, _nav_summary

    flat, normal = _nav_summary(_flat_nav()), _nav_summary(_normal_nav())
    assert flat.get("degenerate_leg") is True
    assert _delta_or_none(normal, flat) is None
    assert _delta_or_none(flat, normal) is None
    assert _delta_or_none(normal, normal) == pytest.approx(0.0)
    assert _delta_or_none({"sharpe": 1.5}, {"sharpe": 0.5}) == pytest.approx(1.0)


def test_plateau_verdict_reports_skipped_neighbors():
    """R6-P1-1：被剔除的退化邻域点必须显式可见（`skipped`），不能静默变少。"""
    from research.verdict_rules import plateau_verdict

    out = plateau_verdict(0.4, [], skipped=3)
    assert out["verdict"] == "unknown"
    assert out["skipped"] == 3
    assert "skipped 3" in out["reason"]
    ok = plateau_verdict(0.4, [0.3, 0.5], skipped=1)
    assert ok["skipped"] == 1 and ok["verdict"] in ("plateau", "peak")


def test_degenerate_leg_warning_is_persisted_in_verdict_record():
    """R6-P3-3：退化腿必须落进 verdict 记录的 warnings（不能只有 null 无解释）。"""
    import inspect

    from research.evaluations import backtest as bt

    src = inspect.getsource(bt._assemble_result)
    assert "degenerate_leg(" in src, "退化腿必须落 warnings"


def test_head_to_head_marks_degenerate_legs():
    """R6-P2-2：head_to_head 也必须判退化腿（否则噪声决定其判定）。"""
    import inspect

    from research.evaluations import head_to_head as h2h

    src = inspect.getsource(h2h.run_head_to_head)
    assert "degenerate_legs" in src
    assert "degenerate_leg(" in src
    assert "d_band = None" in src, "退化时不得输出置信带/判定"
