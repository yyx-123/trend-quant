"""一次性数据迁移：标的分类精简（2026-08）。

目标结构：一级类目只保留 ETF / 股票两类。

    1. 「行业」改名为「ETF」（l2/l3 不变）；
    2. 「宽基」「跨境」「策略」「商品」四个一级类目降级：
       一级=ETF、二级=原一级、三级=原二级，原三级废弃；
    3. 「债券」整体废弃：删除其全部标的及行情/指标/因子数据；
    4. 重建 instrument_categories 分类树并重算标的的 priority_l1/l2/l3。

代码无改动：分类树与标的分类全部由 instrument_categories /
instrument_metadata 两张表驱动，前端、看板、MCP、批量回测均动态读取。
批量回测历史结果（batch_backtest_*）里是当时的分类快照，刻意保留不迁移，
需要时重跑回测即可。

用法（项目根目录，先确认无跑批任务在写库）：
    .venv/bin/python scripts/migrate_category_simplify.py            # 执行迁移
    .venv/bin/python scripts/migrate_category_simplify.py --dry-run  # 只打印计划，不写库

回滚：恢复 data/backups/ 下脚本自动生成的备份文件即可。
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path("data/trend_quant.db")
BACKUP_DIR = Path("data/backups")

DEMOTE_L1 = ["宽基", "跨境", "策略", "商品"]  # 降级为 ETF 二级类目，保持该顺序
RENAME_L1 = "行业"  # 改名为 ETF
DROP_L1 = "债券"
KEEP_L1 = "股票"

MARKET_TABLES = ["market_data_raw", "market_data_qfq", "indicator_daily", "trend_daily", "ex_factors"]


def build_new_tree(old_rows: list[sqlite3.Row]) -> list[tuple]:
    """由旧分类树推导新树，保留各层级原有 priority。

    返回 (path, level, name, parent_path, priority) 列表。
    """
    by_path = {r["path"]: r for r in old_rows}
    children: dict[str | None, list[sqlite3.Row]] = {}
    for r in old_rows:
        children.setdefault(r["parent_path"], []).append(r)

    new_rows: list[tuple] = []

    def add(path: str, level: int, name: str, parent: str | None, priority) -> None:
        new_rows.append((path, level, name, parent, priority))

    add("ETF", 1, "ETF", None, 1)
    add(KEEP_L1, 1, KEEP_L1, None, 2)

    # ETF 二级：原「行业」的二级类目（保持 1..6），随后追加四个降级类目
    for child in children.get(RENAME_L1, []):
        add(f"ETF-{child['name']}", 2, child["name"], "ETF", child["priority"])
    for offset, old_l1 in enumerate(DEMOTE_L1, start=7):
        if old_l1 in by_path:
            add(f"ETF-{old_l1}", 2, old_l1, "ETF", offset)

    # ETF 三级：原「行业」的三级原样挂到新 l2 下；降级类目的旧二级变为新三级
    for child in children.get(RENAME_L1, []):
        for grand in children.get(child["path"], []):
            add(f"ETF-{child['name']}-{grand['name']}", 3, grand["name"], f"ETF-{child['name']}", grand["priority"])
    for old_l1 in DEMOTE_L1:
        for child in children.get(old_l1, []):
            add(f"ETF-{old_l1}-{child['name']}", 3, child["name"], f"ETF-{old_l1}", child["priority"])

    # 「股票」子树原样保留
    for child in children.get(KEEP_L1, []):
        add(child["path"], 2, child["name"], KEEP_L1, child["priority"])
        for grand in children.get(child["path"], []):
            add(grand["path"], 3, grand["name"], grand["parent_path"], grand["priority"])

    return new_rows


def main() -> int:
    parser = argparse.ArgumentParser(description="标的分类精简迁移（ETF/股票两级）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写库")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"[migrate] 找不到数据库 {DB_PATH}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    old_tree = conn.execute("SELECT * FROM instrument_categories").fetchall()
    bond_symbols = [
        r["symbol"] for r in conn.execute(
            "SELECT symbol FROM instrument_metadata WHERE category_l1 = ?", (DROP_L1,)
        )
    ]
    demote_count = conn.execute(
        f"SELECT COUNT(*) AS n FROM instrument_metadata WHERE category_l1 IN ({','.join('?' * len(DEMOTE_L1))})",
        DEMOTE_L1,
    ).fetchone()["n"]
    rename_count = conn.execute(
        "SELECT COUNT(*) AS n FROM instrument_metadata WHERE category_l1 = ?", (RENAME_L1,)
    ).fetchone()["n"]

    print(f"[migrate] 删除「{DROP_L1}」标的 {len(bond_symbols)} 只: {bond_symbols}")
    print(f"[migrate] 降级标的 {demote_count} 只（{'/'.join(DEMOTE_L1)} → ETF 二级）")
    print(f"[migrate] 改名标的 {rename_count} 只（{RENAME_L1} → ETF）")

    new_tree = build_new_tree(old_tree)
    print(f"[migrate] 新分类树节点数: {len(new_tree)}（旧 {len(old_tree)}）")

    if args.dry_run:
        for row in new_tree:
            print(f"  L{row[1]} {row[0]} (priority={row[4]})")
        print("[migrate] dry-run，未写库。")
        conn.close()
        return 0

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f"trend_quant_pre_category_simplify_{datetime.now():%Y%m%d_%H%M%S}.db"
    conn.close()
    shutil.copy2(DB_PATH, backup)
    print(f"[migrate] 已备份: {backup}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    with conn:  # 单事务执行全部改写
        # 1. 删债券标的及其行情/指标/因子数据
        conn.execute("DELETE FROM instrument_metadata WHERE category_l1 = ?", (DROP_L1,))
        if bond_symbols:
            ph = ",".join("?" * len(bond_symbols))
            for table in MARKET_TABLES:
                cur = conn.execute(f"DELETE FROM {table} WHERE symbol IN ({ph})", bond_symbols)
                print(f"[migrate] {table} 删除 {cur.rowcount} 行")

        # 2. 降级：l1=ETF, l2=原l1, l3=原l2
        conn.execute(
            f"""UPDATE instrument_metadata
                SET category_l3 = category_l2, category_l2 = category_l1, category_l1 = 'ETF'
                WHERE category_l1 IN ({','.join('?' * len(DEMOTE_L1))})""",
            DEMOTE_L1,
        )

        # 3. 改名：行业 → ETF
        conn.execute("UPDATE instrument_metadata SET category_l1 = 'ETF' WHERE category_l1 = ?", (RENAME_L1,))

        # 4. 重建分类树
        conn.execute("DELETE FROM instrument_categories")
        conn.executemany(
            """INSERT INTO instrument_categories (path, level, name, parent_path, priority, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now','localtime'))""",
            new_tree,
        )

        # 5. 重算标的 priority_l1/l2/l3
        priority = {row[0]: row[4] for row in new_tree}
        rows = conn.execute(
            "SELECT symbol, category_l1, category_l2, category_l3 FROM instrument_metadata"
        ).fetchall()
        for r in rows:
            l1, l2, l3 = r["category_l1"], r["category_l2"], r["category_l3"]
            conn.execute(
                "UPDATE instrument_metadata SET priority_l1 = ?, priority_l2 = ?, priority_l3 = ? WHERE symbol = ?",
                (
                    priority.get(l1),
                    priority.get(f"{l1}-{l2}"),
                    priority.get(f"{l1}-{l2}-{l3}"),
                    r["symbol"],
                ),
            )

    # 6. 校验
    print("[migrate] 校验:")
    for r in conn.execute(
        "SELECT category_l1, category_l2, COUNT(*) AS n FROM instrument_metadata GROUP BY 1, 2 ORDER BY 1, 2"
    ):
        print(f"  {r['category_l1']}-{r['category_l2']}: {r['n']}")
    empty_l3 = conn.execute(
        "SELECT COUNT(*) AS n FROM instrument_metadata WHERE TRIM(COALESCE(category_l3, '')) = ''"
    ).fetchone()["n"]
    orphans = conn.execute(
        """SELECT COUNT(DISTINCT m.symbol) AS n FROM instrument_metadata m
           LEFT JOIN instrument_categories c
             ON c.path = m.category_l1 || '-' || m.category_l2 || '-' || m.category_l3
           WHERE c.path IS NULL"""
    ).fetchone()["n"]
    stale_bonds = {
        t: conn.execute(f"SELECT COUNT(*) AS n FROM {t} WHERE symbol IN ({','.join('?' * len(bond_symbols))})", bond_symbols).fetchone()["n"]
        for t in ["instrument_metadata", *MARKET_TABLES]
    } if bond_symbols else {}
    print(f"  l3 为空标的数: {empty_l3}（应为 0）")
    print(f"  分类树外的孤儿标的数: {orphans}（应为 0）")
    print(f"  债券残留行: {stale_bonds}（应全为 0）")
    conn.close()

    ok = empty_l3 == 0 and orphans == 0 and not any(stale_bonds.values())
    print("[migrate] 完成。" if ok else "[migrate] 校验未通过，请检查！")
    print("[migrate] 提示：批量回测历史结果未迁移，需要时请重跑回测。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
