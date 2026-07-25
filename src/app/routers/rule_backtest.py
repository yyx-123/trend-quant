from __future__ import annotations

import logging
import threading
from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from core.calendar import previous_trading_day
from rule_backtest.models import DEFAULT_FEE_RATE
from rule_backtest.service import RuleBacktestService, slim_backtest_result

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rule-backtest", tags=["rule-backtest"])
templates = Jinja2Templates(directory="web/templates")
service = RuleBacktestService()

# In-memory async backtest jobs (no persistence; lost on restart by design).
_rule_jobs: dict[str, dict] = {}
_rule_jobs_lock = threading.Lock()
RULE_JOB_TTL_SECONDS = 1800


class RuleBacktestRunRequest(BaseModel):
    strategy_ids: list[str] = Field(default_factory=list)
    position_strategy_ids: list[str] = Field(default_factory=list)
    symbol: str
    start_date: str = Field(default="")
    end_date: str = Field(default="")
    initial_capital: float = Field(default=100_000)
    slippage: float = Field(default=0.002)
    fee_rate: float = Field(default=DEFAULT_FEE_RATE)
    fee_min: float = Field(default=5.0)
    lot_size: int = Field(default=100)
    instrument_type: str = Field(default="")
    stock_stamp_tax_rate: float = Field(default=0.001)
    debug_log_enabled: bool | None = Field(default=None)


class RuleStrategySaveRequest(BaseModel):
    strategy: dict
    overwrite: bool = Field(default=False)


class PositionStrategySaveRequest(BaseModel):
    strategy: dict
    overwrite: bool = Field(default=False)


def _cap_end_date(end_date_str: str) -> str:
    """Ensure backtest *end_date* does not include today or future dates.

    Intraday data is never persisted, so backtesting must stop at the
    most recent confirmed trading day.
    """
    if not end_date_str.strip():
        return end_date_str
    try:
        requested = date.fromisoformat(end_date_str.strip())
    except (ValueError, TypeError):
        return end_date_str
    yesterday = previous_trading_day(date.today())
    if requested >= date.today():
        return yesterday.isoformat()
    return end_date_str


@router.get("", response_class=HTMLResponse)
async def rule_backtest_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        name="rule_backtest.html",
        request=request,
        context={"title": "策略管理"},
    )


@router.get("/api/meta")
async def get_rule_backtest_meta() -> dict:
    return {
        "strategies": service.list_strategies(),
        "instruments": service.list_instruments(),
        "indicators": service.list_indicators(),
        "position_strategies": service.list_position_strategies(),
        "sizer_types": service.list_sizer_types(),
        "sizing": service.list_sizing_flags(),
        # Frontend form defaults (single source — JS must not hardcode these).
        "state_values": [
            "entry_price",
            "hard_stop",
            "highest_high_since_entry",
            "chandelier_stop",
            "days_since_last_exit",
        ],
        "stop_defaults": {
            "hard_stop": {"atr_period": 20, "atr_mul": 1.5},
            "chandelier_stop": {"atr_period": 20, "atr_mul": 2.5},
        },
    }


