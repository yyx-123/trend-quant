"""方案 B 定稿口径：每个三级类目补到 8 只。

门槛规则（按用户 2026-09-16 定稿）：
  1. 优选：近 20 日日均成交额 >= 1 亿；
  2. 兜底：类目太小、按 1 亿门槛凑不满 8 只时，放宽到 >= 7500 万；
  3. 硬底：成交额不得低于 7500 万——宁可该类目少于 8 只（甚至少于 5 只），
     也不放入成交额更低的标的。

其它筛选：有有效报价（剔除退市/停牌）、非 ST/退市整理、上市满 15 个交易日。
排名：组内总市值（最新价 × 总股本）降序 = 龙头→次龙头。
现有池中若含 ST 标的，按"待剔除"处理，不占用类目名额（补足数按剔除后计算）。

产出（均在 data/ 下）：
  plan_b_by_l3.csv      全部 131 个三级类目的补齐状态（现有/目标/新增数/新增名单）
  plan_b_additions.csv  待新增标的（import 就绪：代码/名称/一二级三级/市值/成交额/档位）
  plan_b_relaxed.csv    其中走 7500 万放宽档的标的（单独复核用）
  plan_b_short.csv      放宽后仍凑不满 8 只的类目
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

TARGET_L3 = 8              # 每个三级类目的目标只数
MIN_AMOUNT_YI = 1.0        # 优选门槛：1 亿
RELAX_AMOUNT_YI = 0.75     # 兜底门槛：7500 万（硬底，不再降低）
MIN_BARS = 15

# 人工复核后剔除：市值排名靠前，但更名/主业变更导致行业代表性不可靠
# （2026-09-16 用户确认剔除，名额由同组市值下一名递补）
EXCLUDED: dict[str, str] = {
    "600292.SS": "电投水电：原远达环保，重组为水电，申万分类可能未更新",
    "600388.SS": "紫金龙净：原龙净环保，已更名",
    "601777.SS": "千里科技：原力帆科技，主业转向智驾",
    "600186.SS": "莲花控股：味精主业 + 算力，题材属性强",
    "000629.SZ": "钒钛股份：攀钢钒钛，钒电池概念",
    "002354.SZ": "天娱数科：小市值高换手，投机属性强",
    "000592.SZ": "平潭发展：高换手题材股",
}
# 除权日名称订正：TickFlow 在除权日返回 "XD圆通速" 这种**4 字截断**形式
# （instruments 与 quotes 两个接口都一样，拿不到全名，只能按代码人工订正）。
# 只影响当天除权送转的少数标的，逐只核对后写在这里。
NAME_FIX: dict[str, str] = {
    "600233.SS": "圆通速递",
    "600916.SS": "中国黄金",
    "600113.SS": "浙江东日",
    "603406.SS": "天富龙",
}
# 注意：圆通速递 600233、中国黄金 600916 曾被列入复核表，但疑点只是除权日的
# XD 名称前缀（数据展示问题，非公司问题），不剔除，改为用订正名入库。


def main() -> None:
    snap = pd.read_csv(DATA / "market_snapshot.csv", dtype={"symbol": str, "listing_date": str})
    liq = pd.read_csv(DATA / "liquidity_20d.csv", dtype={"symbol": str})
    df = snap.merge(liq, on="symbol", how="left")

    conn = sqlite3.connect(_common.DB_PATH)
    cur = pd.read_sql_query(
        "SELECT symbol, name, category_l2, category_l3 FROM instrument_metadata "
        "WHERE category_l1='股票' AND enabled=1",
        conn,
    )
    # 全市场分类名册（131 个三级类目的完整清单，含现有池里没有的）
    tree = pd.read_sql_query(
        "SELECT DISTINCT sw_l1_name AS l1, sw_l2_name AS l2 FROM stock_industry",
        conn,
    )
    conn.close()

    df["mktcap_yi"] = pd.to_numeric(df["mktcap_yi"], errors="coerce")
    df["avg_amount_20d_yi"] = pd.to_numeric(df["avg_amount_20d_yi"], errors="coerce")
    df["bars_used"] = pd.to_numeric(df["bars_used"], errors="coerce").fillna(0)
    df["is_st"] = pd.to_numeric(df["is_st"], errors="coerce").fillna(0).astype(int)

    base = df[
        df["price"].notna()
        & (df["mktcap_yi"] > 0)
        & (df["is_st"] == 0)
        & (df["bars_used"] >= MIN_BARS)
        & (df["avg_amount_20d_yi"] >= RELAX_AMOUNT_YI)
    ].copy()
    # 漏斗计数（供文档引用，避免硬编码失真）
    funnel = {
        "全市场": int(len(df)),
        "剔除_无报价": int((df["price"].isna() | (df["mktcap_yi"] <= 0)).sum()),
        "剔除_ST": int(((df["price"].notna()) & (df["is_st"] == 1)).sum()),
        "剔除_次新": int(
            (df["price"].notna() & (df["is_st"] == 0) & (df["bars_used"] < MIN_BARS)).sum()
        ),
        "剔除_日均额不足": int(
            (
                df["price"].notna()
                & (df["is_st"] == 0)
                & (df["bars_used"] >= MIN_BARS)
                & (df["avg_amount_20d_yi"] < RELAX_AMOUNT_YI)
            ).sum()
        ),
        "1亿以上候选": int(((df["price"].notna()) & (df["is_st"] == 0) & (df["bars_used"] >= MIN_BARS) & (df["avg_amount_20d_yi"] >= MIN_AMOUNT_YI) & (~df["symbol"].isin(EXCLUDED))).sum()),
        "放宽档候选": int(( (df["price"].notna()) & (df["is_st"] == 0) & (df["bars_used"] >= MIN_BARS) & (df["avg_amount_20d_yi"] >= RELAX_AMOUNT_YI) & (df["avg_amount_20d_yi"] < MIN_AMOUNT_YI) & (~df["symbol"].isin(EXCLUDED))).sum()),
    }
    print(f"漏斗：{funnel}")
    base["档位"] = [
        "1亿以上" if a >= MIN_AMOUNT_YI else "放宽档(0.75-1亿)"
        for a in base["avg_amount_20d_yi"]
    ]
    n_excluded_in_pool = int(base["symbol"].isin(EXCLUDED).sum())
    # 关键：先按 (类目, 市值降序) 排好，后面按类目切片才不会乱序取到"代码最小的"
    # 而不是"市值最大的"（2026-09-16 踩过：物流补成了 000626/000682 而非圆通速递）。
    base_all = base.sort_values(
        ["sw_l1", "sw_l2", "mktcap_yi"], ascending=[True, True, False]
    ).reset_index(drop=True)
    base = base_all[~base_all["symbol"].isin(EXCLUDED)].reset_index(drop=True)
    print(f"人工剔除 {len(EXCLUDED)} 只（其中 {n_excluded_in_pool} 只原本在候选池里）")
    tier1 = base[base["avg_amount_20d_yi"] >= MIN_AMOUNT_YI]
    tier2 = base[base["avg_amount_20d_yi"] < MIN_AMOUNT_YI]
    print(f"合格池：1 亿以上 {len(tier1)} 只，放宽档 {len(tier2)} 只")

    cur_syms = set(cur["symbol"])
    cur_by_l3 = cur.groupby(["category_l2", "category_l3"])["symbol"].apply(set).to_dict()
    cur_names = cur.set_index("symbol")["name"].to_dict()
    cur_st = {
        (r["category_l2"], r["category_l3"]): 1
        for _, r in cur.iterrows()
        if "ST" in str(r["name"]).upper() or "退" in str(r["name"])
    }
    # 现有池里的 ST 逐个记录（用于文档提示）
    pool_st = [
        {"symbol": r["symbol"], "name": r["name"], "mktcap_yi": None, "avg_amount_20d_yi": None}
        for _, r in cur.iterrows()
        if "ST" in str(r["name"]).upper() or "退" in str(r["name"])
    ]

    def select(pool_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        """按"每类补到 8 只"选标的，返回 (类目状态表, 新增明细)。"""
        rows = []
        additions = []
        for _, t in tree.drop_duplicates().sort_values(["l1", "l2"]).iterrows():
            l1, l2 = t["l1"], t["l2"]
            in_pool = cur_by_l3.get((l1, l2), set())
            have_all = len(in_pool)
            st_have = cur_st.get((l1, l2), 0)
            have_eff = have_all - st_have          # 剔除 ST 后的有效只数，用于算补足数

            sub_all = pool_df[(pool_df["sw_l1"] == l1) & (pool_df["sw_l2"] == l2)]
            cap1 = sub_all[sub_all["avg_amount_20d_yi"] >= MIN_AMOUNT_YI]
            cap2 = sub_all[sub_all["avg_amount_20d_yi"] < MIN_AMOUNT_YI]
            capacity = len(cap1) + len(cap2)

            need = max(0, TARGET_L3 - have_eff)
            picked = []
            for pool in (cap1, cap2):
                for _, r in pool.iterrows():
                    if len(picked) >= need:
                        break
                    if r["symbol"] in in_pool:
                        continue
                    picked.append(r)
            for r in picked:
                additions.append(
                    {
                        "symbol": r["symbol"],
                        "name": r["name"],
                        "category_l1": "股票",
                        "category_l2": l1,
                        "category_l3": l2,
                        "sw_l3": r["sw_l3"],
                        "mktcap_yi": round(float(r["mktcap_yi"]), 2),
                        "avg_amount_20d_yi": round(float(r["avg_amount_20d_yi"]), 4),
                        "listing_date": r["listing_date"],
                        "档位": r["档位"],
                    }
                )

            final = have_eff + len(picked)
            rows.append(
                {
                    "l1": l1,
                    "l2": l2,
                    "现有": have_all,
                    "含ST": st_have,
                    "有效现有": have_eff,
                    "1亿以上候选": len(cap1),
                    "放宽档候选": len(cap2),
                    "候选池": capacity,
                    "新增": len(picked),
                    "补齐后": final,
                    "达标": "是" if final >= TARGET_L3 else "否",
                    "新增名单": "、".join(f"{r['name']}({r['symbol'].split('.')[0]})" for r in picked),
                    "放宽档数": sum(1 for r in picked if r["档位"].startswith("放宽")),
                }
            )
        return pd.DataFrame(rows), pd.DataFrame(additions)

    # 剔除前 / 剔除后各跑一次，用于生成"剔除 → 递补"对照
    by_l3_before, add_before = select(base_all)
    by_l3, add_df = select(base)

    # 逐个被剔除标的找递补者：同组内、剔除后才出现、且市值排名更靠后的那个
    replaced = []
    for sym, reason in EXCLUDED.items():
        was = add_before[add_before["symbol"] == sym]
        if was.empty:
            replaced.append({"剔除": sym, "剔除名称": "", "原因": reason, "类目": "", "递补": "（原方案未纳入，无需递补）"})
            continue
        l1, l2 = was.iloc[0]["category_l2"], was.iloc[0]["category_l3"]
        before_set = set(add_before[(add_before.category_l2 == l1) & (add_before.category_l3 == l2)]["symbol"])
        after_set = set(add_df[(add_df.category_l2 == l1) & (add_df.category_l3 == l2)]["symbol"])
        newcomers = add_df[add_df["symbol"].isin(after_set - before_set)]
        if newcomers.empty:
            replaced.append({
                "剔除": sym, "剔除名称": was.iloc[0]["name"], "原因": reason,
                "类目": f"{l1}/{l2}", "递补": "（该类目候选池已见底，无人递补）",
            })
        else:
            n = newcomers.iloc[0]
            replaced.append({
                "剔除": sym, "剔除名称": was.iloc[0]["name"], "原因": reason,
                "类目": f"{l1}/{l2}",
                "递补": f"{n['name']} {n['symbol'].split('.')[0]}（{n['mktcap_yi']:.0f}亿/{n['avg_amount_20d_yi']:.2f}亿）",
            })
    replaced_df = pd.DataFrame(replaced)

    # 名称订正：除权日 vendor 返回 4 字截断名，按代码订正回正式名。
    fixed = 0
    for idx, r in add_df.iterrows():
        if r["symbol"] in NAME_FIX:
            add_df.at[idx, "行情名"] = r["name"]
            add_df.at[idx, "name"] = NAME_FIX[r["symbol"]]
            fixed += 1
    leftover = add_df[add_df["name"].str.startswith(("XD", "XR", "DR"), na=False)]
    print(f"名称订正：{fixed} 只；残留除权前缀 {len(leftover)} 只"
          + (f"（{list(leftover['symbol'])}）" if len(leftover) else ""))

    relaxed_df = add_df[add_df["档位"].str.startswith("放宽")] if not add_df.empty else add_df
    short = by_l3[by_l3["达标"] == "否"]

    by_l3.to_csv(DATA / "plan_b_by_l3.csv", index=False, encoding="utf-8-sig")
    add_df.to_csv(DATA / "plan_b_additions.csv", index=False, encoding="utf-8-sig")
    relaxed_df.to_csv(DATA / "plan_b_relaxed.csv", index=False, encoding="utf-8-sig")
    short.to_csv(DATA / "plan_b_short.csv", index=False, encoding="utf-8-sig")
    replaced_df.to_csv(DATA / "plan_b_excluded.csv", index=False, encoding="utf-8-sig")

    stats = {
        "漏斗": funnel,
        "目标只数": TARGET_L3,
        "优选门槛_亿": MIN_AMOUNT_YI,
        "兜底门槛_亿": RELAX_AMOUNT_YI,
        "人工剔除数": len(EXCLUDED),
        "现有股票": int(len(cur)),
        "池中ST数": len(pool_st),
        "新增": int(len(add_df)),
        "放宽档新增": int(len(relaxed_df)),
        "扩充后": int(len(cur) - len(pool_st) + len(add_df)),
        "类目总数": int(len(by_l3)),
        "达标类目": int((by_l3["达标"] == "是").sum()),
        "未达标类目": int(len(short)),
        "不足5只类目": int((short["补齐后"] < 5).sum()),
    }
    (DATA / "plan_b_stats.json").write_text(
        __import__("json").dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n新增合计 {len(add_df)} 只（其中放宽档 {len(relaxed_df)} 只）")
    print(f"股票池 {len(cur)} → {len(cur) + len(add_df)}（另需剔除 ST {len(pool_st)} 只）")
    print(f"\n达标情况：{int((by_l3['达标'] == '是').sum())}/{len(by_l3)} 个三级类目补满 8 只")
    print(f"仍不满 8 只的 {len(short)} 个：")
    for _, r in short.iterrows():
        print(f"  {r['l1']}/{r['l2']}：有效现有 {r['有效现有']} + 新增 {r['新增']} = {r['补齐后']}"
              f"（候选池仅 {r['候选池']}）")
    if pool_st:
        print(f"\n现有池中的 ST（建议停用）：{[p['symbol'] for p in pool_st]}")
    print("\n剔除 → 递补：")
    for _, r in replaced_df.iterrows():
        print(f"  {r['剔除名称']} {r['剔除'].split('.')[0]}（{r['类目']}）→ {r['递补']}")


if __name__ == "__main__":
    main()
