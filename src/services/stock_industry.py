"""申万行业分类：名称规范化、归类解析、TickFlow 同步与待分类回补。

方案文档：docs/stock-industry-etf-holdings/2026-08-24-stock-industry-etf-holdings-plan.md

数据流（应用运行时对外部数据源零在线依赖，只读写本地表）：

    tickflow universes（月度，免费） ─┐
                                      ├→ stock_industry 表 → resolve_category()
    tushare index_member_all（季度） ─┘        ↓
                                    ETF 导入 / 手动添加 suggest / 存量迁移 / 待分类回补
"""

from __future__ import annotations

import re
import unicodedata

from audit.app_logger import get_logger
from core import env as _env
from core.symbols import from_vendor_symbol, to_vendor_symbol
from data.storage.db import Database, get_db, record_job_run_safely

logger = get_logger(__name__)

STOCK_L1 = "股票"
UNCLASSIFIED_L2 = "待分类"
UNCLASSIFIED_L3 = "待分类"

SOURCE_TICKFLOW = "tickflow_universe"
SOURCE_TUSHARE = "tushare_sw2021"
SOURCE_MANUAL = "manual"

_TICKFLOW_BASE_URL = "https://api.tickflow.org"

def _tickflow_api_key() -> str:
    """TickFlow API key：缺失时给可操作提示而非 KeyError 裸抛（P2-5）。"""
    key = _env.tickflow_api_key()
    if not key:
        raise RuntimeError(
            "缺少 TICKFLOW_API_KEY 环境变量：请在 .env 配置后重试（月度行业同步跳过）"
        )
    return key

_UNIVERSE_BATCH = 50

# NFKC 之后 Unicode 罗马数字（Ⅱ U+2161 等）与全角字母都已归一为 ASCII，
# 只需处理一种写法；要求前导是 CJK，避免误伤正常英文名。
_ROMAN_SUFFIX_RE = re.compile(r"(?<=[一-鿿])I{1,3}$")
_UNIVERSE_PREFIX_RE = re.compile(r"^SW([123])")


def normalize_industry_name(name: str) -> str:
    """行业名规范化：NFKC（罗马数字/全角→ASCII）→ 去空白 → 剥罗马数字后缀。

    两个数据源（tickflow universe 名 vs tushare index_classify）的字符习惯
    不一致，两源按名称对齐前必须统一走这里（方案 §4.1）。
    例：'白酒Ⅲ' → '白酒'，'家电零部件Ⅱ' → '家电零部件'，'白酒II' → '白酒'。
    """
    text = unicodedata.normalize("NFKC", str(name or ""))
    text = re.sub(r"\s+", "", text)
    return _ROMAN_SUFFIX_RE.sub("", text).strip()


def parse_tickflow_universe_name(name: str) -> tuple[int, str] | None:
    """'SW3白酒Ⅲ' → (3, '白酒Ⅲ')；非申万 universe 名返回 None。"""
    match = _UNIVERSE_PREFIX_RE.match(str(name or ""))
    if not match:
        return None
    return int(match.group(1)), str(name)[len(match.group(0)):]


def tickflow_symbol_to_project(symbol: str) -> str:
    """tickflow/tushare 代码 → 项目格式：.SH → .SS，其余不变。"""
    return from_vendor_symbol(symbol)


def project_symbol_to_tushare(symbol: str) -> str:
    """项目代码 → tushare 格式：.SS → .SH，其余不变。"""
    return to_vendor_symbol(symbol)


def is_a_share(symbol: str) -> bool:
    """仅保留上交所（.SS）与深交所（.SZ）A 股；北交所（.BJ）等丢弃。"""
    text = str(symbol or "").strip().upper()
    return text.endswith((".SS", ".SZ"))


