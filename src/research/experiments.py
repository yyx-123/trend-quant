"""实验对象模型与创建校验（详设 §6.6.1 创建校验·入口卡控）。

骨架五条（对所有实验强制，平台 schema 只卡这些）：
1. hypothesis 非空且 ≥ 最小长度；
2. 对照可声明（评估模块注册表中存在且 spec 过该模块 schema）；
3. 单变量可判定（由模块 schema 机器检查）；
4. 引用合法（策略版本已冻结 / 模块已注册且为 reviewed 态 / 参数域内）；
5. attempt_index 平台赋值。

不合法实验**创建不了**——但尝试本身留痕（status=rejected_intake + 原因）。
"""

from __future__ import annotations

import re
from datetime import date

from research.errors import IntakeRejected, LifecycleError
from research.ledger import alloc_id, dumps, loads
from research.lifecycle import get_experiment
from research.sessions import require_session
from research.topics import require_open_topic

MIN_HYPOTHESIS_LEN = 10


def _normalize_spec_value(value):
    """spec 值的规范化（重复检测签名用）：浮点 4 位、列表排序、dict 键序。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return round(float(value), 4)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return {str(k): _normalize_spec_value(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        normalized = [_normalize_spec_value(v) for v in value]
        return sorted(normalized, key=lambda x: repr(x))
    return repr(value)


def _canonical_spec(spec: dict) -> tuple:
    """全 spec 的结构化签名（评审 DS-P1-5）——不只读 diff：

    event_study 的"同一事件换 context_filter/horizons/universe/window"与
    distribution 的"同指标换池/窗口"都是详设 §6.5.2 点名的另一个实验，
    签名必须区分它们；真重复（同 spec）才命中。
    """
    spec = spec or {}
    return tuple(sorted(
        (str(k), repr(_normalize_spec_value(v))) for k, v in spec.items()
    ))


def _is_date_string(value) -> bool:
    import re

    return isinstance(value, str) and bool(re.match(r"^\d{4}-\d{2}-\d{2}$", value))


def _coerce_scalar(v):
    """标量归一化（GLM53F-P2-4 类型逃逸修复）：数值字符串 → int/float、
    "true"/"false" → bool——语义相同的值不得因 JSON 类型差异绕过两档检测。"""
    if isinstance(v, str):
        t = v.strip()
        if t.lower() in ("true", "false"):
            return t.lower() == "true"
        try:
            return int(t)
        except ValueError:
            pass
        try:
            return float(t)
        except ValueError:
            pass
    return v


def _expand_platform_defaults(spec: dict, evaluation_module: str = "") -> dict:
    """把"省略 = 平台缺省值"的字段展开成显式值（loop-review R1-P2-3）。

    重复检测的逃逸通道：跑过 `window=[2015-01-01, 2024-12-31]` 的实验，
    重提一个**不带 window 键**的同 diff 实验——键集不同被判"另一个实验"
    放行，实际取数窗口与原实验完全相同（runner 侧 `spec.get("window") or
    sample 默认`）。省略 ≠ 改问题。universe 同理（None/"liquidity_default"
    与缺省同为 enabled 全池）。展开后 exact/similar 判定才在同一语义层。

    模块感知：只展开该评估模块**声明**的字段——portfolio_backtest 的 spec
    没有 universe 键（不是它的声明字段），给它强加 universe 反而会被
    _spec_similar 判成"未知字段差异"误报相似。
    """
    from research.holdout import DEFAULT_SAMPLE_END, DEFAULT_SAMPLE_START

    expanded = dict(spec or {})
    known = _DECLARED_SPEC_FIELDS.get(str(evaluation_module or "").split("@")[0], set())
    if "window" in known and not expanded.get("window"):
        expanded["window"] = [DEFAULT_SAMPLE_START, DEFAULT_SAMPLE_END]
    if "universe" in known and expanded.get("universe") in (None, "liquidity_default"):
        expanded["universe"] = "liquidity_default"
    return expanded


def _values_similar(a, b) -> bool:
    """相似判定（DS-R2 P2 两档恢复）：结构相同，仅数值 ±10% / 日期 ±10 天。

    GLM53F-P2-4：先归一化标量（"2.0"≡2.0、"true"≡True、2≡2.0），再按结构
    递归——类型差异不再构成逃逸通道。
    """
    a, b = _coerce_scalar(a), _coerce_scalar(b)
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if isinstance(a, bool):  # 两者皆 bool（一真一假上方已排除）
            return a == b
        return abs(float(a) - float(b)) <= max(abs(float(a)), abs(float(b)), 1e-6) * 0.10
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        if set(a) != set(b):
            return False
        return all(_values_similar(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)):
        if len(a) != len(b):
            return False
        return all(_values_similar(x, y) for x, y in zip(a, b))
    if _is_date_string(a) and _is_date_string(b):
        from datetime import date as _date

        try:
            da, db_ = _date.fromisoformat(a[:10]), _date.fromisoformat(b[:10])
        except ValueError:
            return False
        return abs((da - db_).days) <= 10
    return a == b


# 各评估模块的声明字段（DS-R2 P2：字段差异是"另一个实验"还是"加字段逃逸"
# 的分水岭——声明字段的差异按值判定；未知字段差异一律按可疑相似处理）
_DECLARED_SPEC_FIELDS = {
    "portfolio_backtest": {
        "base", "diff", "is_compound", "compound_reason", "window", "window_mode",
        "n_folds", "mc_bands", "initial_capital", "expect", "primary_horizon",
    },
    "event_study": {
        "event", "context_filter", "universe", "horizons", "path_stats",
        "expect", "primary_horizon", "window",
    },
    "bucket_analysis": {
        "signal_module", "feature", "buckets", "horizons", "universe",
        "expect", "window",
    },
    "distribution": {"metric", "universe", "window", "criterion"},
    "head_to_head": {"base", "ref", "window", "initial_capital"},
}


def _spec_similar(a: dict, b: dict, *, evaluation_module: str) -> bool:
    """spec 级相似（两档第二档）：

    - 声明字段：每个差异都需通过 _values_similar（数值 ±10%/日期 ±10 天；
      字符串/布尔必须一致——换条件/换特征 = 另一个实验，放行）；
    - **未知字段差异：一律判相似**（DS-R2 P2："加一个平台不消费的字段就
      完全绕过"的逃逸必须被堵——未知字段一律要求显式确认）。
    """
    a, b = a or {}, b or {}
    known = _DECLARED_SPEC_FIELDS.get(str(evaluation_module or "").split("@")[0], set())
    extra = (set(a) | set(b)) - known
    if extra:
        return True  # 未知字段差异 → 可疑相似（要求确认）
    shared = set(a) & set(b)
    if set(a) != set(b):
        return False  # 声明字段增删 = 另一个实验（如 event 加 context_filter）
    return all(_values_similar(a[k], b[k]) for k in shared)


def find_duplicates(
    db, *, evaluation_module: str, subject_key: str, spec: dict
) -> tuple[list[str], list[str]]:
    """两档重复检测（详设 §6.6.6 + DS-R2 P2）。返回 (exact_ids, similar_ids)。

    - exact：规范化 spec 完全一致 → 硬拒（allow_duplicate 不救——
      请改 spec 或走 rerun 复现）；
    - similar：同评估模块 + 同研究线 + 结构相同仅数值 ±10%/日期 ±10 天 →
      须显式确认（allow_duplicate=True 才放行），防"加字段/挪一天"逃逸。
    """
    from research.ledger import loads

    # R1-P2-3：两侧先展开平台缺省（window/universe）——"省略键 = 用同一
    # 缺省"必须与显式写缺省判定一致，否则缺省即逃逸通道
    spec = _expand_platform_defaults(spec or {}, evaluation_module)
    target_sig = _canonical_spec(spec)
    target_norm = {k: _normalize_spec_value(v) for k, v in spec.items()}
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT id, spec_json FROM research_experiments
               WHERE evaluation_module = ? AND subject_key = ?
                 AND status <> 'rejected_intake'
               ORDER BY id""",
            (evaluation_module, subject_key),
        ).fetchall()
    exact, similar = [], []
    for row in rows:
        existing = _expand_platform_defaults(
            loads(row["spec_json"], {}), evaluation_module
        )
        if _canonical_spec(existing) == target_sig:
            exact.append(row["id"])
        elif _spec_similar(
            target_norm, {k: _normalize_spec_value(v) for k, v in existing.items()},
            evaluation_module=evaluation_module,
        ):
            similar.append(row["id"])
    return exact, similar


