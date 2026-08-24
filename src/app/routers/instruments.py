from __future__ import annotations

import logging
import threading
from datetime import date, datetime
from typing import Callable

import pandas as pd
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from core.benchmarks import benchmark_instruments
from core.strategy_config import get_strategy_config
from core.symbols import normalize_symbol, symbol_suffix, symbol_to_code
from data.service import DataService
from data.storage.db import get_db, record_job_run_safely
from data.storage.market_store import MarketStore
from services.instrument_admin import (
    _category_path_from_parts,
    _category_priority_map,
    _config_name_map,
    _known_managed_symbols,
    _next_sort_order,
    _normalize_symbol,
    _symbol_suffix,
    _symbol_to_code,
    _to_date,
)
from services.indicator_builder import rebuild_after_backfill
from services.instrument_jobs import (
    add_instrument_manager,
    bulk_backfill_manager,
    etf_constituent_import_manager,
)
from services.stock_industry import resolve_category

router = APIRouter(prefix="/instruments", tags=["instruments"])
templates = Jinja2Templates(directory="web/templates")
market_store = MarketStore()
logger = logging.getLogger(__name__)


class InstrumentBackfillRequest(BaseModel):
    start_date: str = Field(default="")
    end_date: str = Field(default="")
    adjust: str = Field(default="")


class InstrumentBulkBackfillItem(BaseModel):
    symbol: str
    start_date: str = Field(default="")


class InstrumentBulkBackfillRequest(BaseModel):
    items: list[InstrumentBulkBackfillItem] = Field(default_factory=list)
    end_date: str = Field(default="")
    adjust: str = Field(default="")


class InstrumentNameLookupRequest(BaseModel):
    symbol: str


class InstrumentAddRequest(BaseModel):
    symbol: str
    name: str = Field(default="")
    # 类目可整体留空：股票按申万行业自动归类（见 start_add_instrument）
    category_l1: str = Field(default="")
    category_l2: str = Field(default="")
    category_l3: str = Field(default="")
    end_date: str = Field(default="")
    adjust: str = Field(default="")


class InstrumentUpdateRequest(BaseModel):
    category_l1: str
    category_l2: str
    category_l3: str


class EtfConstituentImportRequest(BaseModel):
    etf_symbol: str
    end_date: str = Field(default="")
    adjust: str = Field(default="")








def _category_options() -> list[dict]:
    db = get_db()
    rows = db.list_instrument_categories()
    if rows:
        return rows

    categories: dict[str, dict] = {}
    for item in [*db.list_instrument_metadata(), *_config_items()]:
        if not isinstance(item, dict):
            continue
        l1 = str(item.get("category_l1") or "").strip()
        l2 = str(item.get("category_l2") or "").strip()
        l3 = str(item.get("category_l3") or "").strip()
        if l1:
            categories.setdefault(
                l1,
                {"path": l1, "level": 1, "name": l1, "parent_path": "", "priority": item.get("priority_l1")},
            )
        if l1 and l2:
            path = _category_path_from_parts(l1, l2)
            categories.setdefault(
                path,
                {"path": path, "level": 2, "name": l2, "parent_path": l1, "priority": item.get("priority_l2")},
            )
        if l1 and l2 and l3:
            parent = _category_path_from_parts(l1, l2)
            path = _category_path_from_parts(l1, l2, l3)
            categories.setdefault(
                path,
                {"path": path, "level": 3, "name": l3, "parent_path": parent, "priority": item.get("priority_l3")},
            )
    return sorted(
        categories.values(),
        key=lambda item: (
            int(item.get("level") or 0),
            str(item.get("parent_path") or ""),
            int(item.get("priority") or 9999),
            str(item.get("name") or ""),
        ),
    )


def _category_path(meta: dict | None) -> str:
    if not meta:
        return ""
    parts = [
        str(meta.get("category_l1") or "").strip(),
        str(meta.get("category_l2") or "").strip(),
        str(meta.get("category_l3") or "").strip(),
    ]
    return "-".join(part for part in parts if part)


def _metadata_priority(meta: dict | None) -> tuple:
    if not meta:
        return (1, 9999, 9999, 9999, 999999)
    return (
        0,
        int(meta.get("priority_l1") or 9999),
        int(meta.get("priority_l2") or 9999),
        int(meta.get("priority_l3") or 9999),
        int(meta.get("sort_order") or 999999),
    )


