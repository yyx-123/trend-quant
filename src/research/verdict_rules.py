"""判定规则（平台持有；阈值入配置，调整走架构稿修订流程——详设 §6.10.3）。

confirmed 建议的全部条件（详设 §6.5.1 + §6.6.5 + 评审 DS-P1-6 配对口径）：
主指标改善（ΔSharpe > 0）+ plateau 非孤峰 + regime 无塌陷 +
**配对显著**（配对 t ≥ 单尾 95% t 临界——按 n_pairs 自由度取 t 分布临界值，
小样本不退回到 z=1.645 的反保守近似（GLM53F-P2-1②）；差序列区块
bootstrap 带在 evidence 中作佐证件）+
**DSR（差序列上，按尝试次数校正）> 0**；
显著恶化（ΔSharpe ≤ −0.2）→ rejected；其余 inconclusive。

口径注记（GLM53F-P2-1③④）：
- 换模块类 diff（无数值参数可扰动）拿不到高原证据——plateau=None 时平台
  在 verdict warnings 里记 `plateau_evidence_absent`，不阻断 confirmed
  （模块互换没有参数高原可言），但该注记必须显式可见；
- "换手增幅成本可解释"（§6.5.1）不进硬门槛——Δturnover/fee_total 在
  evidence 中，换手增幅 >50% 时记 `turnover_jump` 警告，由人工 confirm
  环节判读（成本可解释性需要费用语境，不适合阈值化）。

PSR（单序列）保留为展示件，不再作 confirmed 的门槛——改进型实验是
高相关配对，单序列 PSR 用基准已实现 Sharpe 当已知阈值是口径错误
（配对场景下它要么几乎不可达、要么过度宽松，见 DS-P1-6 的蒙特卡洛表）。

诚实检出下限（DS-P1-6/DS-复审-R2 口径澄清）：本判定门的 ΔSharpe 指
**差序列**年化 Sharpe。10 年日频下差序列 ΔSharpe≈0.2 → t≈0.63 不可达
confirmed（检出下限 ≈0.5）；而**策略序列级**改进 0.8→1.0（ρ≈0.98 单槽
diff）对应差序列 Sharpe≈1.0 → 检出率 93%（DS 独立复算）。两个口径别混。
"""

from __future__ import annotations

# 平台判定阈值（默认值；运行期可由 app_config 的 research.rules.* 覆盖，
# 调整走架构稿修订流程）
DEFAULT_RULES = {
    "min_delta_sharpe": 0.0,
    "significant_deterioration_delta_sharpe": -0.2,
    "regime_collapse_delta_sharpe": -0.3,
    "min_t_stat": 1.645,               # 配对 t ≥ 95% 单尾（大样本渐近值）
    "min_dsr_on_diff": 0.0,              # DSR（差序列，尝试次数校正）> 0
    "plateau_sigma": 1.0,  # Alvarez 式标准差检查：选定参数偏离邻域均值 1σ → 孤峰
}


def _t_critical_95(df: int) -> float:
    """单尾 95% t 临界值（Cornish–Fisher 展开，ν≥10 精度 ~1e-3）。

    GLM53F-P2-1②：1.645 是 z 值，小样本（n<~60）真实 t 临界更高，
    用 z 反保守。
    """
    z = 1.6448536269514722
    v = max(int(df), 2)
    g1 = (z ** 3 + z) / (4.0 * v)
    g2 = (5.0 * z ** 5 + 16.0 * z ** 3 + 3.0 * z) / (96.0 * v ** 2)
    g3 = (3.0 * z ** 7 + 19.0 * z ** 5 + 17.0 * z ** 3 - 15.0 * z) / (384.0 * v ** 3)
    return z + g1 + g2 + g3


def load_rules(db=None) -> dict:
    """判定阈值入配置（§6.10.3）：app_config 的 research.rules.<key> 覆盖默认值。"""
    rules = dict(DEFAULT_RULES)
    if db is None:
        return rules
    for key in rules:
        value = db.get_config(f"research.rules.{key}", None)
        if value is None:
            continue
        if isinstance(rules[key], bool):
            rules[key] = str(value).strip() in ("1", "true", "True")
        else:
            rules[key] = float(value)
    return rules


