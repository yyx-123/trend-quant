"""Tushare 字段映射验证：取数能否填满现有 market_data_raw / ex_factors 表结构。

只读测试 —— 对几只代表标的各发几次 API 调用，与库内现有 tickflow 数据逐字段对照。
不批量下载、不写库、不落盘。

用法:
    TUSHARE_TOKEN=xxx [TUSHARE_HTTP_URL=https://tuaremax.top] \
        .venv/bin/python docs/26-09-23-历史龙头数据补齐/test_tushare_field_mapping.py
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from core.symbols import from_vendor_symbol  # noqa: E402
from tushare_common import call_with_retry, get_pro_api  # noqa: E402

DB = ROOT / "data" / "trend_quant.db"


def iso(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"


def api(pro, name: str, **kw):
    return call_with_retry(getattr(pro, name), **kw)


def cmp_kline(pro, con, ts_code: str, api_name: str = "daily") -> None:
    """tushare 最近几根日线 vs 库内同日 tickflow 行：价格一致性 + 量/额单位换算验证。"""
    df = api(pro, api_name, ts_code=ts_code)
    if df is None or not len(df):
        print(f"  {ts_code} {api_name}: 0 行")
        return
    df = df.sort_values("trade_date")
    symbol = from_vendor_symbol(ts_code)
    print(f"  {ts_code} {api_name}: 共 {len(df)} 行(单次上限6000), {df['trade_date'].min()} ~ {df['trade_date'].max()}")
    print(f"  字段: {sorted(df.columns)}")
    for _, r in df.tail(3).iterrows():
        d = iso(r["trade_date"])
        row = con.execute(
            "select close, volume, amount from market_data_raw where symbol=? and substr(time,1,10)=?",
            (symbol, d),
        ).fetchone()
        if row:
            close_l, vol_l, amt_l = row
            vol_ratio = (vol_l / r["vol"]) if r["vol"] else float("nan")
            amt_ratio = (amt_l / (r["amount"] * 1000)) if r["amount"] else float("nan")
            print(
                f"    {d}: close {r['close']} vs 库 {close_l} | "
                f"量单位比值(库/tushare)={vol_ratio:.4f} | 额×1000比值={amt_ratio:.4f}"
            )
        else:
            print(f"    {d}: close={r['close']} vol={r['vol']} amount={r['amount']} (库内无同日行)")


def per_event_factors(adj, shift_days: int) -> list[tuple[str, float]]:
    """tushare 累积 adj_factor → 项目语义逐次因子 f=adj(t)/adj(t-1)，事件日可平移。"""
    adj = adj.sort_values("trade_date").reset_index(drop=True)
    out, prev = [], None
    for _, r in adj.iterrows():
        cur = float(r["adj_factor"])
        if prev is not None and prev > 0:
            ratio = cur / prev
            if abs(ratio - 1.0) > 1e-9:
                d = datetime.strptime(r["trade_date"], "%Y%m%d") + timedelta(days=shift_days)
                out.append((d.strftime("%Y%m%d"), ratio))
        prev = cur
    return out


def cmp_factors(pro, con, ts_code: str) -> None:
    """tushare adj_factor 换算(事件日-1天) vs 库内 tickflow ex_factors。"""
    adj = api(pro, "adj_factor", ts_code=ts_code)
    if adj is None or not len(adj):
        print(f"  {ts_code} adj_factor: 0 行")
        return
    symbol = from_vendor_symbol(ts_code)
    local = {
        d.replace("-", ""): f
        for d, f in con.execute("select substr(time,1,10), factor from ex_factors where symbol=?", (symbol,))
    }
    for shift in (0, -1):
        ts_map = dict(per_event_factors(adj, shift))
        both = sorted(set(ts_map) & set(local))
        max_diff = max((abs(ts_map[d] - local[d]) for d in both), default=0.0)
        print(
            f"  {ts_code} 事件日平移{shift:+d}天: tushare {len(ts_map)} 事件 vs 库 {len(local)} 条 | "
            f"日期交集 {len(both)} | 交集因子值最大偏差 {max_diff:.3g}"
        )
        if shift == -1 and both:
            for d in both[-2:]:
                print(f"    {d}: tushare={ts_map[d]:.6f} vs 库={local[d]:.6f}")


def qfq_end_to_end(pro, con, ts_code: str) -> None:
    """终极验证：tushare raw + 换算因子 本地物化 qfq，与库内 market_data_qfq 全重叠段比对。"""
    import pandas as pd
    from core.adjustment import compute_qfq

    symbol = from_vendor_symbol(ts_code)
    raw_ts = api(pro, "daily", ts_code=ts_code).sort_values("trade_date")
    adj = api(pro, "adj_factor", ts_code=ts_code)
    factors = [(iso(d), f) for d, f in per_event_factors(adj, shift_days=-1)]
    df = raw_ts.rename(columns={"vol": "volume"})[["trade_date", "open", "high", "low", "close", "volume", "amount"]]
    df = df.assign(time=pd.to_datetime(df["trade_date"], format="%Y%m%d")).drop(columns="trade_date")
    qfq = compute_qfq(df, factors)

    rows = con.execute(
        "select substr(time,1,10), close from market_data_qfq where symbol=?", (symbol,)
    ).fetchall()
    local = dict(rows)
    qfq["d"] = qfq["time"].dt.strftime("%Y-%m-%d")
    both = qfq[qfq["d"].isin(local.keys())]
    if not len(both):
        print(f"  {symbol}: market_data_qfq 无重叠行")
        return
    diffs = [(abs(r["close"] - local[r["d"]]) / max(local[r["d"]], 1e-9)) for _, r in both.iterrows()]
    diffs.sort()
    n = len(diffs)
    print(
        f"  {symbol}: qfq 重叠 {n} 天 | 收盘相对偏差 中位={diffs[n//2]:.3g} "
        f"p99={diffs[int(n*0.99)]:.3g} 最大={diffs[-1]:.3g}"
    )


def main() -> None:
    pro = get_pro_api()
    pro._DataApi__timeout = 90  # 镜像站偶发慢响应
    con = sqlite3.connect(str(DB))
    print(f"[db] {DB}（只读对照）\n")

    print("=== 1. 在市股票行情对照: 600519.SH daily vs market_data_raw ===", flush=True)
    cmp_kline(pro, con, "600519.SH")

    print("\n=== 2. 复权因子换算对照(事件日平移0/-1天) ===", flush=True)
    cmp_factors(pro, con, "600519.SH")
    cmp_factors(pro, con, "688549.SH")

    print("\n=== 3. qfq 端到端重建验证: 600519.SH ===", flush=True)
    qfq_end_to_end(pro, con, "600519.SH")

    print("\n=== 4. 退市股全历史: 300104.SZ 乐视退 ===", flush=True)
    cmp_kline(pro, con, "300104.SZ")
    adj = api(pro, "adj_factor", ts_code="300104.SZ")
    print(f"  adj_factor: {len(adj)} 行, {len(per_event_factors(adj, -1))} 个除权事件" if adj is not None else "  adj_factor: 失败")

    print("\n=== 5. ETF: 510300.SH fund_daily vs 库 ===", flush=True)
    cmp_kline(pro, con, "510300.SH", "fund_daily")

    print("\n=== 6. ETF 复权因子接口(510880.SH 红利ETF 有分红) ===", flush=True)
    n_db = con.execute("select count(*) from ex_factors where symbol='510880.SS'").fetchone()[0]
    print(f"  库内 510880.SS ex_factors: {n_db} 条")
    try:
        fa = api(pro, "fund_adj", ts_code="510880.SH")
        print(f"  fund_adj: {len(fa)} 行" if fa is not None else "  fund_adj: 返回None")
        if fa is not None and len(fa):
            print(f"  字段: {sorted(fa.columns)}, 除权事件 {len(per_event_factors(fa, -1))} 个")
    except AttributeError:
        print("  fund_adj: 此 tushare 版本无该接口")
    except Exception as exc:
        print(f"  fund_adj: 调用失败 {exc}")

    con.close()
    print("\n[完成] 量单位比≈1、额×1000比≈1、因子平移-1天后日期全交集且偏差≈0、qfq偏差≈0 → 字段可填满")


if __name__ == "__main__":
    main()