def _provider_priority_from_request(request: Request) -> list[str] | None:
    if hasattr(request.app.state, "settings"):
        return list(getattr(request.app.state.settings.app, "data_provider_priority", []) or [])
    return None


def _default_adjust() -> str:
    return str(get_strategy_config().get("adjust", "qfq"))


@router.get("", response_class=HTMLResponse)
async def instruments_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        name="instruments.html",
        request=request,
        context={"title": "标的管理"},
    )


@router.get("/api/categories")
async def list_categories() -> dict:
    items = _category_options()
    return {"ok": True, "items": items, "count": len(items)}


@router.post("/api/lookup")
async def lookup_instrument_name(payload: InstrumentNameLookupRequest, request: Request) -> dict:
    normalized_symbol = _normalize_symbol(payload.symbol)
    if normalized_symbol == "":
        raise HTTPException(status_code=400, detail="标的代码必填")
    if add_instrument_manager.is_symbol_pending(normalized_symbol):
        raise HTTPException(status_code=409, detail=f"{normalized_symbol} 正在新增补充中")
    if normalized_symbol in _known_managed_symbols():
        raise HTTPException(status_code=409, detail=f"{normalized_symbol} 已被管理")

    data_service = DataService(provider_priority=_provider_priority_from_request(request))
    try:
        result = data_service.fetch_instrument_name(normalized_symbol)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"名称查询失败：{exc}") from exc
    finally:
        data_service.close()

    return {
        "ok": True,
        "symbol": normalized_symbol,
        "code": _symbol_to_code(normalized_symbol),
        "exchange": _symbol_suffix(normalized_symbol),
        "name": result.get("name", ""),
        "provider": result.get("provider", ""),
        "ts": result.get("ts"),
    }


@router.post("/api/add")
async def start_add_instrument(payload: InstrumentAddRequest, request: Request) -> dict:
    normalized_symbol = _normalize_symbol(payload.symbol)
    if normalized_symbol == "":
        raise HTTPException(status_code=400, detail="标的代码必填")
    if add_instrument_manager.is_symbol_pending(normalized_symbol):
        raise HTTPException(status_code=409, detail=f"{normalized_symbol} 正在新增补充中")
    if add_instrument_manager.is_running():
        raise HTTPException(status_code=409, detail="已有新增标的任务正在运行")
    if normalized_symbol in _known_managed_symbols():
        raise HTTPException(status_code=409, detail=f"{normalized_symbol} 已被管理")

    category_values = [
        str(payload.category_l1 or "").strip(),
        str(payload.category_l2 or "").strip(),
        str(payload.category_l3 or "").strip(),
    ]
    if not all(category_values):
        if any(category_values):
            raise HTTPException(
                status_code=400, detail="一二三级类目需同时提供，或同时留空（留空按申万行业自动归类）"
            )
        # 类目留空：股票按申万行业自动归类（方案 §6.3，用户只需输入代码）。
        # ETF 无自动分类来源，未覆盖的次新股也应显式落入「待分类」——
        # 这两种情况都要求调用方手动传类目，避免静默错归。
        resolved = resolve_category(normalized_symbol)
        if not resolved["hit"]:
            raise HTTPException(
                status_code=400,
                detail="未能自动识别该标的行业：ETF 请手动选择类目，股票可选择「待分类」（后续自动回补）",
            )
        category_values = [
            resolved["category_l1"],
            resolved["category_l2"],
            resolved["category_l3"],
        ]

    valid_paths = {str(item.get("path") or "").strip() for item in _category_options()}
    category_path = _category_path_from_parts(*category_values)
    if category_path not in valid_paths:
        raise HTTPException(status_code=400, detail="类目组合不存在，请重新选择")

    try:
        end_date = _to_date(payload.end_date, date.today())
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式必须是 YYYY-MM-DD")

    adjust = str(payload.adjust or _default_adjust()).strip().lower() or "qfq"
    item = {
        "symbol": normalized_symbol,
        "name": str(payload.name or "").strip(),
        "category_l1": category_values[0],
        "category_l2": category_values[1],
        "category_l3": category_values[2],
    }
    started, status = add_instrument_manager.start(
        item=item,
        end_date=end_date,
        adjust=adjust,
        provider_priority=_provider_priority_from_request(request),
    )
    return {"ok": True, "started": started, "job": status}


