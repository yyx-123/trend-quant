"""批量回测路由（方案 §5.3）：按一级类目 × 多策略批量执行规则回测。

执行模型：一个后台 daemon 线程跑一个批次（同时只允许一个 running 批次，
409 拦截 + DB 事务兜底）；取消走内存 threading.Event（协作式，不落库，
服务重启由启动清理把 running 批次置为 interrupted）。
"""

from __future__ import annotations

import json
import threading
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from audit.app_logger import get_logger
from data.storage import db as db_module
from rule_backtest.batch_service import (
    BatchBacktestService,
    aggregate_annual_returns,
    aggregate_stop_diagnostics,
    compare_batches,
    estimate_batch_seconds,
    strategy_uses_random_indicator,
)
from rule_backtest.loader import StrategyLoader

logger = get_logger(__name__)

router = APIRouter(prefix="/batch-backtest", tags=["batch-backtest"])
from core.paths import web_dir as _web_dir

_templates_dir = _web_dir() / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))

# In-memory cancel events for running batches (per-process, like _rule_jobs;
# a restart orphans the worker thread anyway and startup cleanup marks the
# batch 'interrupted').
_batch_cancel_events: dict[str, threading.Event] = {}
_batch_cancel_lock = threading.Lock()


class BatchRunRequest(BaseModel):
    categories: list[str] = Field(default_factory=list)
    strategy_ids: list[str] = Field(default_factory=list)
    name: str = Field(default="")
    start_date: str = Field(default="")
    end_date: str = Field(default="")
    # 止损档位（方案 2026-08-30 §5.1）：tight/loose 复用实盘口径；
    # sweep 为极低频操作，页面不提供入口（走 scripts/run_stop_sweep.py）。
    stop_profile: Literal["default", "tight", "loose", "sweep"] = "default"
    # 复选档位：勾多个时按顺序自动排队跑 N 个批次（紧/松对比的标准姿势）。
    # 与 stop_profile 二选一 —— 传了 stop_profiles 就以它为准。
    stop_profiles: list[Literal["default", "tight", "loose"]] | None = None
    sweep_atr_muls: list[float] | None = None


def _parse_window_date(value: str, field_label: str) -> date | None:
    text = value.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{field_label}格式非法（应为 YYYY-MM-DD）") from exc


