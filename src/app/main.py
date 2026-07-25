from __future__ import annotations

from contextlib import asynccontextmanager
import os
from pathlib import Path
import threading

from dotenv import load_dotenv

# 加载项目根目录 .env（如 TICKFLOW_API_KEY 实时报价密钥）。
# 必须在任何读取环境变量的模块初始化之前执行；已存在的环境变量优先（不覆盖）。
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.routers import (
    instruments,
    manual_trade,
    market_view,
    position_strategy,
    rule_backtest,
    subject_market,
)
from audit.app_logger import get_logger, setup_logging
from core.jobs import daily_market_update_job
from core.scheduler import SchedulerManager
from core.settings import load_settings
from data.storage.db import init_db

settings = load_settings()
setup_logging(settings.logging.level)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Path("data").mkdir(exist_ok=True)
    init_db()

    # Dashboard cache warmup: pre-compute the subject dashboard payload so the
    # first user request after startup / daily update is a cache hit instead of
    # a multi-second cold rebuild.
    def _warm_dashboard() -> None:
        try:
            from app.routers.subject_market import warm_dashboard_cache

            warm_dashboard_cache()
            logger.info("Dashboard cache warmed")
        except Exception:
            logger.exception("Dashboard cache warmup failed")

    # Startup cache check: full rebuild in background when trend params or
    # formula versions drifted (also covers first-ever bootstrap).
    def _rebuild_check() -> None:
        try:
            from services.indicator_builder import rebuild_if_needed

            result = rebuild_if_needed()
            logger.info("Indicator cache startup check: %s", result.get("status"))
        except Exception:
            logger.exception("Indicator cache startup check failed")
        # Warm after the check so a rebuild (which changes the data revision)
        # never leaves a stale warmed payload behind.
        _warm_dashboard()

    threading.Thread(target=_rebuild_check, daemon=True).start()

    scheduler_manager = SchedulerManager(settings=settings)

    def update_job() -> None:
        payload = daily_market_update_job(settings)
        logger.info(
            "Daily market data update (16:30): %s success, %s failed out of %s",
            payload.get("success", 0),
            payload.get("failed", 0),
            payload.get("total", 0),
        )
        if payload.get("status") == "skipped_non_trading_day":
            return
        # Post-update orchestration (dividend detection + indicator rebuild)
        # lives here so that core/jobs stays free of services-layer imports.
        from datetime import date as _date

        from data.service import DataService
        from data.storage.db import record_job_run_safely
        from services.indicator_builder import run_post_update_pipeline

        service = DataService(provider_priority=settings.app.data_provider_priority)
        try:
            pipeline = run_post_update_pipeline(
                settings, service, payload, payload.get("symbols", []), _date.today()
            )
        finally:
            service.close()
        payload["indicator_rebuild"] = pipeline
        record_job_run_safely(
            "indicator_rebuild",
            pipeline,
            run_date=_date.today().isoformat(),
            status=str(pipeline.get("status", "")),
        )
        # Daily update changed the data revision; re-warm in background so the
        # next user request does not pay the cold-rebuild cost.
        threading.Thread(target=_warm_dashboard, daemon=True).start()

    disable_scheduler = str(os.getenv("TREND_QUANT_DISABLE_SCHEDULER", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if disable_scheduler:
        logger.warning("Scheduler disabled by TREND_QUANT_DISABLE_SCHEDULER")
    else:
        scheduler_manager.start(update_job=update_job)

    app.state.settings = settings
    app.state.scheduler_manager = scheduler_manager

    logger.info("Application started")
    try:
        yield
    finally:
        scheduler_manager.shutdown()
        logger.info("Application stopped")


app = FastAPI(title="Trend ETF System", version="0.1.0", lifespan=lifespan)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # 4xx is client noise (internet scanners hit 404 constantly); only 5xx
    # indicates a server-side problem worth investigating.
    if exc.status_code >= 500:
        logger.warning(
            "HTTP %s on %s %s: %s",
            exc.status_code,
            request.method,
            request.url.path,
            exc.detail,
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Keep a traceback in app.log; without this handler uncaught errors only
    # reach stderr/journald with no request context.
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})

static_dir = Path("web/static")
style_file = static_dir / "style.css"
app.state.asset_version = str(int(style_file.stat().st_mtime)) if style_file.exists() else "1"


class AssetVersionMiddleware:
    """Refresh ``asset_version`` per request without touching the response.

    Implemented as a pure ASGI pass-through (no response wrapping), unlike
    ``@app.middleware("http")`` whose BaseHTTPMiddleware buffering asserted
    on streaming responses such as the MCP SSE endpoint.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope["app"].state.asset_version = (
                str(int(style_file.stat().st_mtime)) if style_file.exists() else "1"
            )
        await self.app(scope, receive, send)


app.add_middleware(AssetVersionMiddleware)


if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(rule_backtest.router)
app.include_router(position_strategy.router)
app.include_router(instruments.router)
app.include_router(market_view.router)
app.include_router(subject_market.router)
app.include_router(manual_trade.router)


@app.get("/", include_in_schema=False)
async def root_redirect() -> RedirectResponse:
    """The legacy overview page was removed; land on the subject dashboard."""
    return RedirectResponse(url="/subject-market")

# ── MCP SSE endpoint (optional, requires `mcp` package) ──────────────
try:
    from trend_mcp.server import mcp as _mcp_app

    app.mount("/mcp", _mcp_app.sse_app())
    logger.info("MCP SSE endpoint mounted at /mcp/sse")
except ImportError:
    logger.info("MCP package not installed – skipping /mcp endpoint")
