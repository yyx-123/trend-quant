"""Out-of-band re-run of the 16:30 daily market update (idempotent catch-up).

Use when the scheduled run left partial failures (e.g. provider rate limits):
`ensure_daily_history` only fetches missing bars, so up-to-date symbols cost
no provider requests. Mirrors app.main's `_run_daily_update(force=True)`:
pool update -> post-update pipeline (dividend check + indicator rebuild).

Usage:  .venv/bin/python scripts/rerun_daily_update.py
"""

import sys
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parent))
from datetime import date

import _common  # .env 加载（锚定项目根）+ DB_PATH（P2-13）

from core.jobs import daily_market_update_job
from core.settings import load_settings
from data.service import DataService
from data.storage.db import init_db, record_job_run_safely
from services.indicator_builder import run_post_update_pipeline


def main() -> None:
    init_db(_common.DB_PATH)  # 与应用启动同一路径（锚定项目根）
    settings = load_settings()
    payload = daily_market_update_job(settings, force=True)
    print(
        f"daily update: {payload.get('success', 0)} success, "
        f"{payload.get('failed', 0)} failed out of {payload.get('total', 0)}"
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
