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


# ----------------------------------------------------------------------
# R7 复核后的补钉：退化腿噪声不得残留于任何持久化面（F1/F2/F3）+ 合法值守卫（F4）
# ----------------------------------------------------------------------


def test_head_to_head_degenerate_payload_has_no_noise():
    """R7-F1：退化腿时 `evidence.paired.delta_sharpe_band` 与两侧 summary 的
    Sharpe 都必须是 None（此前只改局部变量，噪声仍被持久化）。"""
    import inspect

    from research.evaluations import head_to_head as h2h
    from research.evaluations.head_to_head import run_head_to_head  # noqa: F401

    src = inspect.getsource(h2h.run_head_to_head)
    # 必须在组装 evidence **之前**清零（源码顺序断言：清零语句出现在 evidence 之前）
    clear_at = src.index("d_band = None")
    build_at = src.index('"delta_sharpe_band": d_band')
    assert clear_at < build_at, "清零必须发生在 evidence 组装之前（R7-F1 的根因）"
    assert '_summary["sharpe"] = None' in src, "两侧 summary 的 Sharpe 必须清空"
    assert "psr_ab = None" in src


def test_degenerate_legs_flag_reaches_evidence_and_conclusion():
    """R7-F2：退化腿必须落机器可读标记，课题 FDR 必须跳过它。"""
    import inspect

    from research import conclusion
    from research.evaluations import backtest as bt

    assert '"degenerate_legs": _degenerate_legs' in inspect.getsource(bt._assemble_result)
    csrc = inspect.getsource(conclusion.build_conclusion_summary)
    assert 'evidence_all.get("degenerate_legs")' in csrc, \
        "课题 FDR 必须跳过退化腿（否则零成交实验被算成显著）"


def test_regime_segment_marks_degenerate_segments():
    """R7-F3：段内噪声（该腿整段零成交）不得进 collapse 门的 ΔSharpe。"""
    import inspect

    from research.evaluations import backtest as bt

    src = inspect.getsource(bt._regime_segment_metrics)
    assert "degenerate_segment" in src
    src2 = inspect.getsource(bt._regime_split)
    assert "_seg_degenerate" in src2 and "sufficient_sample" in src2


def test_benchmark_relative_refuses_noisy_beta():
    """R7-F2 连带：基准腿退化（平坦）时 beta/alpha 记 None 而非 1e12 级噪声。"""
    from portfolio.reports import benchmark_relative

    nav = [{"date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}",
            "equity": 1e6 * (1.003 if i % 2 else 0.997)} for i in range(60)]
    flat = [{"date": r["date"], "equity": 1e6 * (1 + 0.01 / 252) ** i}
            for i, r in enumerate(nav)]
    out = benchmark_relative(nav, flat)
    assert out["beta"] is None and out["alpha_annual"] is None, out
    # 正常基准仍算出有限 beta
    normal = [{"date": r["date"], "equity": 1e6 * (1.002 if i % 3 else 0.998)}
              for i, r in enumerate(nav)]
    ok = benchmark_relative(nav, normal)
    assert ok["beta"] is not None and abs(ok["beta"]) < 100


def test_pbo_marks_degenerate_variants():
    """R7B #1：变体名次全并列时 λ=0.5（不是 0）、并标 degenerate_variants。"""
    import numpy as np

    from research.stats.fdr_pbo import pbo_cscv

    rng = np.random.default_rng(7)
    same = np.tile(rng.normal(0.0005, 0.01, 600)[:, None], (1, 3))
    out = pbo_cscv(same, n_blocks=8)
    assert out["lambda_median"] == pytest.approx(0.5), out
    assert out["degenerate_variants"] is True
    assert out["pbo"] == 0.0, "全并列不是'必然过拟合'"

    overfit = rng.normal(0, 0.01, (600, 4))
    overfit[:300, 0] += 0.01
    overfit[300:, 0] -= 0.01
    overfit[300:, 1] += 0.01
    assert pbo_cscv(overfit, n_blocks=8)["degenerate_variants"] is False


# ----------------------------------------------------------------------
# R8 复核后的定稿钉子：退化闸门必须覆盖**全部**派生面（幅值闸门单点收口）
# ----------------------------------------------------------------------


def _flat(n=120, rate=0.01 / 252):
    return [{"date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}",
             "equity": 1e6 * (1 + rate) ** i} for i in range(n)]


