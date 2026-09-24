"""阶段一：用 Tushare 抓中证300/500/1000 全部历史成分，分类成两份清单。

不拉任何 K 线，不写数据库已有表，只落 CSV。

    TUSHARE_TOKEN=xxx TUSHARE_HTTP_URL=https://tuaremax.top \
        .venv/bin/python fetch_tushare_index_members.py

产出（data/）：
  tushare_index_raw/{index}_{half}.csv   index_weight 原始明细（可续跑）
  tushare_delisted.csv                   退市股全名单
  universe_top1000.csv                   曾经进过市值前 1000 的标的
  universe_1000_1800.csv                 只进过 1001-1800 的标的（与上不重叠）
  universe_stats.json                    汇总口径数字

档位划分规则（每期快照各自判档，跨期取并集）：
  沪深300(000300) 全部           -> top300
  中证500(000905) 全部           -> 301_800
  中证1000(000852) 按 weight 降序 -> 前 200 名 = 801_1000，其余 = 1001_1800
  于是 top1000 = top300 ∪ 301_800 ∪ 801_1000
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from tushare_common import call_with_retry, get_pro_api

from core.paths import default_db_path

DATA_DIR = Path(__file__).resolve().parent / "data"
RAW_DIR = DATA_DIR / "tushare_index_raw"

# (指数代码, 抓取起始年, 是否按权重裁前 200)
INDICES = [
    ("000300.SH", 2005, False),
    ("000905.SH", 2007, False),
    ("000852.SH", 2014, True),
]
LAST_YEAR = 2026
CSI1000_HEAD = 200  # 中证1000 里权重前 200 名 = 市值 801-1000

HALVES = (("0101", "0630"), ("0701", "1231"))


def normalize(symbol: str) -> str:
    s = str(symbol or "").strip().upper()
    m = re.match(r"^(\d{6})\.(SS|SH|SZ|BJ)$", s)
    if not m:
        return s
    code, ex = m.groups()
    return f"{code}.SH" if ex in {"SS", "SH"} else f"{code}.{ex}"


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict], fields: list[str] | None = None) -> None:
    if not rows:
        print(f"[build] skip empty {path.name}")
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields or list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"[build] {len(rows)} rows -> {path.name}")


# ---------------------------------------------------------------- 抓取


def fetch_index_weights(pro) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    for index_code, first_year, _ in INDICES:
        for year in range(first_year, LAST_YEAR + 1):
            for md_start, md_end in HALVES:
                part = RAW_DIR / f"{index_code}_{year}{md_start[:4]}_{md_end}.csv"
                if part.exists():
                    continue
                try:
                    df = call_with_retry(
                        pro.index_weight,
                        index_code=index_code,
                        start_date=f"{year}{md_start}",
                        end_date=f"{year}{md_end}",
                    )
                except Exception as exc:
                    print(f"[fetch] {index_code} {year} {md_start}: {exc}", flush=True)
                    continue
                if df is None or not len(df):
                    part.write_text("index_code,con_code,trade_date,weight\n", encoding="utf-8")
                    continue
                df.to_csv(part, index=False)
        done = len(list(RAW_DIR.glob(f"{index_code}_*.csv")))
        print(f"[fetch] {index_code}: {done} 个半年度分片", flush=True)


def fetch_delisted(pro) -> list[dict]:
    df = call_with_retry(
        pro.stock_basic,
        list_status="D",
        fields="ts_code,symbol,name,list_date,delist_date",
    )
    rows = [
        {
            "symbol": normalize(r["ts_code"]),
            "name": r["name"],
            "list_date": r.get("list_date") or "",
            "delist_date": r.get("delist_date") or "",
        }
        for _, r in df.iterrows()
    ]
    rows.sort(key=lambda r: r["delist_date"])
    write_csv(DATA_DIR / "tushare_delisted.csv", rows)
    return rows


def fetch_listed(pro) -> None:
    """在市名单，仅用于给清单补名称（dev 库里没有的标的需要）。"""
    df = call_with_retry(
        pro.stock_basic, list_status="L", fields="ts_code,symbol,name,list_date"
    )
    rows = [
        {
            "symbol": normalize(r["ts_code"]),
            "name": r["name"],
            "list_date": r.get("list_date") or "",
        }
        for _, r in df.iterrows()
    ]
    write_csv(DATA_DIR / "tushare_listed.csv", rows)


# ---------------------------------------------------------------- 分类


def load_snapshots() -> dict[str, dict[str, list[tuple[str, float]]]]:
    """index_code -> trade_date -> [(con_code, weight), ...]"""
    snaps: dict[str, dict[str, list[tuple[str, float]]]] = defaultdict(lambda: defaultdict(list))
    for index_code, _, _ in INDICES:
        for part in sorted(RAW_DIR.glob(f"{index_code}_*.csv")):
            for r in read_csv(part):
                code = normalize(r.get("con_code"))
                date = str(r.get("trade_date") or "").strip()
                if not code or not date:
                    continue
                try:
                    weight = float(r.get("weight") or 0)
                except ValueError:
                    weight = 0.0
                snaps[index_code][date].append((code, weight))
    return snaps


def classify(snaps) -> tuple[list[dict], list[dict], dict]:
    band_years: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    band_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for date, members in snaps.get("000300.SH", {}).items():
        year = int(date[:4])
        for code, _ in members:
            band_years[code]["top300"].add(year)
            band_counts[code]["top300"] += 1

    for date, members in snaps.get("000905.SH", {}).items():
        year = int(date[:4])
        for code, _ in members:
            band_years[code]["301_800"].add(year)
            band_counts[code]["301_800"] += 1

    for date, members in snaps.get("000852.SH", {}).items():
        year = int(date[:4])
        for rank, (code, _) in enumerate(sorted(members, key=lambda t: -t[1]), start=1):
            band = "801_1000" if rank <= CSI1000_HEAD else "1001_1800"
            band_years[code][band].add(year)
            band_counts[code][band] += 1

    delisted = {r["symbol"]: r for r in read_csv(DATA_DIR / "tushare_delisted.csv")}
    listed = {r["symbol"]: r["name"] for r in read_csv(DATA_DIR / "tushare_listed.csv")}
    con = sqlite3.connect(str(default_db_path()))
    existing = {
        normalize(sym): (name or "")
        for sym, name in con.execute("select symbol, name from instrument_metadata")
    }
    con.close()

    top1000, only_1000_1800 = [], []
    for code, bands in band_years.items():
        in_top = any(b in bands for b in ("top300", "301_800", "801_1000"))
        in_tail = "1001_1800" in bands
        if not in_top and not in_tail:
            continue
        top_years = sorted(set().union(*(bands[b] for b in bands if b != "1001_1800")))
        tail_years = sorted(bands.get("1001_1800", set()))
        best = next(
            (b for b in ("top300", "301_800", "801_1000", "1001_1800") if b in bands), ""
        )
        row = {
            "symbol": code,
            "name": existing.get(code)
            or (delisted.get(code) or {}).get("name")
            or listed.get(code)
            or "",
            "best_band": best,
            "top1000_years": f"{top_years[0]}-{top_years[-1]}" if top_years else "",
            "n_snap_top1000": sum(band_counts[code].get(b, 0) for b in ("top300", "301_800", "801_1000")),
            "band_1001_1800_years": f"{tail_years[0]}-{tail_years[-1]}" if tail_years else "",
            "n_snap_1001_1800": band_counts[code].get("1001_1800", 0),
            "delisted": (delisted.get(code) or {}).get("delist_date", ""),
            "in_project": "Y" if code in existing else "N",
        }
        (top1000 if in_top else only_1000_1800).append(row)

    top1000.sort(key=lambda r: (r["in_project"], r["best_band"], -r["n_snap_top1000"], r["symbol"]))
    only_1000_1800.sort(key=lambda r: (r["in_project"], -r["n_snap_1001_1800"], r["symbol"]))

    stats = {
        "snapshot_dates": {k: len(v) for k, v in snaps.items()},
        "date_range": {
            k: [min(v), max(v)] if v else [] for k, v in snaps.items()
        },
        "top1000_total": len(top1000),
        "top1000_missing": sum(1 for r in top1000 if r["in_project"] == "N"),
        "top1000_delisted": sum(1 for r in top1000 if r["delisted"]),
        "band_1001_1800_total": len(only_1000_1800),
        "band_1001_1800_missing": sum(1 for r in only_1000_1800 if r["in_project"] == "N"),
        "delisted_total": len(delisted),
    }
    return top1000, only_1000_1800, stats


def main() -> None:
    pro = get_pro_api()
    print("[stage1] 抓取 index_weight …", flush=True)
    fetch_index_weights(pro)
    print("[stage1] 抓取退市股名单 …", flush=True)
    fetch_delisted(pro)
    print("[stage1] 抓取在市名单（补名称）…", flush=True)
    fetch_listed(pro)
    print("[stage1] 分类 …", flush=True)
    top1000, tail, stats = classify(load_snapshots())
    write_csv(DATA_DIR / "universe_top1000.csv", top1000)
    write_csv(DATA_DIR / "universe_1000_1800.csv", tail)
    (DATA_DIR / "universe_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2)
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
