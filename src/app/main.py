from __future__ import annotations

import threading
import time as time_module
from contextlib import asynccontextmanager
from datetime import time
from urllib.parse import quote

from dotenv import load_dotenv

from core.paths import data_dir as _data_dir
from core.paths import dotenv_path as _dotenv_path
from core.paths import web_dir as _web_dir

# 加载项目根目录 .env（如 TICKFLOW_API_KEY 实时报价密钥），路径以 __file__
# 锚定（P2-13：非项目根启动时 cwd 相对路径会静默读错位置）。
# 必须在任何读取环境变量的模块初始化之前执行；已存在的环境变量优先（不覆盖）。
load_dotenv(_dotenv_path())

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
from core import env
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
    return env.scheduler_disabled()


_BUILTIN_ADMIN_USERNAME = "yyx"
# 仅在内置管理员「不存在」时使用的引导密码：env 可覆盖，缺省回落默认值
# （README 部署章节写明首次登录后应改密）；已存在时不强制重置（允许改密）。
_BUILTIN_ADMIN_DEFAULT_PASSWORD = "20160702"


def _ensure_builtin_admin(db) -> None:
    """内置管理员 yyx 永远存在（P1-3）：全新部署无需手工建用户即可登录。

    - 不存在：以引导密码（env TREND_QUANT_BOOTSTRAP_ADMIN_PASSWORD，缺省
      回落默认值）创建，is_admin=1；
    - 已存在：不碰密码（允许用户改密），仅确保 is_admin=1。
    挂点在 lifespan 而非 init_db——避免往每个测试临时库塞用户。
    """
    user = db.get_user_by_username(_BUILTIN_ADMIN_USERNAME)
    if user is None:
        password = env.bootstrap_admin_password() or _BUILTIN_ADMIN_DEFAULT_PASSWORD
        db.create_user(_BUILTIN_ADMIN_USERNAME, password, is_admin=True)
        logger.info("Built-in admin '%s' created (bootstrap password; change after first login)",
                    _BUILTIN_ADMIN_USERNAME)
    elif not user.get("is_admin"):
        db.set_user_admin(_BUILTIN_ADMIN_USERNAME, True)
        logger.info("Built-in admin '%s' promoted to is_admin", _BUILTIN_ADMIN_USERNAME)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期。

    注意：本模块及 routers/services 必须在任何测试补丁生效前完成顶层导入
    （见 tests/api/conftest.py）——服务模块顶层 `from data.storage.db import
    get_db` 的值绑定会固化导入瞬间的对象，若首次导入发生在 monkeypatch
    窗口内，会把某个测试的临时 lambda 永久绑进服务命名空间。
    """
    _data_dir().mkdir(parents=True, exist_ok=True)
    db = db_module.init_db()
    _ensure_builtin_admin(db)

    def backup_job() -> None:
        """每日 03:00 数据库备份（P1-2）：VACUUM INTO 在线备份，只留最新一份。"""
        try:
            dest = db.backup_to(keep=1)
            logger.info("Daily DB backup completed: %s", dest)
        except Exception:
            logger.exception("Daily DB backup failed")

    # Batch backtest orphan cleanup: worker threads are daemon and die with
    # the process, so any batch still 'running' at boot is an orphan.
    interrupted = db.mark_interrupted_batch_runs()
    if interrupted:
        logger.warning("Marked %d orphaned batch backtest run(s) as interrupted", interrupted)

    # 三个 JobManager（批量补齐/新增标的/ETF 重仓导入）的中断任务同样
    # 标记 interrupted + 落 job_runs（P2-9，比照批量回测）。
    from services.instrument_jobs import mark_interrupted_at_startup

    interrupted_jobs = mark_interrupted_at_startup(db=db)
    if interrupted_jobs:
        logger.warning("Marked %d orphaned instrument job(s) as interrupted", interrupted_jobs)

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

    def industry_sync_job() -> None:
        """申万行业分类月度同步（TickFlow universes）+ 待分类回补。

        移动清单随 job_runs 落库可追溯（方案 §5）；失败只记日志，下月再试。
        """
        from services.stock_industry import record_industry_sync_job, sync_industry_from_tickflow

        try:
            summary = sync_industry_from_tickflow(db=db)
            record_industry_sync_job("stock_industry_sync_tickflow", summary)
            logger.info(
                "Monthly industry sync: rows=%s written=%s moved=%s",
                summary.get("rows"),
                summary.get("written"),
                len((summary.get("reclassify") or {}).get("moved") or []),
            )
        except Exception:
            logger.exception("Monthly industry sync failed")

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
        from core.calendar import market_now
        from data.service import get_data_service
        from data.storage.db import record_job_run_safely
        from services.indicator_builder import run_post_update_pipeline

        pipeline = run_post_update_pipeline(
            settings, get_data_service(), payload, payload.get("symbols", []), market_now().date()
        )
        payload["indicator_rebuild"] = pipeline
        record_job_run_safely(
            "indicator_rebuild",
            pipeline,
            run_date=market_now().date().isoformat(),
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
            industry_sync_job=industry_sync_job,
            backup_job=backup_job,
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

static_dir = _web_dir() / "static"


def _compute_asset_version() -> str:
    """静态资源版本串：启动时全部文件内容 sha1（部署/回滚后稳定且必然变化）。

    跟踪 web/static 下所有文件（不只看 style.css mtime）——新增/修改任一
    静态资源都会刷新版本，浏览器缓存同步失效（P1-15 前置①）。
    """
    import hashlib

    digest = hashlib.sha1()
    if static_dir.exists():
        for path in sorted(static_dir.rglob("*")):
            if path.is_file():
                digest.update(str(path.relative_to(static_dir)).encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()[:12] or "1"


app.state.asset_version = _compute_asset_version()
_STARTUP_ASSET_VERSION = app.state.asset_version


class AssetVersionMiddleware:
    """Refresh ``asset_version`` per request without touching the response.

    运行期以 max(mtime) 复查静态目录（1s TTL 缓存，避免每请求 stat() 整棵
    目录树，P2-18）；内容 sha1 只在启动时计算一次，mtime 变化触发版本串
    变化但不重算哈希（版本串只需「变」，不需「是哈希」）。

    Implemented as a pure ASGI pass-through (no response wrapping), unlike
    ``@app.middleware("http")`` whose BaseHTTPMiddleware buffering asserted
    on streaming responses such as the MCP SSE endpoint.
    """

    _MTIME_TTL_SECONDS = 1.0

    def __init__(self, app):
        self.app = app
        self._last_check = 0.0
        self._cached_version = _STARTUP_ASSET_VERSION

    def _latest_mtime(self) -> int:
        latest = 0
        if static_dir.exists():
            for path in static_dir.rglob("*"):
                if path.is_file():
                    latest = max(latest, int(path.stat().st_mtime))
        return latest

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            now = time_module.monotonic()
            if now - self._last_check >= self._MTIME_TTL_SECONDS:
                self._last_check = now
                mtime = self._latest_mtime()
                if mtime:
                    self._cached_version = f"{_STARTUP_ASSET_VERSION}-{mtime}"
            scope["app"].state.asset_version = self._cached_version
        await self.app(scope, receive, send)


class SlowRequestMiddleware:
    """慢请求计时（P2-22）：>2s 的 HTTP 请求记 warning。

    纯 ASGI 直通（同 AssetVersionMiddleware 的原因）——2026-08-10 的 115 秒
    事故当时只能靠 access.log 事后翻，这里给 app.log 一条实时告警线。
    SSE 长连接（/mcp/sse）不纳入计时（连接本身常驻）。
    """

    SLOW_THRESHOLD_SECONDS = 2.0

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return
        started = time_module.monotonic()
        status_code = [0]

        async def send_with_status(message):
            if message["type"] == "http.response.start":
                status_code[0] = message.get("status", 0)
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            elapsed = time_module.monotonic() - started
            if elapsed >= self.SLOW_THRESHOLD_SECONDS:
                logger.warning(
                    "Slow request: %s %s → %s in %.1fs",
                    scope.get("method"), scope.get("path", ""), status_code[0], elapsed,
                )


class AuthWallMiddleware:
    """全站登录墙：无有效 session cookie 的请求一律拦截。

    - 页面请求（GET/HEAD 且路径非 /api 前缀）→ 303 跳 /login?next=<原地址>
    - API 请求 → 401 JSON
    - 豁免：/login、/api/auth/login、/static、/favicon.ico、/mcp
      （MCP 为机对机通道，由 McpBearerMiddleware 做 Bearer token 鉴权）
    - CSRF 第二道防线（SameSite=Lax 之外）：豁免名单外的 API 变更请求
      （POST/PUT/DELETE/PATCH）必须携带自定义头 X-Requested-With——
      跨站表单/简单请求无法携带自定义头，浏览器 CORS 预检会拦截。

    纯 ASGI 实现（同 AssetVersionMiddleware 的原因）。有效 session 写入
    ``scope["state"]["user"]`` 供路由依赖与模板使用；滑动续期触发时在
    响应头追加 Set-Cookie 同步浏览器侧有效期。
    """

    _EXEMPT_PATHS = frozenset({"/login", "/api/auth/login", "/favicon.ico"})
    _EXEMPT_PREFIXES = ("/static", "/mcp")

    def __init__(self, app):
        self.app = app
        # 401/403 计数采样：爆破/扫描行为在日志中可见但不刷屏（P1-1）
        self._reject_count = 0

    @classmethod
    def _is_exempt(cls, path: str) -> bool:
        """精确段匹配：/mcp 匹配 /mcp 与 /mcp/...，不匹配 /mcpanything。"""
        if path in cls._EXEMPT_PATHS:
            return True
        return any(path == p or path.startswith(p + "/") for p in cls._EXEMPT_PREFIXES)

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
    def _has_header(scope, name: bytes) -> bool:
        return any(k.lower() == name for k, _ in scope.get("headers", []))

    @staticmethod
    def _is_api_path(path: str) -> bool:
        # API 路由嵌在页面前缀下（/manual-trade/api/...、/subject-market/api/...），
        # 故按「包含 /api/」判定；/api（无尾斜杠）同样按 API 处理（401 JSON 而非 303 跳页）。
        return path == "/api" or "/api/" in path

    @classmethod
    def _is_page_request(cls, scope) -> bool:
        return scope.get("method") in ("GET", "HEAD") and not cls._is_api_path(scope.get("path", ""))

    def _reject_json(self, status: int, scope, detail: str) -> JSONResponse:
        self._reject_count += 1
        if self._reject_count == 1 or self._reject_count % 50 == 0:
            logger.warning(
                "AuthWall rejected %d request(s) so far; latest: %s %s → %d %s",
                self._reject_count, scope.get("method"), scope.get("path", ""), status, detail,
            )
        return JSONResponse(status_code=status, content={"detail": detail})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if self._is_exempt(path):
            await self.app(scope, receive, send)
            return

        # CSRF 防线：豁免名单外的 API 变更请求必须带自定义头
        if (
            scope.get("method") in ("POST", "PUT", "DELETE", "PATCH")
            and self._is_api_path(path)
            and not self._has_header(scope, b"x-requested-with")
        ):
            response = self._reject_json(403, scope, "缺少 X-Requested-With 请求头")
            await response(scope, receive, send)
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
                response = self._reject_json(401, scope, "未登录或登录已过期")
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
app.add_middleware(SlowRequestMiddleware)
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
# 机对机通道：登录墙豁免（AuthWall 提前放行），由 McpBearerMiddleware 做
# Bearer token 鉴权（TREND_MCP_TOKENS，token→用户映射）；写工具的用户身份
# 完全来自 token 映射，不再以工具参数传密码。
try:
    from app.mcp_auth import McpBearerMiddleware, load_mcp_tokens
    from trend_mcp.server import mcp as _mcp_app

    _mcp_tokens = load_mcp_tokens()
    if not _mcp_tokens:
        logger.warning(
            "TREND_MCP_TOKENS 未配置：/mcp 通道将对所有请求返回 401（失败关闭），"
            "请在 .env 配置 tokenA=username[,tokenB=username2] 后重启"
        )
    app.mount("/mcp", McpBearerMiddleware(_mcp_app.sse_app(), _mcp_tokens))
    logger.info("MCP SSE endpoint mounted at /mcp/sse (Bearer token auth, %d token(s))", len(_mcp_tokens))
except ImportError:
    logger.info("MCP package not installed – skipping /mcp endpoint")