@router.get("/api/add/status")
async def get_add_instrument_status() -> dict:
    return {"ok": True, "job": add_instrument_manager.snapshot()}


@router.get("/api/suggest-category/{symbol}")
async def suggest_category(symbol: str) -> dict:
    """手动添加时的自动归类建议（方案 §6.3）。纯本地表查询，毫秒级。"""
    normalized_symbol = _normalize_symbol(symbol)
    if normalized_symbol == "":
        raise HTTPException(status_code=400, detail="标的代码必填")
    resolved = resolve_category(normalized_symbol)
    return {"ok": True, "symbol": normalized_symbol, **resolved}


@router.get("/api/etf-constituents/import/status")
async def get_etf_import_status() -> dict:
    return {"ok": True, "job": etf_constituent_import_manager.snapshot()}


@router.post("/api/etf-constituents/import")
async def import_etf_constituents(payload: EtfConstituentImportRequest, request: Request) -> dict:
    etf_symbol = _normalize_symbol(payload.etf_symbol)
    if etf_symbol == "":
        raise HTTPException(status_code=400, detail="ETF 代码必填")
    if etf_constituent_import_manager.is_running():
        raise HTTPException(status_code=409, detail="已有 ETF 重仓股导入任务正在运行")
    if not get_db().list_current_etf_constituents(etf_symbol):
        raise HTTPException(status_code=400, detail=f"{etf_symbol} 暂无重仓股快照，请先运行季度快照脚本")

    try:
        end_date = _to_date(payload.end_date, date.today())
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式必须是 YYYY-MM-DD")
    adjust = str(payload.adjust or _default_adjust()).strip().lower() or "qfq"

    started, status = etf_constituent_import_manager.start(
        etf_symbol=etf_symbol,
        end_date=end_date,
        adjust=adjust,
        provider_priority=_provider_priority_from_request(request),
    )
    return {"ok": True, "started": started, "job": status}


@router.get("/api/etf-constituents/{etf_symbol}")
async def preview_etf_constituents(etf_symbol: str) -> dict:
    """预览某 ETF 当前前十大重仓股（方案 §6.1）。

    每行带 already_managed / resolved_category / hit；附期次新鲜度。
    """
    normalized_symbol = _normalize_symbol(etf_symbol)
    if normalized_symbol == "":
        raise HTTPException(status_code=400, detail="ETF 代码必填")

    db = get_db()
    rows = db.list_current_etf_constituents(normalized_symbol)
    if not rows:
        return {
            "ok": False,
            "symbol": normalized_symbol,
            "message": "该 ETF 暂无重仓股快照，请先运行季度快照脚本（scripts/fetch_etf_holdings.py）",
            "period": None,
            "items": [],
        }

    known = _known_managed_symbols()
    period = str(rows[0].get("period") or "")
    fetched_at = max(str(r.get("fetched_at") or "") for r in rows)
    months_since = None
    try:
        period_date = datetime.strptime(period, "%Y%m%d").date()
        months_since = round((date.today() - period_date).days / 30.44, 1)
    except ValueError:
        pass

    items: list[dict] = []
    for row in rows:
        stock = str(row.get("stock_symbol") or "").strip().upper()
        resolved = resolve_category(stock, db=db)
        items.append(
            {
                "rank": row.get("rank"),
                "stock_symbol": stock,
                "stock_name": str(row.get("stock_name") or "").strip(),
                "weight": row.get("weight"),
                "already_managed": stock in known,
                "resolved_category": f"{resolved['category_l2']}-{resolved['category_l3']}",
                "hit": bool(resolved["hit"]),
            }
        )
    return {
        "ok": True,
        "symbol": normalized_symbol,
        "period": period,
        "fetched_at": fetched_at,
        "months_since_period": months_since,
        "stale": bool(months_since is not None and months_since > 4),
        "items": items,
    }