_ISO_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_window_spec(window) -> list[str]:
    """window 字段的入口校验（loop-review-ds4f R1-P1-2）。

    `window` 是**平台级**骨架字段（各评估模块共用），必须在入口统一卡形状，
    不能指望各模块的 `_spec_errors`——面板型三模块（event_study /
    bucket_analysis / distribution）此前都不看 window，而 holdout 判定按原始
    字符串比字典序、取数侧用 pandas 解析同一字符串，于是
    `["2024-01-01", "01/01/2026"]` / `" 2026-01-01"` 这类"格式不同、语义相同"
    的窗口既不触发 holdout 卡控、又照样取到 holdout 段数据（且留痕说谎）。

    这里收口为严格 ISO：两元素、`YYYY-MM-DD`、start < end。日期也可解析时
    才允许（`2024-02-31` 这类不存在的日期一并拒绝）。
    """
    if window is None:
        return []
    if not isinstance(window, (list, tuple)) or len(window) != 2:
        return ["spec.window must be a 2-element list [start, end] (YYYY-MM-DD)"]
    # 不做 strip：规范化的宽容度正是逃逸的温床（" 2026-01-01" 曾被判"未触碰"），
    # 入口要求**严格** ISO 字面量；holdout 侧另有解析后比较作为 fail-closed 兜底。
    start, end = str(window[0] if window[0] is not None else ""),         str(window[1] if window[1] is not None else "")
    errors: list[str] = []
    for label, value in (("start", start), ("end", end)):
        if not _ISO_DAY_RE.match(value):
            errors.append(f"spec.window.{label} must be YYYY-MM-DD, got {value!r}")
            continue
        try:
            date.fromisoformat(value)
        except ValueError:
            errors.append(f"spec.window.{label} is not a real date: {value!r}")
    if not errors and start >= end:
        errors.append(f"spec.window start must be < end ({start} >= {end})")
    return errors