def _monotone(n=200, rate=0.0004):
    return [{"date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}",
             "equity": 1e6 * (1 + rate) ** i} for i in range(n)]


def test_degenerate_gate_uses_a_magnitude_floor():
    """R8-F3：退化判据必须含**幅值闸门**——近失配带（std/|mean| ≈ 1e-5 的单调
    低波路径）会漏过纯相对判据，产出 8.7e5 级 Sharpe 并**翻转判定**。"""
    from research.evaluations.backtest import _nav_summary
    from rule_backtest.metrics import DEGENERATE_SHARPE_ABS_LIMIT, is_degenerate_nav

    assert DEGENERATE_SHARPE_ABS_LIMIT == pytest.approx(50.0)
    # 近失配带：单调路径 + 极小平滑噪声（std/|mean| ≈ 1e-5）→ 纯相对判据漏判，
    # 而 Sharpe 高达 1e3 量级 → 幅值闸门必须抓住（R8-F3 的实测形态）
    _eq = 1e6
    near = []
    for i in range(200):
        _eq *= 1.0 + (4e-4 + (5e-6 if i % 2 else -5e-6))  # mean/std ≈ 80 → Sharpe ≈ 1.3e3
        near.append({"date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}", "equity": _eq})
    assert is_degenerate_nav(near) is False, "纯相对判据在近失配带确实漏判"
    summary = _nav_summary(near)
    assert summary["sharpe"] is None and summary.get("degenerate_leg") is True, summary
    # 严格单调（方差为 0）由相对判据抓住
    mono = _monotone()
    assert is_degenerate_nav(mono) is True

    # 正常策略的 Sharpe 不得被幅值闸门误杀
    normal = [{"date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}",
               "equity": 1e6 * (1.004 if i % 2 else 0.997)} for i in range(60)]
    normal_summary = _nav_summary(normal)
    assert normal_summary.get("degenerate_leg") is None
    assert normal_summary["sharpe"] is not None


def test_build_report_gates_degenerate_noise():
    """R8-F1：`full_run_report` 从 NAV 重新派生指标，**不经过** L4 的退化闸门
    → 6e12 级 Sharpe 与 394 条滚动 Sharpe 噪声曾被持久化/发布。"""
    from portfolio.reports import build_report

    rep = build_report(None, run_id="R-degen", nav_rows=_flat(), fills=[],
                       unfilled=[], gate_log=[])
    assert rep["summary"]["sharpe"] is None
    assert rep["summary"]["degenerate_leg"] is True
    assert rep["rolling_sharpe_6m"] == [] and rep["rolling_sharpe_12m"] == []
    # 正常 NAV 不受影响
    normal = [{"date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}",
               "equity": 1e6 * (1.004 if i % 2 else 0.997)} for i in range(300)]
    rep_ok = build_report(None, run_id="R-ok", nav_rows=normal, fills=[],
                          unfilled=[], gate_log=[])
    assert rep_ok["summary"]["sharpe"] is not None
    assert rep_ok["rolling_sharpe_6m"], "正常 NAV 的滚动 Sharpe 不得被清空"


def test_pbo_ignores_degenerate_variants():
    """R8-F2：全现金变体的 1e12 级"Sharpe"会赢下每个 CSCV 组合的 argmax →
    λ≡1 → pbo=0.0（假的"绝不拟合"）。退化列必须不参与比较；全退化则记 None。"""
    import numpy as np

    from research.stats.fdr_pbo import pbo_cscv

    rng = np.random.default_rng(11)
    t = 600
    mixed = rng.normal(0.0004, 0.01, (t, 4))
    mixed[:, 0] = 0.01 / 252.0  # 全现金列
    out = pbo_cscv(mixed, n_blocks=8)
    assert out["pbo"] is not None, out
    all_degen = np.tile(0.01 / 252.0, (t, 3))
    out2 = pbo_cscv(all_degen, n_blocks=8)
    assert out2["pbo"] is None and out2["degenerate_variants"] is True, out2


def test_capture_ratios_refuse_noise_denominators():
    """R8-F4：up/down capture 此前会在退化基准上给出 11.4 这类无意义值。"""
    from portfolio.reports import benchmark_relative

    nav = [{"date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}",
            "equity": 1e6 * (1.003 if i % 2 else 0.997)} for i in range(60)]
    flat = [{"date": r["date"], "equity": 1e6 * (1 + 0.01 / 252) ** i}
            for i, r in enumerate(nav)]
    out = benchmark_relative(nav, flat)
    assert out["up_capture"] is None and out["down_capture"] is None, out
    assert out["beta"] is None and out["alpha_annual"] is None


