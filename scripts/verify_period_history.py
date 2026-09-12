"""周/月K 落库核对：vendor 周期 bar vs 本地日K 聚合（部署后验收用）。

思路：把本地日K（同一复权口径）按 ISO 周 / 自然月聚合出周期 bar ——
open=组内首日、high=max、low=min、close=组内末日、volume/amount=sum ——
再与库里 vendor 的周期 bar 逐根比对。两边独立来源，聚合一致即说明
vendor 周期数据与本地日K 自洽。

前几类对不齐**分类本身有意义**：
- 仅 vendor 有：本地日K 覆盖不到（日K 起得晚、或本地缺该段历史）；
- 仅本地有：该周期还没走完（库里按 core.bars 过滤了进行中的当期）；
- 首尾周期不参与比对：本地日K 覆盖区间的边界周期可能只有半周/半月
  （404 只标的的日K从 2020-01-02 周四起），聚合值天然短于 vendor 全周值；
- 同日期但数值不一致：真正的口径问题，会打印明细（重点看这类）。

用法：
    .venv/bin/python scripts/verify_period_history.py                    # 随机 30 只
    .venv/bin/python scripts/verify_period_history.py --sample 100
    .venv/bin/python scripts/verify_period_history.py --symbols 510300.SS,600036.SS
    .venv/bin/python scripts/verify_period_history.py --periods 1w --price-mode qfq
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parent))

import _common  # noqa: E402  # .env 加载（锚定项目根）+ DB_PATH

import pandas as pd  # noqa: E402

from core.bars import PERIOD_MONTHLY, PERIOD_WEEKLY, normalize_period  # noqa: E402
from data.storage.db import get_db, init_db  # noqa: E402

# 价格按绝对容差（复权价量级 0.01~1000）比对，不一致即判失败。
_PRICE_TOLERANCE = 1e-4
# 求和量（volume/amount）只做提示、不判失败：本地日K 的 amount 存的是四舍五入
# 到百元的值（实测 515630.SS 2026-08-03 日K存 28515700，vendor 为 28515743），
# 一周 5 天累计可达百元级误差（相对约 1e-6）。vendor 侧本身自洽——实测 vendor
# 周K amount 与 vendor 日K amount 求和**逐位相等**，差异全在本地日K 的取整上。
_SUM_RELATIVE_TOLERANCE = 1e-6
_OHLC_COLUMNS = ("open", "high", "low", "close")
_SUM_COLUMNS = ("volume", "amount")


def _period_keys(times: pd.Series, period: str) -> pd.Series:
    """每根日K 所属周期（ISO 年周 / 年月），与 core.bars 的口径一致。"""
    if period == PERIOD_WEEKLY:
        iso = times.dt.isocalendar()
        return pd.Series(list(zip(iso["year"].astype(int), iso["week"].astype(int))), index=times.index)
    return pd.Series(list(zip(times.dt.year, times.dt.month)), index=times.index)


def aggregate_daily(daily: pd.DataFrame, period: str) -> pd.DataFrame:
    """本地日K → 周期 bar（标注日 = 组内最后一个交易日）。

    **首尾两个周期会被丢弃**：本地日K 的覆盖区间本身可能就是「半截周期」——
    例如 404 只标的的日K从 2020-01-02（周四）起，那一周在本地只覆盖 2 天，
    聚合出来的 open/low 天然短于 vendor 按全周算的 bar；末尾则可能是进行中
    的当期。这两类属于「本地日K覆盖不全」，不参与取值比对（否则会把正常的
    vendor 数据误报成不一致，2026-09-12 首次核对时就踩过这个坑）。
    """
    if daily.empty:
        return pd.DataFrame()
    frame = daily.copy()
    frame["time"] = pd.to_datetime(frame["time"], errors="coerce")
    frame = frame.dropna(subset=["time"]).sort_values("time")
    if frame.empty:
        return pd.DataFrame()
    frame["_key"] = _period_keys(frame["time"], period)
    rows = []
    for _key, group in frame.groupby("_key", sort=True):
        rows.append(
            {
                "time": group["time"].max(),
                "open": float(group.iloc[0]["open"]),
                "high": float(group["high"].max()),
                "low": float(group["low"].min()),
                "close": float(group.iloc[-1]["close"]),
                "volume": float(group["volume"].sum()),
                "amount": float(group["amount"].sum()),
            }
        )
    result = pd.DataFrame(rows)
    if len(result) <= 2:
        return result.iloc[0:0]
    return result.iloc[1:-1].reset_index(drop=True)


def _boundary_periods(daily: pd.DataFrame, period: str) -> list[str]:
    """被 aggregate_daily 丢弃的首尾周期标注日（便于核对时心里有数）。"""
    frame = daily.copy()
    frame["time"] = pd.to_datetime(frame["time"], errors="coerce")
    frame = frame.dropna(subset=["time"]).sort_values("time")
    if frame.empty:
        return []
    keys = _period_keys(frame["time"], period)
    ordered = list(dict.fromkeys(keys.tolist()))
    if len(ordered) <= 2:
        return []
    boundary = {ordered[0], ordered[-1]}
    marks = frame[keys.isin(boundary)]["time"]
    return sorted({str(mark.date()) for mark in marks})


def compare_symbol(db, symbol: str, period: str, price_mode: str) -> dict:
    stored = db.load_market_data(symbol, price_mode=price_mode, period=period)
    daily = db.load_market_data(symbol, price_mode=price_mode)
    if stored.empty:
        return {"symbol": symbol, "status": "no_period_data"}
    if daily.empty:
        return {"symbol": symbol, "status": "no_daily_data", "stored": len(stored)}

    stored = stored.copy()
    stored["time"] = pd.to_datetime(stored["time"]).dt.normalize()
    local = aggregate_daily(daily, period)
    boundary = _boundary_periods(daily, period)
    if local.empty:
        return {"symbol": symbol, "status": "no_daily_data", "stored": len(stored)}
    local["time"] = pd.to_datetime(local["time"]).dt.normalize()

    merged = stored.merge(local, on="time", how="outer", suffixes=("_v", "_l"), indicator=True)
    both = merged[merged["_merge"] == "both"]
    mismatches: dict[str, int] = {}
    for column in _OHLC_COLUMNS:
        delta = (both[f"{column}_v"].astype(float) - both[f"{column}_l"].astype(float)).abs()
        if int((delta > _PRICE_TOLERANCE).sum()):
            mismatches[column] = int((delta > _PRICE_TOLERANCE).sum())
    # 求和量偏差只提示（原因见文件头注释）：取最大相对偏差，不判失败
    sum_notes: list[str] = []
    for column in _SUM_COLUMNS:
        left, right = both[f"{column}_v"].astype(float), both[f"{column}_l"].astype(float)
        threshold = right.abs() * _SUM_RELATIVE_TOLERANCE + _SUM_RELATIVE_TOLERANCE
        over = (left - right).abs() > threshold
        if int(over.sum()):
            worst = ((left - right).abs() / right.abs().clip(lower=1e-9)).max()
            sum_notes.append(f"{column} {int(over.sum())} 根（最大相对偏差 {worst:.2e}）")

    return {
        "symbol": symbol,
        "status": "compared" if not mismatches else "mismatch",
        "stored": len(stored),
        "local": len(local),
        "aligned": len(both),
        "boundary": boundary,
        "mismatched_columns": mismatches,
        "sum_notes": sum_notes,
        "vendor_only": merged.loc[merged["_merge"] == "left_only", "time"].dt.date.astype(str).tolist(),
        "local_only": merged.loc[merged["_merge"] == "right_only", "time"].dt.date.astype(str).tolist(),
        "detail": both if mismatches else None,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="周/月K 与本地日K 聚合的核对")
    parser.add_argument("--periods", default="1w,1M", help="周期，逗号分隔")
    parser.add_argument("--price-mode", default="qfq", choices=["qfq", "raw"], help="比对口径")
    parser.add_argument("--sample", type=int, default=30, help="随机抽样标的数（--symbols 优先）")
    parser.add_argument("--symbols", default=None, help="指定标的，逗号分隔")
    parser.add_argument("--seed", type=int, default=20260912, help="抽样随机种子（可复现）")
    parser.add_argument("--max-detail", type=int, default=20, help="数值不一致时打印多少根明细")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    init_db(_common.DB_PATH)
    db = get_db()
    periods = [normalize_period(p) for p in str(args.periods).split(",") if p.strip()]

    if args.symbols:
        symbols = [s.strip().upper() for s in str(args.symbols).split(",") if s.strip()]
    else:
        # 优先从有周期数据的标的里抽（否则抽到没补的会全被跳过）
        pool: list[str] = []
        for period in periods:
            pool.extend(db.list_market_symbols(price_mode=args.price_mode, period=period))
        pool = sorted(set(pool))
        if not pool:
            print("库内没有任何周期行情，先跑 scripts/backfill_period_history.py")
            return 2
        rng = random.Random(int(args.seed))
        symbols = rng.sample(pool, min(int(args.sample), len(pool)))

    print(f"核对 {len(symbols)} 只标的 × 周期 {','.join(periods)} × 口径 {args.price_mode}")
    failures: list[str] = []
    rounding_notes: list[str] = []
    for period in periods:
        stats = {"compared": 0, "mismatch": 0, "no_period_data": 0, "no_daily_data": 0}
        for symbol in symbols:
            result = compare_symbol(db, symbol, period, args.price_mode)
            stats[result["status"]] = stats.get(result["status"], 0) + 1
            if result.get("sum_notes"):
                rounding_notes.append(f"{period}/{symbol}: {'; '.join(result['sum_notes'])}")
            if result["status"] == "mismatch":
                failures.append(f"{period}/{symbol}")
                print(f"\n[不一致] {period} {symbol}：库内 {result['stored']} 根 / "
                      f"本地聚合 {result['local']} 根 / 对齐 {result['aligned']} 根；"
                      f"差异列 {result['mismatched_columns']}")
                print(f"  仅 vendor：{result['vendor_only'][:8]}")
                print(f"  仅本地：{result['local_only'][:8]}")
                if result["detail"] is not None:
                    columns = ["time"] + [f"{c}_{s}" for c in _OHLC_COLUMNS for s in ("v", "l")]
                    print(result["detail"].head(int(args.max_detail))[columns].to_string(index=False))
        print(
            f"[{period}] 一致 {stats['compared']} / 价格不一致 {stats['mismatch']}"
            f" / 库内无周期数据 {stats['no_period_data']} / 无日K {stats['no_daily_data']}"
        )

    if rounding_notes:
        print(f"\n提示：{len(rounding_notes)} 只标的的求和量（volume/amount）有微差，"
              f"源于本地日K 的 amount 被四舍五入到百元（不判失败）：")
        for note in rounding_notes[:10]:
            print(f"  {note}")
        if len(rounding_notes) > 10:
            print(f"  …另有 {len(rounding_notes) - 10} 只")

    if failures:
        print(f"\n结论：{len(failures)} 组价格存在不一致，需排查：{failures[:20]}")
        return 1
    print("\n结论：抽样标的在「vendor 周期 bar == 本地日K 聚合」上价格全部一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
