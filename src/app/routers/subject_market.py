from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from core.calendar import market_now, trading_session_status
from data.storage.db import get_db
from services import auth
from services.dashboard import RevisionCache, build_subject_dashboard_payload
from services.dashboard_snapshot import snapshot_runner

router = APIRouter(prefix="/subject-market", tags=["subject-market"])
templates = Jinja2Templates(directory="web/templates")
logger = logging.getLogger(__name__)

_dashboard_cache = RevisionCache()


def _overlay_holdings(payload: dict, user_id: int, db) -> dict:
    """在共享看板 payload 上叠加当前用户的持仓金额 / 占比（标的行级）。

    最新价取该行 mini K线末根收盘 —— 盘中快照模式下末根是实时报价合成K线，
    即看板正在显示的最新价；EOD 模式下为最新日K收盘。占比分母为看板内全部
    持仓金额之和（同一标的多笔持仓合并计份数）。

    注意：payload 来自 RevisionCache / 快照运行器，是全用户共享对象，绝不可
    原地改写 —— 沿 groups→items→children 路径浅拷贝，仅持有的标的行复制
    dict 并附加 holding_value / holding_weight 字段。
    """
    if not payload.get("groups"):
        return payload
    shares_by_symbol: dict[str, float] = {}
    for t in db.list_manual_trades(user_id):
        if t["status"] == "open":
            shares_by_symbol[t["symbol"]] = shares_by_symbol.get(t["symbol"], 0.0) + float(t["shares"])
    if not shares_by_symbol:
        return payload

    def latest_price(inst: dict) -> float | None:
        kline = inst.get("kline") or []
        if not kline:
            return None
        price = kline[-1].get("c")
        return float(price) if price and float(price) > 0 else None

    values: dict[str, float] = {}
    for group in payload["groups"]:
        for l2 in group.get("items", []):
            for l3 in l2.get("children", []):
                for inst in l3.get("children", []):
                    symbol = inst.get("symbol")
                    if symbol in shares_by_symbol:
                        price = latest_price(inst)
                        if price:
                            values[symbol] = round(shares_by_symbol[symbol] * price, 2)
    if not values:
        return payload
    total = sum(values.values())

    def wrap_inst(inst: dict) -> dict:
        value = values.get(inst.get("symbol"))
        if value is None:
            return inst
        symbol = inst.get("symbol")
        return {
            **inst,
            "holding_value": value,
            "holding_weight": round(value / total * 100, 2) if total > 0 else None,
            # 悬停说明用：金额的两个计算组件（看板最新价 / 持仓份数）
            "holding_price": latest_price(inst),
            "holding_shares": shares_by_symbol[symbol],
        }

    new_groups = []
    for group in payload["groups"]:
        new_items = []
        for l2 in group.get("items", []):
            new_l3s = []
            for l3 in l2.get("children", []):
                children = l3.get("children", [])
                if any(c.get("symbol") in values for c in children):
                    new_l3s.append({**l3, "children": [wrap_inst(c) for c in children]})
                else:
                    new_l3s.append(l3)
            new_items.append({**l2, "children": new_l3s})
        new_groups.append({**group, "items": new_items})
    # holdings_total：占比分母（看板内全部持仓金额），供前端悬停文案展示
    return {**payload, "groups": new_groups, "holdings_total": round(total, 2)}


def warm_dashboard_cache() -> None:
    """Pre-compute the EOD dashboard payload (called from app lifespan hooks).

    Fills the same RevisionCache the endpoint reads, so the first user hit
    after startup or after the daily 16:30 update is served from cache.
    """
    db = get_db()
    revision = db.get_market_dashboard_revision()
    _dashboard_cache.get_or_compute(revision, lambda: build_subject_dashboard_payload(db))


@router.get("", response_class=HTMLResponse)
async def subject_market_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        name="subject_market.html", request=request, context={"title": "标的看板"}
    )


@router.get("/api/dashboard")
async def subject_market_dashboard(user: dict = Depends(auth.get_current_user)) -> dict:
    """快照优先：有当日盘中快照就返回快照，否则返回 EOD 看板。

    EOD 数据已覆盖今日（收盘补库落库）时以盘后确认值为准，忽略快照。
    返回前叠加当前登录用户的持仓金额/占比（标的行级，见 _overlay_holdings）。
    """
    db = get_db()
    revision = db.get_market_dashboard_revision()
    today = market_now().date().isoformat()
    max_bar = str(revision[0])[:10] if revision and revision[0] else ""
    if max_bar < today:
        snapshot = snapshot_runner.latest_snapshot()
        # 快照 as_of 是完整时间戳（如 2026-08-24T10:15:19），按日期部分比对。
        if snapshot and str(snapshot.get("as_of") or "")[:10] == today:
            payload = {**snapshot["payload"], "snapshot_ts": snapshot["computed_at"]}
            return _overlay_holdings(payload, user["id"], db)
    # 冷缓存重建是全市场秒级 CPU 计算 —— 放到线程池，避免在事件循环上
    # 同步执行时把整个服务卡住（RevisionCache 内部有锁，并发冷请求只算一次）。
    payload = await run_in_threadpool(
        _dashboard_cache.get_or_compute,
        revision,
        lambda: build_subject_dashboard_payload(db),
    )
    return _overlay_holdings(payload, user["id"], db)


@router.get("/api/trading-status")
async def get_trading_status() -> dict:
    """Return current A-share trading session status."""
    return trading_session_status()


@router.get("/api/dashboard/refresh-status")
async def refresh_status() -> dict:
    """轮询定时快照任务进度；snapshot_ts 变化即代表有新快照可取。"""
    return snapshot_runner.status()