# ----------------------------------------------------------------------
# R9 复核后的定稿钉子：判据必须**两条腿**都看（NAV + Sharpe），且窗口局部生效
# ----------------------------------------------------------------------


def test_judge_entry_point_takes_both_legs():
    """R9-2a：`is_degenerate_summary` 必须自行从 NAV 现算 Sharpe——调用点漏传
    不再可能（此前 5 个调用点里 2 个只传 NAV，合法可配置的极小仓位腿
    （|sharpe| 85~287）被判"未退化"并翻转判定）。"""
    from rule_backtest.metrics import (
        annualized_sharpe,
        is_degenerate_nav,
        is_degenerate_summary,
    )

    # 极小仓位腿：日收益 ≈ 1e-4 量级 + 小幅波动 → 年化 Sharpe 数百
    eq = 1e6
    rows = []
    for i in range(200):
        eq *= 1.0 + (2e-4 + (2e-5 if i % 2 else -2e-5))
        rows.append({"date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}", "equity": eq})
    sharpe = annualized_sharpe(rows)
    assert sharpe is not None and abs(sharpe) > 50, sharpe
    # 只传 NAV（相对判据）：漏判；走新入口（两条腿）：抓住
    assert is_degenerate_nav(rows) is False
    assert is_degenerate_summary(rows, None) is True
    assert is_degenerate_summary(rows, {"sharpe": sharpe}) is True
    # 诚实序列不得被误杀
    normal = [{"date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}",
               "equity": 1e6 * (1.004 if i % 2 else 0.997)} for i in range(60)]
    assert is_degenerate_summary(normal, None) is False


def test_rolling_sharpe_is_gated_per_window():
    """R9-1：滚动 Sharpe 的噪声是**窗口局部**的——"前 N 日无成交、之后正常"
    的腿整序列 Sharpe 正常，但落在无成交段里的窗口仍是 1e12 级噪声
    （实测 392 条曾被持久化）。"""
    from portfolio.reports import _rolling_sharpe_gated, build_report
    from rule_backtest.metrics import is_degenerate_nav

    equity = 1e6
    rows = []
    for i in range(420):
        if i < 300:  # 前 300 日无成交：只有计息
            equity *= 1.0 + 0.01 / 252.0
        else:
            equity *= 1.004 if i % 2 else 0.997
        rows.append({"date": f"{2010 + i // 250}-{1 + (i % 250) // 28:02d}-"
                             f"{1 + (i % 250) % 28:02d}", "equity": equity})
    assert is_degenerate_nav(rows) is False, "整序列 Sharpe 正常（这正是漏判的原因）"
    gated6 = _rolling_sharpe_gated(rows, 126)
    assert all(p["sharpe"] is not None and abs(p["sharpe"]) < 1e3 for p in gated6), \
        "落在无成交段的滚动窗口必须被剔除"
    report = build_report(None, run_id="R-roll", nav_rows=rows, fills=[],
                          unfilled=[], gate_log=[])
    assert all(abs(p["sharpe"]) < 1e3 for p in report["rolling_sharpe_6m"]), \
        "持久化的滚动 Sharpe 不得含 1e12 级噪声"
    assert all(abs(p["sharpe"]) < 1e3 for p in report["rolling_sharpe_12m"])


def test_benchmark_relative_judges_both_legs():
    """R9-2b：基准的近失配噪声（年化 Sharpe 数百）也必须判退化——
    此前只做相对方差判定，仍给出 beta=-195…-67470、capture=46.9。"""
    from portfolio.reports import benchmark_relative

    nav = [{"date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}",
            "equity": 1e6 * (1.004 if i % 2 else 0.997)} for i in range(120)]
    # 近失配基准：日收益 2e-4 ± 2e-5（std/|mean| = 1e-1，纯相对判据漏判）
    eq = 1e6
    near = []
    for i in range(120):
        eq *= 1.0 + (2e-4 + (2e-5 if i % 2 else -2e-5))
        near.append({"date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}", "equity": eq})
    out = benchmark_relative(nav, near)
    assert out["beta"] is None and out["alpha_annual"] is None, out
    assert out["up_capture"] is None and out["down_capture"] is None, out