@router.get("/api/list")
async def list_instruments() -> dict:
    db = get_db()
    # Single table scan; derive every lookup from the same rows.
    metadata_rows = db.list_instrument_metadata()
    config_by_symbol: dict[str, dict] = {}
    metadata_by_symbol: dict[str, dict] = {}
    name_map: dict[str, str] = {}
    for item in metadata_rows:
        symbol = str(item.get("symbol", "")).strip().upper()
        if symbol == "":
            continue
        config_by_symbol[symbol] = item
        metadata_by_symbol[symbol] = item
        name_map[symbol] = str(item.get("name", "") or "").strip()

    benchmark_by_symbol = {
        str(item.get("symbol", "")).strip().upper(): item
        for item in benchmark_instruments()
        if str(item.get("symbol", "")).strip()
    }

    known_symbols = set(config_by_symbol.keys()) | set(benchmark_by_symbol.keys()) | set(metadata_by_symbol.keys())
    for symbol in db.list_market_symbols():
        known_symbols.add(symbol.upper())

    items: list[dict] = []
    for symbol in sorted(known_symbols):
        meta = metadata_by_symbol.get(symbol)
        summary = db.get_market_data_summary(symbol)
        rows = summary.get("rows", 0)
        local_start = summary.get("start")
        local_end = summary.get("end")

        cfg = config_by_symbol.get(symbol, {})
        in_config = bool(cfg)
        is_benchmark = symbol in benchmark_by_symbol
        enabled = bool(cfg.get("enabled", False)) if in_config else False
        name = str((meta or {}).get("name") or name_map.get(symbol, "") or "")
        sort_key = _metadata_priority(meta)
        items.append(
            {
                "symbol": symbol,
                "code": _symbol_to_code(symbol),
                "exchange": _symbol_suffix(symbol),
                "name": name,
                "category_l1": str((meta or {}).get("category_l1") or ""),
                "category_l2": str((meta or {}).get("category_l2") or ""),
                "category_l3": str((meta or {}).get("category_l3") or ""),
                "category_path": _category_path(meta),
                "factor_tags": list((meta or {}).get("factor_tags") or []),
                "region_tag": str((meta or {}).get("region_tag") or ""),
                "priority_l1": sort_key[1],
                "priority_l2": sort_key[2],
                "priority_l3": sort_key[3],
                "sort_order": sort_key[4],
                "enabled": enabled,
                "in_config": in_config,
                "is_benchmark": is_benchmark,
                "rows": rows,
                "local_start_date": local_start,
                "local_end_date": local_end,
                "path": f"sqlite/{symbol}",
            }
        )

    items.sort(
        key=lambda x: (
            1 if not x.get("category_path") else 0,
            int(x.get("priority_l1") or 9999),
            int(x.get("priority_l2") or 9999),
            int(x.get("priority_l3") or 9999),
            int(x.get("sort_order") or 999999),
            str(x.get("symbol", "")),
        )
    )
    return {"items": items, "count": len(items), "as_of": datetime.now().isoformat()}


@router.post("/api/{symbol}/update")
async def update_instrument(symbol: str, payload: InstrumentUpdateRequest) -> dict:
    normalized_symbol = _normalize_symbol(symbol)
    if normalized_symbol == "":
        raise HTTPException(status_code=400, detail="标的无效")

    db = get_db()
    existing_meta = db.get_instrument_metadata(normalized_symbol)
    if not existing_meta:
        raise HTTPException(status_code=404, detail=f"{normalized_symbol} 不在标的管理列表中")

    category_values = [
        str(payload.category_l1 or "").strip(),
        str(payload.category_l2 or "").strip(),
        str(payload.category_l3 or "").strip(),
    ]
    if not all(category_values):
        raise HTTPException(status_code=400, detail="一二三级类目均必选")

    valid_paths = {str(item.get("path") or "").strip() for item in _category_options()}
    category_path = _category_path_from_parts(*category_values)
    if category_path not in valid_paths:
        raise HTTPException(status_code=400, detail="类目组合不存在，请重新选择")

    priorities = _category_priority_map()
    updates = {
        "category_l1": category_values[0],
        "category_l2": category_values[1],
        "category_l3": category_values[2],
        "priority_l1": priorities.get(_category_path_from_parts(category_values[0])),
        "priority_l2": priorities.get(_category_path_from_parts(category_values[0], category_values[1])),
        "priority_l3": priorities.get(category_path),
        "asset_type": "stock" if category_values[0] == "股票" else "etf",
    }

    meta = dict(existing_meta or {})
    meta.update(updates)
    meta["symbol"] = normalized_symbol
    if not str(meta.get("name") or "").strip():
        meta["name"] = _config_name_map().get(normalized_symbol, "")
    if meta.get("sort_order") is None:
        meta["sort_order"] = _next_sort_order()
    if not str(meta.get("source") or "").strip():
        meta["source"] = "manual_edit"
    saved = db.save_instrument_metadata([meta])

    return {
        "ok": True,
        "symbol": normalized_symbol,
        "category_l1": updates["category_l1"],
        "category_l2": updates["category_l2"],
        "category_l3": updates["category_l3"],
        "category_path": category_path,
        "priority_l1": updates["priority_l1"],
        "priority_l2": updates["priority_l2"],
        "priority_l3": updates["priority_l3"],
        "config_saved": saved,
        "metadata_saved": saved,
    }