def resolve_category(symbol: str, db: Database | None = None) -> dict:
    """唯一归类入口：stock_industry 命中 → (股票, 申万一级, 申万二级)；否则待分类。"""
    db = db or get_db()
    row = db.get_stock_industry(symbol)
    if row is None:
        return {
            "category_l1": STOCK_L1,
            "category_l2": UNCLASSIFIED_L2,
            "category_l3": UNCLASSIFIED_L3,
            "hit": False,
            "source": None,
            "sw_l3_name": "",
        }
    return {
        "category_l1": STOCK_L1,
        "category_l2": str(row.get("sw_l1_name") or "").strip(),
        "category_l3": str(row.get("sw_l2_name") or "").strip(),
        "hit": True,
        "source": row.get("source"),
        "sw_l3_name": str(row.get("sw_l3_name") or "").strip(),
    }


def category_path_set(db: Database | None = None) -> set[str]:
    db = db or get_db()
    return {
        str(row.get("path") or "").strip()
        for row in db.list_instrument_categories()
        if str(row.get("path") or "").strip()
    }


def reclassify_pending_stocks(db: Database | None = None) -> dict:
    """待分类回补：对当前类目为「待分类」的在管股票重跑 resolve_category。

    目标类目路径不在分类树中时跳过（deferred）—— 存量迁移（P2）之前新树
    尚不存在，同步脚本可以安全地先跑。移动清单由调用方落 job_runs（方案 §5）。
    """
    from services.instrument_admin import category_priorities  # 避免模块级环依赖

    db = db or get_db()
    valid_paths = category_path_set(db)
    pending = [
        item
        for item in db.list_instrument_metadata()
        if item.get("enabled")
        and str(item.get("category_l1") or "").strip() == STOCK_L1
        and str(item.get("category_l2") or "").strip() == UNCLASSIFIED_L2
    ]

    moved: list[dict] = []
    deferred = 0
    still_unclassified: list[str] = []
    for item in pending:
        symbol = str(item.get("symbol") or "").strip()
        resolved = resolve_category(symbol, db=db)
        if not resolved["hit"]:
            still_unclassified.append(symbol)
            continue
        path = "-".join(
            [resolved["category_l1"], resolved["category_l2"], resolved["category_l3"]]
        )
        if path not in valid_paths:
            deferred += 1
            continue
        p1, p2, p3 = category_priorities(
            resolved["category_l1"], resolved["category_l2"], resolved["category_l3"]
        )
        db.update_instrument_category(
            symbol,
            resolved["category_l1"],
            resolved["category_l2"],
            resolved["category_l3"],
            p1,
            p2,
            p3,
        )
        moved.append(
            {
                "symbol": symbol,
                "name": str(item.get("name") or "").strip(),
                "to": path,
                "source": resolved["source"],
            }
        )

    return {
        "pending": len(pending),
        "moved": moved,
        "deferred": deferred,
        "still_unclassified": still_unclassified,
    }


def _build_industry_rows(universes: list[dict], details: dict[str, dict]) -> list[dict]:
    """universe 名录 + 成分 → stock_industry 行（项目代码格式）。"""
    code_names = collect_sw_code_names(universes)

    rows: dict[str, dict] = {}
    for uid, detail in details.items():
        code = uid.rsplit("_", 1)[-1]
        names = code_names.get(code)
        if not names or not all(level in names for level in (1, 2, 3)):
            logger.warning("universe %s 缺少层级名称，跳过", uid)
            continue
        for raw_symbol in detail.get("symbols", []) or []:
            symbol = tickflow_symbol_to_project(raw_symbol)
            if not is_a_share(symbol):
                continue
            rows[symbol] = {
                "symbol": symbol,
                "sw_l1_name": normalize_industry_name(names[1]),
                "sw_l2_name": normalize_industry_name(names[2]),
                "sw_l3_name": str(names[3]).strip(),
                "sw_l3_code": code,
            }
    return list(rows.values())


def collect_sw_code_names(universes: list[dict]) -> dict[str, dict[int, str]]:
    """universe 名录 → {6 位行业码: {1: l1名, 2: l2名, 3: l3名}}（原始名称）。"""
    code_names: dict[str, dict[int, str]] = {}
    for u in universes:
        uid = str(u.get("id") or "")
        for level in (1, 2, 3):
            prefix = f"CN_Equity_SW{level}_"
            if uid.startswith(prefix):
                parsed = parse_tickflow_universe_name(u.get("name"))
                if parsed is not None:
                    code_names.setdefault(uid[len(prefix):], {})[parsed[0]] = parsed[1]
                break
    return code_names


