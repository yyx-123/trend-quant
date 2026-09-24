"""research 包的异常类型。"""

from __future__ import annotations


class ResearchError(Exception):
    """L4 投研层基础异常。"""


class IntakeRejected(ResearchError):
    """实验创建被骨架校验拒绝（rejected_intake，留痕）。

    ``reasons`` 为全部拒绝原因（一次说清，不逐条挤牙膏）。
    """

    def __init__(self, reasons: list[str], experiment_id: str | None = None) -> None:
        self.reasons = list(reasons)
        self.experiment_id = experiment_id
        super().__init__("; ".join(self.reasons))


class LifecycleError(ResearchError):
    """非法生命周期转移/终态前置条件未满足。"""


class SessionRequired(ResearchError):
    """会话不存在或类型不符。"""


class TopicError(ResearchError):
    """课题相关错误（不存在/已关闭/引用校验失败）。"""


class HoldoutError(ResearchError):
    """holdout 窗口访问未获放行。"""


class PermissionDenied(ResearchError):
    """越权操作（如非 human session 发放 holdout token）。"""
