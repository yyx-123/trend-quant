from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import time
import os
from pathlib import Path
import threading
from urllib.parse import quote

from dotenv import load_dotenv

# 加载项目根目录 .env（如 TICKFLOW_API_KEY 实时报价密钥）。
# 必须在任何读取环境变量的模块初始化之前执行；已存在的环境变量优先（不覆盖）。
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.routers import (
    auth,
    batch_backtest,
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
# Import the module (not the function) so tests can monkeypatch init_db
# and have the lifespan use the patched test double.
from data.storage import db as db_module
from services import auth as auth_service

settings = load_settings()
setup_logging(settings.logging.level)
logger = get_logger(__name__)


def _background_tasks_disabled() -> bool:
    return str(os.getenv("TREND_QUANT_DISABLE_SCHEDULER", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期。

    注意：本模块及 routers/services 必须在任何测试补丁生效前完成顶层导入
    （见 tests/api/conftest.py）——服务模块顶层 `from data.storage.db import
    get_db` 的值绑定会固化导入瞬间的对象，若首次导入发生在 monkeypatch
    窗口内，会把某个测试的临时 lambda 永久绑进服务命名空间。
    """
    Path("data").mkdir(exist_ok=True)
    db = db_module.init_db()

    # Batch backtest orphan cleanup: worker threads are daemon and die with
    # the process, so any batch still 'running' at boot is an orphan.
    interrupted = db.mark_interrupted_batch_runs()
    if interrupted:
        logger.warning("Marked %d orphaned batch backtest run(s) as interrupted", interrupted)

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

    # 测试环境（TREND_QUANT_DISABLE_SCHEDULER=1）不启动该线程：它是 daemon
    # 线程且不被 shutdown 回收，会逃出单测的 DB 补丁窗口，污染后续测试的
    # 临时库，甚至在补丁撤销后触达默认路径的生产库。
    if not _background_tasks_disabled():
        threading.Thread(target=_rebuild_check, daemon=True).start()

    scheduler_manager = SchedulerManager(settings=settings)

    # 防止定时触发与启动补偿并发重入（同一进程只允许一个更新任务）。
    _update_job_lock = threading.Lock()

    def update_job(force: bool = False) -> None:
        if not _update_job_lock.acquire(blocking=False):
            logger.info("Daily market data update already running; skipping duplicate trigger")
            return
        try:
            _run_daily_update(force=force)
        finally:
            _update_job_lock.release()

    def intraday_snapshot_job() -> None:
        """盘中看板快照定时入口：单例运行器内部完成交易日/时段/并发守卫。"""
        from services.dashboard_snapshot import snapshot_runner

        result = snapshot_runner.ensure_running(trigger="schedule")
        if result.get("status") != "skipped":
            logger.info("Intraday snapshot scheduled trigger: %s", result.get("status"))

    def _run_daily_update(force: bool = False) -> None:
        payload = daily_market_update_job(settings, force=force)
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

    def _daily_update_catchup() -> None:
        """启动补偿：EOD 数据落后于「本应已持久化的最近交易日」时补跑一次。

        覆盖三类漏更：服务在 16:30 不在线（内存 jobstore 无 misfire 持久化）、
        上次运行整体失败、行情商延迟发布导致「成功但没拿到当日K线」。
        补跑是幂等的（ensure_daily_history 增量补齐缺口）。
        """
        from datetime import timedelta as _td

        from core.calendar import is_trading_day, market_now, previous_trading_day

        now = market_now()
        upd_h, upd_m = settings.app.update_time_after_close.split(":")
        upd_time = time(int(upd_h), int(upd_m))
        today = now.date()
        if is_trading_day(today) and now.time() >= upd_time:
            expected = today
        else:
            expected = previous_trading_day(today - _td(days=1))

        run = db.get_latest_job_run("daily_update")
        last_ok = ""
        if run and str(run.get("status")) in ("completed", "partial"):
            last_ok = str(run.get("run_date") or "")
        behind_schedule = not last_ok or last_ok < expected.isoformat()

        # 行情商延迟检查：任务「成功」但库里最新K线仍落后于 expected。
        revision = db.get_market_dashboard_revision()
        max_bar = str(revision[0])[:10] if revision and revision[0] else ""
        data_behind = not max_bar or max_bar < expected.isoformat()

        if not behind_schedule and not data_behind:
            logger.info("Daily update catch-up check: up to date (expected %s)", expected)
            return
        logger.warning(
            "Daily update catch-up triggered: expected=%s last_ok=%s max_bar=%s",
            expected, last_ok or "none", max_bar or "none",
        )
        # force=True：补跑可能发生在周末/节假日（错过周五 16:30 周六才开机），
        # 此时 daily_market_update_job 的非交易日跳过守卫必须放行。
        update_job(force=True)

    disable_scheduler = _background_tasks_disabled()
    if disable_scheduler:
        logger.warning("Scheduler disabled by TREND_QUANT_DISABLE_SCHEDULER")
    else:
        scheduler_manager.start(
            update_job=update_job,
            intraday_snapshot_job=intraday_snapshot_job,
        )
        threading.Thread(target=_daily_update_catchup, daemon=True).start()

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


class AuthWallMiddleware:
    """全站登录墙：无有效 session cookie 的请求一律拦截。

    - 页面请求（GET/HEAD 且路径不含 /api/）→ 303 跳 /login?next=<原地址>
    - API 请求 → 401 JSON
    - 豁免：/login、/api/auth/login、/api/auth/logout、/static、/favicon.ico、/mcp
      （MCP 为机对机通道，工具调用自带 username/password 逐次鉴权）

    纯 ASGI 实现（同 AssetVersionMiddleware 的原因）。有效 session 写入
    ``scope["state"]["user"]`` 供路由依赖与模板使用；滑动续期触发时在
    响应头追加 Set-Cookie 同步浏览器侧有效期。
    """

    _EXEMPT_PATHS = {"/login", "/api/auth/login", "/api/auth/logout", "/favicon.ico"}
    _EXEMPT_PREFIXES = ("/static", "/mcp")

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _read_token(scope) -> str | None:
        for name, value in scope.get("headers", []):
            if name.lower() != b"cookie":
                continue
            for part in value.decode("latin-1").split(";"):
                key, _, val = part.strip().partition("=")
                if key == auth_service.SESSION_COOKIE:
                    return val
        return None

    @staticmethod
    def _is_page_request(scope) -> bool:
        return scope.get("method") in ("GET", "HEAD") and "/api/" not in scope.get("path", "")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in self._EXEMPT_PATHS or path.startswith(self._EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        token = self._read_token(scope)
        user, renewed = auth_service.resolve_session(token)
        if user is None:
            if self._is_page_request(scope):
                query = scope.get("query_string", b"").decode("latin-1")
                target = path + ("?" + query if query else "")
                response = RedirectResponse(
                    url="/login?next=" + quote(target, safe=""), status_code=303
                )
            else:
                response = JSONResponse(
                    status_code=401, content={"detail": "未登录或登录已过期"}
                )
            await response(scope, receive, send)
            return

        scope.setdefault("state", {})["user"] = user
        if not renewed:
            await self.app(scope, receive, send)
            return

        # 滑动续期：服务端已顺延 expires_at，响应里同步重发 cookie
        set_cookie = (
            f"{auth_service.SESSION_COOKIE}={token}; HttpOnly; Path=/; "
            f"Max-Age={int(auth_service.SESSION_TTL.total_seconds())}; SameSite=lax"
        ).encode("latin-1")

        async def send_with_renewed_cookie(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"set-cookie", set_cookie))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_renewed_cookie)


app.add_middleware(AuthWallMiddleware)
app.add_middleware(AssetVersionMiddleware)
# Compress larger JSON/HTML bodies (backtest results, market series). The
# app sits directly behind the frp relay with no nginx in between, so
# compression must happen here; browsers decompress natively.
app.add_middleware(GZipMiddleware, minimum_size=1000)


if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(rule_backtest.router)
app.include_router(position_strategy.router)
app.include_router(instruments.router)
app.include_router(market_view.router)
app.include_router(subject_market.router)
app.include_router(manual_trade.router)
app.include_router(batch_backtest.router)
app.include_router(auth.router)


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