def propose_experiment(
    db,
    *,
    session_id: str,
    title: str,
    topic_id: str,
    evaluation_module: str,
    spec: dict,
    hypothesis: str,
    parent_experiment_id: str | None = None,
    registry=None,
    allow_duplicate: bool = False,
) -> dict:
    """创建实验（入口卡控）。返回实验行。

    骨架校验全部通过 → status=queued；任一不过 → status=rejected_intake
    并抛 IntakeRejected（实验行已落库留痕，id 在异常上可取）。
    """
    from research import evaluations

    session = require_session(db, session_id)
    topic = require_open_topic(db, topic_id)

    reasons: list[str] = []

    title = str(title or "").strip()
    if not title:
        reasons.append("title must be non-empty")

    # 骨架 1：假设先行
    hypothesis = str(hypothesis or "").strip()
    if len(hypothesis) < MIN_HYPOTHESIS_LEN:
        reasons.append(
            f"hypothesis must be non-empty and >= {MIN_HYPOTHESIS_LEN} chars"
        )

    # 骨架 2：对照可声明（评估模块已注册）
    module = evaluations.get_evaluation(evaluation_module)
    if module is None:
        reasons.append(f"evaluation module not registered: {evaluation_module}")

    if parent_experiment_id:
        parent = get_experiment(db, parent_experiment_id)
        if parent is None:
            reasons.append(f"parent experiment not found: {parent_experiment_id}")

    # 骨架 3/4：单变量可判定 + 引用合法（评估模块自己的 spec schema 机器检查）
    ctx = {"db": db, "registry": registry}
    if module is not None:
        try:
            reasons.extend(module.validate_spec(spec or {}, ctx))
        except Exception as exc:  # 模块 schema 自身的错误也视为不合法 spec
            reasons.append(f"spec validation failed: {exc}")

    # 骨架 3.5：window 形状与格式（loop-review-ds4f R1-P1-2）——平台级字段
    # 统一卡口，见 validate_window_spec 的说明。
    if isinstance(spec, dict) and "window" in spec:
        reasons.extend(validate_window_spec(spec.get("window")))

    # 骨架 5：attempt_index 平台赋值 = 同研究线（subject_key）已达入口的
    # 实验数 + 1（rejected_intake 未真正取证，不计入尝试次数）。
    subject_key = module.subject_key(spec or {}) if module is not None else ""
    with db.connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM research_experiments
               WHERE subject_key = ? AND status <> 'rejected_intake'
                 AND is_reproduction = 0""",
            (subject_key,),
        ).fetchone()
    attempt_index = int(row["n"] or 0) + 1

    if not reasons:
        exact_dupes, similar_dupes = find_duplicates(
            db, evaluation_module=evaluation_module, subject_key=subject_key, spec=spec or {}
        )
        if exact_dupes:
            reasons.append(
                "duplicate_of: " + ",".join(exact_dupes)
                + "（与历史实验完全重复，硬拒——请修改 spec 或用 rerun 复现）"
            )
        elif similar_dupes and not allow_duplicate:
            reasons.append(
                "similar_to: " + ",".join(similar_dupes)
                + "（相似实验已存在，含失败记录；确认非重复发现后用 allow_duplicate=True 重提）"
            )

    status = "queued" if not reasons else "rejected_intake"
    reject_reason = "; ".join(reasons) if reasons else None

    with db.connect() as conn:
        exp_id = alloc_id(conn, "research_experiments", "E", width=4)
        conn.execute(
            """INSERT INTO research_experiments
               (id, title, owner_session, created_by, topic_id, evaluation_module,
                subject_key, spec_json, hypothesis, parent_experiment_id,
                status, attempt_index, reject_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                exp_id,
                title,
                session["session_id"],
                session["kind"],
                topic["id"],
                evaluation_module,
                subject_key,
                dumps(spec or {}),
                hypothesis,
                parent_experiment_id,
                status,
                attempt_index,
                reject_reason,
            ),
        )

    exp = get_experiment(db, exp_id)
    if reasons:
        raise IntakeRejected(reasons, experiment_id=exp_id)
    return exp


