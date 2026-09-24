"""阶段一（ETF）：用 Tushare 抓全部 ETF 的直接信息 + 逐年日均成交额。

不写数据库任何表，只落 CSV。token 走环境变量：
    TUSHARE_TOKEN=xxx TUSHARE_HTTP_URL=https://tuaremax.top .venv/bin/python fetch_tushare_etf.py

产出（data/）：
  tushare_etf_basic.csv       全部场内基金（含已摘牌），带 benchmark/invest_type/费率
  tushare_etf_daily_agg.csv   逐年成交额聚合（按 ts_code 增量追加，可断点续跑）
  tushare_etf_share.csv       最新交易日的份额 + 收盘价快照（一次调用取全市场）
  etf_universe.csv            逐标的总表（含跟踪主题、规模、是否主题代表）
  etf_by_year.csv             逐年明细（长表，便于换阈值重筛）
  etf_theme_representatives.csv  每个跟踪主题选出的代表
  etf_stats.json              汇总

口径：
  跟踪主题 = fund_basic.benchmark（业绩比较基准）切掉「收益率/×95%」等修饰后的主指数名
  当年日均成交额(亿元) = Σamount(千元) × 1000 / 当年有行情的交易日数 / 1e8
  规模(亿元) = 份额(万份) × 10000 × 最新收盘价 / 1e8
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from tushare_common import call_with_retry, get_pro_api

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

BASIC_PATH = DATA_DIR / "tushare_etf_basic.csv"
AGG_PATH = DATA_DIR / "tushare_etf_daily_agg.csv"
SNAPSHOT_PATH = DATA_DIR / "tushare_etf_share.csv"

REPORT_FIRST_YEAR = 2013
THRESHOLD_YI = 0.5   # 5000 万元/日
MIN_BARS = 60        # 当年至少 60 个交易日，避免次新/摘牌干扰
CALL_INTERVAL = 0.22
AGG_FIELDS = ["symbol", "year", "bars", "avg_daily_amount_yi", "total_amount_yi"]

# benchmark 里「收益率」「×95%」这类修饰一律切掉，只留主指数名
BM_SPLIT = re.compile(r"收益率|收益|×|＋|\+|\*|\(|（")
FUND_COMPANY = re.compile(r"(基金|资产管理|证券)$")


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _write(rows: list[dict], path: Path) -> None:
    if not rows:
        print(f"[etf] skip empty {path.name}")
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"[etf] {len(rows)} rows -> {path.name}")


def is_etf(name: str) -> bool:
    return "ETF" in str(name or "").upper()


def theme_of(benchmark: str, name: str) -> str:
    """跟踪主题：优先用 benchmark，缺失时退回 ETF 名称（剥掉基金公司后缀）。

    benchmark 有很多写法（「沪深300指数收益率×100%」/「活期存款利率(税后)×5%+
    中证1000指数×95%」），所以要切开后挑出真正的指数片段，不能只取第一段。
    """
    bm = str(benchmark or "").strip()
    if bm:
        parts = [p.strip() for p in BM_SPLIT.split(bm) if p.strip()]
        indexed = [p for p in parts if "指数" in p]
        main = max(indexed, key=len) if indexed else (max(parts, key=len) if parts else "")
        main = main.removesuffix("指数")
        main = main.strip().strip(")）(").strip()
        if main:
            return main
    s = str(name or "").strip()
    s = re.sub(r"ETF|基金|交易型开放式指数", "", s)
    for token in ("华泰柏瑞", "易方达", "华夏", "南方", "嘉实", "广发", "富国", "汇添富",
                  "招商", "华宝", "天弘", "银华", "工银瑞信", "建信", "鹏华", "平安",
                  "大成", "景顺长城", "中欧", "华安", "国泰", "博时", "万家", "华富",
                  "国联安", "海富通", "中金", "兴业", "民生加银", "中银", "泰康", "新华",
                  "永赢", "国金", "前海开源", "创金合信", "西部利得", "汇安", "中航",
                  "华商", "诺安", "光大", "东吴", "金鹰", "长城", "长盛", "宝盈", "融通"):
        s = s.replace(token, "")
    return s.strip() or name


def latest_trade_date(pro) -> str:
    end = dt.date.today().strftime("%Y%m%d")
    df = call_with_retry(pro.trade_cal, exchange="SSE", start_date="20260801", end_date=end)
    days = sorted(d for d in df[df["is_open"] == 1]["cal_date"].tolist() if d <= end)
    return days[-1]


# ------------------------------------------------------------------ 抓取


def fetch_basic(pro) -> list[dict]:
    if BASIC_PATH.exists():
        rows = _read_csv(BASIC_PATH)
    else:
        rows = []
        for status in ("L", "D"):
            df = call_with_retry(pro.fund_basic, market="E", status=status)
            for _, r in df.iterrows():
                rows.append(
                    {k: ("" if v is None else v) for k, v in r.items()} | {"list_status": status}
                )
            time.sleep(CALL_INTERVAL)
        _write(rows, BASIC_PATH)
    etfs = [r for r in rows if is_etf(r["name"])]
    print(f"[etf] 场内基金 {len(rows)} 条 → 其中 ETF {len(etfs)} 只（含摘牌）", flush=True)
    return etfs


def fetch_daily_agg(pro, etfs: list[dict]) -> None:
    done: set[str] = {r["symbol"] for r in _read_csv(AGG_PATH)}
    todo = [r for r in etfs if r["ts_code"] not in done]
    print(f"[etf] 成交额：已聚合 {len(done)} 只，待拉 {len(todo)} 只", flush=True)
    if not todo:
        return

    header_needed = not AGG_PATH.exists() or AGG_PATH.stat().st_size == 0
    fh = AGG_PATH.open("a", newline="", encoding="utf-8")
    writer = csv.DictWriter(fh, fieldnames=AGG_FIELDS)
    if header_needed:
        writer.writeheader()
    failures = 0
    try:
        for idx, r in enumerate(todo, start=1):
            code = r["ts_code"]
            try:
                df = call_with_retry(pro.fund_daily, ts_code=code)
                if df is not None and len(df) >= 5000:  # 命中单次上限 → 分两段重取
                    df = pd.concat(
                        [
                            call_with_retry(
                                pro.fund_daily, ts_code=code, start_date="19000101", end_date="20151231"
                            ),
                            call_with_retry(
                                pro.fund_daily, ts_code=code, start_date="20160101", end_date="20261231"
                            ),
                        ],
                        ignore_index=True,
                    )
            except Exception as exc:
                failures += 1
                print(f"[etf] {code} 失败: {exc}", flush=True)
                time.sleep(CALL_INTERVAL)
                continue
            if df is not None and len(df):
                per_year: dict[int, list[float]] = {}
                for _, row in df.iterrows():
                    amt = row.get("amount")
                    if amt is None or pd.isna(amt):
                        continue
                    bucket = per_year.setdefault(int(str(row["trade_date"])[:4]), [0, 0.0])
                    bucket[0] += 1
                    bucket[1] += float(amt)
                for year, (bars, total) in sorted(per_year.items()):
                    writer.writerow(
                        {
                            "symbol": code,
                            "year": year,
                            "bars": bars,
                            "avg_daily_amount_yi": round(total * 1000 / bars / 1e8, 4),
                            "total_amount_yi": round(total * 1000 / 1e8, 2),
                        }
                    )
            if idx % 100 == 0:
                fh.flush()
                print(f"[etf] {idx}/{len(todo)} 已处理", flush=True)
            time.sleep(CALL_INTERVAL)
    finally:
        fh.flush()
        fh.close()
    print(f"[etf] 成交额聚合完成（本次失败 {failures} 只，重跑可续）", flush=True)


def snapshot_dates(pro) -> list[str]:
    """候选快照日：最近几个月的最后一个交易日（份额数据 T+1~T+2 才补齐，最新日常常不全）。"""
    end = dt.date.today().strftime("%Y%m%d")
    df = call_with_retry(pro.trade_cal, exchange="SSE", start_date="20260101", end_date=end)
    days = sorted(d for d in df[df["is_open"] == 1]["cal_date"].tolist() if d <= end)
    last_of_month: dict[str, str] = {}
    for d in days:
        last_of_month[d[:6]] = d
    out = list(reversed(list(last_of_month.values())))
    if days and days[-1] not in out:
        out.insert(0, days[-1])
    return out


def fetch_snapshot(pro, etf_count: int) -> dict[str, dict]:
    """份额 + 收盘价快照（各一次调用取全市场），挑份额覆盖够的那天。"""
    if SNAPSHOT_PATH.exists():
        rows = _read_csv(SNAPSHOT_PATH)
        if rows:
            return {r["symbol"]: r for r in rows}
    need = max(1000, int(etf_count * 0.75))
    for date in snapshot_dates(pro):
        try:
            share = call_with_retry(pro.fund_share, trade_date=date)
        except Exception as exc:
            print(f"[etf] 份额 {date} 失败: {exc}", flush=True)
            continue
        n = 0 if share is None else len(share)
        print(f"[etf] 份额 {date}: {n} 条（需要 {need}）", flush=True)
        if n < need:
            time.sleep(CALL_INTERVAL)
            continue
        time.sleep(CALL_INTERVAL)
        daily = call_with_retry(pro.fund_daily, trade_date=date)
        close = {r["ts_code"]: r["close"] for _, r in daily.iterrows()}
        rows = [
            {
                "symbol": r["ts_code"],
                "trade_date": str(r["trade_date"]),
                "fd_share": r["fd_share"],
                "close": close.get(r["ts_code"], ""),
            }
            for _, r in share.iterrows()
        ]
        _write(rows, SNAPSHOT_PATH)
        print(f"[etf] 快照 {date}: 份额 {len(rows)} 条 / 收盘价 {len(close)} 条", flush=True)
        return {r["symbol"]: r for r in rows}
    print("[etf] 份额快照取不到，跳过规模列", flush=True)
    return {}


# ------------------------------------------------------------------ 构建


def build(etfs: list[dict], snapshot: dict[str, dict]) -> dict:
    meta = {r["ts_code"]: r for r in etfs}
    agg: dict[str, list[dict]] = defaultdict(list)
    for r in _read_csv(AGG_PATH):
        agg[r["symbol"]].append(r)

    by_year: list[dict] = []
    universe: list[dict] = []
    for code, rows in agg.items():
        m = meta.get(code, {})
        theme = theme_of(m.get("benchmark", ""), m.get("name", ""))
        yearly = [
            {"year": int(r["year"]), "bars": int(r["bars"]), "amount_yi": float(r["avg_daily_amount_yi"])}
            for r in sorted(rows, key=lambda x: int(x["year"]))
            if int(r["year"]) >= REPORT_FIRST_YEAR and int(r["bars"]) >= MIN_BARS
        ]
        if not yearly:
            continue
        for y in yearly:
            by_year.append(
                {
                    "symbol": code,
                    "name": m.get("name", ""),
                    "theme": theme,
                    "year": y["year"],
                    "bars": y["bars"],
                    "avg_daily_amount_yi": y["amount_yi"],
                    "qualifies": "Y" if y["amount_yi"] >= THRESHOLD_YI else "",
                }
            )
        qualifying = [y for y in yearly if y["amount_yi"] >= THRESHOLD_YI]
        peak = max(yearly, key=lambda y: y["amount_yi"])
        latest = yearly[-1]
        snap = snapshot.get(code) or {}
        scale_yi = ""
        # 实测交叉核对：非货币 ETF 的 fd_share 与 TickFlow total_shares 一致（偏差 1~8%），
        # 货币型 ETF 两者差约 100 倍（Tushare 份额口径异常），故货币型不给规模。
        scale_reliable = "Y" if str(m.get("invest_type", "")) != "货币型" else "N"
        if scale_reliable == "Y" and snap.get("fd_share") and snap.get("close"):
            try:
                scale_yi = round(float(snap["fd_share"]) * 10000 * float(snap["close"]) / 1e8, 2)
            except (TypeError, ValueError):
                scale_yi = ""
        universe.append(
            {
                "symbol": code,
                "name": m.get("name", ""),
                "theme": theme,
                "benchmark": m.get("benchmark", ""),
                "invest_type": m.get("invest_type", ""),
                "management": m.get("management", ""),
                "list_date": m.get("list_date", ""),
                "delist_date": m.get("delist_date", ""),
                "list_status": m.get("list_status", ""),
                "m_fee": m.get("m_fee", ""),
                "c_fee": m.get("c_fee", ""),
                "scale_yi": scale_yi,
                "scale_reliable": scale_reliable,
                "n_qualify_years": len(qualifying),
                "first_qualify_year": qualifying[0]["year"] if qualifying else "",
                "last_qualify_year": qualifying[-1]["year"] if qualifying else "",
                "peak_year": peak["year"],
                "peak_avg_daily_yi": peak["amount_yi"],
                "latest_year": latest["year"],
                "latest_avg_daily_yi": latest["amount_yi"],
            }
        )

    best: dict[str, str] = {}
    for r in sorted(universe, key=lambda r: -float(r["latest_avg_daily_yi"])):
        best.setdefault(r["theme"], r["symbol"])
    for r in universe:
        r["theme_representative"] = "Y" if best.get(r["theme"]) == r["symbol"] else ""

    universe.sort(
        key=lambda r: (r["theme_representative"] != "Y", -float(r["peak_avg_daily_yi"]))
    )
    _write(universe, DATA_DIR / "etf_universe.csv")
    by_year.sort(key=lambda r: (r["symbol"], r["year"]))
    _write(by_year, DATA_DIR / "etf_by_year.csv")
    reps = sorted(
        (r for r in universe if r["theme_representative"] == "Y"),
        key=lambda r: -float(r["latest_avg_daily_yi"]),
    )
    _write(reps, DATA_DIR / "etf_theme_representatives.csv")

    # 最终候选清单：排除货币型 + 每个跟踪主题只留一只（组内近一年日均最大者）
    eligible = [r for r in universe if r["invest_type"] != "货币型" and r["n_qualify_years"]]
    best: dict[str, dict] = {}
    for r in sorted(eligible, key=lambda r: -float(r["latest_avg_daily_yi"])):
        best.setdefault(r["theme"], r)
    candidates = sorted(best.values(), key=lambda r: -float(r["latest_avg_daily_yi"]))
    _write(candidates, DATA_DIR / "etf_candidates.csv")

    stats = {
        "etf_scanned": len(etfs),
        "etf_with_history": len(universe),
        "etf_qualify_5000w": sum(1 for r in universe if r["n_qualify_years"]),
        "themes_total": len({r["theme"] for r in universe}),
        "themes_with_qualify": len({r["theme"] for r in universe if r["n_qualify_years"]}),
        "theme_representatives": len(reps),
        "delisted_etf": sum(1 for r in universe if r["list_status"] == "D"),
        "delisted_etf_qualify": sum(
            1 for r in universe if r["list_status"] == "D" and r["n_qualify_years"]
        ),
        "threshold_yi": THRESHOLD_YI,
        "report_first_year": REPORT_FIRST_YEAR,
    }
    (DATA_DIR / "etf_stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2))
    return stats


def main() -> None:
    pro = get_pro_api()
    etfs = fetch_basic(pro)
    fetch_daily_agg(pro, etfs)
    date = latest_trade_date(pro)
    print(f"[etf] 最新交易日: {date}", flush=True)
    snapshot = fetch_snapshot(pro, len(etfs))
    print(json.dumps(build(etfs, snapshot), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