def build_sw_tree(code_names: dict[str, dict[int, str]]) -> list[dict]:
    """由行业码名录构建申万树（l1/l2 为规范化名称，order 取自行业码）。

    返回 [{'name': l1, 'order': int, 'l2': [{'name': l2, 'order': int}]}]，
    供迁移脚本重建 instrument_categories。含路径分隔符的名字剔除并告警
    （评审 B2）；同 l1 下规范化后撞名的 l2 只保留先出现者并告警。
    """
    l1_order: dict[str, int] = {}
    l2_order: dict[tuple[str, str], int] = {}
    l2_raw: dict[tuple[str, str], str] = {}
    for code, names in sorted(code_names.items()):
        if not code.isdigit() or len(code) != 6:
            continue
        if not all(level in names for level in (1, 2)):
            continue
        l1 = normalize_industry_name(names[1])
        l2 = normalize_industry_name(names[2])
        if "-" in l1 or "-" in l2:
            logger.error("行业名含路径分隔符 '-'，剔除: %s / %s (code=%s)", l1, l2, code)
            continue
        l1_order[l1] = min(l1_order.get(l1, 999), int(code[:2]))
        key = (l1, l2)
        if key in l2_order:
            # 同一 L2 的多个 L3 叶子是正常合并；只有原始名不同（剥罗马数字
            # 后缀导致的真撞名）才告警（评审 B2）
            if l2_raw.get(key) != str(names[2]).strip():
                logger.warning(
                    "同 L1 下撞名: %s / %s vs %s (code=%s)，保留先出现者",
                    l1, l2_raw.get(key), names[2], code,
                )
            continue
        l2_order[key] = int(code[:4])
        l2_raw[key] = str(names[2]).strip()

    tree: list[dict] = []
    for l1 in sorted(l1_order, key=lambda n: (l1_order[n], n)):
        children = [
            {"name": l2, "order": order}
            for (parent, l2), order in sorted(l2_order.items(), key=lambda kv: (kv[1], kv[0][1]))
            if parent == l1
        ]
        tree.append({"name": l1, "order": l1_order[l1], "l2": children})
    return tree


def persist_sw_tree(db: Database, code_names: dict[str, dict[int, str]]) -> list[dict]:
    """把 tickflow 版申万树写入 app_config（迁移脚本的数据源，含官方顺序）。

    tickflow 的 universe 名录可能缺个别 L2 叶子（如 计算机-IT服务），而
    tushare 官方全量同步会补齐这些分支（add_missing_tree_branches）——
    这里合并保留既有树里 tickflow 缺失的分支，避免月度同步把 tushare
    补的分支冲掉。
    """
    tree = build_sw_tree(code_names)
    if not tree:
        return tree
    existing = (db.get_config("sw2021_tree") or {}).get("tree") or []
    index = {l1["name"]: l1 for l1 in tree}
    kept: list[str] = []
    for old_l1 in existing:
        target = index.get(old_l1["name"])
        if target is None:
            tree.append(old_l1)
            kept.append(str(old_l1["name"]))
            continue
        have = {c["name"] for c in target["l2"]}
        for child in old_l1["l2"]:
            if child["name"] not in have:
                target["l2"].append(dict(child))
                have.add(child["name"])
                kept.append(f"{old_l1['name']}/{child['name']}")
    if kept:
        logger.info("申万树保留 %d 个 tickflow 缺失的分支: %s", len(kept), kept)
    db.set_config("sw2021_tree", {"source": SOURCE_TICKFLOW, "tree": tree})
    return tree


