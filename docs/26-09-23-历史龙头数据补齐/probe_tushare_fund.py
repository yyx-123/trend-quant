"""Tushare 基金/ETF 接口能力探测。

    TUSHARE_TOKEN=xxx TUSHARE_HTTP_URL=https://tuaremax.top .venv/bin/python probe_tushare_fund.py

关注：有没有「直接给」的信息，而不是靠 K 线自己算。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from tushare_common import get_pro_api

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def show(tag: str, df, cols: list[str] | None = None) -> None:
    if df is None or not len(df):
        print(f"{tag}: 0 行")
        return
    print(f"{tag}: {len(df)} 行 | 字段: {list(df.columns)}")
    if cols:
        cols = [c for c in cols if c in df.columns]
        print(df[cols].head(5).to_string(index=False))


def main() -> None:
    pro = get_pro_api()
    print(f"[probe] 镜像站: {bool(os.getenv('TUSHARE_HTTP_URL'))}\n", flush=True)

    print("=== 1. fund_basic 场内基金全量（存续） ===", flush=True)
    try:
        f = pro.fund_basic(market="E")
        show("fund_basic(market='E')", f)
        print("  fund_type 分布:", f["fund_type"].value_counts().to_dict() if "fund_type" in f else "-")
        if "invest_type" in f:
            print("  invest_type 分布:", f["invest_type"].value_counts().to_dict())
        f.to_csv(DATA_DIR / "tushare_fund_basic_E.csv", index=False)
    except Exception as exc:
        print("  失败:", exc)

    print("\n=== 2. fund_basic 已退市/摘牌的场内基金 ===", flush=True)
    for status in ("D",):
        try:
            d = pro.fund_basic(market="E", status=status)
            show(f"fund_basic(market='E', status='{status}')", d, ["ts_code", "name", "list_date", "delist_date", "fund_type"])
            if len(d):
                d.to_csv(DATA_DIR / "tushare_fund_basic_E_delisted.csv", index=False)
        except Exception as exc:
            print(f"  status={status} 失败:", exc)

    print("\n=== 3. 基准字段（跟踪指数）是否可用 ===", flush=True)
    try:
        f = pro.fund_basic(market="E")
        if "benchmark" in f.columns:
            hit = f[f["name"].astype(str).str.contains("沪深300", na=False)]
            print(f"  名称含「沪深300」的场内基金 {len(hit)} 只，benchmark 去重值：")
            for v in hit["benchmark"].dropna().unique()[:6]:
                print("   ", str(v)[:70])
            hit2 = f[f["name"].astype(str).str.contains("中证1000", na=False)]
            print(f"  名称含「中证1000」的 {len(hit2)} 只，benchmark 去重值：")
            for v in hit2["benchmark"].dropna().unique()[:6]:
                print("   ", str(v)[:70])
    except Exception as exc:
        print("  失败:", exc)

    print("\n=== 4. fund_daily 成交额 / fund_share 份额 / fund_nav 净值 ===", flush=True)
    for fn, kwargs, tag in (
        (pro.fund_daily, {"ts_code": "510300.SH", "start_date": "20250101", "end_date": "20250115"}, "fund_daily"),
        (pro.fund_share, {"ts_code": "510300.SH", "start_date": "20250101", "end_date": "20250115"}, "fund_share"),
        (pro.fund_nav, {"ts_code": "510300.SH", "start_date": "20250101", "end_date": "20250115"}, "fund_nav"),
        (pro.fund_adj, {"ts_code": "510300.SH"}, "fund_adj"),
    ):
        try:
            df = fn(**kwargs)
            show(tag, df)
        except Exception as exc:
            print(f"  {tag} 失败:", exc)

    print("\n=== 5. 退市 ETF 的日线能否取到 ===", flush=True)
    try:
        d = pro.fund_basic(market="E", status="D")
        if len(d):
            code = d.iloc[0]["ts_code"]
            daily = pro.fund_daily(ts_code=code)
            print(f"  {code} {d.iloc[0]['name']}: fund_daily {len(daily)} 行")
    except Exception as exc:
        print("  失败:", exc)


if __name__ == "__main__":
    main()
