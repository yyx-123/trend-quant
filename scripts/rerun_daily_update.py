"""Out-of-band re-run of the 16:30 daily market update (idempotent catch-up).

Use when the scheduled run left partial failures (e.g. provider rate limits):
`ensure_daily_history` only fetches missing bars, so up-to-date symbols cost
no provider requests. Mirrors app.main's `_run_daily_update(force=True)`:
pool update -> post-update pipeline (dividend check + indicator rebuild).

Usage:  .venv/bin/python scripts/rerun_daily_update.py
"""

from datetime import date

from dotenv import load_dotenv

load_dotenv()

from core.jobs import daily_market_update_job  # noqa: E402
from core.settings import load_settings  # noqa: E402
from data.service import DataService  # noqa: E402
from data.storage.db import init_db, record_job_run_safely  # noqa: E402
from services.indicator_builder import run_post_update_pipeline  # noqa: E402


def main() -> None:
    init_db()  # same default path data/trend_quant.db as app startup
    settings = load_settings()
    payload = daily_market_update_job(settings, force=True)
    print(
        "daily update: %(success)s success, %(failed)s failed out of %(total)s"
        % {"success": payload.get("success", 0), "failed": payload.get("failed", 0), "total": payload.get("total", 0)}
    )
    if payload.get("failed_symbols"):
        print("failed symbols:", payload["failed_symbols"])
    if payload.get("status") == "skipped_non_trading_day":
        return

    service = DataService(provider_priority=settings.app.data_provider_priority)
    try:
        pipeline = run_post_update_pipeline(
            settings, service, payload, payload.get("symbols", []), date.today()
        )
    finally:
        service.close()
    payload["indicator_rebuild"] = pipeline
    record_job_run_safely(
        "indicator_rebuild",
        pipeline,
        run_date=date.today().isoformat(),
        status=str(pipeline.get("status", "")),
    )
    print("indicator pipeline:", pipeline)


if __name__ == "__main__":
    main()
