"""回测分析数据导出（方案 2026-08-30 §8）：long-format CSV + manifest，供 AI 离线分析。

设计决策（方案 §8.2）：
1. long-format 不透视 —— 透视留给分析端，一行一事实；
2. CSV 用 utf-8-sig（Excel 兼容），数值不格式化（保留全精度）；
3. manifest.json 逐字段口径说明 + 三个已知偏差声明（止损成交价假设 /
   幸存者偏差 / ATR 口径差异）—— AI 没有口径说明会自己脑补；
4. 数据源零重跑：全部读 batch_backtest_cells 现有列/blob 与
   batch_backtest_symbol_features，纯查询 + 写文件。

入口：scripts/export_backtest_analysis.py（CLI）与
GET /batch-backtest/api/runs/{batch_id}/export（远程/自动化）共用本模块。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from data.storage.db import Database
from rule_backtest.batch_service import (
    ATR_BASIS,
    SWEEP_CHANDELIER_RATIO,
    aggregate_annual_returns,
    aggregate_stop_diagnostics,
    compare_batches,
)

# round_trips.csv 逐字段口径（manifest 原样带出；AI 消费的分水岭，方案 §8.2.3）。
ROUND_TRIP_FIELD_DOCS: dict[str, str] = {
    "symbol": "标的代码",
    "entry_date / exit_date": "入/出场日期（YYYY-MM-DD）",
    "entry_price / exit_price": "含滑点成交价（前复权口径，与回测行情一致）",
    "exit_reason": "hard_stop / chandelier_stop / chandelier_stop_ratchet / exit_conditions_passed",
    "qty": "成交股数",
    "pnl": "净盈亏（扣佣金与印花税），单位元",
    "r_multiple": "pnl ÷ (qty × hard_stop_atr_mul × entry_atr)；分母≤0（无硬止损）时为 NULL",
    "mae_pct / mfe_pct": "持有期最低/最高价相对入场价的最大不利/有利偏移，单位 %（含出入场当日）",
    "mae_atr / mfe_atr": "同上，单位 = 入场 ATR；entry_atr≤0 时为 NULL",
    "holding_days / days_to_trigger": "入场到出场相隔交易日数（两者同义）",
    "entry_atr": "入场时 ATR（T-1 收盘口径，不含入场日当根 K 线）",
    "entry_atr_pct": "entry_atr ÷ entry_price；波动率分桶的直接依据",
    "asset_type": "stock / etf",
    "category_l1": "标的一级类目（批量服务层补记）",
    "hard_stop_atr_mul / chandelier_atr_mul": "本笔实际使用的止损 ATR 倍数（紧/松档溯源）",
    "post_exit_ret_5d/10d/20d": "出场后第 N 个交易日收盘相对出场价；回测区间末尾出场、未来 K 线不足时为 NULL（聚合跳过）",
    "reentry_above_entry_5d/10d/20d": "出场后 N 日内收盘是否重新站回入场价（假止损判据）；未来数据不足时 NULL",
    "strategy / trend_score_avg": "来自格子的策略名与标的趋势分均值（分桶语境）",
}

BIAS_DISCLOSURES = {
    "stop_fill_assumption": (
        "止损成交价假设：stop_gap_fill=on 时止损触发日开盘价穿透止损价按开盘价成交；"
        "旧批次（2026-08-30 前）无此修正，假设永远能在止损价成交，系统性美化紧止损档。"
    ),
    "survivorship_bias": (
        "幸存者偏差：标的池来自 instrument_metadata.enabled=1（现在还活着的票），"
        "退市/腰斩的昔日龙头不在池中；暴雷股恰是松止损亏最多的地方 —— 系统性美化松档。"
        "一期只声明不消除。"
    ),
    "atr_basis": (
        "ATR 口径：回测引擎入场 ATR 为 T-1 收盘已完成的值（prev_close，2026-08-30 起）；"
        "实盘 stop_loss.py 是盘中实时守护，含当根 K 线，口径有意不同。旧批次为含当根口径。"
    ),
}


def _write_csv(df: pd.DataFrame, path: Path) -> int:
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return len(df)


def _round_trips_frame(rows: list[dict]) -> pd.DataFrame:
    """展平 ok 格子的 round_trips_json，附格子级语境列。"""
    flat: list[dict] = []
    for row in rows:
        try:
            trips = json.loads(row.get("round_trips_json") or "[]")
        except (ValueError, TypeError):
            continue
        for t in trips or []:
            if isinstance(t, dict):
                flat.append(
                    {
                        "strategy": row.get("strategy_name") or row.get("strategy_id"),
                        "strategy_id": row.get("strategy_id"),
                        "symbol_name": row.get("symbol_name"),
                        "trend_score_avg": row.get("trend_score_avg"),
                        **t,
                    }
                )
    return pd.DataFrame(flat)


def _live_trades_frame(db: Database) -> pd.DataFrame:
    """实盘逐笔（已清仓）+ 同口径 post-exit 漂移（方案 §8.1 live_trades.csv）。

    价格为实盘实际成交价（未复权），post-exit 漂移用 raw 行情（同一口径）。
    """
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT * FROM manual_trades WHERE status = 'closed' ORDER BY user_id, id"
        ).fetchall()
    out: list[dict[str, Any]] = []
    bars_cache: dict[str, pd.DataFrame] = {}
    for row in rows:
        r = dict(row)
        buy_price = float(r["buy_price"])
        sell_price = float(r["sell_price"])
        item: dict[str, Any] = {
            "user_id": r["user_id"],
            "symbol": r["symbol"],
            "buy_date": r["buy_date"],
            "buy_price": buy_price,
            "sell_date": r["sell_date"],
            "sell_price": sell_price,
            "shares": r["shares"],
            "realized_pnl": (sell_price - buy_price) * float(r["shares"]),
            "realized_pnl_pct": (sell_price / buy_price - 1.0) * 100.0 if buy_price > 0 else None,
        }
        symbol = str(r["symbol"])
        if symbol not in bars_cache:
            # 实盘成交价是未复权口径，漂移优先用 raw 行情保持同一把尺；
            # raw 缺失（未做 raw→qfq 迁移的库）回退 qfq，漂移为近似值。
            bars = db.load_market_data(symbol, price_mode="raw")
            if bars.empty:
                bars = db.load_market_data(symbol, price_mode="qfq")
            bars_cache[symbol] = bars
        bars = bars_cache[symbol]
        if not bars.empty:
            df = bars.copy()
            df["day"] = pd.to_datetime(df["time"], errors="coerce").dt.normalize()
            sell_day = pd.Timestamp(str(r["sell_date"]))
            future = df[df["day"] > sell_day].sort_values("day")
            closes = pd.to_numeric(future["close"], errors="coerce").dropna().tolist()
            for n in (5, 10, 20):
                if len(closes) >= n and sell_price > 0:
                    window = closes[:n]
                    item[f"post_exit_ret_{n}d"] = window[-1] / sell_price - 1.0
                    item[f"reentry_above_entry_{n}d"] = any(c > buy_price for c in window)
                else:
                    item[f"post_exit_ret_{n}d"] = None
                    item[f"reentry_above_entry_{n}d"] = None
        out.append(item)
    return pd.DataFrame(out)


def export_batch_analysis(
    db: Database,
    batch_id: str,
    alt_batch_id: str | None = None,
    out_dir: Path | str = Path("exports"),
    include_live: bool = True,
) -> dict:
    """导出一个批次（可选与另一批次对比）的全部分析文件，返回导出目录与文件清单。"""
    batch = db.get_batch_run(batch_id)
    if batch is None:
        raise ValueError(f"批次不存在: {batch_id}")
    alt = db.get_batch_run(alt_batch_id) if alt_batch_id else None
    if alt_batch_id and alt is None:
        raise ValueError(f"批次不存在: {alt_batch_id}")

    out_root = Path(out_dir)
    dir_name = f"batch_{batch_id}" + (f"_vs_{alt_batch_id}" if alt else "")
    target = out_root / dir_name
    target.mkdir(parents=True, exist_ok=True)

    files: dict[str, int] = {}

    cells = db.get_batch_cells(batch_id)
    files["cells.csv"] = _write_csv(pd.DataFrame(cells), target / "cells.csv")

    rt_rows = db.get_batch_roundtrip_rows(batch_id)
    files["round_trips.csv"] = _write_csv(_round_trips_frame(rt_rows), target / "round_trips.csv")

    annual = aggregate_annual_returns(db.get_batch_annual_blobs(batch_id))
    files["annual_aggregates.csv"] = _write_csv(pd.DataFrame(annual), target / "annual_aggregates.csv")

    diags = aggregate_stop_diagnostics(rt_rows)
    files["stop_diagnostics.csv"] = _write_csv(pd.DataFrame(diags), target / "stop_diagnostics.csv")

    if alt is not None:
        alt_rt_rows = db.get_batch_roundtrip_rows(alt_batch_id)
        files["cells_alt.csv"] = _write_csv(
            pd.DataFrame(db.get_batch_cells(alt_batch_id)), target / "cells_alt.csv"
        )
        files["round_trips_alt.csv"] = _write_csv(
            _round_trips_frame(alt_rt_rows), target / "round_trips_alt.csv"
        )
        files["stop_diagnostics_alt.csv"] = _write_csv(
            pd.DataFrame(aggregate_stop_diagnostics(alt_rt_rows)),
            target / "stop_diagnostics_alt.csv",
        )
        comparison = compare_batches(db, batch_id, alt_batch_id)
        files["compare_cell_diffs.csv"] = _write_csv(
            pd.DataFrame(comparison.pop("cell_diffs")), target / "compare_cell_diffs.csv"
        )
        files["compare_bootstrap_ci.csv"] = _write_csv(
            pd.DataFrame(comparison.pop("bootstrap_ci")), target / "compare_bootstrap_ci.csv"
        )
        (target / "compare_meta.json").write_text(
            json.dumps(comparison, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        files["compare_meta.json"] = 1

    if include_live:
        live = _live_trades_frame(db)
        files["live_trades.csv"] = _write_csv(live, target / "live_trades.csv")

    config = json.loads(batch.get("config_json") or "{}")
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "generator": "scripts/export_backtest_analysis.py（方案 2026-08-30 §8）",
        "batch": {
            "batch_id": batch_id,
            "name": batch.get("name"),
            "status": batch.get("status"),
            "stop_profile": batch.get("stop_profile", "default"),
            "atr_basis": batch.get("atr_basis") or "entry_bar(旧批次)",
            "data_anchor_date": batch.get("data_anchor_date"),
            "data_version": batch.get("data_version"),
            "engine_version": batch.get("engine_version"),
            "config": config,
        },
        "alt_batch": (
            {
                "batch_id": alt_batch_id,
                "name": alt.get("name"),
                "stop_profile": alt.get("stop_profile", "default"),
                "atr_basis": alt.get("atr_basis") or "entry_bar(旧批次)",
            }
            if alt
            else None
        ),
        "calibers": {
            "price_adjustment": "qfq 前复权（回测行情与 round-trip 价格；live_trades 为未复权实际成交价）",
            "fees": "pnl 已扣佣金与印花税；滑点含在成交价中",
            "none_means": "NULL = 数据不足或不适用（如区间末尾出场的 post-exit 字段、无硬止损时的 r_multiple）",
            "sweep_chandelier_ratio": (
                f"sweep 批次吊灯倍数固定 = hard × {SWEEP_CHANDELIER_RATIO}（方案 §10 拍板）"
                if batch.get("stop_profile") == "sweep"
                else None
            ),
        },
        "bias_disclosures": BIAS_DISCLOSURES,
        "round_trip_fields": ROUND_TRIP_FIELD_DOCS,
        "low_confidence_rule": "stop_diagnostics 中 n < 30 的桶 low_confidence=true",
        "files": {name: {"rows": rows} for name, rows in files.items()},
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    files["manifest.json"] = 1

    return {"export_dir": str(target), "files": files}