@router.post("/api/run")
async def run_rule_backtest(payload: RuleBacktestRunRequest) -> dict:
    """Start a rule backtest in a background thread and return immediately.

    The client polls GET /api/progress/{run_id} for K-line-level progress
    (small responses only) and fetches the finished result once from
    GET /api/result/{run_id}. Jobs live only in memory (see
    RULE_JOB_TTL_SECONDS).
    """
    # Cap end_date to previous trading day (intraday data is never persisted).
    payload.end_date = _cap_end_date(payload.end_date)

    run_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
    now = datetime.now()
    with _rule_jobs_lock:
        # Lazy eviction of expired jobs whenever a new run is created.
        expired = [
            jid for jid, j in _rule_jobs.items()
            if (now - j.get("created_at", now)).total_seconds() > RULE_JOB_TTL_SECONDS
        ]
        for jid in expired:
            _rule_jobs.pop(jid, None)
        _rule_jobs[run_id] = {
            "run_id": run_id,
            "status": "running",
            "progress_current": 0,
            "progress_total": 1,
            "error": None,
            "result": None,
            "created_at": now,
        }

    body = payload.model_dump()
    logger.info(
        "Rule backtest started run_id=%s symbol=%s strategies=%s sizers=%s range=%s~%s",
        run_id,
        payload.symbol,
        payload.strategy_ids,
        payload.position_strategy_ids,
        payload.start_date or "-",
        payload.end_date or "-",
    )

    def _progress_callback(current: int, total: int) -> None:
        with _rule_jobs_lock:
            job = _rule_jobs.get(run_id)
            if job:
                job["progress_current"] = current
                job["progress_total"] = max(int(total), 1)

    def _run() -> None:
        started_at = datetime.now()
        try:
            # Per-run service instance to avoid sharing engine state across threads.
            result = RuleBacktestService().run(body, progress_callback=_progress_callback)
            with _rule_jobs_lock:
                job = _rule_jobs.get(run_id)
                if job:
                    job["status"] = result.get("status", "ok")
                    job["progress_current"] = job.get("progress_total", 1)
                    # Full result stays in memory (future on-demand detail
                    # endpoints); only the slimmed copy goes on the wire.
                    job["result_full"] = result
                    job["result"] = slim_backtest_result(result)
            logger.info(
                "Rule backtest completed run_id=%s status=%s elapsed=%.1fs",
                run_id,
                result.get("status", "ok"),
                (datetime.now() - started_at).total_seconds(),
            )
        except (FileNotFoundError, ValueError) as exc:
            logger.warning("Rule backtest run_id=%s failed: %s", run_id, exc)
            with _rule_jobs_lock:
                job = _rule_jobs.get(run_id)
                if job:
                    job["status"] = "error"
                    job["error"] = str(exc)
        except Exception as exc:
            logger.exception("Rule backtest run_id=%s failed", run_id)
            with _rule_jobs_lock:
                job = _rule_jobs.get(run_id)
                if job:
                    job["status"] = "error"
                    job["error"] = str(exc)

    thread = threading.Thread(target=_run, daemon=True, name=f"rule-backtest-{run_id}")
    thread.start()

    return {"run_id": run_id, "status": "running"}


@router.get("/api/progress/{run_id}")
async def get_rule_backtest_progress(run_id: str) -> dict:
    """Progress-only response: intentionally never carries the result, so
    polling stays cheap even when the finished payload is large."""
    with _rule_jobs_lock:
        job = _rule_jobs.get(run_id)
    if job is None:
        raise HTTPException(status_code=404, detail="回测任务不存在或已过期")
    return {
        "run_id": job["run_id"],
        "status": job["status"],
        "progress_current": job.get("progress_current", 0),
        "progress_total": job.get("progress_total", 1),
        "error": job.get("error"),
    }


@router.get("/api/result/{run_id}")
async def get_rule_backtest_result(run_id: str) -> dict:
    """Fetch the (slimmed) backtest result once the run has finished.

    Reads the same in-memory job as the progress endpoint — nothing is
    persisted (jobs expire after RULE_JOB_TTL_SECONDS).
    """
    with _rule_jobs_lock:
        job = _rule_jobs.get(run_id)
    if job is None:
        raise HTTPException(status_code=404, detail="回测任务不存在或已过期")
    if job["status"] == "running":
        raise HTTPException(status_code=409, detail="回测仍在运行中")
    if job.get("result") is None:
        raise HTTPException(status_code=404, detail="回测无可用结果")
    return job["result"]


@router.post("/api/strategies")
async def save_rule_strategy(payload: RuleStrategySaveRequest) -> dict:
    try:
        return service.save_strategy(payload.strategy, overwrite=payload.overwrite)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/strategies/{strategy_id}")
async def delete_rule_strategy(strategy_id: str) -> dict:
    try:
        return service.delete_strategy(strategy_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/position-strategies")
async def save_position_strategy(payload: PositionStrategySaveRequest) -> dict:
    try:
        return service.save_position_strategy(payload.strategy, overwrite=payload.overwrite)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/position-strategies/{strategy_id}")
async def delete_position_strategy(strategy_id: str) -> dict:
    try:
        return service.delete_position_strategy(strategy_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
