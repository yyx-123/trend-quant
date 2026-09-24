"""在服务端库上重跑差分（dev 库标的少于生产库，所以这份差分必须在服务端再做一次）。

用法（把整个 research 目录拷到服务端后，在服务端项目根目录执行）：
    .venv/bin/python <此脚本路径>                 # 两份清单都差分
    .venv/bin/python <此脚本路径> data/universe_top1000.csv   # 只差分指定清单

只读，不写数据库；结果输出到清单同目录的 `*.missing.txt`。
"""

from __future__ import annotations

import csv
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_LISTS = [HERE / "data" / "universe_top1000.csv", HERE / "data" / "universe_1000_1800.csv"]


def find_project_root() -> Path:
    for parent in [HERE, *HERE.parents]:
        if (parent / "pyproject.toml").exists() and (parent / "src" / "core" / "paths.py").exists():
            return parent
    raise SystemExit("找不到项目根目录（向上找不到 pyproject.toml + src/core/paths.py）")


def normalize(symbol: str) -> str:
    s = str(symbol or "").strip().upper()
    m = re.match(r"^(\d{6})\.(SS|SH|SZ|BJ)$", s)
    if not m:
        return s
    code, ex = m.groups()
    return f"{code}.SH" if ex in {"SS", "SH"} else f"{code}.{ex}"


def main() -> None:
    root = find_project_root()
    sys.path.insert(0, str(root / "src"))
    from core.paths import default_db_path

    listed = [Path(a) for a in sys.argv[1:]] or DEFAULT_LISTS
    con = sqlite3.connect(str(default_db_path()))
    local = {normalize(x) for (x,) in con.execute("select symbol from instrument_metadata")}
    con.close()
    print(f"[diff] 库: {default_db_path()}  本地标的 {len(local)} 只")

    for path in listed:
        rows = list(csv.DictReader(path.open(encoding="utf-8")))
        missing = sorted(normalize(r["symbol"]) for r in rows if normalize(r["symbol"]) not in local)
        out = path.with_suffix(".missing.txt")
        out.write_text("\n".join(missing) + "\n", encoding="utf-8")
        print(f"[diff] {path.name}: 清单 {len(rows)} 只，本地已有 {len(rows) - len(missing)} 只，需补 {len(missing)} 只 -> {out.name}")


if __name__ == "__main__":
    main()
