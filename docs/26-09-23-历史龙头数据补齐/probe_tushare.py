"""Tushare 连通性与能力探测（阶段一前置检查）。

token / 镜像站地址通过环境变量注入，脚本内不出现、不落盘：
    TUSHARE_TOKEN=xxx TUSHARE_HTTP_URL=https://tuaremax.top .venv/bin/python probe_tushare.py

检查项：
  1. stock_basic(list_status='D') 退市股名单是否可调
  2. index_weight 对 000300 / 000905 / 000852 是否可调，各自最早可回溯到哪一天
  3. 退市股是否能取到日线（daily / adj_factor），确认"Tushare 覆盖退市股行情"
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


def main() -> None:
    pro = get_pro_api()
    print(f"[probe] 镜像站覆盖: {bool(os.getenv('TUSHARE_HTTP_URL'))}", flush=True)

    print("\n=== 1. 退市股名单 stock_basic(list_status='D') ===", flush=True)
    try:
        df = pro.stock_basic(
            list_status="D", fields="ts_code,symbol,name,list_date,delist_date"
        )
        print(f"  行数: {len(df)}")
        if len(df):
            print(f"  退市日期区间: {df['delist_date'].min()} ~ {df['delist_date'].max()}")
            print("  样例:")
            print(df.head(5).to_string(index=False))
            df.to_csv(DATA_DIR / "tushare_delisted.csv", index=False)
            print(f"  已存 -> {DATA_DIR / 'tushare_delisted.csv'}")
    except Exception as exc:
        print(f"  失败: {exc}")

    print("\n=== 2. index_weight 各指数最早可回溯日期 ===", flush=True)
    for code in ("000300.SH", "000905.SH", "000852.SH", "000906.SH", "399300.SZ"):
        for span in (("20050101", "20071231"), ("20080101", "20141231")):
            try:
                df = pro.index_weight(
                    index_code=code, start_date=span[0], end_date=span[1]
                )
            except Exception as exc:
                print(f"  {code} {span}: 失败 {exc}")
                break
            if df is None or not len(df):
                print(f"  {code} {span[0][:4]}-{span[1][:4]}: 0 行")
                continue
            print(
                f"  {code} {span[0][:4]}-{span[1][:4]}: {len(df)} 行, "
                f"{df['trade_date'].min()} ~ {df['trade_date'].max()}, "
                f"{df['con_code'].nunique()} 只"
            )
            break

    print("\n=== 3. 退市股日线是否可取 ===", flush=True)
    for code, tag in (("300104.SZ", "乐视退(2020退)"), ("600005.SH", "武钢股份(2017退)")):
        try:
            d = pro.daily(ts_code=code)
            a = pro.adj_factor(ts_code=code)
            span = (
                f"{d['trade_date'].min()} ~ {d['trade_date'].max()}" if len(d) else "—"
            )
            print(f"  {code} {tag}: daily {len(d)} 行 ({span}), adj_factor {len(a)} 行")
        except Exception as exc:
            print(f"  {code} {tag}: 失败 {exc}")


if __name__ == "__main__":
    main()
