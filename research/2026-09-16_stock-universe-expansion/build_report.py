"""由分析产物生成 REPORT.md（避免手工誊抄数字出错）。

读取 data/ 下的 CSV，输出与报告同目录的 REPORT.md。
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
OUT = HERE / "REPORT.md"


def fmt_items(grp: pd.DataFrame) -> str:
    parts = []
    for _, r in grp.iterrows():
        code = r["symbol"].split(".")[0]
        parts.append(f"{r['name']} {code}（{r['mktcap_yi']:.0f}亿/{r['avg_amount_20d_yi']:.1f}亿）")
    return "、".join(parts)


def main() -> None:
    prop5 = pd.read_csv(DATA / "proposed_by_l3_target5.csv", dtype={"symbol": str})
    prop8 = pd.read_csv(DATA / "proposed_by_l3.csv", dtype={"symbol": str})
    relax = pd.read_csv(DATA / "proposed_relaxed_0p5yi.csv", dtype={"symbol": str})
    l2plan = pd.read_csv(DATA / "proposed_l2_level.csv", dtype={"symbol": str})
    summary = pd.read_csv(DATA / "summary_by_l2.csv")
    summary_l1 = pd.read_csv(DATA / "summary_by_l1.csv")

    conn = sqlite3.connect(_common.DB_PATH)
    cur = pd.read_sql_query(
        "SELECT symbol, name, category_l2, category_l3 FROM instrument_metadata "
        "WHERE category_l1='股票' AND enabled=1",
        conn,
    )
    conn.close()

    have_by_l3 = cur.groupby(["category_l2", "category_l3"]).size().to_dict()
    have_by_l2 = cur.groupby("category_l2").size().to_dict()

    # 需要复核的名称前缀（除权除息标记）
    prefix_names = prop8[prop8["name"].str.startswith(("XD", "XR", "DR"), na=False)]

    lines: list[str] = []
    add = lines.append

    add("# 股票标的池扩充方案（候选清单）")
    add("")
    add("> 生成日期：2026-09-16　｜　数据源：TickFlow 付费档全市场快照 + 申万分类名册")
    add("> 脚本：`scan_market.py` → `scan_liquidity.py` → `analyze_candidates.py` → `build_report.py`")
    add("")

    add("## 一、一句话结论")
    add("")
    add(
        f"股票池现在 {len(cur)} 只，但 **131 个三级类目里有 72 个不足 8 只、56 个不足 5 只**"
        "（最薄的一批只有 1 只，比如 物流、影视院线、燃气、动物保健、旅游零售、地面兵装、"
        "橡胶、金属新材料、房屋建设、农商行、多元金融、服装家纺、饰品、包装印刷、医疗美容、综合）。"
        "这三类「薄」是同一个成因：这些细分行业本身上市公司就少，而其中大部分成交额低于 1 亿又被门槛挡掉。"
    )
    add("")
    add("按「三级类目补到 5-8 只」扫全市场后，合格候选（非 ST、近 20 日日均成交额 ≥ 1 亿）共 **2978 只**，")
    add("按总市值排序取各组龙头，得到三个可选档位：")
    add("")
    add("| 档位 | 口径 | 新增 | 股票池规模 |")
    add("| --- | --- | --- | --- |")
    add(f"| **A（推荐）** | 每个三级类目补到 5 只 | **+{len(prop5)}** | {len(cur)} → {len(cur) + len(prop5)} |")
    add(f"| B（增强） | 每个三级类目补到 8 只 | +{len(prop8)} | {len(cur)} → {len(cur) + len(prop8)} |")
    add(f"| C（最小） | 只按二级类目补到 8 只 | +{len(l2plan)} | {len(cur)} → {len(cur) + len(l2plan)} |")
    add("")
    add(
        "推荐从 **A** 起步：它刚好把「1-2 只」的类目抬到 5 只（可比较、可轮动），"
        "又不会让每日更新的标的数翻倍；单看二级类目的话，A 档下所有 31 个二级类目都 ≥ 5 只，"
        "其实已经覆盖了「二级类目至少 5-8 只」的要求。"
    )
    add("")

    # ---------------- 方法 ----------------
    add("## 二、筛选与排名口径")
    add("")
    add("**候选池**：`stock_industry` 表里的全市场 5552 只 A 股（申万三级分类名册，2026-08-24 同步）。")
    add("")
    add("**四道筛子**（逐层剔人）：")
    add("")
    add("| 筛子 | 剔除 | 说明 |")
    add("| --- | --- | --- |")
    add("| 有有效报价 | 342 | 退市/长期停牌 |")
    add("| 非 ST / 非退市整理 | 201 | 名称含 ST、退 |")
    add("| 上市满 15 个交易日 | 2 | 次新股指标不可用 |")
    add("| 近 20 日日均成交额 ≥ 1 亿 | 2029 | 你的流动性要求 |")
    add("")
    add("**为什么用近 20 日均额而不是当日成交额**：扫描时点是盘中 14:15，当日成交额只有半个交易日，")
    add("会把「全天刚过 1 亿」的一批票误杀。20 日均额同时对单日异动免疫。")
    add("")
    add("**龙头怎么定**：组内（三级类目）按 **总市值**（最新价 × 总股本，TickFlow 的 `total_shares`）降序。")
    add("这比「我认为谁是龙头」客观，也能自动跟上 2026 年的行情变化——比如玻纤组按市值排出来的是")
    add("中国巨石、宏和科技、国际复材、中材科技，正是这轮 AI 电子布涨上来的几家。")
    add("")

    # ---------------- 方案 A 明细 ----------------
    add("## 三、方案 A 明细：每个薄弱三级类目补到 5 只")
    add("")
    add(f"共 {prop5.groupby(['sw_l1', 'sw_l2']).ngroups} 个三级类目需要补，合计 {len(prop5)} 只。")
    add("按二级类目分组，括号里是「现有 → 补后」。")
    add("")
    for l1, grp in prop5.groupby("sw_l1", sort=True):
        have = have_by_l2.get(l1, 0)
        add(f"### {l1}（{have} 只 → {have + len(grp)} 只）")
        add("")
        for l2, g in grp.groupby("sw_l2", sort=True):
            h = have_by_l3.get((l1, l2), 0)
            add(f"- **{l2}**（现有 {h} → {h + len(g)}）：{fmt_items(g)}")
        add("")

    # ---------------- 方案 B 增量 ----------------
    extra8 = prop8.merge(
        prop5[["symbol"]], on="symbol", how="left", indicator=True
    )
    extra8 = extra8[extra8["_merge"] == "left_only"]
    add(f"## 四、方案 B 的增量部分（补到 8 只，比 A 多 {len(extra8)} 只）")
    add("")
    add("A 档已经列过的名单不重复，这里只列「A 之外、B 之内」的部分。")
    add("")
    for l1, grp in extra8.groupby("sw_l1", sort=True):
        add(f"### {l1}")
        add("")
        for l2, g in grp.groupby("sw_l2", sort=True):
            h = have_by_l3.get((l1, l2), 0)
            add(f"- **{l2}**（现有 {h} → {h + len(g) + len(prop5[(prop5.sw_l1 == l1) & (prop5.sw_l2 == l2)])}）：{fmt_items(g)}")
        add("")

    # ---------------- 容量受限 ----------------
    add("## 五、16 个「凑不满 5 只」的类目")
    add("")
    add("这些细分行业在 1 亿成交额门槛下，全市场合格票本身就不到 5 只——不是筛选太严，是行业太小。")
    add("")
    add("| 二级类目 | 三级类目 | 现有 | 全市场合格票 | 补到 |")
    add("| --- | --- | --- | --- | --- |")
    limited = summary[summary["合格候选总数"] < 5]
    for _, r in limited.sort_values(["sw_l1", "sw_l2"]).iterrows():
        add(f"| {r['sw_l1']} | {r['sw_l2']} | {r['现有']} | {r['合格候选总数']} | {r['现有'] + r['待补']} |")
    add("")
    add("**放宽档**：如果愿意把门槛降到 0.5 亿，主方案之外还能再补 "
        f"{len(relax)} 只（覆盖 {relax['sw_l2'].nunique()} 个受限类目），且补进来的多是真二线龙头——")
    add("瑞普生物（0.96 亿）、中牧股份（0.56 亿）、华熙生物（0.79 亿）、稳健医疗（0.86 亿）、")
    add("中国汽研（0.82 亿）、招商积余（0.80 亿）、三只松鼠（0.98 亿）、江苏国泰（0.75 亿）、")
    add("华帝股份（0.66 亿）、明月镜片（0.58 亿）。这些票的日成交额大多在 0.5-1 亿，")
    add("按「少于 1 亿排除」的口径本该出局，所以单列一档，由你决定是否放宽。")
    add("")
    add("| 二级类目 / 三级类目 | 放宽档可补 |")
    add("| --- | --- |")
    for (l1, l2), g in relax.groupby(["sw_l1", "sw_l2"]):
        add(f"| {l1} / {l2} | {fmt_items(g)} |")
    add("")

    # ---------------- 方案 C ----------------
    add("## 六、方案 C：只按二级类目（申万一级）补到 8 只")
    add("")
    add("如果你说的「二级类目」就是我库里的 `category_l2`（申万一级行业，31 个），那只有 8 个行业不足 8 只，")
    add(f"补 {len(l2plan)} 只即可全市场达标：")
    add("")
    for l1, g in l2plan.groupby("项目L2", sort=True):
        add(f"- **{l1}**（{have_by_l2.get(l1, 0)} → {have_by_l2.get(l1, 0) + len(g)}）：{fmt_items(g.rename(columns={'sw_l2': 'sw_l2'}))}")
    add("")
    add("注意：C 档只是把一级行业总数抬到 8，**三级类目仍然是空的**——比如给「商贸零售」补 7 只票，")
    add("但旅游零售、一般零售、贸易里的三个桶可能还是 2-3 只。所以如果目的是「每个类目都能横向比强度」，")
    add("A/B 才解决问题；如果只是「一级行业下别太空」，C 就够。")
    add("")

    # ---------------- 落地 ----------------
    add("## 七、怎么落地")
    add("")
    add("1. **分类树不用动**：`instrument_categories` 里 131 个三级分支（`股票-<一级>-<二级>`）已经全在了，")
    add("   新增标的不会产生新分类。")
    add("2. **加标的**：网页端 `/instruments` 逐只添加，或 `POST /api/add`。")
    add("   类目可以留空——后端按 `stock_industry` 的申万行业自动归类；添加成功后会自动回填历史日 K")
    add("   并重建指标（`InstrumentAddJobManager._run`）。")
    add("3. **注意串行**：新增任务是单标的串行的（`已有新增标的任务正在运行` 会返回 409），")
    add(f"   A 档 {len(prop5)} 只、B 档 {len(prop8)} 只都要排队跑完回填。建议先上 A 档，")
    add("   或者写一个批量脚本直接调 `_build_new_instrument_record` + `backfill_daily_history`。")
    add("4. **加完跑一次全量回填**：`POST /api/backfill-all`，确保新标的的周/月滚动趋势值")
    add("   （`trend_rolling_daily`）也补齐。")
    add("")

    # ---------------- 风险与待办 ----------------
    add("## 八、开工前要处理的 5 件事")
    add("")
    add("### 1. 现有池里还有 2 只 ST")
    add("")
    add("| 代码 | 名称 | 分类 | 20 日均额 |")
    add("| --- | --- | --- | --- |")
    add("| 600759.SS | ST洲际 | 石油石化 / 油气开采 | 3.23 亿 |")
    add("| 688270.SS | ST臻镭 | 电子 / 半导体 | 3.11 亿 |")
    add("")
    add("这两只成交额不低、且 ST 后往往有博弈行情，要不要留由你定；但按你自己「排除 ST」的口径，")
    add("建议在 `/instruments` 里停用（`enabled=0`），而不是删除——历史数据保留，随时可恢复。")
    add("")
    add("### 2. 名称要清洗除权前缀")
    add("")
    xd = "、".join(f"{r['name']} {r['symbol'].split('.')[0]}" for _, r in prefix_names.iterrows())
    add(f"扫描当天有 {len(prefix_names)} 只带除权标记（{xd}），TickFlow 在除权日会把名称压成 `XD圆通速` 这种")
    add("四字截断形式。入库前应剥掉 `XD/XR/DR` 前缀并按 `instruments.get` 的正式名写入，")
    add("否则类目列表里会出现看起来像错字的名字。")
    add("")
    add("### 3. 北交所完全没覆盖")
    add("")
    add("`stock_industry` 里没有任何 `.BJ` 标的（申万 universe 只给了沪深两市），所以像锦波生物这种")
    add("北交所的细分龙头不在候选池里。这次不影响结论（北交所成交额普遍偏小），但要知道这是盲区。")
    add("")
    add("### 4. 扩充会重算全部股票的「强度百分位」")
    add("")
    add("`services/dashboard_common.assign_strength` 的 scope 是 `category_l1`——股票类标的的强度百分位")
    add(f"是在**整个股票池内**排名，不是类目内。池子从 {len(cur)} 只扩到 {len(cur) + len(prop5)} 只后，")
    add("所有股票的 `strength` 数值都会变（ETF 不受影响）。如果哪条信号用了强度的绝对阈值，")
    add("扩池前后要重新标定。")
    add("")
    add("### 5. 建议人工过一眼的名单")
    add("")
    add("下面这些是市值排名算出来的，但存在**更名/主业变更/分类可能过时**的情况，建议开仓前确认：")
    add("")
    add("| 代码 | 名称 | 被排进的类目 | 疑点 |")
    add("| --- | --- | --- | --- |")
    add("| 600292.SS | 电投水电 | 环保 / 环境治理 | 原远达环保，重组为水电，申万分类可能未更新 |")
    add("| 600388.SS | 紫金龙净 | 环保 / 环保设备 | 原龙净环保，已更名 |")
    add("| 601777.SS | 千里科技 | 汽车 / 摩托车及其他 | 原力帆科技，主业转向智驾 |")
    add("| 600186.SS | 莲花控股 | 食品饮料 / 调味发酵品 | 味精主业 + 算力，题材属性强（20 日均额 28.6 亿） |")
    add("| 000629.SZ | 钒钛股份 | 钢铁 / 冶钢原料 | 攀钢钒钛，钒电池概念 |")
    add("| 600676.SS | 久事动娱 | 汽车 / 汽车服务 | 已更名，主业存疑 |")
    add("| 002354.SZ | 天娱数科 | 传媒 / 广告营销 | 小市值高换手（21.6 亿），投机属性强 |")
    add("| 000592.SZ | 平潭发展 | 农林牧渔 / 林业 | 高换手（21.3 亿），题材股 |")
    add("")
    add("这几只的共同特征是「市值排名靠前但行业代表性一般」，是纯量化排序的已知副作用。")
    add("剔掉它们、用组内第 N+1 名递补（`data/candidates_by_l3.csv` 里有完整的组内排名）即可。")
    add("")

    # ---------------- 产物清单 ----------------
    add("## 九、产物文件")
    add("")
    add("| 文件 | 内容 |")
    add("| --- | --- |")
    add("| `data/market_snapshot.csv` | 全市场 5552 只：名称/现价/成交额/总市值/换手率/上市日/ST 标记 |")
    add("| `data/liquidity_20d.csv` | 每只票近 20 日日均成交额 |")
    add("| `data/candidates_by_l3.csv` | **完整合格池**，按三级类目分组、市值降序、标注是否已纳入 |")
    add("| `data/proposed_by_l3_target5.csv` | 方案 A 名单（267 只） |")
    add("| `data/proposed_by_l3.csv` | 方案 B 名单（469 只） |")
    add("| `data/proposed_l2_level.csv` | 方案 C 名单（45 只） |")
    add("| `data/proposed_relaxed_0p5yi.csv` | 放宽档增量名单（41 只，覆盖 14 个受限类目） |")
    add("| `data/summary_by_l1.csv` / `summary_by_l2.csv` | 二级/三级类目维度的补齐汇总 |")
    add("")
    add("## 十、二级类目总览（补齐后）")
    add("")
    add("| 二级类目 | 现有 | 补到 5 只后 | 补到 8 只后 | 不足 8 只的三级类目 |")
    add("| --- | --- | --- | --- | --- |")
    for _, r in summary_l1.sort_values("现有").iterrows():
        add(
            f"| {r['项目L2']} | {r['现有']} | {r['补到5只']} | {r['补到8只']} | {r['不足8只的三级类目']} |"
        )
    add("")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"写出 {OUT}（{len(lines)} 行）")


if __name__ == "__main__":
    main()
