"""死 CSS 类检查（P2-7 配套，CI 用）：对比 style.css 类定义与模板引用。

规则：style.css 中定义的每个 ``.class-name``，若在任何 web/templates/*.html
或 web/static/*.js 中（含 JS 字符串/模板字面量）零出现，则判定为死类，
非零退出并打印清单。已知例外（选择器拼配、JS 动态拼接词根）列入
``ALLOW_DYNAMIC_PREFIXES``。

用法：python scripts/check_dead_css.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STYLE = ROOT / "web" / "static" / "style.css"
SOURCES = sorted((ROOT / "web" / "templates").glob("*.html")) + sorted(
    (ROOT / "web" / "static").rglob("*.js")
)

# JS 动态拼接/组合生成的类名（词根匹配），人工核对后豁免
ALLOW_DYNAMIC_PREFIXES = (
    "is-",        # 状态类（is-active/is-error/...）普遍经 classList.toggle 动态切换
    "mt-stop-",   # 止损卡族在 app-common.js 中拼接
    "subject-",   # 看板行动态渲染词根
    "index-",     # 看板卡片词根
    "batch-status--",       # batch_backtest.html 按 run.status 拼接
    "batch-status-cell--",  # batch_backtest.html 按 cell.status 拼接
)

_CLASS_DEF_RE = re.compile(r"\.(-?[a-zA-Z_][a-zA-Z0-9_-]*)")


def defined_classes() -> set[str]:
    names: set[str] = set()
    for line in STYLE.read_text(encoding="utf-8").splitlines():
        # 只统计选择器行的类名（粗糙但够用：属性值里不含 .class 形态）
        if "{" in line or line.strip().startswith("."):
            names.update(_CLASS_DEF_RE.findall(line.split("{")[0]))
    return names


def referenced_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in SOURCES)


def main() -> int:
    defined = defined_classes()
    haystack = referenced_text()
    dead = sorted(
        name
        for name in defined
        if name not in haystack and not name.startswith(ALLOW_DYNAMIC_PREFIXES)
    )
    if dead:
        print(f"发现 {len(dead)} 个疑似死 CSS 类（templates/static js 零引用）：")
        for name in dead:
            print(f"  .{name}")
        return 1
    print(f"OK：{len(defined)} 个 CSS 类均有模板/JS 引用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
