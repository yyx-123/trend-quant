"""全市场股票快照扫描：为股票标的池扩充提供客观筛选依据。

产出 ``data/market_snapshot.csv``：全 A 股（以 stock_industry 的申万分类名册为准）
的名称、最新价、当日成交额、总市值、换手率、上市日期。

用途：按「申万一级行业(项目 L2) / 申万二级行业(项目 L3)」分组，用
总市值降序定龙头，用成交额剔除流动性不足的票，用名称剔除 ST。

数据源：TickFlow 付费档（quotes 批量上限 50、60 次/分钟；instruments 含
total_shares 与 listing_date，据此算真实总市值）。

用法（项目根目录）：
    .venv/bin/python research/2026-09-16_stock-universe-expansion/scan_market.py
"""

from __future__ import annotations

import csv
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _common  # noqa: E402  (.env 加载 + DB_PATH)
from core import env  # noqa: E402
from core.symbols import from_vendor_symbol, to_vendor_symbol  # noqa: E402
from tickflow import TickFlow  # noqa: E402

OUT = Path(__file__).resolve().parent / "data" / "market_snapshot.csv"
QUOTE_CHUNK = 50          # 服务端硬上限
QUOTE_INTERVAL = 1.05     # 60 次/分钟
INSTRUMENT_CHUNK = 200
INSTRUMENT_INTERVAL = 0.5

ST_MARKERS = ("ST", "退")


def load_universe(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT symbol, sw_l1_name, sw_l2_name, sw_l3_name FROM stock_industry ORDER BY symbol"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def fetch_quotes(client: TickFlow, symbols: list[str]) -> dict[str, dict]:
    """返回 {项目代码: quote}。

    vendor 只认 .SH，项目内部用 .SS —— 必须经 to_vendor_symbol 转换，
    否则沪市段整段静默返回空（2026-09-16 实测：不转换只拿到 2895/5552）。
    """
    out: dict[str, dict] = {}
    total = (len(symbols) + QUOTE_CHUNK - 1) // QUOTE_CHUNK
    for idx, chunk in enumerate(chunked(symbols, QUOTE_CHUNK), start=1):
        vendor_chunk = [to_vendor_symbol(s) for s in chunk]
        last_err = None
        for attempt in range(3):
            try:
                raw = client.quotes.get(symbols=vendor_chunk)
                items = raw if isinstance(raw, list) else [raw]
                for item in items:
                    if isinstance(item, dict) and item.get("symbol"):
                        out[from_vendor_symbol(str(item["symbol"]))] = item
                last_err = None
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                time.sleep(2.0 + attempt * 2.0)
        if last_err is not None:
            print(f"  [quotes] chunk {idx}/{total} 失败：{type(last_err).__name__} {last_err}", flush=True)
        if idx % 20 == 0 or idx == total:
            print(f"  [quotes] {idx}/{total} 批，已取 {len(out)} 只", flush=True)
        time.sleep(QUOTE_INTERVAL)
    return out


def fetch_instruments(client: TickFlow, symbols: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    total = (len(symbols) + INSTRUMENT_CHUNK - 1) // INSTRUMENT_CHUNK
    for idx, chunk in enumerate(chunked(symbols, INSTRUMENT_CHUNK), start=1):
        try:
            raw = client.instruments.batch(symbols=[to_vendor_symbol(s) for s in chunk])
            items = raw if isinstance(raw, list) else [raw]
            for item in items:
                if isinstance(item, dict) and item.get("symbol"):
                    out[from_vendor_symbol(str(item["symbol"]))] = item
        except Exception as exc:  # noqa: BLE001
            print(f"  [instruments] chunk {idx}/{total} 失败：{type(exc).__name__} {exc}", flush=True)
        if idx % 10 == 0 or idx == total:
            print(f"  [instruments] {idx}/{total} 批，已取 {len(out)} 只", flush=True)
        time.sleep(INSTRUMENT_INTERVAL)
    return out


def main() -> int:
    universe = load_universe(_common.DB_PATH)
    symbols = [r["symbol"] for r in universe]
    print(f"候选池：{len(symbols)} 只（stock_industry）", flush=True)

    client = TickFlow(api_key=env.tickflow_api_key(), base_url="https://api.tickflow.org")

    print("拉取实时报价 …", flush=True)
    quotes = fetch_quotes(client, symbols)
    print("拉取证券基础信息（总股本/上市日）…", flush=True)
    insts = fetch_instruments(client, symbols)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with OUT.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "symbol", "name", "sw_l1", "sw_l2", "sw_l3",
                "price", "amount", "amount_yi", "mktcap_yi", "turnover_rate",
                "listing_date", "is_st", "quote_date",
            ]
        )
        for row in universe:
            sym = row["symbol"]
            q = quotes.get(sym) or {}
            inst = insts.get(sym) or {}
            ext = q.get("ext") if isinstance(q.get("ext"), dict) else {}
            iext = inst.get("ext") if isinstance(inst.get("ext"), dict) else {}

            name = str(ext.get("name") or inst.get("name") or "").strip()
            price = q.get("last_price")
            amount = q.get("amount")
            shares = iext.get("total_shares")
            mktcap_yi = ""
            if isinstance(price, (int, float)) and isinstance(shares, (int, float)) and price and shares:
                mktcap_yi = round(float(price) * float(shares) / 1e8, 2)
            ts = q.get("timestamp")
            quote_date = ""
            if ts:
                quote_date = time.strftime("%Y-%m-%d", time.localtime(int(ts) / 1000))
            writer.writerow(
                [
                    sym, name, row["sw_l1_name"], row["sw_l2_name"], row["sw_l3_name"],
                    price if price is not None else "",
                    amount if amount is not None else "",
                    round(float(amount) / 1e8, 3) if isinstance(amount, (int, float)) else "",
                    mktcap_yi,
                    round(float(ext.get("turnover_rate", 0.0)), 6) if ext.get("turnover_rate") is not None else "",
                    str(iext.get("listing_date") or ""),
                    1 if any(m in name.upper() or m in name for m in ST_MARKERS) else 0,
                    quote_date,
                ]
            )
            written += 1

    print(f"写出 {written} 行 → {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