def add_missing_tree_branches(db: Database, rows: list[dict]) -> list[str]:
    """把 rows（tushare 官方全量）里树中缺失的 L1/L2 分支补进 app_config 树。

    返回新增分支描述列表。树不存在（还没跑过 tickflow 同步）时返回空并告警。
    """
    config = db.get_config("sw2021_tree") or {}
    tree = config.get("tree") or []
    if not tree:
        logger.warning("申万树不存在，请先运行 scripts/sync_stock_industry.py")
        return []
    index = {l1["name"]: l1 for l1 in tree}
    added: list[str] = []
    for row in rows:
        l1_name = str(row.get("sw_l1_name") or "").strip()
        l2_name = str(row.get("sw_l2_name") or "").strip()
        if not l1_name or not l2_name:
            continue
        l1 = index.get(l1_name)
        if l1 is None:
            l1 = {"name": l1_name, "order": 900 + len(tree), "l2": []}
            index[l1_name] = l1
            tree.append(l1)
            added.append(f"L1:{l1_name}")
        if not any(c["name"] == l2_name for c in l1["l2"]):
            order = max([c["order"] for c in l1["l2"]] or [0]) + 1
            l1["l2"].append({"name": l2_name, "order": order})
            added.append(f"{l1_name}/{l2_name}")
    if added:
        db.set_config("sw2021_tree", {**config, "tree": tree})
    return added


def sync_industry_from_tickflow(
    db: Database | None = None,
    client=None,
    reclassify: bool = True,
    write: bool = True,
) -> dict:
    """从 TickFlow universes 全量同步申万行业分类（≈10 次请求，starter 免费档）。

    write=False 时只拉取统计不落库（dry-run）。返回汇总 dict；调用方负责落 job_runs。
    """
    db = db or get_db()
    if client is None:
        try:
            from tickflow import TickFlow
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("缺少 tickflow 依赖") from exc
        client = TickFlow(
            api_key=_tickflow_api_key(), base_url=_TICKFLOW_BASE_URL
        )

    universes = client.universes.list()
    sw3_ids = sorted(
        str(u["id"]) for u in universes if str(u.get("id") or "").startswith("CN_Equity_SW3_")
    )
    details: dict[str, dict] = {}
    for i in range(0, len(sw3_ids), _UNIVERSE_BATCH):
        details.update(client.universes.batch(sw3_ids[i: i + _UNIVERSE_BATCH]))

    rows = _build_industry_rows(universes, details)
    written = db.upsert_stock_industry(rows, SOURCE_TICKFLOW) if write else 0
    tree: list[dict] = []
    if write:
        tree = persist_sw_tree(db, collect_sw_code_names(universes))

    summary: dict = {
        "universes": len(sw3_ids),
        "rows": len(rows),
        "written": written,
        "skipped_by_priority": (len(rows) - written) if write else 0,
        "tree_l1": len(tree),
        "dry_run": not write,
    }
    if reclassify and write:
        summary["reclassify"] = reclassify_pending_stocks(db=db)
    return summary


def record_industry_sync_job(job_type: str, summary: dict) -> None:
    """同步结果落 job_runs；待分类回补的移动清单一并记录（方案 §5 可追溯）。"""
    reclassify = summary.get("reclassify") or {}
    moved = reclassify.get("moved") or []
    payload = {
        **{k: v for k, v in summary.items() if k != "reclassify"},
        "reclassify": {
            "pending": reclassify.get("pending"),
            "moved_count": len(moved),
            "moved": moved,
            "deferred": reclassify.get("deferred"),
            "still_unclassified": reclassify.get("still_unclassified"),
        },
    }
    # 状态按实际形态：部分行被高优先级挡下 / 回补 deferred / 仍有未分类 →
    # partial；全部成功 → success；全未写入 → failed（旧实现无条件 success，
    # job_runs 失去监控意义）。
    skipped = int(summary.get("skipped_by_priority") or 0)
    rows = int(summary.get("rows") or 0)
    written = int(summary.get("written") or 0)
    deferred = reclassify.get("deferred") or []
    still_unclassified = reclassify.get("still_unclassified") or []
    if rows > 0 and written == 0:
        status = "failed"
    elif skipped > 0 or deferred or still_unclassified:
        status = "partial"
    else:
        status = "success"
    record_job_run_safely(job_type, payload, status=status)
