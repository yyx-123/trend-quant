"""日循环上下文（详设 §5.2 DayContext）。

所有插槽共享一个上下文对象；模块拿数据的途径：
- ``ctx.panel``：本运行经 L1.5 一次性取出的面板（as_of 锚定）的**逐日
  限窗视图**——任何 series/matrix 访问都只返回到当日（含）的数据，PIT
  卡控不靠模块自觉；
- ``ctx.gateway``：L1.5 受限句柄（as_of 已绑定），供元数据/可交易性/
  生产指标等面板外数据；
- ``ctx.account``：L2 账户只读视图（cash/equity/heat/positions）；
- ``ctx.history``：本运行截至昨日的日结记录（波动率/回撤类闸门要用）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from engine.models import Account
    from gateway.panel import Panel
    from gateway.service import BoundGateway


def _readonly(arr: np.ndarray) -> np.ndarray:
    """把面板切片标为只读（不拷贝，零开销）。

    口径如实：`writeable=False` 的视图拒绝**直接**赋值，
    挡住"顺手改一行"这类事故；但它不是安全边界——`arr.base` 仍是可写的父数组，
    刻意绕过依然可以。真正的边界是"内置件不写面板"的代码纪律 + 面板不被视为
    可变对象。"""
    arr.setflags(write=False)
    return arr


class PanelView:
    """Panel 的逐日限窗视图：所有读取截到 upto_idx（含当日）。"""

    # 信任边界（评审 DS-P2-11）：__panel 名称改写只是"随手窥探"的摩擦——
    # 真正的 PIT 保障 = 门面 as-of 强制 + 前缀稳定性探针 + token 持有者信任模型，
    # 与 python 模块门的信任模型同口径（见 research/modules.py）。
    __slots__ = ("__panel", "_upto")

    def __init__(self, panel: Panel, upto_idx: int) -> None:
        self.__panel = panel
        self._upto = upto_idx

    @property
    def full_panel(self) -> Panel:
        raise PermissionError(
            "full panel is not exposed to modules (PIT); use windowed accessors"
        )

    @property
    def dates(self) -> tuple[date, ...]:
        return self.__panel.dates[: self._upto + 1]

    @property
    def upto(self) -> int:
        """当前日在全面板中的行下标（模块 precompute 矩阵的行坐标）。"""
        return self._upto

    @property
    def symbols(self) -> tuple[str, ...]:
        return self.__panel.symbols

    def series(self, symbol: str, field_name: str) -> np.ndarray:
        """某标的截至当日的字段序列（含当日，因果无未来）。

        返回**只读**视图（不拷贝，零开销）——面板是全体模块共享的
        数据面，此前返回可写切片意味着任一模块可以静默改写后续所有日子看到的
        行情（无告警、无留痕）。
        """
        col = self.__panel._symbol_index.get(symbol)
        if col is None:
            return np.empty(0)
        return _readonly(self.__panel.data[field_name][: self._upto + 1, col])

    def matrix(self, field_name: str) -> np.ndarray:
        """(t+1, N) 矩阵视图（截面计算用；缺数据 NaN）。只读，理由同 series。"""
        return _readonly(self.__panel.data[field_name][: self._upto + 1, :])

    def bar(self, symbol: str) -> dict | None:
        return self.__panel.bar_at(symbol, self.__panel.dates[self._upto])

    def value(self, symbol: str, field_name: str) -> float | None:
        col = self.__panel._symbol_index.get(symbol)
        if col is None:
            return None
        v = self.__panel.data[field_name][self._upto, col]
        return float(v) if np.isfinite(v) else None

    def lookback(self, symbol: str, field_name: str, n: int) -> np.ndarray:
        """最近 n 根（含当日）。n<=0 返回空数组（`[-0:]` 是整条序列）。"""
        if n <= 0:
            return np.empty(0)
        return self.series(symbol, field_name)[-n:]

    def has_symbol(self, symbol: str) -> bool:
        return symbol in self.__panel._symbol_index

    def symbol_col(self, symbol: str) -> int | None:
        """列下标（模块查行数据的合法公开入口）。"""
        return self.__panel._symbol_index.get(symbol)

    def date_at(self, idx: int):
        """按全面板行下标取日期（事件日回查的合法公开入口）。

        钳制到 upto：越界下标不得探测未来日期——
        返回值 clamp 到当前日（越界 = 当日），不给模块任何日历前视。"""
        if idx > self._upto:
            idx = self._upto
        return self.__panel.dates[max(0, min(idx, self._upto))]


@dataclass(slots=True)
class AccountView:
    """L2 账户只读视图（对模块暴露；改账户必须经引擎订单）。"""

    _account: Account = field(repr=False)
    close_prices: dict[str, float] = field(default_factory=dict)

    @property
    def cash(self) -> float:
        return self._account.cash

    @property
    def positions(self):
        # 只读视图契约——返回 Mapping 代理，模块 .clear()/乱插
        # Position 直改引擎账户的路径被 TypeError 掐断（读用法不受影响）
        from types import MappingProxyType

        return MappingProxyType(self._account.positions)

    def equity(self) -> float:
        return self._account.equity(self.close_prices)

    def heat(self) -> float | None:
        return self._account.heat(self.close_prices)

    def positions_value(self) -> float:
        return self._account.positions_value(self.close_prices)

    def unstopped_symbols(self) -> list[str]:
        """无止损价的持仓清单（heat None 时的归因入口）。"""
        return self._account.unstopped_symbols()


@dataclass(slots=True)
class DayContext:
    date: date
    as_of: datetime
    gateway: BoundGateway
    panel: PanelView
    account: AccountView
    params: dict = field(default_factory=dict)  # 当前调用槽的参数（已过 schema 校验）
    data_version: int = 0
    history: list = field(default_factory=list)  # 截至昨日的日结记录（engine_daily_nav 行形；回测器传 live list，日内读时当日未 append）
    gate_log: list = field(default_factory=list)  # 风控拦截记录（run 日志）
    extras: dict = field(default_factory=dict)    # 回测器预计算矩阵（如 atr 面板）

    @property
    def run_seed(self) -> int:
        """确定性随机锚点：data_version 无关，随日期变化（random 模块用）。"""
        return self.date.toordinal()

    def history_equity(self) -> np.ndarray:
        """截至昨日的净值序列（波动率/回撤闸门用）。"""
        return np.array([float(r["equity"]) for r in self.history], dtype=float)
