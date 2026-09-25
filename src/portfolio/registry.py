"""模块注册表（详设 §5.1/§6.2.2）。

插槽模块（universe/signal/rank/sizing/portfolio_risk/position_risk/execution）
以 ``name@version`` 注册，参数域随模块声明（params_schema）。内置模块
（kind=builtin）由 portfolio.slots.* 在导入时注册；AI 提的新模块
（kind=dsl/python）先落 module_drafts，过自动测试门（reviewed）后由
research.modules 装载进本注册表——实验永远引用不到未过测试门的模块。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

SEVEN_SLOTS: tuple[str, ...] = (
    "universe",
    "signal",
    "rank",
    "sizing",
    "portfolio_risk",
    "position_risk",
    "execution",
)

# 组合逻辑元模块（详设 §5.2.8）：any_of / all_of，全插槽通用、不算独立插槽。
META_MODULES: tuple[str, ...] = ("any_of", "all_of")


def parse_module_ref(ref: str) -> tuple[str, int]:
    """"name@2" -> ("name", 2)；非法引用抛 ValueError。"""
    text = str(ref or "").strip()
    if "@" not in text:
        raise ValueError(f"module ref must be name@version, got: {ref!r}")
    name, _, ver = text.rpartition("@")
    name = name.strip()
    if not name or not ver.isdigit() or int(ver) < 1:
        raise ValueError(f"module ref must be name@version, got: {ref!r}")
    return name, int(ver)


@dataclass(frozen=True)
class ModuleSpec:
    """一个已注册的插槽模块。

    factory: 以已校验参数构造模块实例的可调用对象（params -> module）。
    params_schema: {param: {"type": "number"|"integer"|"string"|"boolean"|"list"|"dict",
                            "required": bool, "default": v, "min": x, "max": y,
                            "choices": [...]}}
    """

    slot: str
    name: str
    version: int
    factory: Callable[..., Any]
    params_schema: dict[str, dict] = field(default_factory=dict)
    kind: str = "builtin"  # builtin | dsl | python
    description: str = ""
    draft_id: str | None = None

    @property
    def key(self) -> str:
        return f"{self.name}@{self.version}"


class ModuleRegistrationError(ValueError):
    pass


class ModuleRegistry:
    """注册表内部键 = "slot:name@version"（元模块 any_of/all_of 可存在
    于多个插槽——同名不同槽是两个 ModuleSpec）。外部引用仍是 "name@version"，
    解析时带插槽上下文优先匹配同槽实现。
    """

    def __init__(self) -> None:
        self._modules: dict[str, ModuleSpec] = {}

    @staticmethod
    def _key(slot: str, name: str, version: int) -> str:
        return f"{slot}:{name}@{version}"

    def register(self, spec: ModuleSpec, *, replace: bool = False) -> ModuleSpec:
        if spec.slot not in SEVEN_SLOTS and spec.name not in META_MODULES:
            raise ModuleRegistrationError(f"unknown slot: {spec.slot}")
        key = self._key(spec.slot, spec.name, spec.version)
        if key in self._modules and not replace:
            raise ModuleRegistrationError(f"module already registered: {key}")
        self._modules[key] = spec
        return spec

    def get(self, ref: str, slot: str | None = None) -> ModuleSpec | None:
        try:
            name, version = parse_module_ref(ref)
        except ValueError:
            return None
        if slot is not None:
            hit = self._modules.get(self._key(slot, name, version))
            if hit is not None:
                return hit
        # 无槽上下文：找唯一匹配；多槽同名（元模块）时优先非元模块实现，
        # 否则返回首个（调用方应传 slot 消歧）。
        matches = [
            s for s in self._modules.values()
            if s.name == name and s.version == version
        ]
        if not matches:
            return None
        non_meta = [s for s in matches if s.name not in META_MODULES]
        return (non_meta or sorted(matches, key=lambda s: s.slot))[0]

    def require(self, ref: str, slot: str | None = None) -> ModuleSpec:
        spec = self.get(ref, slot=slot)
        if spec is None:
            raise ModuleRegistrationError(f"module not registered: {ref}")
        return spec

    def unregister(self, ref: str, *, slot: str | None = None) -> bool:
        """按 ref 摘除注册项（模块下架用；返回是否真的摘掉）。

        R1-P3-13：`retire_module` 此前只改库行，进程内注册表纹丝不动——被下架
        的模块在**重启前**仍可被新实验引用，与"下架只禁止新引用"的宣称不符。
        """
        name, version = parse_module_ref(ref)
        slots = [slot] if slot else list(SEVEN_SLOTS) + list(META_MODULES)
        removed = False
        for s in slots:
            key = self._key(s, name, version)
            if key in self._modules:
                del self._modules[key]
                removed = True
        return removed

    def has(self, ref: str, slot: str | None = None) -> bool:
        return self.get(ref, slot=slot) is not None

    def list(self, slot: str | None = None, kind: str | None = None) -> list[ModuleSpec]:
        specs = sorted(self._modules.values(), key=lambda s: (s.slot, s.name, s.version))
        if slot:
            specs = [s for s in specs if s.slot == slot]
        if kind:
            specs = [s for s in specs if s.kind == kind]
        return specs

    def clear_non_builtin(self) -> None:
        """测试辅助：卸载 dsl/python 模块，保留内置。"""
        self._modules = {k: s for k, s in self._modules.items() if s.kind == "builtin"}


def validate_params(schema: dict[str, dict], params: dict | None) -> tuple[dict, list[str]]:
    """按模块声明的参数域校验并归一化参数。

    返回 (normalized_params, errors)。normalized = 默认值填充 + 类型转换后
    的参数；errors 非空即不合法（实验入口/配置载入据此拒绝）。
    """
    params = dict(params or {})
    schema = schema or {}
    errors: list[str] = []
    normalized: dict[str, Any] = {}

    for key, value in params.items():
        if key not in schema:
            errors.append(f"unknown param: {key}")

    for key, rule in schema.items():
        rule = rule or {}
        if key in params:
            value = params[key]
        elif "default" in rule:
            value = rule["default"]
        elif rule.get("required"):
            errors.append(f"missing required param: {key}")
            continue
        else:
            continue

        ptype = rule.get("type", "number")
        try:
            if ptype in ("number", "integer"):
                # R3B-P3-3：布尔是 int 子类——True 会被静默当 1.0（如
                # risk_budget_pct: true → 100% 风险预算），必须显式拒绝
                if isinstance(value, bool):
                    raise TypeError("boolean not allowed for numeric param")
                if ptype == "number":
                    value = float(value)
                else:
                    f = float(value)
                    if not f.is_integer():
                        # R3B-P3-3：integer 参数不接受非整值 float（12.7 静默
                        # 截断成 12 是语义改变）
                        raise ValueError(f"non-integral value for integer param: {value!r}")
                    value = int(f)
            elif ptype == "boolean":
                if isinstance(value, str):
                    # R3B-P2-2：字符串只接受两个真值集合——"bogus"/"" 此前
                    # 静默归 False，笔误会无声改变策略语义（如 use_exit）
                    v = value.strip().lower()
                    if v in ("1", "true", "yes", "on"):
                        value = True
                    elif v in ("0", "false", "no", "off"):
                        value = False
                    else:
                        raise ValueError(f"unrecognized boolean string: {value!r}")
                elif isinstance(value, bool):
                    pass
                else:
                    raise ValueError(f"boolean param must be bool or string, got {type(value).__name__}")
            elif ptype == "string":
                value = str(value)
            elif ptype in ("list", "dict"):
                if ptype == "list" and not isinstance(value, (list, tuple)):
                    raise TypeError("not a list")
                if ptype == "dict" and not isinstance(value, dict):
                    raise TypeError("not a dict")
            else:
                errors.append(f"param {key}: unknown schema type {ptype}")
                continue
        except (TypeError, ValueError):
            errors.append(f"param {key}: cannot coerce {value!r} to {ptype}")
            continue

        if ptype in ("number", "integer"):
            if "min" in rule and value < rule["min"]:
                errors.append(f"param {key}: {value} < min {rule['min']}")
            if "max" in rule and value > rule["max"]:
                errors.append(f"param {key}: {value} > max {rule['max']}")
        if "choices" in rule and value not in rule["choices"]:
            errors.append(f"param {key}: {value!r} not in {rule['choices']}")
        normalized[key] = value

    return normalized, errors


# 进程级唯一注册表。内置模块在 portfolio.slots.* 导入时填入；
# 测试间需要干净注册表时用 fresh_registry 隔离。
REGISTRY = ModuleRegistry()


def fresh_registry() -> ModuleRegistry:
    """一个全新的空注册表（测试用）。"""
    return ModuleRegistry()
