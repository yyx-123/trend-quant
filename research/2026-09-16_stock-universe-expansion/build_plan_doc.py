"""方案 B 定稿版完整文档生成：读 plan_b*.csv → PLAN.md。

文档面向"照着做"：全部 131 个三级类目的补齐状态 + 每个待补类目的新增名单
（放宽档用 * 标注），外加放宽档单独复核清单与落地步骤。
"""

from __future__ import annotations

import json
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
OUT = HERE / "PLAN.md"

TARGET = 8


def main() -> None:
    by_l3 = pd.read_csv(DATA / "plan_b_by_l3.csv")
    add = pd.read_csv(DATA / "plan_b_additions.csv", dtype={"symbol": str})
    relaxed = pd.read_csv(DATA / "plan_b_relaxed.csv", dtype={"symbol": str})
    short = pd.read_csv(DATA / "plan_b_short.csv")
    excluded = pd.read_csv(DATA / "plan_b_excluded.csv")
    stats = json.loads((DATA / "plan_b_stats.json").read_text(encoding="utf-8"))
    funnel = stats["漏斗"]

    conn = sqlite3.connect(_common.DB_PATH)
    pool_total = pd.read_sql_query(
        "SELECT COUNT(*) c FROM instrument_metadata WHERE category_l1='股票' AND enabled=1", conn
    ).iloc[0, 0]
    conn.close()

    add_by_l3: dict[tuple[str, str], pd.DataFrame] = {
        k: v for k, v in add.groupby(["category_l2", "category_l3"])
    }
    n_add = len(add)
    n_relax = len(relaxed)
    n_short = len(short)
    short_under5 = short[short["补齐后"] < 5]
    final_total = stats["扩充后"]  # 停用 2 只 ST 后 + 新增

    L: list[str] = []
    add_line = L.append

    add_line("# 股票标的池扩充完整方案（方案 B 定稿）")
    add_line("")
    add_line("> 定稿日期：2026-09-16　｜　目标：每个三级类目 8 只")
    add_line("> 数据源：TickFlow 付费档全市场快照（2026-09-16）+ 申万分类名册（2026-08-24 同步）")
    add_line("> 脚本：`scan_market.py` → `scan_liquidity.py` → `plan_b.py` → `build_plan_doc.py`")
    add_line("")

    # ---------- 一、口径 ----------
    add_line("## 一、定稿口径")
    add_line("")
    add_line("| 项 | 规则 |")
    add_line("| --- | --- |")
    add_line("| 目标只数 | 每个三级类目 **8 只** |")
    add_line("| 成交额（优选） | 近 20 日日均成交额 **≥ 1 亿** |")
    add_line("| 成交额（兜底） | 类目太小、按 1 亿凑不满 8 只时，放宽到 **≥ 7500 万** |")
    add_line("| 成交额（硬底） | **不低于 7500 万**；宁可该类目少于 8 只、甚至少于 5 只，也不放更低成交额的进来 |")
    add_line("| 排除 | ST / 退市整理、无有效报价（退市停牌）、上市不足 15 个交易日 |")
    add_line("| 排序 | 组内按**总市值**（最新价 × 总股本）降序 = 龙头 → 次龙头 |")
    add_line("| ST 处理 | 现有池中的 ST 视为**待剔除**，不占用类目名额（下面的「补齐后」都已扣除） |")
    add_line("")
    add_line("成交额口径说明：用**近 20 日日均成交额**，不用扫描当日成交额——扫描时点在盘中 14:15，")
    add_line("当日成交额只有半个交易日，会把「全天刚过 1 亿」的票误杀；20 日均额同时对单日异动免疫。")
    add_line("")
    add_line("放宽档只在类目凑不满 8 只时才启用，且**优先用满 1 亿以上的候选**，不足的名额才用 7500 万-1 亿的补。")
    add_line("")

    # ---------- 二、总账 ----------
    add_line("## 二、总账")
    add_line("")
    add_line("| 指标 | 数值 |")
    add_line("| --- | --- |")
    add_line(f"| 现有股票标的 | {pool_total} 只 |")
    add_line(f"| 人工剔除 | {stats['人工剔除数']} 只（更名/转型导致行业代表性不可靠，见第七节） |")
    add_line(f"| 需停用 | {stats['池中ST数']} 只 ST（ST臻镭 688270.SS、ST洲际 600759.SS） |")
    add_line(f"| **待新增** | **{n_add} 只**（其中走 7500 万放宽档的 {n_relax} 只） |")
    add_line(f"| 扩充后规模 | **{stats['扩充后']} 只**（{pool_total} − {stats['池中ST数']} 停用 + {n_add} 新增） |")
    add_line(f"| 三级类目达标 | 131 个里 **{len(by_l3) - n_short} 个** 补齐到 8 只 |")
    add_line(f"| 仍不足 8 只 | {n_short} 个（候选池在 7500 万硬底下就不够） |")
    add_line(f"| 其中不足 5 只 | {len(short_under5)} 个（按你的口径保持现状，不再降门槛） |")
    add_line("")
    n_cand = funnel["1亿以上候选"] + funnel["放宽档候选"]
    add_line(
        f"筛选漏斗：全市场 {funnel['全市场']} 只 → 剔除无报价 {funnel['剔除_无报价']}、"
        f"ST {funnel['剔除_ST']}、次新 {funnel['剔除_次新']}、"
        f"日均额 < 7500 万 {funnel['剔除_日均额不足']}；"
        f"再扣掉人工剔除的 {stats['人工剔除数']} 只，得成交额 ≥ 1 亿 {funnel['1亿以上候选']} 只"
        f"＋7500 万-1 亿放宽档 {funnel['放宽档候选']} 只 = **{n_cand} 只合格候选**，"
        f"从中按市值取龙头补齐。"
    )
    add_line("")

    add_line("### 各二级类目新增分布")
    add_line("")
    add_line("| 二级类目 | 现有 | 新增 | 扩充后 | 未达标三级类目数 |")
    add_line("| --- | --- | --- | --- | --- |")
    grp = by_l3.groupby("l1").agg(
        现有=("现有", "sum"),
        新增=("新增", "sum"),
        补齐后=("补齐后", "sum"),
        未达标=("达标", lambda s: int((s == "否").sum())),
    )
    for l1, r in grp.sort_values("新增", ascending=False).iterrows():
        add_line(f"| {l1} | {r['现有']} | {r['新增']} | {r['补齐后']} | {r['未达标'] or '—'} |")
    add_line("")

    # ---------- 三、完整清单 ----------
    add_line("## 三、完整清单（按二级类目）")
    add_line("")
    add_line("每个三级类目一条。**加粗**是补进去的标的，带 `*` 的是走 7500 万放宽档的（建议单独过一眼）。")
    add_line("")
    for l1, g in by_l3.groupby("l1"):
        tot_have = int(g["现有"].sum())
        tot_new = int(g["新增"].sum())
        add_line(f"### {l1}（现有 {tot_have} → {tot_have - int(g['含ST'].sum()) + tot_new}）")
        add_line("")
        for _, r in g.sort_values("l2").iterrows():
            name = r["l2"]
            have = int(r["现有"])
            st = int(r["含ST"])
            after = int(r["补齐后"])
            new = int(r["新增"])
            st_note = f"，含 ST {st} 只待剔除" if st else ""
            if new == 0:
                add_line(f"- **{name}**：现有 {have}{st_note} → {after}，已达标，无需新增。")
                continue
            items = add_by_l3.get((l1, name))
            parts = []
            for _, x in items.iterrows():
                mark = "*" if str(x["档位"]).startswith("放宽") else ""
                parts.append(
                    f"{x['name']}{mark} {x['symbol'].split('.')[0]}"
                    f"（{x['mktcap_yi']:.0f}亿/{x['avg_amount_20d_yi']:.2f}亿）"
                )
            line = f"- **{name}**：现有 {have}{st_note} → {after}，新增 {new} 只：" + "、".join(parts)
            if r["达标"] == "否":
                line += f"　⚠️ 候选池仅 {int(r['候选池'])} 只，放宽到 7500 万也凑不满 8 只。"
            add_line(line)
        add_line("")

    # ---------- 四、放宽档 ----------
    add_line("## 四、放宽档单独复核（7500 万 - 1 亿）")
    add_line("")
    add_line(f"共 {n_relax} 只，是为了把类目凑到 8 只才启用的兜底。这些票的成交额都低于你原本的 1 亿线，")
    add_line("但都是各细分行业里排名靠前的公司，建议逐只确认是否接受。")
    add_line("")
    add_line("| 二级类目 | 三级类目 | 代码 | 名称 | 总市值 | 20 日均额 |")
    add_line("| --- | --- | --- | --- | --- | --- |")
    for _, r in relaxed.sort_values(["category_l2", "category_l3", "mktcap_yi"], ascending=[True, True, False]).iterrows():
        add_line(
            f"| {r['category_l2']} | {r['category_l3']} | {r['symbol']} | {r['name']} | "
            f"{r['mktcap_yi']:.0f} 亿 | {r['avg_amount_20d_yi']*10000:.0f} 万 |"
        )
    add_line("")
    add_line("如果复核后不接受其中的某几只，直接删掉即可——对应类目会变成 7 只或更少，")
    add_line("这符合「宁可少也不要更低成交额」的原则。")
    add_line("")

    # ---------- 五、凑不满的类目 ----------
    add_line(f"## 五、放宽到 7500 万后仍凑不满 8 只的 {n_short} 个类目")
    add_line("")
    add_line("这些细分行业在 7500 万硬底下，全市场合格票本身就不到 8 只。这是行业容量问题，不是筛选问题：")
    add_line("比如「国有大型银行」全市场就只有 6 家，「旅游零售」只有中国中免 + 珠免集团两家。")
    add_line("")
    add_line("| 二级类目 | 三级类目 | 有效现有 | 新增 | 补齐后 | 候选池 |")
    add_line("| --- | --- | --- | --- | --- | --- |")
    for _, r in short.sort_values("补齐后").iterrows():
        add_line(
            f"| {r['l1']} | {r['l2']} | {r['有效现有']} | {r['新增']} | {r['补齐后']} | {r['候选池']} |"
        )
    add_line("")
    under5 = short[short["补齐后"] < 5].sort_values("补齐后")
    add_line(f"其中 **{len(under5)} 个连 5 只都到不了**："
              + "、".join(f"{r['l1']}/{r['l2']}（{r['补齐后']}）" for _, r in under5.iterrows())
              + "。按你的要求，这些类目就保持现状，不再往下降成交额门槛。")
    add_line("")

    # ---------- 六、落地 ----------
    add_line("## 六、落地步骤")
    add_line("")
    add_line("1. **先剔除 2 只 ST**")
    add_line("")
    add_line("   在 `/instruments` 把 `ST臻镭 688270.SS`、`ST洲际 600759.SS` 停用（`enabled=0`）。")
    add_line("   用停用而不是删除：历史行情与趋势缓存都保留，随时可恢复。")
    add_line("")
    add_line("2. **导入新增标的**")
    add_line("")
    add_line(f"   名单在 `data/plan_b_additions.csv`（{n_add} 只，字段：symbol / name / category_l1 / category_l2 / category_l3 / 市值 / 成交额 / 档位）。")
    add_line("   两种方式：")
    add_line("")
    add_line("   - 网页端 `/instruments` 逐只添加，或 `POST /api/add`。**类目可以留空**——后端会按")
    add_line("     `stock_industry` 的申万行业自动归类，结果与本文档一致；添加成功会自动回填历史日 K")
    add_line("     并重建指标（`InstrumentAddJobManager._run`）。")
    add_line("   - 写批量脚本直接调 `_build_new_instrument_record` + `backfill_daily_history`，")
    add_line("     避免逐只排队。")
    add_line("")
    add_line("   注意新增任务是**单标的串行**的（并发会返回 409「已有新增标的任务正在运行」），")
    add_line(f"   {n_add} 只逐只跑会比较久，建议分几批做，或者用批量脚本。")
    add_line("")
    add_line("3. **加完跑一次全量回填**")
    add_line("")
    add_line("   `POST /api/backfill-all`，确保新标的的周/月滚动趋势值（`trend_rolling_daily`）也补齐。")
    add_line("")
    add_line("4. **分类树不用动**")
    add_line("")
    add_line("   `instrument_categories` 里 131 个三级分支（`股票-<一级>-<二级>`）已经全部存在，")
    add_line("   新增标的不会产生新分类路径。")
    add_line("")

    # ---------- 七、注意事项 ----------
    add_line("## 七、执行前必须知道的几件事")
    add_line("")
    add_line("### 1. 扩充会让全部股票的「强度百分位」重算")
    add_line("")
    add_line("`services/dashboard_common.assign_strength` 的 scope 是 `category_l1`——股票的强度百分位是在")
    add_line(f"**整个股票池内**排名，不是类目内。池子从 {pool_total} 只扩到 {final_total} 只后，所有股票的")
    add_line("`strength` 数值都会变（ETF 不受影响）。如果有信号用了强度的绝对阈值，扩池前后要重新标定。")
    add_line("")
    _pct = round((stats["扩充后"] / pool_total - 1) * 100)
    add_line(f"### 2. 每日更新耗时与库体积会上升约 {_pct}%")
    add_line("")
    add_line(f"标的数从 {pool_total} 增到 {stats['扩充后']}，16:30 的日更、指标缓存、周/月滚动趋势派生都会同比变重。")
    add_line("首次全量回填最吃时间，建议放在非交易时段做。")
    add_line("")
    add_line("### 3. 除权日名称会被压成截断形式（已处理）")
    add_line("")
    add_line("TickFlow 在除权日返回的名称形如 `XD圆通速`——**被压成 4 个字，末字丢失**，")
    add_line("而且 `quotes` 与 `instruments` 两个接口都是这个截断名，**拿不到全名**。扫描当天有 4 只：")
    add_line("")
    add_line("| 代码 | vendor 返回 | 订正为 |")
    add_line("| --- | --- | --- |")
    add_line("| 600233.SS | XD圆通速 | 圆通速递 |")
    add_line("| 600916.SS | XD中国黄 | 中国黄金 |")
    add_line("| 600113.SS | XD浙江东 | 浙江东日 |")
    add_line("| 603406.SS | XD天富龙 | 天富龙 |")
    add_line("")
    add_line("`plan_b.py` 里有一张 `NAME_FIX` 订正表按代码处理，并且会对残留的 `XD/XR/DR` 前缀**告警**")
    add_line("（当前残留 0 只）。导入时务必用订正后的名字，否则看板类目列表里会出现「XD圆通速」这种错字。")
    add_line("")
    add_line("### 4. 北交所是盲区")
    add_line("")
    add_line("`stock_industry` 里没有任何 `.BJ` 标的（申万 universe 只给了沪深两市），所以像锦波生物这类")
    add_line("北交所细分龙头不在候选池里，本次也不会被补进来。")
    add_line("")
    add_line("### 5. 人工剔除的 7 只与递补结果（已落到名单里）")
    add_line("")
    add_line("纯市值排序的已知副作用：会选出**市值靠前但行业代表性一般**的票（更名转型、题材属性强）。")
    add_line("下列 7 只已从名单中剔除，名额由同组市值排名下一名递补：")
    add_line("")
    add_line("| 剔除 | 代码 | 类目 | 剔除原因 | 递补 |")
    add_line("| --- | --- | --- | --- | --- |")
    for _, r in excluded.iterrows():
        # 原因列去掉重复的「名称：」前缀
        reason = str(r["原因"])
        if "：" in reason:
            reason = reason.split("：", 1)[1]
        add_line(
            f"| {r['剔除名称']} | {r['剔除']} | {r['类目']} | {reason} | {r['递补']} |"
        )
    add_line("")
    add_line("**有 3 只剔除后没有递补**（莲花控股、钒钛股份、平潭发展），因为对应类目的候选池在 7500 万")
    add_line("硬底下已经见底——按你的口径这是正确结果：宁可少一只，也不放成交额更低的进来。")
    add_line("受此影响，食品饮料/调味发酵品 8→7、钢铁/冶钢原料 7→6、农林牧渔/林业 2→1。")
    add_line("")
    add_line("另外两点需要说明：")
    add_line("")
    add_line("- **圆通速递（600233）、中国黄金（600916）没有剔除**。它们当初进复核表只是因为除权日的 XD")
    add_line("  名称前缀，那是数据展示问题、不是公司问题，两家分别是快递和黄金零售的龙头，已订正名称后保留。")
    add_line("- **久事动娱（600676）本来就是虚惊**：它的 20 日均额只有 5030 万，低于 7500 万硬底，")
    add_line("  从来就没进过最终名单（那条标注来自更早的 0.5 亿放宽尝试，已过时）。")
    add_line("")
    add_line("还有一个新面孔值得留意：环保/环保设备的递补者 **法尔胜（000890）** 只有 32 亿市值却有 4.72 亿")
    add_line("日成交额，换手偏高、基本面偏弱，是纯市值递补的典型产物。不接受的话同类目没有下一名可补，")
    add_line("该类目会变成 7 只。")
    add_line("")
    add_line("如需继续调整个别标的，完整组内排名见 `data/candidates_by_l3.csv`")
    add_line("（按三级类目分组、市值降序、标注是否已纳入），换名后重跑 `plan_b.py` 即可同步全表。")
    add_line("")

    # ---------- 八、产物 ----------
    add_line("## 八、产物文件")
    add_line("")
    add_line("| 文件 | 内容 |")
    add_line("| --- | --- |")
    add_line("| `data/plan_b_additions.csv` | **待新增名单（import 就绪，493 只）** |")
    add_line("| `data/plan_b_by_l3.csv` | 全部 131 个三级类目的补齐状态 |")
    add_line("| `data/plan_b_relaxed.csv` | 放宽档 28 只，单独复核用 |")
    add_line("| `data/plan_b_short.csv` | 凑不满 8 只的类目 |")
    add_line("| `data/plan_b_excluded.csv` | 人工剔除的 7 只 → 递补对照 |")
    add_line("| `data/plan_b_stats.json` | 本次方案的统计快照（漏斗/总数/达标数） |")
    add_line("| `data/candidates_by_l3.csv` | 全市场完整合格池（含组内排名，用于递补） |")
    add_line("| `data/market_snapshot.csv` | 全市场原始快照（名称/现价/成交额/总市值/上市日/ST 标记） |")
    add_line("| `data/liquidity_20d.csv` | 每只票近 20 日日均成交额 |")
    add_line("| `REPORT.md` | 上一轮的三档位调研（A/B/C 与 0.5 亿放宽档） |")
    add_line("")

    OUT.write_text("\n".join(L), encoding="utf-8")
    print(f"写出 {OUT}（{len(L)} 行）")


if __name__ == "__main__":
    main()
