"""导出行业分类/ETF重仓股种子数据为 SQL 文件（线上导入用）。

本地已跑完 tickflow/tushare 同步后，把成果数据导出，scp 到服务器导入即可，
服务器无需安装 tushare、无需消耗账号窗口。

用法（项目根目录）：
    .venv/Scripts/python scripts/export_industry_seed.py
    # 生成 scripts/temp/sw2021_seed.sql
"""

from __future__ import annotations

import sys
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parent))
import sqlite3
import sys
from pathlib import Path

import _common  # .env 加载 + DB_PATH + TickFlow 构造（P2-13）

DB_PATH = _common.DB_PATH
OUT_PATH = Path("scripts/temp/sw2021_seed.sql")

TABLES = ("stock_industry", "etf_constituents")

def main() -> int:
    if not DB_PATH.exists():
        print(f"[export] 找不到数据库 {DB_PATH}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = [
        "-- 行业分类/ETF重仓股种子数据（scripts/export_industry_seed.py 生成）",
        "-- 目标库需已存在同名表（先启动一次新代码的服务自动建表）",
        "BEGIN;",
    ]
    total = 0
    for table in TABLES:
        cursor = conn.execute(f"SELECT * FROM {table}")
        columns = [d[0] for d in cursor.description]
        count = 0
        for row in cursor:
            values = ", ".join(
                "NULL" if row[c] is None else "'" + str(row[c]).replace("'", "''") + "'"
                for c in columns
            )
            lines.append(f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ({values});")
            count += 1
        print(f"[export] {table}: {count} 行")
        total += count

    tree = conn.execute("SELECT value FROM app_config WHERE key = 'sw2021_tree'").fetchone()
    if tree:
        value = str(tree["value"]).replace("'", "''")
        lines.append(
            "INSERT OR REPLACE INTO app_config (key, value, updated_at) "
            f"VALUES ('sw2021_tree', '{value}', datetime('now','localtime'));"
        )
        print("[export] app_config: sw2021_tree")
    else:
        print("[export] 警告：app_config 中没有 sw2021_tree（先跑 scripts/sync_stock_industry.py）")

    lines.append("COMMIT;")
    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"[export] 已生成 {OUT_PATH}（{total} 行 + 树配置）")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
