"""DSL 表达式引擎（详设 §6.2.2：算子白名单、构造上无前视，免测试直接用）。

用途：AI 提的信号/筛选类模块以表达式定义，例如
    ``cross_above(close, sma(close, 20))``
    ``(close > sma(close, 20)) & (volume > ref(volume, 1))``

安全由构造保证：
- ast 白名单（仅允许列出的节点与函数名；无属性访问、无下标、无赋值）；
- 求值域只有面板矩阵（(T,N) DataFrame）与白名单函数——全部因果变换
  （rolling/ewm/shift 向后看），构造上无前视；
- 无 builtins、无 import。
"""

from __future__ import annotations

import ast

import numpy as np
import pandas as pd


class DslError(ValueError):
    pass


def _sma(x: pd.DataFrame, n: int) -> pd.DataFrame:
    return x.rolling(int(n), min_periods=int(n)).mean()


def _ema(x: pd.DataFrame, n: int) -> pd.DataFrame:
    return x.ewm(span=int(n), adjust=False).mean()


def _ref(x: pd.DataFrame, k: int) -> pd.DataFrame:
    return x.shift(int(k))


def _cross_above(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
    return ((a.shift(1) <= b.shift(1)) & (a > b)).astype(float)


def _cross_below(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
    return ((a.shift(1) >= b.shift(1)) & (a < b)).astype(float)


_FUNC_NAMESPACE = {
    "sma": _sma,
    "ema": _ema,
    "ref": _ref,
    "cross_above": _cross_above,
    "cross_below": _cross_below,
    "maximum": np.maximum,
    "minimum": np.minimum,
    "abs": np.abs,
}
_VAR_NAMES = {"open", "high", "low", "close", "volume", "amount"}

_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.Call,
    ast.Name, ast.Load, ast.Constant,
    ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod,
    ast.BitAnd, ast.BitOr, ast.Invert,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)


def compile_expression(expr: str):
    """编译 DSL 表达式为 (fields_map) -> DataFrame 的可调用对象。"""
    text = str(expr or "").strip()
    if not text:
        raise DslError("empty expression")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise DslError(f"syntax error: {exc}") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise DslError(f"disallowed syntax: {type(node).__name__}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _FUNC_NAMESPACE:
                raise DslError("calls allowed only to whitelisted functions")
            if node.keywords:
                raise DslError("keyword arguments not supported")
            # 构造上无前视：ref(x, k) 的 k 只允许非负整数字面量——
            # 任何表达式（ref(close, 0-1) 这类算术绕行）一律拒（评审 B-P1-1）
            if node.func.id in ("ref",) and len(node.args) >= 2:
                k_arg = node.args[1]
                if not (isinstance(k_arg, ast.Constant) and isinstance(k_arg.value, int)
                        and k_arg.value >= 0):
                    raise DslError(
                        "ref(x, k) requires a non-negative integer literal "
                        "(expressions/negative shifts = lookahead)"
                    )
        if isinstance(node, ast.Name) and node.id not in _VAR_NAMES and node.id not in _FUNC_NAMESPACE:
            raise DslError(f"unknown name: {node.id}")
    code = compile(tree, "<dsl>", "eval")

    def _eval(fields: dict[str, pd.DataFrame]) -> pd.DataFrame:
        namespace = {**_FUNC_NAMESPACE, **{k: v for k, v in fields.items() if k in _VAR_NAMES}}
        return eval(code, {"__builtins__": {}}, namespace)

    return _eval


def make_dsl_signal_class(expr: str, *, valid_days: int = 1):
    """DSL 布尔表达式 → signal 模块类（entry = False→True 边沿）。"""
    from portfolio.slots.signal import SignalEvent

    evaluator = compile_expression(expr)

    class DslSignalModule:
        def __init__(self, params: dict | None = None) -> None:
            self.valid_days = int((params or {}).get("valid_days", valid_days))

        def prepare(self, panel) -> None:
            fields = {
                name: pd.DataFrame(panel.data[name], index=pd.Index(panel.dates),
                                   columns=panel.symbols)
                for name in ("open", "high", "low", "close", "volume", "amount")
                if name in panel.data
            }
            cond = evaluator(fields)
            cond = cond.fillna(False).astype(bool)
            edge = cond & ~cond.shift(1, fill_value=False)
            self._cond = cond.to_numpy(dtype=bool)
            self._edge = edge.to_numpy(dtype=bool)
            from portfolio.slots.signal import _last_true_idx

            self._last_edge = _last_true_idx(self._edge)

        def scan(self, ctx, members):
            t = ctx.panel.upto
            events = []
            for m in members:
                col = ctx.panel.symbol_col(m.symbol)
                if col is None:
                    continue
                le = self._last_edge[t, col]
                if le >= 0 and (t - le) < self.valid_days and self._cond[t, col]:
                    day = ctx.panel.date_at(int(le))
                    events.append(SignalEvent(
                        symbol=m.symbol, kind="entry", date=day,
                        meta={"event_date": day.isoformat(), "dsl": True},
                    ))
            return events

    DslSignalModule.__name__ = "DslSignalModule"
    return DslSignalModule
