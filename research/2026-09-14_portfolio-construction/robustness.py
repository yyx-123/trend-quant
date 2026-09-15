"""稳健性检验：结论到底是「真信号」还是「假设/时代造成的假象」。

三项检验（都是针对选股边际的，因为它才是决策依据）：

【检验一】滑点假设敏感性
  引擎默认 slippage=0.002（单边 0.2%）。对一只 1.000 元的宽基 ETF，
  真实最小变动价位 0.001 元即 0.1%，实际盘口滑点约 0.03%。
  固定 1.5×ATR 止损下，低 ATR 标的的止损距离只有 2%~3%，0.4% 的往返
  滑点占了止损距离的 15%~20%，而高 ATR 标的只占 5%。
  → 若「低 ATR 标的吃亏」在低滑点下消失，那它是假设的产物，不是市场规律。

【检验二】按交易日分块自助法
  同日候选的收益共享同一个市场走势，不能按"笔"当独立样本。
  按交易日重采样给出边际的置信区间 —— 这是唯一诚实的显著性口径。

【检验三】分半/分年稳定性
  边际是不是只由 2024-09 那波行情贡献。

输出: data/robustness_slippage.csv, data/robustness_bootstrap.csv
"""
from __future__ import annotations

import warnings
from pathlib import Path

import numpy as np
import pandas as pd

import trade_sim

warnings.filterwarnings("ignore")

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

FEATURES = ["trend_score", "price_direction", "er", "confidence", "bias_atr",
            "atr_pct", "ret20", "macd_hist_norm", "days_in_golden"]
TOPK = 3
MIN_CANDIDATES = 10
N_BOOT = 2000
SPLIT = pd.Timestamp("2021-01-01")


