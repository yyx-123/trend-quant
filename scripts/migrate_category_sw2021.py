"""一次性数据迁移：股票类目树重建为申万 2021 体系（2026-08）。

方案文档：docs/stock-industry-etf-holdings/2026-08-24-stock-industry-etf-holdings-plan.md §7

    1. 旧「股票」二三级类目归档到 stock_category_archive（可回溯）；
    2. instrument_categories 的「股票」子树重建：二级=申万一级 31 个、
       三级=申万二级 134 个、外加「待分类」兜底（树数据来自 app_config
       的 sw2021_tree，由 scripts/sync_stock_industry.py 写入）；
    3. 每只在管股票按 stock_industry 重归类，未命中进「待分类」；
       priority_l1/l2/l3 重算，updated_at 显式刷新（看板 revision 依赖，
       评审 B1 —— 上一个迁移脚本 migrate_category_simplify.py 漏了这一点）；
    4. ETF 子树与 ETF 标的不动；批量回测历史结果快照刻意不迁移（先例）。

执行前提（评审 B6）：本地库即生产库 —— **停服执行**，跑完重启服务并人工
冒烟看板分组。

用法（项目根目录）：
    .venv/Scripts/python scripts/migrate_category_sw2021.py            # 执行迁移
    .venv/Scripts/python scripts/migrate_category_sw2021.py --dry-run  # 只打印对照报告

回滚：恢复 data/backups/ 下脚本自动生成的备份文件即可。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import _common  # .env 加载 + DB_PATH + TickFlow 构造（P2-13）

from data.storage.db import init_db, record_job_run_safely
from services.stock_industry import STOCK_L1, UNCLASSIFIED_L2, UNCLASSIFIED_L3

DB_PATH = _common.DB_PATH
MIGRATION_TAG = "sw2021_2026_q3"
_PENDING_PATHS = {f"{STOCK_L1}-{UNCLASSIFIED_L2}", f"{STOCK_L1}-{UNCLASSIFIED_L2}-{UNCLASSIFIED_L3}"}


def build_category_rows(tree: list[dict], stock_l1_priority: int) -> list[tuple]:
    """申万树 → instrument_categories 行 (path, level, name, parent, priority)。"""
    rows: list[tuple] = [(STOCK_L1, 1, STOCK_L1, None, stock_l1_priority)]
    for i, l1 in enumerate(tree, start=1):
        l2_path = f"{STOCK_L1}-{l1['name']}"
        rows.append((l2_path, 2, l1["name"], STOCK_L1, i))
        for j, l2 in enumerate(l1["l2"], start=1):
            rows.append((f"{l2_path}-{l2['name']}", 3, l2["name"], l2_path, j))
    rows.append((f"{STOCK_L1}-{UNCLASSIFIED_L2}", 2, UNCLASSIFIED_L2, STOCK_L1, 9999))
    rows.append(
        (
            f"{STOCK_L1}-{UNCLASSIFIED_L2}-{UNCLASSIFIED_L3}",
            3,
            UNCLASSIFIED_L3,
            f"{STOCK_L1}-{UNCLASSIFIED_L2}",
            9999,
        )
    )
    return rows


def validate_tree(tree: list[dict]) -> list[str]:
    """建树前校验（评审 B2）。返回错误列表，空为通过。"""
    errors: list[str] = []
    for l1 in tree:
        if "-" in l1["name"]:
            errors.append(f"L1 名含路径分隔符 '-': {l1['name']}")
        seen: set[str] = set()
        for l2 in l1["l2"]:
            if "-" in l2["name"]:
                errors.append(f"L2 名含路径分隔符 '-': {l1['name']}/{l2['name']}")
            if l2["name"] in seen:
                errors.append(f"同 L1 下 L2 撞名: {l1['name']}/{l2['name']}")
            seen.add(l2["name"])
    return errors


def _service_running_guard(db_path: Path) -> None:
    """迁移会大规模改写类目/元数据，要求停服执行（P2-24：不再靠自觉）。

    仅在目标是默认生产库时检测（测试/临时库豁免）：本机 8000 端口有
    服务在跑即拒绝执行（启发式——trend-quant web 的监听端口）。
    """
    if db_path.resolve() != Path(DB_PATH).resolve():
        return
    import socket

    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=1):
            print(
                "[migrate] 检测到本机 8000 端口有服务在跑（疑似 trend-quant web）。\n"
                "[migrate] 迁移要求停服执行：请先 systemctl stop trend-quant（或停掉本地 uvicorn）再跑。",
                file=sys.stderr,
            )
            raise SystemExit(2)
    except (TimeoutError, ConnectionRefusedError, OSError):
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="股票类目树重建为申万 2021（存量重归类）")
    parser.add_argument("--dry-run", action="store_true", help="只打印对照报告，不写库")
    parser.add_argument("--db", default=str(DB_PATH), help="数据库路径")
    args = parser.parse_args()

    if not args.dry_run:
        _service_running_guard(Path(args.db))

    if not Path(args.db).exists():
        print(f"[migrate] 找不到数据库 {args.db}", file=sys.stderr)
        return 1

    db = init_db(args.db)
    config = db.get_config("sw2021_tree")
    if not config or not config.get("tree"):
        print(
            "[migrate] 缺少申万树数据：请先运行 scripts/sync_stock_industry.py",
            file=sys.stderr,
        )
        return 1
    tree = config["tree"]

    errors = validate_tree(tree)
    if errors:
        for e in errors:
            print(f"[migrate] 树校验失败: {e}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    stock_l1_priority = conn.execute(
        "SELECT priority FROM instrument_categories WHERE path = ?", (STOCK_L1,)
    ).fetchone()
    stock_l1_priority = (stock_l1_priority["priority"] if stock_l1_priority else None) or 2

    stocks = conn.execute(
        "SELECT symbol, name, category_l2, category_l3, enabled FROM instrument_metadata WHERE category_l1 = ?",
        (STOCK_L1,),
    ).fetchall()
    industry = {
        row["symbol"]: row for row in conn.execute("SELECT * FROM stock_industry")
    }

    category_rows = build_category_rows(tree, stock_l1_priority)
    tree_paths = {row[0] for row in category_rows}

    # 逐股票计算新归类
    assignments: list[dict] = []
    tree_miss = 0
    for s in stocks:
        row = industry.get(s["symbol"])
        if row is not None:
            l2, l3 = row["sw_l1_name"], row["sw_l2_name"]
            if f"{STOCK_L1}-{l2}-{l3}" not in tree_paths:
                # 行业表有记录但树里缺该分支（树与行业表来源不一致）—— 进待分类，绝不留孤儿
                tree_miss += 1
                l2, l3 = UNCLASSIFIED_L2, UNCLASSIFIED_L3
        else:
            l2, l3 = UNCLASSIFIED_L2, UNCLASSIFIED_L3
        assignments.append(
            {"symbol": s["symbol"], "name": s["name"], "old_l2": s["category_l2"],
             "old_l3": s["category_l3"], "new_l2": l2, "new_l3": l3}
        )

    # 对照报告
    hit = sum(1 for a in assignments if a["new_l2"] != UNCLASSIFIED_L2)
    print(f"[migrate] 在管股票 {len(assignments)} 只：命中行业 {hit} 只，待分类 {len(assignments) - hit} 只"
          f"（其中树缺分支 {tree_miss} 只）")
    print(f"[migrate] 新树节点数: {len(category_rows)}（二级 {len(tree) + 1}，三级 {sum(len(l['l2']) for l in tree) + 1}）")
    print("[migrate] 旧类目 → 新类目 对照:")
    crosstab = Counter((a["old_l2"], a["new_l2"]) for a in assignments)
    for (old, new), n in sorted(crosstab.items(), key=lambda x: (x[0][0], -x[1])):
        print(f"  {old} -> {new}: {n}")
    unclassified = [a for a in assignments if a["new_l2"] == UNCLASSIFIED_L2]
    if unclassified:
        print("[migrate] 待分类清单:")
        for a in unclassified:
            print(f"  {a['symbol']} {a['name']}（原 {a['old_l2']}-{a['old_l3']}）")

    if args.dry_run:
        print("[migrate] dry-run，未写库。")
        conn.close()
        return 0

    backup = db.backup_to()
    print(f"[migrate] 已备份: {backup}")

    priority = {row[0]: row[4] for row in category_rows}
    with conn:  # 单事务执行全部改写
        # 1. 归档旧类目
        conn.executemany(
            """INSERT INTO stock_category_archive (symbol, category_l2, category_l3, migration, archived_at)
               VALUES (?, ?, ?, ?, datetime('now','localtime'))
               ON CONFLICT(symbol) DO NOTHING""",
            [(a["symbol"], a["old_l2"], a["old_l3"], MIGRATION_TAG) for a in assignments],
        )
        # 2. 重建「股票」子树（ETF 子树不动）
        conn.execute("DELETE FROM instrument_categories WHERE path = ? OR path LIKE ?",
                     (STOCK_L1, f"{STOCK_L1}-%"))
        conn.executemany(
            """INSERT INTO instrument_categories (path, level, name, parent_path, priority, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now','localtime'))""",
            category_rows,
        )
        # 3. 逐股票重归类 + 重算 priority + 显式刷新 updated_at（评审 B1）
        for a in assignments:
            conn.execute(
                """UPDATE instrument_metadata
                   SET category_l2 = ?, category_l3 = ?,
                       priority_l1 = ?, priority_l2 = ?, priority_l3 = ?,
                       updated_at = datetime('now','localtime')
                   WHERE symbol = ?""",
                (
                    a["new_l2"], a["new_l3"],
                    priority.get(STOCK_L1),
                    priority.get(f"{STOCK_L1}-{a['new_l2']}"),
                    priority.get(f"{STOCK_L1}-{a['new_l2']}-{a['new_l3']}"),
                    a["symbol"],
                ),
            )

    # 4. 校验
    print("[migrate] 校验:")
    empty_l3 = conn.execute(
        f"""SELECT COUNT(*) AS n FROM instrument_metadata
            WHERE category_l1 = '{STOCK_L1}' AND TRIM(COALESCE(category_l3, '')) = ''"""
    ).fetchone()["n"]
    orphans = conn.execute(
        f"""SELECT COUNT(DISTINCT m.symbol) AS n FROM instrument_metadata m
            LEFT JOIN instrument_categories c
              ON c.path = m.category_l1 || '-' || m.category_l2 || '-' || m.category_l3
            WHERE m.category_l1 = '{STOCK_L1}' AND c.path IS NULL"""
    ).fetchone()["n"]
    print(f"  l3 为空标的数: {empty_l3}（应为 0）")
    print(f"  分类树外的孤儿标的数: {orphans}（应为 0）")
    print("  新类目分布:")
    for r in conn.execute(
        f"""SELECT category_l2, COUNT(*) AS n FROM instrument_metadata
            WHERE category_l1 = '{STOCK_L1}' GROUP BY 1 ORDER BY n DESC"""
    ):
        print(f"    {r['category_l2']}: {r['n']}")
    conn.close()

    ok = empty_l3 == 0 and orphans == 0
    record_job_run_safely(
        "migrate_category_sw2021",
        {
            "migration": MIGRATION_TAG,
            "stocks": len(assignments),
            "classified": hit,
            "unclassified": [a["symbol"] for a in unclassified],
            "tree_miss": tree_miss,
            "tree_nodes": len(category_rows),
            "checks": {"empty_l3": empty_l3, "orphans": orphans},
        },
        status="success" if ok else "failed",
    )
    print("[migrate] 完成。请重启服务并冒烟看板分组。" if ok else "[migrate] 校验未通过，请检查！")
    print("[migrate] 提示：批量回测历史结果未迁移，需要时请重跑回测。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
