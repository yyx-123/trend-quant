"""候选股筛选与排名：按申万行业分组，用总市值定龙头、用 20 日均额定流动性。

输入：
  data/market_snapshot.csv   （scan_market.py：名称/价格/市值/上市日/ST 标记）
  data/liquidity_20d.csv     （scan_liquidity.py：近 20 日日均成交额）
  生产库 instrument_metadata （已纳入标的）

筛选口径（用户要求）：
  1. 有有效报价（退市/长期停牌自然出局）
  2. 非 ST / 非退市整理
  3. 近 20 日日均成交额 ≥ 1 亿元

排名口径：组内总市值降序 = 龙头→次龙头（客观替代"我认为谁是龙头"）。

产出：
  data/candidates_by_l3.csv   全部合格候选的组内排名（含是否已纳入）
  data/proposed_by_l3.csv     按目标只数补齐的待新增名单
  data/summary_by_l2.csv      申万一级（项目 L2）汇总
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _common  # noqa: E402

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

MIN_AMOUNT_YI = 1.0        # 剔除日均成交额 < 1 亿
MIN_AMOUNT_RELAX_YI = 0.5  # 容量受限类目的放宽档门槛
TARGET_MIN = 5             # 每个类目的目标下限
TARGET_MAX = 8             # 每个类目的目标上限（名字够就补到 8）
MIN_BARS = 15              # 上市不足 15 个交易日的不参与排名


def load_current(db_path: Path) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query(
        """SELECT symbol, name, category_l2, category_l3, enabled
           FROM instrument_metadata WHERE category_l1='股票'""",
        conn,
    )
    conn.close()
    return df


def main() -> None:
    snap = pd.read_csv(DATA / "market_snapshot.csv", dtype={"symbol": str, "listing_date": str})
    liq = pd.read_csv(DATA / "liquidity_20d.csv", dtype={"symbol": str})
    cur = load_current(_common.DB_PATH)

    df = snap.merge(liq, on="symbol", how="left")
    df["listed_ok"] = df["listing_date"].notna() & (df["listing_date"] != "")

    # --- 口径校验：现有标的的分类是否都在申万名册里 ---
    key_cur = set(zip(cur["category_l2"], cur["category_l3"]))
    key_sw = set(zip(df["sw_l1"], df["sw_l2"]))
    print(f"现有标的 (L2,L3) 组合 {len(key_cur)} 个，申万名册 (L1,L2) 组合 {len(key_sw)} 个")
    missing = sorted(key_cur - key_sw)
    if missing:
        print(f"  ⚠ 申万名册里没有的现有分类 {len(missing)} 个：{missing}")
    else:
        print("  ✓ 现有分类全部能在申万名册里对上")

    # --- 筛选 ---
    df["mktcap_yi"] = pd.to_numeric(df["mktcap_yi"], errors="coerce")
    df["avg_amount_20d_yi"] = pd.to_numeric(df["avg_amount_20d_yi"], errors="coerce")
    df["bars_used"] = pd.to_numeric(df["bars_used"], errors="coerce").fillna(0)
    df["is_st"] = pd.to_numeric(df["is_st"], errors="coerce").fillna(0).astype(int)

    reasons = {}
    ok = df["price"].notna() & (df["mktcap_yi"] > 0)
    reasons["退市/无报价"] = int((~ok).sum())
    st = ok & (df["is_st"] == 1)
    reasons["ST/退市整理"] = int(st.sum())
    short = ok & ~st & (df["bars_used"] < MIN_BARS)
    reasons[f"上市不足{MIN_BARS}日"] = int(short.sum())
    illiq = ok & ~st & ~short & (
        df["avg_amount_20d_yi"].isna() | (df["avg_amount_20d_yi"] < MIN_AMOUNT_YI)
    )
    reasons[f"日均额<{MIN_AMOUNT_YI}亿"] = int(illiq.sum())

    elig = df[ok & ~st & ~short & ~illiq].copy()
    print(f"\n筛选漏斗（全市场 {len(df)} 只）：")
    for k, v in reasons.items():
        print(f"  剔除 {k}: {v}")
    print(f"  → 合格候选 {len(elig)} 只")

    # --- 已纳入标记 ---
    liquidated = df[df["is_st"] == 1].merge(cur[["symbol"]], on="symbol", how="inner")
    if not liquidated.empty:
        print(f"\n⚠ 现有池中的 ST 标的 {len(liquidated)} 只：")
        for _, r in liquidated.iterrows():
            print(f"   {r['symbol']} {r['name']} ({r['sw_l1']}/{r['sw_l2']}) 日均额 {r['avg_amount_20d_yi']} 亿")

    cur_syms = set(cur["symbol"])
    cur_map = cur.set_index("symbol")["name"].to_dict()
    elig["已纳入"] = elig["symbol"].isin(cur_syms)

    # --- 组内市值排名（组 = 申万二级 = 项目 L3） ---
    elig = elig.sort_values(["sw_l1", "sw_l2", "mktcap_yi"], ascending=[True, True, False])
    elig["组内排名"] = elig.groupby(["sw_l1", "sw_l2"]).cumcount() + 1
    elig["组内合格数"] = elig.groupby(["sw_l1", "sw_l2"])["symbol"].transform("size")
    elig["组内已纳入数"] = elig.groupby(["sw_l1", "sw_l2"])["已纳入"].transform("sum")

    out_cols = [
        "sw_l1", "sw_l2", "sw_l3", "组内排名", "symbol", "name",
        "mktcap_yi", "avg_amount_20d_yi", "price", "listing_date", "is_st", "已纳入",
    ]
    elig[out_cols].to_csv(DATA / "candidates_by_l3.csv", index=False, encoding="utf-8-sig")

    # --- 按目标只数补齐：已纳入的优先占位，再按市值顺序补 ---
    def build(target_mode: str) -> tuple[pd.DataFrame, pd.DataFrame]:
        """target_mode: 'min' 补到 5 只 / 'max' 补到 8 只（均受合格容量上限约束）。

        目标是**绝对只数**（类目内应有几只），不是"再补几只"——因此空类目在
        容量够时要补满 8 只，而不是只补到下限 5。
        """
        cap = TARGET_MIN if target_mode == "min" else TARGET_MAX
        proposals = []
        rows = []
        for (l1, l2), grp in elig.groupby(["sw_l1", "sw_l2"], sort=True):
            grp = grp.sort_values("mktcap_yi", ascending=False)
            have = int(grp["已纳入"].sum())
            capacity = len(grp)
            target = min(cap, capacity)
            need = max(0, target - have)
            rows.append(
                {
                    "sw_l1": l1, "sw_l2": l2, "现有": have, "合格候选总数": capacity,
                    "目标": target, "待补": need,
                    "候选前8": "、".join(grp["name"].head(8).tolist()),
                }
            )
            if need > 0:
                add = grp[~grp["已纳入"]].head(need)
                for _, r in add.iterrows():
                    proposals.append(
                        {
                            "sw_l1": l1, "sw_l2": l2, "symbol": r["symbol"], "name": r["name"],
                            "组内排名": r["组内排名"], "mktcap_yi": r["mktcap_yi"],
                            "avg_amount_20d_yi": r["avg_amount_20d_yi"],
                            "listing_date": r["listing_date"], "sw_l3": r["sw_l3"],
                        }
                    )
        return pd.DataFrame(rows), pd.DataFrame(proposals)

    summary, prop = build("max")
    summary5, prop5 = build("min")

    summary.to_csv(DATA / "summary_by_l2.csv", index=False, encoding="utf-8-sig")
    prop.to_csv(DATA / "proposed_by_l3.csv", index=False, encoding="utf-8-sig")
    prop5.to_csv(DATA / "proposed_by_l3_target5.csv", index=False, encoding="utf-8-sig")

    # --- 容量受限：全市场合格票都凑不到目标下限的类目 ---
    limited = summary[(summary["合格候选总数"] < TARGET_MIN) | (summary["现有"] + summary["待补"] < TARGET_MIN)]
    print(f"\n=== 容量受限类目（全市场合格票不足 {TARGET_MIN} 只）===")
    if limited.empty:
        print("  无")
    else:
        print(limited[["sw_l1", "sw_l2", "现有", "合格候选总数", "目标", "待补"]].to_string(index=False))

    # --- 放宽档：对容量受限类目把流动性门槛降到 0.5 亿，只取主方案之外的增量 ---
    relaxed = df[ok & ~st & ~short].copy()
    relaxed["已纳入"] = relaxed["symbol"].isin(cur_syms)
    relaxed_keys = set(zip(limited["sw_l1"], limited["sw_l2"]))
    already = set(prop["symbol"]) | cur_syms
    relax_rows = []
    for (l1, l2), grp in relaxed.groupby(["sw_l1", "sw_l2"], sort=True):
        if (l1, l2) not in relaxed_keys:
            continue
        grp = grp.sort_values("mktcap_yi", ascending=False)
        have = int(grp["已纳入"].sum())
        low = grp[grp["avg_amount_20d_yi"] >= MIN_AMOUNT_RELAX_YI]
        extra = low[~low["symbol"].isin(already)].head(max(0, min(TARGET_MIN, len(low)) - have))
        for _, r in extra.iterrows():
            relax_rows.append(
                {
                    "sw_l1": l1, "sw_l2": l2, "symbol": r["symbol"], "name": r["name"],
                    "mktcap_yi": r["mktcap_yi"], "avg_amount_20d_yi": r["avg_amount_20d_yi"],
                    "listing_date": r["listing_date"],
                    "备注": "放宽档：日均额 0.5-1 亿" if r["avg_amount_20d_yi"] < MIN_AMOUNT_YI else "",
                }
            )
    relax = pd.DataFrame(relax_rows)
    relax.to_csv(DATA / "proposed_relaxed_0p5yi.csv", index=False, encoding="utf-8-sig")
    print(f"放宽档（门槛 0.5 亿）主方案之外还能再补 {len(relax)} 只，"
          f"覆盖 {relax['sw_l2'].nunique() if not relax.empty else 0} 个受限类目")

    # --- 项目 L2（申万一级）视角 ---
    l2_rows = []
    for l1 in sorted(elig["sw_l1"].unique()):
        cur_have = cur[cur["category_l2"] == l1]
        cur_l3 = set(cur_have["category_l3"])
        sub = summary[summary["sw_l1"] == l1]
        sub5 = summary5[summary5["sw_l1"] == l1]
        thin_l3 = []
        for l3 in sorted(set(elig[elig["sw_l1"] == l1]["sw_l2"]) | cur_l3):
            have = int((cur_have["category_l3"] == l3).sum())
            if have < TARGET_MAX:
                thin_l3.append(f"{l3}({have})")
        l2_rows.append(
            {
                "项目L2": l1,
                "现有": len(cur_have),
                "三级类目数": len(set(elig[elig["sw_l1"] == l1]["sw_l2"]) | cur_l3),
                "补到5只": len(cur_have) + int(sub5["待补"].sum()),
                "补到8只": len(cur_have) + int(sub["待补"].sum()),
                "不足8只的三级类目": "、".join(thin_l3) if thin_l3 else "—",
            }
        )
    l2_df = pd.DataFrame(l2_rows)
    l2_df.to_csv(DATA / "summary_by_l1.csv", index=False, encoding="utf-8-sig")

    print("\n=== 申万一级（项目 L2）视角 ===")
    print(l2_df.sort_values("现有").to_string(index=False))

    # --- 方案 C：只在申万一级（项目 L2）层面补齐到 8 只 ---
    # 与 A/B 的区别：不看三级类目是否薄，只要这个一级行业总数不足 8 就补，
    # 补进来的票自然落在各自三级类目下。用于回答"把我说的二级类目补到 8 只"。
    l2_plan = []
    for l1, grp in elig.groupby("sw_l1", sort=True):
        have_cur = int(cur[cur["category_l2"] == l1].shape[0])
        need = max(0, min(TARGET_MAX, len(grp)) - have_cur)
        if need <= 0:
            continue
        add = grp.sort_values("mktcap_yi", ascending=False)
        add = add[~add["已纳入"]].head(need)
        for _, r in add.iterrows():
            l2_plan.append(
                {
                    "项目L2": l1, "symbol": r["symbol"], "name": r["name"],
                    "sw_l2": r["sw_l2"], "mktcap_yi": r["mktcap_yi"],
                    "avg_amount_20d_yi": r["avg_amount_20d_yi"],
                }
            )
    l2_plan_df = pd.DataFrame(l2_plan)
    l2_plan_df.to_csv(DATA / "proposed_l2_level.csv", index=False, encoding="utf-8-sig")

    # --- 现状统计 ---
    cur_l3 = cur.groupby(["category_l2", "category_l3"]).size()
    stats = {
        "现有股票": int(len(cur)),
        "现有L2": int(cur["category_l2"].nunique()),
        "现有L3(有票的)": int(len(cur_l3)),
        "全市场L3(申万名册)": int(elig.groupby(["sw_l1", "sw_l2"]).ngroups or 0),
        "L3不足5只": int((cur_l3 < 5).sum()),
        "L3不足8只": int((cur_l3 < 8).sum()),
        "全量L3数": int(df.groupby(["sw_l1", "sw_l2"]).ngroups),
    }
    print(f"\n=== 现状 ===\n{stats}")

    print(
        f"\n三级类目补齐：待补类目 {int((summary['待补'] > 0).sum())} 个\n"
        f"  A 三级补到 5 只 → 新增 {len(prop5)} 只（股票 {len(cur)} → {len(cur) + len(prop5)}）\n"
        f"  B 三级补到 8 只 → 新增 {len(prop)} 只（股票 {len(cur)} → {len(cur) + len(prop)}）\n"
        f"  C 只按二级补到 8 只 → 新增 {len(l2_plan_df)} 只（股票 {len(cur)} → {len(cur) + len(l2_plan_df)}）\n"
        f"  放宽档（0.5 亿门槛）→ 再补 {len(relax)} 只，覆盖 {relax['sw_l2'].nunique() if not relax.empty else 0} 个受限类目"
    )
    print(f"\n明细目录：{DATA}")


if __name__ == "__main__":
    main()
if __name__ == "__main__":
    main()