@router.post("/api/{symbol}/backfill")
async def backfill_instrument(symbol: str, payload: InstrumentBackfillRequest, request: Request) -> dict:
    normalized_symbol = _normalize_symbol(symbol)
    if normalized_symbol == "":
        raise HTTPException(status_code=400, detail="标的无效")

    if str(payload.start_date or "").strip() == "":
        raise HTTPException(status_code=400, detail="开始日期必填")

    try:
        start_date = _to_date(payload.start_date, date(2020, 1, 1))
        end_date = _to_date(payload.end_date, date.today())
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式必须是 YYYY-MM-DD")

    adjust = str(payload.adjust or _default_adjust()).strip().lower() or "qfq"

    data_service = DataService(provider_priority=_provider_priority_from_request(request))
    try:
        result = data_service.backfill_daily_history(
            symbol=normalized_symbol,
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
        )
    finally:
        data_service.close()
    result["adjust"] = adjust
    result["requested_symbol"] = symbol
    result["symbol"] = normalized_symbol
    result["request_adjusted_to_earliest_available"] = bool(
        result.get("fetched_start")
        and result.get("requested_start")
        and str(result.get("fetched_start")) > str(result.get("requested_start"))
    )

    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    result["job_stamp"] = stamp
    if str(result.get("status") or "") == "updated":
        rebuild_after_backfill([normalized_symbol])
    record_job_run_safely("instrument_backfill", result, status=str(result.get("status") or ""))
    return {"ok": True, "result": result}


@router.post("/api/backfill-all")
async def start_bulk_backfill(payload: InstrumentBulkBackfillRequest, request: Request) -> dict:
    try:
        end_date = _to_date(payload.end_date, date.today())
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式必须是 YYYY-MM-DD")

    adjust = str(payload.adjust or _default_adjust()).strip().lower() or "qfq"
    items: list[dict] = []
    seen_symbols: set[str] = set()
    for raw_item in payload.items:
        symbol = _normalize_symbol(raw_item.symbol)
        if symbol == "" or symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        try:
            start_date = _to_date(raw_item.start_date, date(2020, 1, 1))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"{symbol} 的开始日期格式必须是 YYYY-MM-DD")
        items.append({"symbol": symbol, "start_date": start_date})

    started, status = bulk_backfill_manager.start(
        items=items,
        end_date=end_date,
        adjust=adjust,
        provider_priority=_provider_priority_from_request(request),
    )
    return {"ok": True, "started": started, "job": status}


@router.get("/api/backfill-all/status")
async def get_bulk_backfill_status() -> dict:
    return {"ok": True, "job": bulk_backfill_manager.snapshot()}


@router.get("/api/daily-update/status")
async def daily_update_status() -> dict:
    """Latest 16:30 daily data update status for the global notification bar."""
    run = get_db().get_latest_job_run("daily_update")
    if not run:
        return {"ts": None, "completed": False, "ok": False, "message": "暂无更新记录"}
    payload = run.get("payload") or {}
    status = str(run.get("status") or "")
    if status == "failed":
        # 任务整体失败：必须如实上抛，不能伪装成「成功 0 只」。
        return {
            "ts": payload.get("ts") or run.get("created_at"),
            "date": run.get("run_date"),
            "total": 0,
            "success": 0,
            "failed": 0,
            "failed_symbols": [],
            "completed": True,
            "ok": False,
            "status": status,
            "message": f"盘后数据更新失败：{payload.get('error', '未知错误')}",
        }
    return {
        "ts": payload.get("ts") or run.get("created_at"),
        "date": run.get("run_date"),
        "total": payload.get("total", 0),
        "success": payload.get("success", 0),
        "failed": payload.get("failed", 0),
        "failed_symbols": payload.get("failed_symbols", []),
        "completed": True,
        "ok": status in ("completed", "partial", ""),
        "status": status or "completed",
    }