# 参数高原邻域表（§6.5.1）：按参数名类型分派扰动幅度
_PCT20_PARAMS = {
    "atr_mul", "pct", "buffer", "band", "threshold", "risk_budget_pct",
    "target_vol", "dd_line", "max_heat_pct", "slippage_base", "slippage_tail",
}
_INT2_PARAMS = {"max_positions", "slots", "top_n", "per_l2", "max_swaps", "per_day"}
_DAY_PARAMS = {
    "n", "lookback", "period", "fast", "slow", "signal", "window",
    "max_days", "entry_n", "exit_n", "valid_days", "atr_period", "ma",
}


def plateau_neighbors(param: str, value: float) -> list[float]:
    """该参数的 ±邻域点（止损倍数 ±20%、持仓数 ±2、天数 ±20% 取整）。

    DS-P3-8：±2 分支不得把选定值本身算进邻居（污染邻域同向/1σ 统计）。
    """
    if param in _INT2_PARAMS:
        return sorted({max(1, int(round(value - 2))), max(1, int(round(value + 2)))} - {int(value)})
    if param in _DAY_PARAMS:
        lo = max(1, int(round(value * 0.8)))
        hi = max(2, int(round(value * 1.2)))
        return sorted({lo, hi} - {int(value)})
    # 默认 ±20%
    return sorted({value * 0.8, value * 1.2} - {value})


def plateau_verdict(selected_delta: float, neighbor_deltas: list[float],
                    rules: dict | None = None) -> dict:
    """高原/孤峰判定：邻域同向 + Alvarez 1σ 检查（选定参数偏离邻域均值
    1σ 以上 → 疑似过拟合）。"""
    rules = rules or DEFAULT_RULES
    if not neighbor_deltas:
        return {"verdict": "unknown", "reason": "no neighbors"}
    import numpy as np

    arr = np.asarray(neighbor_deltas, dtype=float)
    mean = float(arr.mean())
    std = float(arr.std(ddof=1)) if len(arr) > 1 else 0.0
    same_direction = all(
        (d > 0) == (selected_delta > 0) for d in arr
    )
    deviates = std > 0 and abs(selected_delta - mean) > rules["plateau_sigma"] * std
    verdict = "plateau" if (same_direction and not deviates) else "peak"
    return {
        "verdict": verdict,
        "neighbor_mean": mean,
        "neighbor_std": std,
        "selected": selected_delta,
        "same_direction": same_direction,
        "deviates_over_1sigma": deviates,
    }


def suggest_backtest_verdict(evidence: dict, rules: dict | None = None) -> str:
    """backtest 实验的判定建议（平台按阈值化规则给出；AI 不能改判定规则）。"""
    rules = rules or DEFAULT_RULES
    deltas = evidence.get("deltas_vs_base") or {}
    delta_sharpe = deltas.get("delta_sharpe")
    if delta_sharpe is None:
        return "inconclusive"
    if delta_sharpe <= rules["significant_deterioration_delta_sharpe"]:
        return "rejected"
    plateau = evidence.get("plateau")
    regime = evidence.get("regime_split") or {}
    paired = (evidence.get("stats") or {}).get("paired")

    collapse = any(
        (seg.get("delta_sharpe") is not None
         and seg["delta_sharpe"] <= rules["regime_collapse_delta_sharpe"])
        for seg in regime.values()
    )
    is_plateau = plateau is None or plateau.get("verdict") != "peak"
    paired_ok = False
    if paired is not None:
        # GLM53F-P2-1②：小样本用 t 分布临界（自由度 n_pairs−1），不退回 z
        t_crit = max(rules["min_t_stat"], _t_critical_95(int(paired.get("n_pairs", 30)) - 1))
        paired_ok = (
            paired["t_stat"] >= t_crit
            and paired["dsr_on_diff"] > rules["min_dsr_on_diff"]
        )
    if delta_sharpe > rules["min_delta_sharpe"] and is_plateau and not collapse and paired_ok:
        return "confirmed"
    return "inconclusive"
