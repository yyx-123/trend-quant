"""批量回测路由（方案 §5.3）：按一级类目 × 多策略批量执行规则回测。

执行模型：一个后台 daemon 线程跑一个批次（同时只允许一个 running 批次，
409 拦截 + DB 事务兜底）；取消走内存 threading.Event（协作式，不落库，
服务重启由启动清理把 running 批次置为 interrupted）。
"""

from __future__ import annotations

import json
import logging
import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from data.storage import db as db_module
from rule_backtest.batch_service import (
    BatchBacktestService,
    estimate_batch_seconds,
    strategy_uses_random_indicator,
)
from rule_backtest.loader import StrategyLoader

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/batch-backtest", tags=["batch-backtest"])
templates = Jinja2Templates(directory="web/templates")

# In-memory cancel events for running batches (per-process, like _rule_jobs;
# a restart orphans the worker thread anyway and startup cleanup marks the
# batch 'interrupted').
_batch_cancel_events: dict[str, threading.Event] = {}
_batch_cancel_lock = threading.Lock()


class BatchRunRequest(BaseModel):
    categories: list[str] = Field(default_factory=list)
    strategy_ids: list[str] = Field(default_factory=list)
    name: str = Field(default="")


@router.get("", response_class=HTMLResponse)
async def batch_backtest_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        name="batch_backtest.html",
        request=request,
        context={"title": "批量回测"},
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

    service = BatchBacktestService()
    try:
        batch = service.prepare_batch(
            categories=payload.categories,
            strategy_ids=payload.strategy_ids,
            name=payload.name,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not db_module.get_db().create_batch_run_if_idle(batch):
        running = db_module.get_db().get_running_batch_run()
        raise HTTPException(
            status_code=409,
            detail=f"已有批次正在运行（{(running or {}).get('name', '?')}），请等待完成或先取消",
        )

    batch_id = batch["batch_id"]
    cancel_event = threading.Event()
    with _batch_cancel_lock:
        _batch_cancel_events[batch_id] = cancel_event

    logger.info(
        "Batch backtest queued batch_id=%s name=%s cells=%d",
        batch_id, batch["name"], batch["total_cells"],
    )

    def _run() -> None:
        try:
            # Per-run service instance to avoid sharing engine state across threads.
            BatchBacktestService().run_batch(batch_id, cancel_event=cancel_event)
        except Exception:
            logger.exception("Batch backtest thread crashed batch_id=%s", batch_id)
        finally:
            with _batch_cancel_lock:
                _batch_cancel_events.pop(batch_id, None)

    thread = threading.Thread(target=_run, daemon=True, name=f"batch-backtest-{batch_id}")
    thread.start()
    return {"batch_id": batch_id, "status": "running", "total_cells": batch["total_cells"]}


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
async def get_batch_cells(batch_id: str) -> dict:
    batch = db_module.get_db().get_batch_run(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
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

