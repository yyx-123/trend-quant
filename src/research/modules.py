"""模块治理（详设 §6.2.2，决策 16）：propose_module → draft 落库 →
自动测试门 → reviewed 可执行；人保留抽检与下架权。

- DSL 表达式类模块：算子白名单、构造上无前视 → 免测试直接用；
- python 代码模块：唯一需要写代码的环节，隔离在实验循环之外——
  平台自动跑契约/确定性/前缀稳定性测试，通过即 reviewed，不通过附原因打回；
- 下架（retire）只禁止新引用，不影响已完成实验的记录（run 里存有
  resolved 配置与模块版本，可复现）。
"""

from __future__ import annotations

import json

from research.errors import ResearchError
from research.ledger import alloc_id, loads, row_to_dict, rows_to_dicts
from research.sessions import require_human_session, require_session


def propose_module(
    db,
    *,
    session_id: str,
    slot: str,
    name: str,
    version: int,
    kind: str,
    source: str,
    params_schema: dict | None = None,
    auto_review: bool = True,
) -> dict:
    """提新模块。返回 draft 行（status: draft/reviewed/rejected）。"""
    from portfolio.registry import META_MODULES, SEVEN_SLOTS
    from research import module_gate
    from research.dsl import DslError, compile_expression

    session = require_session(db, session_id)
    slot = str(slot or "").strip()
    name = str(name or "").strip()
    version = int(version)
    kind = str(kind or "").strip()
    if slot not in SEVEN_SLOTS:
        raise ResearchError(f"unknown slot: {slot}")
    if name in META_MODULES:
        raise ResearchError("meta module names are reserved")
    if kind not in ("dsl", "python"):
        raise ResearchError(f"unknown module kind: {kind}")
    if not name or version < 1:
        raise ResearchError("name/version invalid")

    with db.connect() as conn:
        clash = conn.execute(
            "SELECT id FROM module_drafts WHERE name = ? AND version = ?",
            (name, version),
        ).fetchone()
    if clash:
        raise ResearchError(f"module already exists: {name}@{version}")

    status = "draft"
    report: dict = {}
    reject_reason = None

    if kind == "dsl":
        try:
            compile_expression(source)  # 白名单构造检查
            status = "reviewed"  # DSL 免测（安全由 DSL 引擎构造保证）
            report = {"gate": "dsl_exempt", "ok": True}
        except DslError as exc:
            status = "rejected"
            reject_reason = f"dsl parse: {exc}"
            report = {"gate": "dsl_exempt", "ok": False, "error": str(exc)}
    else:
        if auto_review:
            # python 代码门：装载 + 三门自动测试
            try:
                cls = _load_python_module(source)
                report = module_gate.run_module_gate(cls, slot=slot, params={})
                status = "reviewed" if report["passed"] else "rejected"
                if not report["passed"]:
                    reject_reason = json.dumps(report["checks"], ensure_ascii=False)[:500]
            except Exception as exc:
                status = "rejected"
                # R1-P3-19：对外只给业务原因（异常细节只进日志）——装载/门的
                # 内部异常可能含本机路径与栈内细节，与 _error_payload 的口径
                # 保持一致（detail 由 reject_reason 暴露给 MCP 客户端）。
                import logging

                # 异常细节（可能含本机路径/栈内信息）只进日志；对外文案见 report
                logging.getLogger(__name__).exception("module draft review failed")
                report = {
                    "gate": "python", "ok": False,
                    "error": f"{type(exc).__name__}: 模块装载/自动测试失败"
                             "（细节见服务端日志）",
                }
                reject_reason = "load: module failed to load or pass the gate"


    with db.connect() as conn:
        draft_id = alloc_id(conn, "module_drafts", "M", width=4)
        conn.execute(
            """INSERT INTO module_drafts
               (id, slot, name, version, kind, source, params_schema_json, status,
                test_report_json, reject_reason, created_by, owner_session, reviewed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       CASE WHEN ? IN ('reviewed','rejected')
                            THEN datetime('now','localtime') END)""",
            (
                draft_id, slot, name, version, kind, source,
                json.dumps(params_schema or {}, ensure_ascii=False, sort_keys=True),
                status, json.dumps(report, ensure_ascii=False, sort_keys=True),
                reject_reason, session["kind"], session["session_id"], status,
            ),
        )
    return get_draft(db, draft_id)


