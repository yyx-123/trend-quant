"""评估模块注册表与基类（详设 §6.1.2）。

实验 = 固定骨架 + 一个已注册评估模块 + spec + 假设 + verdict。
评估模块是实验的必填插槽，取本注册表中已注册模块（name@version）；
**新增研究形态 = 新增注册评估模块，不是新增实验类别**——骨架、台账、
纪律一动不动。平台 schema 只卡骨架字段；spec 的具体内容由评估模块
自定义（模块 = 代码 + 参数空间，随模块版本演化）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# verdict 强度序（confirm_verdict 的"可降不可升"判定依据）：
# confirmed 主张最强，rejected 最弱（不给该假设背书）。
VERDICT_RANK: dict[str, int] = {"confirmed": 2, "inconclusive": 1, "rejected": 0}

# 评估运行上下文（runner 的入参；stage 2+ 才用到，先定义形状）。
EvalContext = dict[str, Any]

# runner(db, experiment_row, ctx) -> {
#   "baseline": dict, "evidence": dict, "warnings": list[str],
#   "report": dict, "suggested_verdict": str,
#   "runs": [{engine_run_id, window_start, window_end, window_kind,
#             holdout_touched}, ...],
# }
EvalRunner = Callable[..., dict]


@dataclass
class EvaluationModule:
    name: str
    version: int
    description: str = ""
    validate_spec: Callable[[dict, EvalContext], list[str]] = lambda spec, ctx: []
    subject_key: Callable[[dict], str] = lambda spec: ""
    runner: EvalRunner | None = None
    # 允许的 final_verdict 集合（相对 suggested 再叠加"可降不可升"）。
    # None = 不额外限制；distribution 类模块固定 inconclusive。
    allowed_finals: tuple[str, ...] | None = None

    @property
    def key(self) -> str:
        return f"{self.name}@{self.version}"

    def final_verdict_allowed(self, suggested: str, final: str) -> bool:
        if final not in VERDICT_RANK or suggested not in VERDICT_RANK:
            return False
        if self.allowed_finals is not None and final not in self.allowed_finals:
            return False
        # 同向约束（评审 K3-P2-7）：confirmed↔rejected 是方向翻转不是降级——
        # 支持的说成驳斥不允许（强度序管升降，方向另行约束）
        if {suggested, final} == {"confirmed", "rejected"}:
            return False
        return VERDICT_RANK[final] <= VERDICT_RANK[suggested]


_MODULES: dict[str, EvaluationModule] = {}


def register_evaluation(module: EvaluationModule) -> EvaluationModule:
    _MODULES[module.key] = module
    return module


def get_evaluation(ref: str) -> EvaluationModule | None:
    return _MODULES.get(str(ref or "").strip())


def require_evaluation(ref: str) -> EvaluationModule:
    module = get_evaluation(ref)
    if module is None:
        raise KeyError(f"evaluation module not registered: {ref}")
    return module


def list_evaluations() -> list[dict]:
    return [
        {
            "key": m.key,
            "name": m.name,
            "version": m.version,
            "description": m.description,
            "runnable": m.runner is not None,
        }
        for m in sorted(_MODULES.values(), key=lambda m: m.key)
    ]
