from __future__ import annotations

import pandas as pd

from rule_backtest.models import PositionState
from rule_backtest.value_resolver import ValueResolver

CROSS_OPERATORS = frozenset({"cross_above", "cross_below"})


class ConditionEngine:
    def __init__(self, resolver: ValueResolver) -> None:
        self.resolver = resolver

    def evaluate_group(
        self,
        group: dict,
        bars: pd.DataFrame,
        position: PositionState,
        debug: bool = False,
        combinator: str | None = None,
    ) -> tuple[bool, list[dict]]:
        if not isinstance(group, dict):
            return False, []
        children = group.get("children", []) or []
        combinator = combinator or str(group.get("combinator", "all")).strip().lower()
        traces: list[dict] = []
        results: list[bool] = []
        for idx, child in enumerate(children):
            passed, trace = self.evaluate_condition(child, bars=bars, position=position, debug=debug)
            trace["condition_index"] = idx
            traces.append(trace)
            results.append(passed)

        if not results:
            return False, traces
        if combinator == "any":
            return any(results), traces
        return all(results), traces

    def evaluate_condition(
        self,
        condition: dict,
        bars: pd.DataFrame,
        position: PositionState,
        debug: bool = False,
    ) -> tuple[bool, dict]:
        left_value, left_trace = self.resolver.resolve(condition.get("left", {}) or {}, bars, position, debug=debug)
        right_value, right_trace = self.resolver.resolve(condition.get("right", {}) or {}, bars, position, debug=debug)
        operator = str(condition.get("operator", "")).strip()

        passed = False
        left_prev_value: float | None = None
        right_prev_value: float | None = None
        if operator in CROSS_OPERATORS:
            # 上穿/下穿需要前一日的左右值：把 bars 截掉最后一天再 resolve 一次。
            # memoized 热路径下这只是缓存序列的 idx-1 查找；legacy/debug 路径
            # 截断后重算同样得到昨日值。注意 random_uniform（仅测试用）与 cross
            # 组合时每天每侧会消耗两次 RNG draw。
            if len(bars) >= 2:
                prev_bars = bars.iloc[:-1]
                left_prev_value, _ = self.resolver.resolve(condition.get("left", {}) or {}, prev_bars, position, debug=False)
                right_prev_value, _ = self.resolver.resolve(condition.get("right", {}) or {}, prev_bars, position, debug=False)
            if (
                left_value is not None
                and right_value is not None
                and left_prev_value is not None
                and right_prev_value is not None
            ):
                if operator == "cross_above":
                    passed = float(left_prev_value) <= float(right_prev_value) and float(left_value) > float(right_value)
                elif operator == "cross_below":
                    passed = float(left_prev_value) >= float(right_prev_value) and float(left_value) < float(right_value)
        elif left_value is not None and right_value is not None:
            if operator == ">=":
                passed = float(left_value) >= float(right_value)
            elif operator == "<=":
                passed = float(left_value) <= float(right_value)
        elif operator == ">=" and left_value is None and right_value is not None:
            # 离场冷却期特判：days_since_last_exit 从未卖出时为 None，
            # 语义为「无冷却限制」—— `>= X` 直接通过，首次入场不被阻塞。
            # （`<= X` 与 None 比较仍不通过，cross 操作符也不做特判。）
            left_spec = condition.get("left", {}) or {}
            if (
                str(left_spec.get("type", "")).strip() == "state_value"
                and str(left_spec.get("name", "")).strip() == "days_since_last_exit"
            ):
                passed = True

        trace = {
            "condition_id": condition.get("id"),
            "operator": operator,
            "left_value": left_value,
            "right_value": right_value,
            "passed": passed,
        }
        if operator in CROSS_OPERATORS:
            trace["left_prev_value"] = left_prev_value
            trace["right_prev_value"] = right_prev_value
        if debug:
            trace["left_trace"] = left_trace
            trace["right_trace"] = right_trace
        return passed, trace