# 白名单：科学计算三件套 + 本栈的协议数据类型（SignalEvent/OrderIntent/
# UniverseMember/DayContext 等——模块实现协议的合法载体）。
_ALLOWED_IMPORTS = {
    "numpy", "pandas", "math",
    "portfolio.slots.signal", "portfolio.slots.universe", "portfolio.context",
    "engine.models",
}
# 受限 builtins：模块逻辑的足够集（无 open/eval/exec/input/compile/getattr 等）
_RESTRICTED_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "enumerate": enumerate, "float": float, "int": int, "len": len,
    "list": list, "max": max, "min": min, "range": range, "round": round,
    "set": set, "sorted": sorted, "str": str, "sum": sum, "tuple": tuple,
    "zip": zip, "isinstance": isinstance, "print": print,
    "True": True, "False": False, "None": None,
    # import 语句需要 __import__；动态调用 __import__("os") 由 AST 预筛的
    # Name 白名单拦截（二者配合：语句走白名单库，调用语法被禁）。
    "__import__": __import__,
    "__build_class__": __build_class__,  # class 定义的必需内建
}


# 危险模块属性名（R1-D-1 低成本缓解）：白名单库上仍可经属性链取到"模块对象"
# 的逃生口（`pd.io.common.os`、`np.ctypeslib.ctypes` 等）。命名级黑名单掐断
# 常见链路；**不是**真沙箱——真隔离需子进程/容器，见 round1-review R1-D-1。
_DENIED_ATTR_NAMES = frozenset({
    "os", "sys", "subprocess", "ctypes", "ctypeslib", "pickle", "shutil",
    "importlib", "builtins", "socket", "urllib", "requests", "multiprocessing",
    "threading", "tempfile", "pathlib", "glob", "signal", "pty", "platform",
    "resource", "gc", "inspect", "marshal", "code", "codeop", "cmd",
    "system", "popen", "spawn", "spawnl", "spawnv", "execv", "execve",
    "fork", "kill", "remove", "unlink", "rmtree", "chmod", "chown",
    "write", "writelines", "to_csv", "to_pickle", "to_json", "to_sql",
    "read_csv", "read_pickle", "read_sql", "load", "loads", "dump", "dumps",
})