@router.get("", response_class=HTMLResponse)
async def batch_backtest_page(request: Request) -> HTMLResponse:
    # no-cache：页面内联了全部交互 JS，部署修复后必须让浏览器重新校验，
    # 否则旧脚本（无超时/无防重的版本）可能被缓存继续用。
    return templates.TemplateResponse(
        name="batch_backtest.html",
        request=request,
        context={"title": "批量回测"},
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/api/meta")
async def get_batch_meta() -> dict:
    """L1 categories (with symbol counts + ETA) and strategies (random flagged)."""
    db = db_module.get_db()
    categories: dict[str, dict] = {}
    loader = StrategyLoader()
    strategies = []
    for item in loader.list_strategies():
        sid = str(item.get("id", ""))
        is_random = False
        try:
            is_random = strategy_uses_random_indicator(loader.load(sid))
        except Exception as exc:  # unloadable strategies are excluded from batch anyway
            logger.debug("skip unloadable strategy %s in batch meta: %s", sid, exc)
            continue
        strategies.append(
            {
                "id": sid,
                "name": str(item.get("name", "") or sid),
                "uses_random_indicator": is_random,
            }
        )

    cat_map: dict[str, list[dict]] = {}
    # resolve_batch_symbols expects categories; reuse its filtering per L1.
    all_items = db.list_instrument_metadata()
    bar_counts = db.count_bars_by_symbol()
    for item in all_items:
        if not item.get("enabled", True):
            continue
        l1 = str(item.get("category_l1") or "")
        if not l1:
            continue
        cat_map.setdefault(l1, []).append(item)
    for l1, items in sorted(cat_map.items()):
        symbols = [
            {"symbol": str(i.get("symbol")), "bar_count": bar_counts.get(str(i.get("symbol")), 0)}
            for i in items
        ]
        eta_one = estimate_batch_seconds(symbols, 1)
        categories[l1] = {
            "name": l1,
            "symbol_count": len(symbols),
            "estimated_seconds_per_strategy": round(eta_one, 1),
        }

    running = db.get_running_batch_run()
    return {
        "categories": list(categories.values()),
        "strategies": strategies,
        "running_batch_id": running["batch_id"] if running else None,
    }


@router.post("/api/run")
async def run_batch_backtest(payload: BatchRunRequest) -> dict:
    if not payload.categories:
        raise HTTPException(status_code=400, detail="至少需要选择一个一级类目")
    if not payload.strategy_ids:
        raise HTTPException(status_code=400, detail="至少需要选择一个策略")

    # 复选档位（去重保序）；单档走 stop_profile，向后兼容
    profiles: list[str] = []
    for p in payload.stop_profiles or [payload.stop_profile]:
        if p not in profiles:
            profiles.append(p)

    service = BatchBacktestService()
    start = _parse_window_date(payload.start_date, "开始日期")
    end = _parse_window_date(payload.end_date, "结束日期")
    try:
        # 所有档位同步 prepare（校验失败在排队前暴露，400 而不是后台静默）
        batches = [
            service.prepare_batch(
                categories=payload.categories,
                strategy_ids=payload.strategy_ids,
                name=payload.name,
                start_date=start,
                end_date=end,
                stop_profile=profile,
                sweep_atr_muls=payload.sweep_atr_muls,
            )
            for profile in profiles
        ]
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not db_module.get_db().create_batch_run_if_idle(batches[0]):
        running = db_module.get_db().get_running_batch_run()
        raise HTTPException(
            status_code=409,
            detail=f"已有批次正在运行（{(running or {}).get('name', '?')}），请等待完成或先取消",
        )

    # 一个 cancel event 贯穿整链：取消当前批次即终止后续排队档位。
    cancel_event = threading.Event()
    first = batches[0]
    with _batch_cancel_lock:
        _batch_cancel_events[first["batch_id"]] = cancel_event

    logger.info(
        "Batch backtest queued batch_id=%s name=%s cells=%d profiles=%s",
        first["batch_id"], first["name"], first["total_cells"], profiles,
    )

    def _run() -> None:
        try:
            for i, batch in enumerate(batches):
                if cancel_event.is_set():
                    break
                if i > 0:
                    # 前一批次跑完才创建下一批（idle 检查）；恰有别的批次插入时
                    # 跳过该档而不是卡住整链。
                    if not db_module.get_db().create_batch_run_if_idle(batch):
                        logger.warning(
                            "Queued profile batch skipped (busy) batch_id=%s profile=%s",
                            batch["batch_id"], batch.get("stop_profile"),
                        )
                        continue
                    with _batch_cancel_lock:
                        _batch_cancel_events[batch["batch_id"]] = cancel_event
                try:
                    # Per-run service instance to avoid sharing engine state across threads.
                    BatchBacktestService().run_batch(batch["batch_id"], cancel_event=cancel_event)
                finally:
                    with _batch_cancel_lock:
                        _batch_cancel_events.pop(batch["batch_id"], None)
        except Exception:
            logger.exception("Batch backtest thread crashed batch_id=%s", first["batch_id"])

    thread = threading.Thread(target=_run, daemon=True, name=f"batch-backtest-{first['batch_id']}")
    thread.start()
    return {
        "batch_id": first["batch_id"],
        "status": "running",
        "total_cells": first["total_cells"],
        "stop_profiles": profiles,
        "queued_batches": len(profiles),
    }


@router.get("/api/progress/{batch_id}")
async def get_batch_progress(batch_id: str) -> dict:
    batch = db_module.get_db().get_batch_run(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    return {
        "batch_id": batch["batch_id"],
        "status": batch["status"],
        "total_cells": batch["total_cells"],
        "done_cells": batch["done_cells"],
        "ok_cells": batch["ok_cells"],
        "failed_cells": batch["failed_cells"],
        "skipped_cells": batch["skipped_cells"],
        "current_symbol": batch["current_symbol"],
        "error": batch["error"],
    }


@router.post("/api/cancel/{batch_id}")
async def cancel_batch(batch_id: str) -> dict:
    batch = db_module.get_db().get_batch_run(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    if batch["status"] != "running":
        return {"batch_id": batch_id, "status": batch["status"], "message": "批次已结束"}
    with _batch_cancel_lock:
        event = _batch_cancel_events.get(batch_id)
    if event is not None:
        event.set()
    return {"batch_id": batch_id, "status": "cancelling"}


@router.get("/api/runs")
async def list_batch_runs() -> dict:
    return {"runs": db_module.get_db().list_batch_runs()}


def _parse_cell_blobs(row: dict) -> dict:
    for key in (
        "annual_returns_json",
        "monthly_heatmap_json",
        "trades_json",
        "skipped_buys_json",
        "monthly_nav_json",
        "round_trips_json",
    ):
        text = row.pop(key, None)
        out_key = key[: -len("_json")]
        if text:
            try:
                row[out_key] = json.loads(text)
            except (ValueError, TypeError):
                row[out_key] = None
        else:
            row[out_key] = None
    return row


@router.get("/api/runs/{batch_id}/cells")
async def get_batch_cells(batch_id: str, response: Response) -> dict:
    batch = db_module.get_db().get_batch_run(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    # 已结束批次的格子不可变（重跑会产生新批次号），允许浏览器缓存这份数 MB 的
    # 响应，避免弱网环境下每次载入都重传；运行中的批次结果仍在增长，禁止缓存。
    response.headers["Cache-Control"] = (
        "no-store" if batch["status"] == "running" else "private, max-age=3600"
    )
    return {
        "batch": batch,
        "cells": db_module.get_db().get_batch_cells(batch_id),
    }


@router.get("/api/runs/{batch_id}/cell")
async def get_batch_cell_detail(batch_id: str, symbol: str, strategy_id: str) -> dict:
    row = db_module.get_db().get_batch_cell_detail(batch_id, symbol.strip().upper(), strategy_id)
    if row is None:
        raise HTTPException(status_code=404, detail="格子不存在")
    return _parse_cell_blobs(row)


@router.get("/api/runs/{batch_id}/annual-aggregates")
async def get_batch_annual_aggregates(batch_id: str) -> dict:
    """策略×年份聚合：ok 格子的年度收益 blob 按（策略, 年份）取中位数。"""
    batch = db_module.get_db().get_batch_run(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    rows = db_module.get_db().get_batch_annual_blobs(batch_id)
    return {"batch_id": batch_id, "aggregates": aggregate_annual_returns(rows)}


@router.get("/api/runs/{batch_id}/stop-diagnostics")
async def get_batch_stop_diagnostics(batch_id: str) -> dict:
    """止损专项诊断（方案 §3.2/§4）：分桶 × 策略 的 long-format 聚合。"""
    batch = db_module.get_db().get_batch_run(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    rows = db_module.get_db().get_batch_roundtrip_rows(batch_id)
    return {
        "batch_id": batch_id,
        "stop_profile": batch.get("stop_profile", "default"),
        "diagnostics": aggregate_stop_diagnostics(rows),
    }


@router.get("/api/compare")
async def compare_batch_runs(base_batch_id: str, alt_batch_id: str) -> dict:
    """紧/松（或任意两批次）并排对比（方案 §6.3）：逐格差值 + 诊断 + bootstrap CI。

    要求两批次同标的池同策略：取 symbol×strategy 交集，交集为空报 409。
    """
    db = db_module.get_db()
    base = db.get_batch_run(base_batch_id)
    alt = db.get_batch_run(alt_batch_id)
    if base is None or alt is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    if base["status"] == "running" or alt["status"] == "running":
        raise HTTPException(status_code=409, detail="批次仍在运行，完成后再对比")
    result = compare_batches(db, base_batch_id, alt_batch_id)
    if result["common_cells"] == 0:
        raise HTTPException(status_code=409, detail="两批次没有共同的 标的×策略 格子，无法对比")
    return result


@router.get("/api/runs/{batch_id}/snapshot")
async def get_batch_strategy_snapshot(batch_id: str, strategy_id: str, symbol: str = "") -> dict:
    """钻取链路：返回批次快照中的策略配置 + 格子的实际回测区间。

    market_view 快照模式用它直接构造回测请求（跳过 StrategyLoader），
    保证钻取重跑与批次时的策略版本、数据区间一致。
    """
    batch = db_module.get_db().get_batch_run(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    snapshot = json.loads(batch["strategy_snapshot_json"])
    entry = next((s for s in snapshot if s.get("id") == strategy_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="批次快照中不存在该策略")
    start_date = None
    end_date = batch.get("data_anchor_date")
    if symbol.strip():
        cell = db_module.get_db().get_batch_cell_detail(batch_id, symbol.strip().upper(), strategy_id)
        if cell is not None:
            start_date = cell.get("start_date")
            end_date = cell.get("end_date") or end_date
    return {
        "batch_id": batch_id,
        "batch_name": batch.get("name", ""),
        "strategy_id": strategy_id,
        "strategy_name": entry.get("name", ""),
        "strategy_config": entry["strategy_config"],
        "start_date": start_date,
        "end_date": end_date,
        "data_anchor_date": batch.get("data_anchor_date"),
    }


@router.delete("/api/runs/{batch_id}")
async def delete_batch_run(batch_id: str) -> dict:
    if not db_module.get_db().delete_batch_run(batch_id):
        raise HTTPException(status_code=409, detail="批次不存在或正在运行（请先取消）")
    return {"batch_id": batch_id, "deleted": True}


@router.get("/api/runs/{batch_id}/export")
async def export_batch(batch_id: str, compare: str = "", live: bool = True) -> dict:
    """导出批次分析数据（方案 §8.2.6）：与 scripts/export_backtest_analysis.py
    同一实现，返回导出目录路径。页面不加入口，供远程/自动化调用。"""
    from services.backtest_export import export_batch_analysis

    db = db_module.get_db()
    if db.get_batch_run(batch_id) is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    try:
        return export_batch_analysis(
            db,
            batch_id,
            alt_batch_id=compare.strip() or None,
            include_live=live,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

