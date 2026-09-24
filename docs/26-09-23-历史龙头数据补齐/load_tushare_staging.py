"""tushare-only 标的灌临时表（staging_*，与正式表完全隔离）。

输入: data/tushare_only_symbols.csv
  列: symbol,need_kline,need_factors  —— need_kline=0 时只补因子（tf 有 K 线但无因子的混合情形）

产出（同在 data/trend_quant.db，但现有代码不读这些表，对现有流程零影响）:
  staging_ts_market_data   结构同 market_data_raw（amount 已×1000 换算为元，provider='tushare'）
  staging_ts_ex_factors    结构同 ex_factors（累积 adj_factor 已换算为逐次因子，事件日=跳变日-1 个日历日）
  staging_ts_instruments   标的元数据（name/list_date/delist_date，来自本地清单 CSV，不调 API）
  staging_ts_fetch_log     逐标的逐类抓取状态，ok 的重跑自动跳过（断点续跑）

限流: 每次 API 调用间隔 0.35s（≈170 次/分钟，对 5000 积分档偏保守），失败重试 4 次 × 3s。
单次返回上限 6000 行 —— 顶到上限自动按 3 年窗口分段重取并去重。

用法:
  TUSHARE_TOKEN=xxx TUSHARE_HTTP_URL=https://tuaremax.top \
    .venv/bin/python docs/26-09-23-历史龙头数据补齐/load_tushare_staging.py [tushare_only_symbols.csv]
"""

from __future__ import annotations

import csv
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from core.symbols import from_vendor_symbol  # noqa: E402
from tushare_common import call_with_retry, get_pro_api  # noqa: E402

DB = ROOT / "data" / "trend_quant.db"
DATA = ROOT / "docs" / "26-09-23-历史龙头数据补齐" / "data"
INPUT = Path(__file__).resolve().parent / "data" / "tushare_only_symbols.csv"
THROTTLE_S = 0.35
ROW_CAP = 6000

DDL = """
CREATE TABLE IF NOT EXISTS staging_ts_market_data (
    symbol TEXT NOT NULL, time TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
    provider TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, time)
);
CREATE TABLE IF NOT EXISTS staging_ts_ex_factors (
    symbol TEXT NOT NULL, time TEXT NOT NULL, factor REAL NOT NULL,
    provider TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, time)
);
CREATE TABLE IF NOT EXISTS staging_ts_instruments (
    symbol TEXT PRIMARY KEY, name TEXT, asset_type TEXT,
    list_date TEXT, delist_date TEXT, source_list TEXT, sector TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS staging_ts_fetch_log (
    symbol TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
    rows INTEGER, message TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, kind)
);
"""


