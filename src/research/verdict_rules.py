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

诚实检出下限（DS-P1-6/DS-复审-R2 口径澄清，对齐实现重写）：
confirmed 门里有两个不同的 ΔSharpe，勿混——
- ``min_delta_sharpe``（主指标改善 > 0）作用在 evidence.deltas_vs_base.delta_sharpe，
  即**序列级**年化 Sharpe 差（sharpe_exp − sharpe_base，backtest.py:_delta_metrics）；
  它只是初筛（>0 才进入后续显著性与高原检查）；
- **配对显著性（t_stat / DSR_on_diff）作用在差序列**上——这是真正的
  binding constraint：10 年日频下差序列 ΔSharpe≈0.2 → t≈0.63 不可达
  confirmed（差序列口径检出下限 ≈0.5）；策略序列级改进 0.8→1.0（ρ≈0.98
  单槽 diff）对应差序列 ≈1.0 → 检出率 93%（DS 独立复算）。
"""

from __future__ import annotations

# 平台判定阈值（默认值；运行期可由 app_config 的 research.rules.* 覆盖，
# 调整走架构稿修订流程）
DEFAULT_RULES = {
    "min_delta_sharpe": 0.0,
    "significant_deterioration_delta_sharpe": -0.2,
    "regime_collapse_delta_sharpe": -0.3,
    "min_t_stat": 1.645,               # 配对 t ≥ 95% 单尾（大样本渐近值）
    # DSR（差序列，尝试次数校正）阈值。**注意（R18B-P2-1）**：DSR = Φ(z) ∈ (0,1]，
    # 阈值 0.0 下该门**恒真**（t 门通过已蕴含 z>0）→ 平台实际上没有任何多重检验
    # 折扣；论文口径是 DSR ≥ 0.95（Bailey & López de Prado 2014），且需要
    # 跨试验方差 sr_var 才有判别力。是否采纳属研究纪律决策（R18-D-1）。
    "min_dsr_on_diff": 0.0,
    "plateau_sigma": 1.0,  # Alvarez 式标准差检查：选定参数偏离邻域均值 1σ → 孤峰
}


def dsr_gate_binding(rules: dict | None = None) -> bool:
    """该阈值下 DSR 门是否可能否决（False = 名义存在但恒真，不得声称有折扣）。"""
    return float((rules or DEFAULT_RULES).get("min_dsr_on_diff", 0.0)) > 0.0


def paired_gate_ok(
    paired: dict | None, *, rules: dict | None = None, direction: str = "positive"
) -> bool:
    """配对显著性门（**单一真源**；R23A-F1：backtest 与 head_to_head 共用）。

    direction="positive"：ΔSharpe 显著为正 —— t 统计 ≥ max(min_t_stat,
    t_{0.95}(n_pairs−1)) 且差序列 DSR > min_dsr_on_diff；
    direction="negative"：对称的显著为负（判定 A 显著劣于 B；DSR 是同向单调量，
    负向判定只看 t 临界）。

    为什么必须是配对口径：改进型实验是**高相关配对**（同窗同池），单序列 PSR
    的分母是实验自身方差、阈值是基准的已实现 Sharpe（`stats/psr.py` 注释里
    点名的口径错误）。实测（n=2430、ρ=0.98、真 ΔSR=0.3）：配对 t 命中 99.9%，
    单序列 PSR 门命中 0.0% → 高相关场景 confirmed 恒不可达。
    """
    if not isinstance(paired, dict):
        return False
    # R25A-F11：配对数 <2 时 t 临界退化到最松档（1.645）——与"小样本不给判定"同纪律
    if int(paired.get("n_pairs") or 0) < 2:
        return False
    rules = rules or load_rules()
    t_stat = paired.get("t_stat")
    if t_stat is None:
        return False
    t_stat = float(t_stat)
    n_pairs = int(paired.get("n_pairs") or 0)
    t_crit = (
        max(float(rules["min_t_stat"]), _t_critical_95(n_pairs - 1))
        if n_pairs >= 2 else float(rules["min_t_stat"])
    )
    if direction == "negative":
        return t_stat <= -t_crit
    dsr_on_diff = paired.get("dsr_on_diff")
    return t_stat >= t_crit and float(dsr_on_diff or 0.0) > float(
        rules["min_dsr_on_diff"]
    )


def _t_critical_95(df: int) -> float:
    """单尾 95% t 临界值（R24B-F1：改用无依赖的精确反演 `stats.tdist.t_ppf`，
    与 scipy 对拍差 ≤2.6e-10；此前的 Cornish–Fisher 展开在小自由度上偏差 ~1e-3）。

    GLM53F-P2-1②：1.645 是 z 值，小样本（n<~60）真实 t 临界更高，
    用 z 反保守。
    """
    from research.stats.tdist import t_ppf

    return float(t_ppf(0.95, max(int(df), 1)))


def _t_critical_95_legacy(df: int) -> float:
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
        return sorted({max(1, round(value - 2)), max(1, round(value + 2))} - {int(value)})
    if param in _DAY_PARAMS:
        lo = max(1, round(value * 0.8))
        hi = max(2, round(value * 1.2))
        return sorted({lo, hi} - {int(value)})
    # 默认 ±20%
    return sorted({value * 0.8, value * 1.2} - {value})


def plateau_verdict(selected_delta: float, neighbor_deltas: list[float],
                    rules: dict | None = None, *, skipped: int = 0) -> dict:
    """高原/孤峰判定：邻域同向 + **95% 预测区间**偏离检查。

    R23A-F5（P2）重写判据。此前是"选定值偏离邻域均值 `plateau_sigma`(=1.0) × σ̂"，
    而 σ̂ 取自 k 个邻域点、选定点又**不在**邻域内——偏离量的 sd 是
    σ√(1+1/k)，拿它去比"1.0×σ̂"在结构上就不是 1σ 检验；`same_direction` 更是
    "逐点符号全一致"（k=2 时 3 个符号相乘），近零效应下退化成硬币。
    实测（20000~40000 次 MC，见 round23-review.md 的表）：
      - 真高原（等均值）误判 peak 50%~60%（k=2 时 59%）；
      - 边际真实改进（ΔSharpe≈0.10，E0002 量级）误判 peak **74%~91%**；
      - 而 `low_confidence`（<5 点）恰好在误判最重处静默。
    新判据：
      - 同向 = **邻域均值**与选定值同号（零均值/零选定视为中性，R23A-F7：
        逐点符号全一致会把"该参数在此取值不生效"的零效应点判成反向）；
      - 偏离 = |选定 − 邻域均值| > t_{0.975, k−1} · σ̂ · √(1+1/k)（预测区间，
        双侧名义 5%）；
      - `low_confidence` 仍按去重邻域点数 <5 标注（σ̂ 由很少的点估计），但此时的
        误判率已由判据本身校准（实测 H0 6.5%~7.4%，而旧判据 50%~60%）。
    实测功效（真孤峰 +0.5 vs 邻域 0±0.1）：k=2 61%、k=5 94%、k=10 99%。

    ``skipped``：因退化腿（Sharpe 不可用）被剔除的邻域点数——必须显式可见，
    否则"邻域点变少"会静默降低判定的可信度（连带修）。
    """
    rules = rules or DEFAULT_RULES
    if not neighbor_deltas:
        reason = "no neighbors" if not skipped else f"no usable neighbors (skipped {skipped})"
        return {"verdict": "unknown", "reason": reason, "skipped": int(skipped)}
    import numpy as np

    from research.stats.tdist import t_ppf  # 无第三方依赖（R24B-F1：

    # 判定主路径不得依赖未声明的 scipy——部署用 .venv 里没有它，
    # 任何 import scipy 都会让实验直接 failed）
    arr = np.asarray(neighbor_deltas, dtype=float)
    mean = float(arr.mean())
    # 邻域点太少时 σ 没有意义，判据必须**停止**而不是给一个随机答案（R18B-P2-2）：
    #  - 1 个点（合法配置 max_positions/top_n/max_positions=1 等，或两个邻域点取值相同）
    #    → σ=0 → 旧实现 `deviates=False` → 孤立峰被**静默**记成"高原"；
    # 统一按"证据不足 = unknown + 可见告警"处理（与该函数对"无邻域点"的既有口径一致）。
    distinct = np.unique(arr)
    if distinct.size < 2:
        return {
            "verdict": "unknown",
            "reason": f"neighbor_points_insufficient({distinct.size})",
            "neighbor_mean": mean,
            "neighbor_std": float(arr.std(ddof=1)) if len(arr) > 1 else 0.0,
            "selected": float(selected_delta),
            "same_direction": None,
            "deviates_over_1sigma": None,
            "skipped": int(skipped),
            "insufficient_neighbors": True,
        }
    # σ̂ 用**去重后的邻域值**估计，与 t 临界值的自由度（k−1）同源（R24B-F3：
    # 此前 σ̂ 用含重复值的 n 个点、df 用去重点数 k−1，邻域 [1.0,1.0,3.0] 的半宽
    # 因此被放宽 3 倍）。
    k = int(distinct.size)
    std = float(distinct.std(ddof=1)) if k > 1 else 0.0
    sel = float(selected_delta)
    # 同向只作**诊断量**：R24B-F3 实测"邻域均值符号"在近零效应下就是掷硬币
    # （H0 误判 peak 52%），而偏离腿已按 95% 预测区间校准（1.5%~4.8%）——
    # 方向相反的邻域必然被偏离腿抓住（差距相对自身离散度很大），故不再当门。
    # 这里用带容差的符号，避免 1e-17 浮点尘翻转诊断值。
    tol = 1e-12 * max(1.0, abs(sel))
    same_direction = bool(
        abs(sel) <= tol or abs(mean) <= tol
        or (np.sign(mean) == np.sign(sel))
    )
    low_confidence = distinct.size < 5
    t_crit = float(t_ppf(0.975, max(k - 1, 1)))
    se = std * float(np.sqrt(1.0 + 1.0 / k))
    deviation = abs(sel - mean)
    deviates = std > 0 and deviation > t_crit * se
    verdict = "peak" if deviates else "plateau"
    return {
        "verdict": verdict,
        "neighbor_mean": mean,
        "neighbor_std": std,
        "selected": sel,
        "same_direction": same_direction,
        # 键名沿用（消费面/台账兼容）：语义已从"偏离 1σ"改为"落在 95% 预测区间外"
        "deviates_over_1sigma": bool(deviates),
        "deviation": float(deviation),
        "pi_t_crit": t_crit,
        "pi_half_width": float(t_crit * se),
        "neighbor_points": int(distinct.size),
        "low_confidence": bool(low_confidence),
        "skipped": int(skipped),
    }
def suggest_backtest_verdict(evidence: dict, rules: dict | None = None) -> str:
    """backtest 实验的判定建议（平台按阈值化规则给出；AI 不能改判定规则）。"""
    rules = rules or DEFAULT_RULES
    deltas = evidence.get("deltas_vs_base") or {}
    delta_sharpe = deltas.get("delta_sharpe")
    if delta_sharpe is None:
        return "inconclusive"
    if delta_sharpe <= rules["significant_deterioration_delta_sharpe"]:
        # R24B-F4：负向判定此前**只看序列级点估计**（ΔSharpe ≤ −0.2），不查配对
        # 显著性——而 confirmed 侧要求 paired_gate_ok。实测 H0（两条腿同分布、
        # 真实 ΔSharpe=0、n=2430、ρ≤0.7）下有 **20%~34%** 被判 "rejected"
        # （"假设被证伪"，且 append-only 台账只能降不能升），其中 78%~87% 的配对
        # t 根本不显著。这里与 confirmed 对称：恶化也要过配对负向门。
        paired_neg = (evidence.get("stats") or {}).get("paired")
        if paired_neg is not None and paired_gate_ok(
            paired_neg, rules=rules, direction="negative"
        ):
            return "rejected"
        # 无配对证据（样本 <30 / 无基准腿）→ 证据不足，不判"证伪"
        return "inconclusive"
    plateau = evidence.get("plateau")
    regime = evidence.get("regime_split") or {}
    paired = (evidence.get("stats") or {}).get("paired")

    # 只有**样本足够**的 regime 分段才有资格行使塌陷否决——
    # 极短分段（如 9 个交易日）的 ΔSharpe 是噪声，却足以一票否决 confirmed。
    # 样本不足的段视为不可用（不足以否决），其存在本身由报告的 n_days 可见。
    collapse = any(
        (seg.get("delta_sharpe") is not None
         and seg.get("sufficient_sample", True)
         and seg["delta_sharpe"] <= rules["regime_collapse_delta_sharpe"])
        for seg in regime.values()
    )
    is_plateau = plateau is None or plateau.get("verdict") != "peak"
    # GLM53F-P2-1②：小样本用 t 分布临界（自由度 n_pairs−1），不退回 z；
    # R23A-F1：判定逻辑收敛到 paired_gate_ok 单一真源（h2h 同用）
    paired_ok = paired_gate_ok(paired, rules=rules)
    if delta_sharpe > rules["min_delta_sharpe"] and is_plateau and not collapse and paired_ok:
        return "confirmed"
    return "inconclusive"
