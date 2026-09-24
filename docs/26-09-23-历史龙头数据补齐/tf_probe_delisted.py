"""对 237 只"疑似 tushare-only"标的（235 退市股 + 2 摘牌 ETF）逐一探测 tf 覆盖。

产出 data/tf_probe_delisted.csv：
  symbol, name, asset_type, list_date, delist_date, tf_rows, tf_first, tf_last, tf_n_factors, tf_error

只读 tf API，不写库。tf 单接口限流 60 次/分钟（provider 内置节流），约 8-12 分钟跑完。
"""

from __future__ import annotations

import csv
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from data.provider_tickflow import TickFlowProvider  # noqa: E402

DATA = ROOT / "docs" / "26-09-23-历史龙头数据补齐" / "data"
OUT = Path(__file__).resolve().parent / "data" / "tf_probe_delisted.csv"
END = date(2026, 9, 24)


def normalize(s: str) -> str:
    s = str(s or "").strip().upper()
    m = re.match(r"^(\d{6})\.(SS|SH|SZ|BJ)$", s)
    if not m:
        return s
    c, ex = m.groups()
    return f"{c}.SS" if ex in {"SS", "SH"} else f"{c}.{ex}"


def collect_candidates() -> list[dict]:
    con = sqlite3.connect(str(ROOT / "data" / "trend_quant.db"))
    local = {normalize(s) for (s,) in con.execute("select symbol from instrument_metadata")}
    con.close()

    listed = {
        normalize(r["symbol"]): r
        for r in csv.DictReader((DATA / "tushare_listed.csv").open(encoding="utf-8"))
    }
    out = []
    for fname in ("universe_top1000.csv", "universe_1000_1800.csv"):
        for r in csv.DictReader((DATA / fname).open(encoding="utf-8")):
            s = normalize(r["symbol"])
            if s not in local and r["delisted"].strip():
                out.append({
                    "symbol": s, "name": r["name"], "asset_type": "stock",
                    "list_date": listed.get(s, {}).get("list_date", ""),
                    "delist_date": r["delisted"].strip(),
                })
    for r in csv.DictReader((DATA / "etf_candidates_final.csv").open(encoding="utf-8")):
        s = normalize(r["symbol"])
        if s not in local and r["list_status"].strip() == "D":
            out.append({
                "symbol": s, "name": r["name"], "asset_type": "etf",
                "list_date": r["list_date"], "delist_date": r["delist_date"],
            })
    return out


def main() -> None:
    candidates = collect_candidates()
    print(f"[probe] 候选 {len(candidates)} 只", flush=True)
    pro = TickFlowProvider()

    symbols = [c["symbol"] for c in candidates]
    factors_map, factor_errors = pro.fetch_ex_factors(symbols)
    print(f"[probe] 因子批量返回 {len(factors_map)} 只, 错误 {len(factor_errors)} 只", flush=True)

    with OUT.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "symbol", "name", "asset_type", "list_date", "delist_date",
            "tf_rows", "tf_first", "tf_last", "tf_n_factors", "tf_error",
        ])
        writer.writeheader()
        for i, c in enumerate(candidates, 1):
            rec = dict(c)
            try:
                df = pro.fetch_daily_history(c["symbol"], date(1990, 1, 1), END, "none")
                rec["tf_rows"] = len(df)
                rec["tf_first"] = str(df["time"].min().date()) if len(df) else ""
                rec["tf_last"] = str(df["time"].max().date()) if len(df) else ""
                rec["tf_error"] = ""
            except Exception as exc:
                rec["tf_rows"] = -1
                rec["tf_first"] = rec["tf_last"] = ""
                rec["tf_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            rec["tf_n_factors"] = len(factors_map.get(c["symbol"], []))
            writer.writerow(rec)
            fh.flush()
            if i % 20 == 0 or i == len(candidates):
                print(f"[probe] {i}/{len(candidates)}", flush=True)
    print(f"[probe] 完成 -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