def _prescreen_python_source(source: str) -> list[str]:
    """exec 前 AST 静态筛查（评审 B-P1-4）：白名单 import + 禁属性逃逸。

    信任模型（写死在文档，R1-D-1 如实化）：本系统是单人本地研究基建，
    python 模块门 + 预筛的定位是"防误伤的前视/越界探针 + 常见逃逸拦截"，
    **不是对抗恶意代码的真沙箱**——门与装载仍在**进程内 exec**，命名级
    属性黑名单只能抬高门槛、不能给出"无任意代码执行路径"的保证。因此
    MCP 通道应按**完全信任通道**理解；若要兑现详设 §6.7 的"AI 无任意代码
    执行路径"，须把门/装载迁到子进程或容器（见 loop-review-ds4f
    round1-review 的 R1-D-1 决策点）。
    """
    import ast

    errors: list[str] = []
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        return [f"syntax error: {exc}"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in _ALLOWED_IMPORTS and alias.name.split(".")[0] not in _ALLOWED_IMPORTS:
                    errors.append(f"import not allowed: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "") not in _ALLOWED_IMPORTS:
                errors.append(f"import not allowed: {node.module}")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                errors.append(f"dunder attribute access not allowed: {node.attr}")
            elif node.attr in _DENIED_ATTR_NAMES:
                # R1-D-1 缓解：白名单库的**模块对象属性链**此前不受限——
                # `pd.io.common.os.system(...)` / `np.ctypeslib.ctypes...` 都能
                # 过预筛拿到任意命令执行（代理已实证 marker 文件写出）。这里
                # 掐断常见链路；真隔离需子进程/容器（决策点 R1-D-1）。
                errors.append(
                    f"attribute name not allowed: {node.attr} "
                    "(dangerous module attribute chain)"
                )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # R2-P3-5：真逃逸向量是 "{0.__class__}".format(x)——dunder 藏在
            # 字符串常量里（f-string 内表达式反而会被上面的 Attribute 扫描
            # 抓住）。对字符串常量做 ".__" 记号预筛：正常研究表达式不需要
            # 在字符串字面量里引用属性逃逸形。
            if ".__" in node.value:
                errors.append(
                    "string literal may not contain attribute-escape token '.__' "
                    "(format-string dunder access is not visible to AST scan)"
                )
        elif isinstance(node, ast.Name) and node.id in (
            "eval", "exec", "open", "compile", "getattr", "setattr",
            "globals", "locals", "vars", "input", "breakpoint",
            "__import__", "__builtins__", "__globals__",
        ):
            # __builtins__/__globals__（R2VB B-6）：下标取 __import__ 的逃逸
            # 路径（__builtins__["__import__"]("os")）从这里掐断
            errors.append(f"name not allowed: {node.id}")
    return errors


def _load_python_module(source: str):
    """装载 python 模块源码：AST 预筛 + 受限 builtins + 白名单库。

    模块必须定义 ``Module`` 类（工厂签名：Module(params: dict)）。
    """
    errors = _prescreen_python_source(source)
    if errors:
        raise ResearchError("prescreen failed: " + "; ".join(errors[:5]))

    import math

    import numpy as np
    import pandas as pd

    namespace = {
        "np": np, "numpy": np, "pd": pd, "pandas": pd, "math": math,
        "__name__": "module_draft",
        "__builtins__": dict(_RESTRICTED_BUILTINS),
    }
    exec(compile(source, "<module_draft>", "exec"), namespace)  # noqa: S102 - 测试门隔离环节
    cls = namespace.get("Module")
    if cls is None:
        raise ResearchError("python module must define a Module class")
    return lambda params={}: cls(params)


def get_draft(db, draft_id: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM module_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
    out = row_to_dict(row)
    if out:
        out["params_schema"] = loads(out.get("params_schema_json"), {})
        out["test_report"] = loads(out.get("test_report_json"), {})
    return out


def list_drafts(db, status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM module_drafts"
    params: tuple = ()
    if status:
        sql += " WHERE status = ?"
        params = (status,)
    sql += " ORDER BY id"
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = rows_to_dicts(rows)
    for row in out:
        row["params_schema"] = loads(row.get("params_schema_json"), {})
        row["test_report"] = loads(row.get("test_report_json"), {})
    return out


def retire_module(db, *, draft_id: str, session_id: str) -> dict:
    """下架（仅 human；只禁止新引用，不影响已完成实验的记录）。

    R1-P3-13：除库行改态外，**同步从进程内注册表摘除**——否则被下架的模块
    在重启前仍能被新实验引用（引用合法性判定读的是注册表、不是库行），
    "下架只禁止新引用"名不副实。
    """
    require_human_session(db, session_id)
    draft = get_draft(db, draft_id)
    if draft is None:
        raise ResearchError(f"draft not found: {draft_id}")
    with db.connect() as conn:
        conn.execute(
            """UPDATE module_drafts SET status = 'retired',
               reviewed_at = datetime('now','localtime') WHERE id = ?""",
            (draft_id,),
        )
    try:
        from portfolio.registry import REGISTRY

        REGISTRY.unregister(f"{draft['name']}@{draft['version']}", slot=draft["slot"])
    except Exception:  # 注册表不可用不影响下架本身（库行已改态，重启后即生效）
        import logging

        logging.getLogger(__name__).exception(
            "retire_module: failed to unregister %s@%s from in-process registry",
            draft["name"], draft["version"],
        )
    return get_draft(db, draft_id)


def load_reviewed_modules(db, registry) -> int:
    """把 reviewed 态草稿装载进注册表（AI 提的模块经此进入可引用域）。

    dsl → DSL 引擎构造模块类；python → 装载源码取 Module 类。
    返回装载数（幂等：已注册的跳过）。
    """
    from portfolio.registry import ModuleSpec
    from research.dsl import make_dsl_signal_class

    loaded = 0
    for draft in list_drafts(db, status="reviewed"):
        ref = f"{draft['name']}@{draft['version']}"
        if registry.has(ref, slot=draft["slot"]):
            continue
        try:
            if draft["kind"] == "dsl":
                if draft["slot"] != "signal":
                    continue  # DSL 一期只支持 signal 槽
                factory_cls = make_dsl_signal_class(draft["source"])
                factory = lambda params, _c=factory_cls: _c(params)
            else:
                factory = _load_python_module(draft["source"])
        except Exception:
            # GLM53F-P2-12：单条草稿装载失败（依赖漂移等）不得让全站起不来
            # （含看板/手工交易/MCP）——记录并跳过；草稿留在 reviewed 态
            # 供人工 retire/修复
            import logging

            logging.getLogger(__name__).exception(
                "reviewed module %s (draft %s) failed to load; skipped", ref, draft["id"]
            )
            continue
        try:
            # replace=True（R3B-P3-11）：与内置/已装载模块撞 name@version 时
            # 幂等覆盖（同草稿重装载），而不是 ModuleRegistrationError 打穿
            # 装载循环；replace=True 保留——reviewed 草稿的注册即最新评审态
            registry.register(ModuleSpec(
                slot=draft["slot"], name=draft["name"], version=draft["version"],
                factory=factory, params_schema=draft["params_schema"],
                kind=draft["kind"], draft_id=draft["id"],
                description=f"AI-proposed {draft['kind']} module",
            ), replace=True)
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "reviewed module %s (draft %s) failed to register; skipped",
                ref, draft["id"],
            )
            continue
        loaded += 1
    return loaded
