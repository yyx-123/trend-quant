"""策略配置模型（详设 §5.3）：策略 = 一份七插槽 YAML 配置，不是代码。

- 每个插槽引用必须是注册表中的已注册模块（name@version），参数过该模块
  的参数 schema 校验——非法配置在载入时即拒绝；
- 入库即冻结：config_hash 唯一、版本不可变；``parent_version`` 让
  "实验 = 基准 + diff" 在配置层机器可算；
- portfolio_risk 槽是多 gate 顺序叠加（list），其余槽是单模块绑定。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import yaml

from portfolio.registry import (
    META_MODULES,
    SEVEN_SLOTS,
    ModuleRegistry,
    parse_module_ref,
    validate_params,
)


class StrategyConfigError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class SlotBinding:
    """单槽绑定：模块引用 + 已校验参数。module=None 表示该槽为空（none）。"""

    module: str | None  # "name@version"；None = 空槽
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"module": self.module, "params": dict(self.params)}


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    description: str
    # 单模块槽：universe/signal/rank/sizing/position_risk/execution
    slots: dict[str, SlotBinding]
    # 组合风控槽：gate 顺序叠加（可为空列表）
    gates: list[SlotBinding]

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "portfolio_risk": [g.to_dict() for g in self.gates],
        }
        for slot in SEVEN_SLOTS:
            if slot == "portfolio_risk":
                continue
            out[slot] = self.slots[slot].to_dict()
        return out

    def canonical_yaml(self) -> str:
        return yaml.safe_dump(self.to_dict(), allow_unicode=True, sort_keys=True)

    def config_hash(self) -> str:
        return config_hash_of(self.to_dict())


def config_hash_of(config_dict: dict) -> str:
    """配置内容寻址 hash（sha256，键序无关）。改一个字就是新版本。"""
    blob = json.dumps(config_dict, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


_SINGLE_SLOTS = tuple(s for s in SEVEN_SLOTS if s != "portfolio_risk")


def _validate_binding(
    slot: str,
    raw: Any,
    registry: ModuleRegistry,
    errors: list[str],
) -> SlotBinding:
    if raw is None:
        return SlotBinding(module=None, params={})
    if isinstance(raw, str):
        raw = {"module": raw}
    if not isinstance(raw, dict):
        errors.append(f"{slot}: binding must be a mapping, got {type(raw).__name__}")
        return SlotBinding(module=None, params={})
    module_ref = raw.get("module")
    if module_ref in (None, "", "none"):
        return SlotBinding(module=None, params={})
    module_ref = str(module_ref).strip()
    try:
        name, _ver = parse_module_ref(module_ref)
    except ValueError as exc:
        errors.append(f"{slot}: {exc}")
        return SlotBinding(module=None, params={})
    spec = registry.get(module_ref, slot=slot)
    if spec is None:
        errors.append(f"{slot}: module not registered: {module_ref}")
        return SlotBinding(module=None, params={})
    if spec.slot != slot and spec.name not in META_MODULES:
        errors.append(f"{slot}: module {module_ref} belongs to slot {spec.slot}")
        return SlotBinding(module=None, params={})
    params, param_errors = validate_params(spec.params_schema, raw.get("params"))
    errors.extend(f"{slot}: {e}" for e in param_errors)
    # 元模块嵌套有界（详设 §5.2.8）：any_of/all_of 的成员不得再是元模块
    if name in META_MODULES:
        members = (raw.get("params") or {}).get("members") or []
        for item in members:
            sub_ref = item.get("module") if isinstance(item, dict) else item
            sub_ref = str(sub_ref or "").strip()
            sub_name = sub_ref.split("@")[0]
            if sub_name in META_MODULES:
                errors.append(f"{slot}: meta module {name} cannot nest {sub_name}")
                continue
            # R1-P3-7：成员参数同样在载入期过 schema——"非法配置载入即拒绝"
            # （§5.3）此前对 members 弱化了一步（坏 atr_mul 到 run 启动实例化
            # 时才炸）。与 _MetaBase 工厂内的校验同构，只是前移到 parse。
            sub_spec = registry.get(sub_ref, slot=slot)
            if sub_spec is None:
                continue  # 未注册由工厂/注册表路径报错，此处不重复报
            sub_params_raw = (item.get("params") if isinstance(item, dict) else None) or {}
            _norm, sub_errors = validate_params(sub_spec.params_schema, sub_params_raw)
            errors.extend(f"{slot}: member {sub_ref}: {e}" for e in sub_errors)
    return SlotBinding(module=module_ref, params=params)


def parse_strategy_config(data: dict, registry: ModuleRegistry) -> StrategyConfig:
    """校验并构造策略配置；非法即 StrategyConfigError（载入时拒绝）。"""
    errors: list[str] = []
    if not isinstance(data, dict):
        raise StrategyConfigError(["strategy config must be a mapping"])

    name = str(data.get("name") or "").strip()
    if not name:
        errors.append("name must be non-empty")
    description = str(data.get("description") or "")

    unknown = set(data) - (set(SEVEN_SLOTS) | {"name", "description"})
    for key in sorted(unknown):
        errors.append(f"unknown top-level key: {key}")

    slots: dict[str, SlotBinding] = {}
    for slot in _SINGLE_SLOTS:
        slots[slot] = _validate_binding(slot, data.get(slot), registry, errors)

    gates_raw = data.get("portfolio_risk") or []
    if not isinstance(gates_raw, (list, tuple)):
        errors.append("portfolio_risk must be a list of gate bindings")
        gates_raw = []
    gates = [
        _validate_binding("portfolio_risk", item, registry, errors) for item in gates_raw
    ]
    gates = [g for g in gates if g.module is not None or g.params]

    if errors:
        raise StrategyConfigError(errors)
    return StrategyConfig(
        name=name, description=description, slots=slots, gates=gates
    )


def parse_strategy_yaml(text: str, registry: ModuleRegistry) -> StrategyConfig:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise StrategyConfigError([f"invalid YAML: {exc}"]) from exc
    return parse_strategy_config(data, registry)


def apply_diff(
    base: StrategyConfig,
    diff: list[dict],
    registry: ModuleRegistry,
) -> StrategyConfig:
    """基准 + 插槽 diff → 新配置（详设 §5.6 resolve_experiment_config 的核心）。

    diff 每项：{slot, to, params?}；``to`` 为 null/none 即清空该槽。
    portfolio_risk 槽：``to`` 可为单模块（追加一个 gate）或模块列表（整体替换
    gate 清单）；``from`` 字段是信息性的，不参与计算。
    ``to`` 支持 {module, params} 字典形态（GLM53F-P2-7：intake 校验端接受
    字典形态，resolve 端必须同口径支持——否则白烧 attempt_index 并落脏
    failed 记录）。
    """
    data = base.to_dict()
    for item in diff or []:
        if not isinstance(item, dict):
            raise StrategyConfigError([f"diff item must be a mapping: {item!r}"])
        slot = str(item.get("slot") or "")
        if slot not in SEVEN_SLOTS:
            raise StrategyConfigError([f"diff: unknown slot {slot!r}"])
        to = item.get("to")
        params = item.get("params")
        if isinstance(to, dict):
            # 字典形态 {module, params}——item 级 params 兜底
            params = to.get("params") or params
            to = to.get("module")

        if slot == "portfolio_risk":
            current = list(data.get("portfolio_risk") or [])
            if to in (None, "", "none") or to == []:
                data["portfolio_risk"] = []
                continue
            if isinstance(to, list):
                data["portfolio_risk"] = [
                    {"module": t.get("module") if isinstance(t, dict) else t,
                     "params": (t.get("params") if isinstance(t, dict) else None) or {}}
                    for t in to
                ]
            else:
                current.append({"module": to, "params": params or {}})
                data["portfolio_risk"] = current
            continue

        if to in (None, "", "none"):
            data[slot] = {"module": None, "params": {}}
        else:
            data[slot] = {"module": to, "params": params or {}}

    config = parse_strategy_config(data, registry)
    return config
