"""ETF 主题去重：把「同一只指数的 benchmark 写法差异」与「同一赛道」归并。

分两级，互相独立：
  Level A（机械、可复现）：剥离汇率前缀、交易所全称/简称、黄金写法等 —— 这些是同一只指数。
  Level B（主观、可调）：按赛道关键词把相近指数归到一组（如各类半导体指数合成「半导体芯片」）。

产出：
  data/etf_candidates_dedup.csv   带 theme_norm（Level A）与 sector（Level B）两列
  data/etf_dedup_preview.md        分组预览，按赛道列出每组留下的代表

用法：.venv/bin/python dedup_etf_themes.py
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
SRC = DATA / "etf_candidates.csv"
OUT = DATA / "etf_candidates_dedup.csv"
PREVIEW = DATA / "etf_dedup_preview.md"

# ---------------------------------------------------------------- Level A

FX_PREFIX = re.compile(
    r"^(经估值汇率调整的|经估值汇率调整后的|经人民币汇率调整的|经汇率调整后的|人民币汇率调整的|同期)"
)
THEME_FIX = {
    "中证小盘500": "中证500",
    "创业板指数P": "创业板",
    "上海证券交易所50成份": "上证50",
    "上海证券交易所180成份": "上证180",
    "上海证券交易所综合": "上证综合",
    "上海证券交易所上证红利": "上证红利",
    "深圳证券交易所成份": "深证成指",
    "中小企业100": "中小100",
    "德国法兰克福DAX": "德国DAX",
    "巴西伊博维斯帕": "巴西IBOVESPA",
    "A股上限": "标普中国新经济行业",
}


def norm_theme(theme: str) -> str:
    s = FX_PREFIX.sub("", str(theme or "")).strip()
    s = re.sub(r"(指数同期|指数)$", "", s).strip() or s
    if s in THEME_FIX:
        return THEME_FIX[s]
    if "Au99.99" in s or "黄金现货实盘" in s:
        return "黄金现货Au99.99"
    if "上海金" in s or "SHAU" in s:
        return "上海金集中定价"
    return s


# ---------------------------------------------------------------- Level B

CROSS_BORDER = re.compile(
    r"港股|恒生|香港|纳斯达克|标普|道琼斯|日经|德国|法国|东证|沙特|巴西|亚太|东南亚|亚洲|"
    r"中韩|全球|海外|美国|中概|MSCI|富时|新交所|IBOVESPA|DAX|CAC|TOPIX|中国互联网"
)
# (赛道名, 关键词) —— 顺序即优先级，先匹配到者胜
DOMESTIC = [
    ("半导体芯片", ["半导体", "芯片", "集成电路"]),
    ("消费电子", ["消费电子", "电子50", "中证电子", "国证电子"]),
    ("人工智能", ["人工智能"]),
    ("机器人", ["机器人"]),
    ("云计算软件", ["软件", "计算机", "云计算", "大数据", "信息技术", "数据"]),
    ("通信", ["通信", "5G"]),
    ("传媒游戏", ["传媒", "游戏", "动漫"]),
    ("军工", ["军工", "国防", "兵装"]),
    ("航天航空卫星", ["航空", "航天", "卫星"]),
    ("船舶", ["船舶"]),
    ("创新药", ["创新药"]),
    ("医疗器械", ["医疗器械"]),
    ("中药", ["中药"]),
    ("医药生物", ["医药", "医疗", "生物"]),
    ("白酒", ["酒"]),
    ("食品饮料", ["食品饮料", "主要消费", "消费50", "消费80"]),
    ("家电", ["家用电器", "家电"]),
    ("旅游", ["旅游"]),
    ("农业养殖", ["农业", "养殖", "畜牧", "粮食"]),
    ("汽车", ["汽车", "零部件"]),
    ("新能源车电池", ["新能源汽车", "新能源车", "电池", "储能"]),
    ("光伏", ["光伏"]),
    ("新能源", ["新能源"]),
    ("有色金属", ["有色", "金属", "稀土"]),
    ("煤炭", ["煤炭"]),
    ("钢铁", ["钢铁"]),
    ("石油天然气", ["石油", "油气", "能源"]),
    ("化工", ["化工", "石化"]),
    ("电力", ["电力", "公用事业"]),
    ("电网设备", ["电网"]),
    ("环保低碳", ["环保", "碳中和", "低碳", "绿色电力"]),
    ("证券", ["证券", "券商"]),
    ("银行", ["银行"]),
    ("保险非银", ["保险", "非银行金融"]),
    ("地产", ["地产", "房地产"]),
    ("基建建筑建材", ["基建", "建筑", "建材"]),
    ("机械", ["机械", "机床"]),
    ("交运物流", ["交运", "物流", "港口"]),
]
# 港股系：医药必须排在科技之前，否则「恒生生物科技」会被判成科技
HK_BUCKETS = [
    ("跨境-中概互联", ["中国互联网", "中概"]),
    ("跨境-港股医药", ["医药", "医疗", "生物", "创新药", "保健"]),
    ("跨境-港股科技", ["科技", "互联网", "信息技术", "软件", "数据", "通信", "半导体", "芯片"]),
    ("跨境-港股金融", ["证券", "金融", "银行", "保险"]),
    ("跨境-港股消费", ["消费", "汽车", "教育", "旅游", "博彩"]),
    ("跨境-港股红利", ["红利", "高股息", "低波"]),
]
US_BUCKETS = [
    ("跨境-美股行业", ["生物科技", "生物医药", "消费精选", "石油", "天然气", "房地产"]),
]
BROAD = ("沪深300", "中证500", "中证1000", "中证2000", "国证2000", "中证800", "中证A500", "中证A50",
         "中证A100", "上证50", "上证180", "上证380", "上证综合", "上证中盘", "中小100", "深证100",
         "深证50", "深证成指", "创业板", "创业板50", "上证科创板50成份", "上证科创板100",
         "上证科创板200", "上证科创板综合", "中证科创创业50", "中证全指")
DIVIDEND = ("红利", "高股息")
FUTURES = ("期货", "豆粕", "能源化工", "有色金属期货")
HK_MARK = ("港股", "恒生", "香港", "中国互联网", "海外中国")
US_MARK = ("纳斯达克", "标普", "道琼斯", "美国", "日经", "德国", "法国", "东证", "沙特", "巴西",
           "亚太", "东南亚", "亚洲", "中韩", "越南", "DAX", "CAC", "TOPIX", "IBOVESPA")


def sector_of(t: str) -> str:
    """赛道归并：只有「同一暴露的不同版本」才合并，不同指数各自独立。

    - 黄金：黄金现货Au99.99 与 上海金集中定价 合成「黄金」
    - 红利族合并；行业主题按赛道合并
    - 宽基、债券、美股宽基（纳指≠标普≠道指）、港股宽基（恒生≠恒生中国企业）一律保持独立
    """
    if "黄金" in t or "上海金" in t or "Au99.99" in t:
        return "黄金"
    if any(k in t for k in FUTURES):
        return t
    if "债" in t or "短融" in t or "存款" in t:
        return t
    if any(b in t for b in BROAD):
        return t
    # 「标普中国A股…」跟踪的是 A 股，不是港股
    if "标普中国A股" in t:
        return "红利策略" if any(k in t for k in DIVIDEND) else t
    if any(k in t for k in HK_MARK):
        for name, kws in HK_BUCKETS:
            if any(k in t for k in kws):
                return name
        return t
    if any(k in t for k in US_MARK):
        for name, kws in US_BUCKETS:
            if any(k in t for k in kws):
                return name
        return t
    if any(k in t for k in DIVIDEND):
        return "红利策略"
    for name, kws in DOMESTIC:
        if any(k in t for k in kws):
            return name
    return t


def main() -> None:
    rows = list(csv.DictReader(SRC.open(encoding="utf-8")))
    for r in rows:
        r["theme_norm"] = norm_theme(r["theme"])
        r["sector"] = sector_of(r["theme_norm"])

    # 逐级去重：Level A 按 theme_norm，Level B 按 sector，组内留近一年日均最大者
    by_a: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_a[r["theme_norm"]].append(r)
    for v in by_a.values():
        v.sort(key=lambda r: -float(r["latest_avg_daily_yi"]))
    level_a = [v[0] for v in by_a.values()]

    by_b: dict[str, list[dict]] = defaultdict(list)
    for r in level_a:
        by_b[r["sector"]].append(r)
    for v in by_b.values():
        v.sort(key=lambda r: -float(r["latest_avg_daily_yi"]))
    level_b = [v[0] for v in by_b.values()]

    fields = list(rows[0].keys())
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: -float(r["latest_avg_daily_yi"])))

    # 最终清单：Level B 代表（每赛道一只）
    final = sorted(level_b, key=lambda r: -float(r["latest_avg_daily_yi"]))
    with (DATA / "etf_candidates_final.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(final)

    lines = [
        f"原清单 {len(rows)} 只 / {len({r['theme'] for r in rows})} 个原始主题",
        f"Level A（同一指数写法归并）后：{len(level_a)} 只 / {len(by_a)} 个主题",
        f"Level B（赛道归并）后：{len(level_b)} 只 / {len(by_b)} 个赛道",
        "",
    ]
    for sector in sorted(by_b, key=lambda s: -len(by_b[s])):
        group = by_b[sector]
        lines.append(f"### {sector}（{len(group)} 个主题 → 代表 {group[0]['symbol']} {group[0]['name']}）")
        if len(group) > 1:
            for r in group:
                mark = "★" if r is group[0] else " "
                lines.append(
                    f"  {mark} {r['symbol']} {r['name'][:26]:28s} [{r['theme_norm'][:22]:24s}] "
                    f"{r['latest_avg_daily_yi']:>9s}亿"
                )
        lines.append("")
    PREVIEW.write_text("\n".join(lines), encoding="utf-8")

    print("\n".join(lines[:3]))
    print(f"-> {OUT.name} / {PREVIEW.name}")


if __name__ == "__main__":
    main()
