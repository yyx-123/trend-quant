"""评估模块注册入口：导入本包即注册全部内置评估模块。

首批四个（详设 §6.1.2）：portfolio_backtest / event_study / bucket_analysis /
distribution。新增研究形态 = 新增注册评估模块，不是新增实验类别。
"""

from __future__ import annotations

from research.evaluations import backtest, bucket, distribution, event, head_to_head  # noqa: F401
from research.evaluations.base import (
    EvaluationModule,
    get_evaluation,
    list_evaluations,
    register_evaluation,
    require_evaluation,
)

__all__ = [
    "EvaluationModule",
    "get_evaluation",
    "list_evaluations",
    "register_evaluation",
    "require_evaluation",
]