def get_experiment_detail(db, experiment_id: str, registry=None) -> dict | None:
    """实验 + spec 解码 + verdicts（台账查询面）。"""
    from research.verdict import list_verdicts

    exp = get_experiment(db, experiment_id)
    if exp is None:
        return None
    exp["spec"] = loads(exp.get("spec_json"), {})
    exp["verdicts"] = list_verdicts(db, experiment_id)
    return exp


def rerun_experiment(db, *, experiment_id: str, session_id: str) -> dict:
    """复现一个已到终态的实验（详设 §6.4.1："复现代码"= spec 本身）。

    语义（评审 DS-P1-2）：
    - 复制原实验的 topic/spec/hypothesis/evaluation_module，parent 指向原实验；
    - attempt_index 与原实验相同（复现不是新尝试，不收紧 DSR）；
    - is_reproduction=1 标记，后续尝试计数不计入；
    - 不走 propose 的骨架校验与重复检测（spec 当年已过校验；复现是显式平台动作）。
    """
    from research.sessions import require_session

    session = require_session(db, session_id)
    original = get_experiment(db, experiment_id)
    if original is None:
        raise LifecycleError(f"experiment not found: {experiment_id}")
    if original["status"] not in ("verdicted", "failed"):
        raise LifecycleError(
            f"only terminal experiments can be rerun (status={original['status']})"
        )
    # R2-P3-2：复现挂原课题——课题必须仍 open（"关题不带在途实验"不变式
    # §6.4.1）。propose/append 都被 require_open_topic 挡住，rerun 是唯一
    # 缺口；已关课题的复现请先新建课题（append_experiment_to_topic 迁移）。
    from research.topics import require_open_topic

    require_open_topic(db, original["topic_id"])

    from research.ledger import alloc_id

    with db.connect() as conn:
        new_id = alloc_id(conn, "research_experiments", "E", width=4)
        conn.execute(
            """INSERT INTO research_experiments
               (id, title, owner_session, created_by, topic_id, evaluation_module,
                subject_key, spec_json, hypothesis, parent_experiment_id,
                status, attempt_index, is_reproduction)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, 1)""",
            (
                new_id,
                f"复现 {experiment_id}",
                session["session_id"],
                session["kind"],
                original["topic_id"],
                original["evaluation_module"],
                original["subject_key"],
                original["spec_json"],
                original["hypothesis"],
                experiment_id,
                int(original["attempt_index"]),
            ),
        )
    return get_experiment(db, new_id)