def iso(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"


class Throttler:
    def __init__(self, interval: float) -> None:
        self.interval = interval
        self.next_at = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        if now < self.next_at:
            time.sleep(self.next_at - now)
        self.next_at = max(now, self.next_at) + self.interval


def fetch_paged(pro, throttle: Throttler, api_name: str, ts_code: str):
    """全历史抓取；顶到 6000 行上限时按 3 年窗口分段重取。

    无日期区间的调用对个别老退市股会返回空（镜像站怪癖，000406.SZ 实测），
    空结果先用显式全区间（19900101~今天）重试一次再下定论。
    """
    throttle.wait()
    df = call_with_retry(getattr(pro, api_name), ts_code=ts_code)
    if df is not None and len(df) == 0:
        throttle.wait()
        df = call_with_retry(
            getattr(pro, api_name), ts_code=ts_code,
            start_date="19900101", end_date=datetime.now().strftime("%Y%m%d"),
        )
    if df is None or len(df) < ROW_CAP:
        return df
    parts = []
    for y0 in range(1990, 2027, 3):
        throttle.wait()
        part = call_with_retry(
            getattr(pro, api_name), ts_code=ts_code,
            start_date=f"{y0}0101", end_date=f"{y0 + 2}1231",
        )
        if part is not None and len(part):
            parts.append(part)
    if not parts:
        return df
    import pandas as pd

    out = pd.concat(parts).drop_duplicates(subset=["trade_date"], keep="last")
    return out.sort_values("trade_date").reset_index(drop=True)


def to_market_rows(symbol: str, df) -> list[tuple]:
    rows = []
    for _, r in df.iterrows():
        rows.append((
            symbol, f"{iso(r['trade_date'])} 00:00:00",
            r["open"], r["high"], r["low"], r["close"],
            r["vol"], (r["amount"] or 0) * 1000.0, "tushare",
        ))
    return rows


def to_factor_rows(symbol: str, adj) -> list[tuple]:
    """累积 adj_factor → 逐次因子（f=adj(t)/adj(t-1)），事件日=跳变日-1 个日历日。"""
    adj = adj.sort_values("trade_date").reset_index(drop=True)
    rows, prev = [], None
    for _, r in adj.iterrows():
        cur = float(r["adj_factor"])
        if prev is not None and prev > 0:
            ratio = cur / prev
            if abs(ratio - 1.0) > 1e-9:
                d = datetime.strptime(r["trade_date"], "%Y%m%d") - timedelta(days=1)
                rows.append((symbol, d.strftime("%Y-%m-%d"), ratio, "tushare"))
        prev = cur
    return rows


def stage_instruments(con, symbols: list[str]) -> None:
    """从本地清单 CSV 填元数据（不调 API）。"""
    meta: dict[str, dict] = {}
    for fname, source in (("universe_top1000.csv", "top1000"), ("universe_1000_1800.csv", "band1001_1800")):
        for r in csv.DictReader((DATA / fname).open(encoding="utf-8")):
            s = from_vendor_symbol(r["symbol"])
            meta[s] = {
                "name": r["name"], "asset_type": "stock", "list_date": "",
                "delist_date": r["delisted"].strip(), "source_list": source, "sector": r["best_band"],
            }
    for r in csv.DictReader((DATA / "tushare_listed.csv").open(encoding="utf-8")):
        s = from_vendor_symbol(r["symbol"])
        if s in meta:
            meta[s]["list_date"] = r.get("list_date", "")
    for r in csv.DictReader((DATA / "etf_candidates_final.csv").open(encoding="utf-8")):
        s = from_vendor_symbol(r["symbol"])
        meta[s] = {
            "name": r["name"], "asset_type": "etf", "list_date": r["list_date"],
            "delist_date": "" if r["delist_date"] in ("", "nan") else r["delist_date"],
            "source_list": "etf_final175", "sector": r["sector"],
        }
    for s in symbols:
        m = meta.get(s)
        if not m:
            continue
        for k in ("list_date", "delist_date"):
            v = m[k]
            m[k] = iso(v) if v and len(v) == 8 and v.isdigit() else v
        con.execute(
            "INSERT OR REPLACE INTO staging_ts_instruments"
            " (symbol,name,asset_type,list_date,delist_date,source_list,sector)"
            " VALUES (?,?,?,?,?,?,?)",
            (s, m["name"], m["asset_type"], m["list_date"], m["delist_date"], m["source_list"], m["sector"]),
        )
    con.commit()


def main() -> None:
    input_path = Path(sys.argv[1]) if len(sys.argv) > 1 else INPUT
    tasks = []
    for r in csv.DictReader(input_path.open(encoding="utf-8")):
        tasks.append({
            "symbol": r["symbol"].strip(),
            "need_kline": r.get("need_kline", "1").strip() != "0",
            "need_factors": r.get("need_factors", "1").strip() != "0",
        })
    print(f"[staging] 任务 {len(tasks)} 只 <- {input_path}", flush=True)

    con = sqlite3.connect(str(DB))
    con.executescript(DDL)
    stage_instruments(con, [t["symbol"] for t in tasks])

    pro = get_pro_api()
    pro._DataApi__timeout = 90
    throttle = Throttler(THROTTLE_S)

    def done(symbol: str, kind: str) -> bool:
        return con.execute(
            "select 1 from staging_ts_fetch_log where symbol=? and kind=? and status='ok'",
            (symbol, kind),
        ).fetchone() is not None

    def log(symbol: str, kind: str, status: str, rows: int, message: str = "") -> None:
        con.execute(
            "INSERT OR REPLACE INTO staging_ts_fetch_log (symbol,kind,status,rows,message)"
            " VALUES (?,?,?,?,?)", (symbol, kind, status, rows, message[:200]),
        )
        con.commit()

    etf_symbols = {
        s for (s,) in con.execute("select symbol from staging_ts_instruments where asset_type='etf'")
    }
    ok_k = ok_f = err = 0
    for i, t in enumerate(tasks, 1):
        symbol = t["symbol"]
        ts_code = symbol if symbol.endswith(".SZ") else symbol.replace(".SS", ".SH")
        is_etf = symbol in etf_symbols
        try:
            if t["need_kline"] and not done(symbol, "daily"):
                api_name = "fund_daily" if is_etf else "daily"
                df = fetch_paged(pro, throttle, api_name, ts_code)
                rows = to_market_rows(symbol, df) if df is not None and len(df) else []
                con.executemany(
                    "INSERT OR REPLACE INTO staging_ts_market_data"
                    " (symbol,time,open,high,low,close,volume,amount,provider) VALUES (?,?,?,?,?,?,?,?,?)",
                    rows,
                )
                con.commit()
                log(symbol, "daily", "ok" if rows else "empty", len(rows))
            if t["need_factors"] and not done(symbol, "factors"):
                api_name = "fund_adj" if is_etf else "adj_factor"
                adj = fetch_paged(pro, throttle, api_name, ts_code)
                rows = to_factor_rows(symbol, adj) if adj is not None and len(adj) else []
                con.executemany(
                    "INSERT OR REPLACE INTO staging_ts_ex_factors (symbol,time,factor,provider) VALUES (?,?,?,?)",
                    rows,
                )
                con.commit()
                log(symbol, "factors", "ok" if rows else "empty", len(rows))
            ok_k += 1
        except Exception as exc:
            err += 1
            log(symbol, "daily" if t["need_kline"] else "factors", "error", -1, f"{type(exc).__name__}: {exc}")
            print(f"[staging] {symbol} 失败: {exc}", flush=True)
        if i % 10 == 0 or i == len(tasks):
            print(f"[staging] {i}/{len(tasks)} (失败累计 {err})", flush=True)

    stats = con.execute(
        "select kind, status, count(*), coalesce(sum(rows),0) from staging_ts_fetch_log group by kind, status"
    ).fetchall()
    n_md = con.execute("select count(*), count(distinct symbol) from staging_ts_market_data").fetchone()
    n_fx = con.execute("select count(*), count(distinct symbol) from staging_ts_ex_factors").fetchone()
    print(f"\n[staging] 完成。fetch_log: {stats}", flush=True)
    print(f"[staging] market_data {n_md[0]} 行/{n_md[1]} 只; ex_factors {n_fx[0]} 行/{n_fx[1]} 只", flush=True)
    con.close()


if __name__ == "__main__":
    main()