# ------------------------------------------------------------------ 检验一
def recollect(panel: pd.DataFrame, kind: str, slippage: float, cap: int = 250) -> pd.DataFrame:
    """按给定滑点重新收集入场样本。"""
    d = panel[panel["ready"]].copy()
    rows: list[dict] = []
    for symbol, sub in d.groupby("symbol", sort=False):
        sub = sub.reset_index(drop=True)
        idxs = np.flatnonzero(sub[kind].to_numpy())
        if len(idxs) == 0:
            continue
        if len(idxs) > cap:
            idxs = idxs[:: max(len(idxs) // cap, 1)]
        arr = trade_sim.SymbolArrays(
            symbol=symbol,
            date=sub["date"].to_numpy(dtype="datetime64[ns]"),
            open=sub["open"].to_numpy(dtype=float),
            high=sub["high"].to_numpy(dtype=float),
            low=sub["low"].to_numpy(dtype=float),
            close=sub["close"].to_numpy(dtype=float),
            atr=sub["atr"].to_numpy(dtype=float),
            asset_type=str(sub["asset_type"].iloc[0] or "etf"),
            features={c: sub[c].to_numpy(dtype=float) for c in FEATURES},
        )
        for i in idxs:
            r = trade_sim.simulate_entry(arr, int(i), slippage=slippage)
            if r is None:
                continue
            rows.append({"symbol": symbol, "date": sub["date"].iloc[i],
                         "asset_type": arr.asset_type,
                         "ret_net_pct": r.ret_net_pct,
                         "atr_pct": r.features["atr_pct"],
                         **{c: r.features[c] for c in FEATURES}})
    out = pd.DataFrame(rows)
    out["date"] = pd.to_datetime(out["date"])
    return out


def daily_edge(d: pd.DataFrame, feat: str, k: int = TOPK) -> pd.DataFrame:
    recs = []
    for day, g in d.groupby("date", sort=True):
        sub = g[[feat, "ret_net_pct"]].dropna()
        if len(sub) < MIN_CANDIDATES or sub[feat].nunique() < 5:
            continue
        m = float(sub["ret_net_pct"].mean())
        recs.append({"date": day, "n": len(sub),
                     "top": float(sub.sort_values(feat, ascending=False)["ret_net_pct"].head(k).mean()) - m})
    return pd.DataFrame(recs)


def test_slippage(panel: pd.DataFrame) -> pd.DataFrame:
    print("\n" + "=" * 96)
    print("【检验一】滑点假设敏感性（event 口径，目标 as-traded 净收益）")
    print("每日取特征最强 3 名相对当日平均的边际（pp，日等权）")
    print(f"\n{'特征':16s}" + "".join(f"{'滑点' + s:>14s}" for s in ("0.05%", "0.20%", "0.30%")))
    rows = []
    cache: dict[float, pd.DataFrame] = {}
    for slip, label in ((0.0005, "0.05%"), (0.002, "0.20%"), (0.003, "0.30%")):
        cache[slip] = recollect(panel, "is_event", slip)
        print(f"  （滑点 {label} 收集 {len(cache[slip]):,} 笔）", flush=True)
    for feat in FEATURES:
        vals = []
        for slip in (0.0005, 0.002, 0.003):
            rec = daily_edge(cache[slip], feat)
            vals.append(float(rec["top"].mean()) if not rec.empty else np.nan)
            if slip == 0.002:
                rows.append({"feature": feat, "slip_low": vals[0], "slip_mid": vals[1],
                             "slip_high": np.nan})
        print(f"{feat:16s}" + "".join(f"{v * 100:>+13.3f}p" for v in vals))
    out = pd.DataFrame(rows)
    # 补 high
    highs = []
    for feat in FEATURES:
        rec = daily_edge(cache[0.003], feat)
        highs.append(float(rec["top"].mean()) if not rec.empty else np.nan)
    out["slip_high"] = highs
    out.to_csv(DATA / "robustness_slippage.csv", index=False)
    print("\n 读法：若某特征在低滑点下边际大幅缩水或翻负，说明它的「优势」来自成本假设，不是市场规律。")
    return out


# ------------------------------------------------------------------ 检验二
def test_bootstrap(df: pd.DataFrame, kind: str) -> pd.DataFrame:
    print("\n" + "=" * 96)
    print(f"【检验二】按交易日分块自助法（{kind} 口径，{N_BOOT} 次重采样）")
    print(f"{'特征':16s} {'点估计':>9s} {'95%区间':>22s} {'P(≤0)':>8s} {'判定':>10s}")
    rng = np.random.default_rng(20260914)
    rows = []
    for feat in FEATURES:
        rec = daily_edge(df, feat)
        if rec.empty:
            continue
        arr = rec["top"].to_numpy()
        n = len(arr)
        boot = np.array([arr[rng.integers(0, n, n)].mean() for _ in range(N_BOOT)])
        lo, hi = np.percentile(boot, [2.5, 97.5])
        p_le0 = float((boot <= 0).mean())
        if lo > 0:
            verdict = "显著为正"
        elif hi < 0:
            verdict = "显著为负"
        else:
            verdict = "不显著"
        print(f"{feat:16s} {arr.mean() * 100:>+8.3f}p "
              f"[{lo * 100:>+7.3f}p, {hi * 100:>+7.3f}p] {p_le0:>8.3f} {verdict:>10s}")
        rows.append({"kind": kind, "feature": feat, "days": n, "point_pp": arr.mean(),
                     "lo95_pp": lo, "hi95_pp": hi, "p_le_0": p_le0, "verdict": verdict})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ 检验三
def test_stability(df: pd.DataFrame, kind: str) -> None:
    print("\n" + "=" * 96)
    print(f"【检验三】逐年年份稳定性（{kind} 口径，日等权边际 pp）")
    years = sorted(df["date"].dt.year.unique())
    show = [y for y in years if y >= 2015]
    print(f"{'特征':16s}" + "".join(f"{y:>7d}" for y in show))
    for feat in FEATURES:
        rec = daily_edge(df, feat)
        if rec.empty:
            continue
        rec = rec.assign(year=rec["date"].dt.year)
        by = rec.groupby("year")["top"].mean()
        cells = "".join(
            f"{by.get(y, np.nan) * 100:>+7.2f}" if y in by.index else f"{'--':>7s}" for y in show
        )
        print(f"{feat:16s}{cells}")


def main() -> None:
    panel = pd.read_parquet(DATA / "panel.parquet")
    panel["ready"] = (
        panel["dif"].notna() & panel["dea"].notna() & panel["atr"].notna()
        & panel["trend_score"].notna() & panel["trend_ma5"].notna()
    )
    # 复用主分析已算好的事件/状态标记
    import entry_quality as eq

    panel = eq.prepare(panel)
    panel = panel[panel["date"] >= "2015-01-01"]

    test_slippage(panel)

    frames = []
    for kind in ("is_event", "is_state"):
        df = pd.read_csv(DATA / f"entry_quality_{'event' if kind == 'is_event' else 'state'}.csv",
                         parse_dates=["date"])
        frames.append(test_bootstrap(df, kind[3:]))
        test_stability(df, kind[3:])
    pd.concat(frames, ignore_index=True).to_csv(DATA / "robustness_bootstrap.csv", index=False)
    print(f"\n输出 -> {DATA / 'robustness_bootstrap.csv'}")


if __name__ == "__main__":
    main()
