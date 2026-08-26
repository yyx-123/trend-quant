"""Instrument add / bulk-backfill job managers (service layer).

Moved out of app.routers.instruments — the router now only orchestrates HTTP.
Job runs are recorded into the job_runs table (job_type:
instrument_add / instrument_bulk_backfill / instrument_backfill).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import date, datetime
from typing import TYPE_CHECKING

from fastapi import HTTPException

from core.symbols import normalize_symbol

if TYPE_CHECKING:
    from data.service import DataService

# P1-14：原 instrument_admin 的纯转发包装已删，直接用 core 层实现
_normalize_symbol = normalize_symbol
from audit.app_logger import get_logger
from data.service import get_data_service
from data.storage.db import get_db, record_job_run_safely
from services.indicator_builder import rebuild_after_backfill
from services.instrument_admin import (
    _append_instrument_config,
    _build_new_instrument_record,
    _known_managed_symbols,
    add_constituent_stock,
)
from services.stock_industry import is_a_share

logger = get_logger(__name__)

def _empty_bulk_status() -> dict:
    return {
        "job_id": None,
        "status": "idle",
        "started_at": None,
        "finished_at": None,
        "progress_current": 0,
        "progress_total": 0,
        "current_symbol": None,
        "message": "空闲。",
        "error": None,
        "summary": {
            "total": 0,
            "finished": 0,
            "updated": 0,
            "no_data": 0,
            "failed": 0,
            "added_rows": 0,
        },
    }



from core.strategy_config import backfill_start_date as _backfill_start_date


class BulkBackfillJobManager:
    def __init__(
        self,
        data_service_factory: Callable[[list[str] | None], DataService] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._status = _empty_bulk_status()
        self._data_service_factory = data_service_factory or (
            lambda provider_priority: get_data_service()
        )

    def snapshot(self) -> dict:
        with self._lock:
            return self._copy_status()

    def is_running(self) -> bool:
        with self._lock:
            return self._status.get("status") == "running"

    def start(
        self,
        *,
        items: list[dict],
        end_date: date,
        adjust: str,
        provider_priority: list[str] | None,
    ) -> tuple[bool, dict]:
        if not items:
            raise HTTPException(status_code=400, detail="没有可补齐的标的")

        now = datetime.now()
        job_id = now.strftime("%Y%m%d%H%M%S%f")
        with self._lock:
            if self._status.get("status") == "running":
                return False, self._copy_status()

            record_job_run_safely(
                "instrument_bulk_backfill",
                {"job_id": job_id, "total": len(items)},
                run_date=now.date().isoformat(),
                status="running",
            )
            self._status = {
                "job_id": job_id,
                "status": "running",
                "started_at": now.isoformat(),
                "finished_at": None,
                "progress_current": 0,
                "progress_total": len(items),
                "current_symbol": None,
                "message": f"后台补齐任务已启动，共 {len(items)} 个标的。",
                "error": None,
                "summary": {
                    "total": len(items),
                    "finished": 0,
                    "updated": 0,
                    "up_to_date": 0,
                    "no_data": 0,
                    "failed": 0,
                    "added_rows": 0,
                    "attempt": 1,
                    "max_attempts": 4,
                    "retrying": 0,
                },
                "results": [],
                "adjust": adjust,
                "requested_end": end_date.isoformat(),
            }
            snapshot = self._copy_status()

        thread = threading.Thread(
            target=self._run,
            kwargs={
                "job_id": job_id,
                "items": items,
                "end_date": end_date,
                "adjust": adjust,
                "provider_priority": provider_priority,
            },
            daemon=True,
        )
        thread.start()
        return True, snapshot

    def _copy_status(self) -> dict:
        status = dict(self._status)
        status["summary"] = dict(self._status.get("summary") or {})
        status["results"] = list(self._status.get("results") or [])
        return status

    def _run(
        self,
        *,
        job_id: str,
        items: list[dict],
        end_date: date,
        adjust: str,
        provider_priority: list[str] | None,
    ) -> None:
        data_service = self._data_service_factory(provider_priority)
        try:
            def _on_progress(update: dict) -> None:
                with self._lock:
                    if self._status.get("job_id") != job_id:
                        return
                    event = str(update.get("event") or "")
                    summary = self._status["summary"]
                    summary["attempt"] = int(update.get("attempt") or summary.get("attempt") or 1)
                    summary["max_attempts"] = int(update.get("max_attempts") or summary.get("max_attempts") or 4)
                    if event == "retry_sleep":
                        summary["retrying"] = int(update.get("remaining") or 0)
                    else:
                        summary["retrying"] = int(summary.get("retrying") or 0)
                    self._status["progress_current"] = int(
                        update.get("finished") or self._status.get("progress_current") or 0
                    )
                    self._status["progress_total"] = int(update.get("total") or len(items))
                    if event == "attempt_start":
                        self._status["message"] = (
                            f"第 {summary['attempt']}/{summary['max_attempts']} 轮批量补齐，"
                            f"待处理 {int(update.get('remaining') or 0)} 个标的。"
                        )
                    elif event == "request_start":
                        symbols = list(update.get("symbols") or [])
                        self._status["current_symbol"] = symbols[0] if symbols else None
                        self._status["message"] = (
                            f"正在批量请求第 {summary['attempt']}/{summary['max_attempts']} 轮 "
                            f"第 {int(update.get('chunk_index') or 0)}/{int(update.get('chunk_total') or 0)} 批，"
                            f"本批 {len(symbols)} 个标的。"
                        )
                    elif event == "item_done":
                        symbol = str(update.get("symbol") or "")
                        self._status["current_symbol"] = symbol or None
                        self._status["message"] = (
                            f"已完成 {self._status['progress_current']}/{self._status['progress_total']}：{symbol}"
                        )
                    elif event == "retry_sleep":
                        self._status["current_symbol"] = None
                        self._status["message"] = (
                            f"本轮有 {int(update.get('remaining') or 0)} 个标的失败，"
                            f"{float(update.get('wait_seconds') or 0):.1f} 秒后只重试失败标的。"
                        )

            result_payloads = data_service.backfill_daily_histories(
                items=items,
                end_date=end_date,
                adjust=adjust,
                max_retries=3,
                batch_size=100,
                request_interval_seconds=2.0,
                retry_delay_seconds=2.0,
                progress_callback=_on_progress,
            )

            for result_payload in result_payloads:
                with self._lock:
                    if self._status.get("job_id") != job_id:
                        return
                    summary = self._status["summary"]
                    summary["finished"] = int(summary.get("finished", 0)) + 1
                    if result_payload["ok"]:
                        result = result_payload["result"]
                        status = str(result.get("status") or "")
                        if status == "no_data":
                            summary["no_data"] = int(summary.get("no_data", 0)) + 1
                        elif status == "up_to_date":
                            summary["up_to_date"] = int(summary.get("up_to_date", 0)) + 1
                        else:
                            summary["updated"] = int(summary.get("updated", 0)) + 1
                        summary["added_rows"] = int(summary.get("added_rows", 0)) + int(
                            result.get("added_rows") or 0
                        )
                    else:
                        summary["failed"] = int(summary.get("failed", 0)) + 1

                    self._status.setdefault("results", []).append(result_payload)

            with self._lock:
                if self._status.get("job_id") != job_id:
                    return
                summary = self._status["summary"]
                all_failed = (
                    int(summary.get("total") or len(items)) > 0
                    and int(summary.get("failed") or 0) == int(summary.get("total") or len(items))
                    and int(summary.get("updated") or 0) == 0
                    and int(summary.get("up_to_date") or 0) == 0
                    and int(summary.get("no_data") or 0) == 0
                )
                first_error = ""
                if all_failed:
                    for result_payload in self._status.get("results") or []:
                        if not result_payload.get("ok"):
                            first_error = str(result_payload.get("error") or "")
                            break
                self._status["status"] = "failed" if all_failed else "completed"
                self._status["progress_current"] = int(summary.get("total") or len(items))
                self._status["progress_total"] = int(summary.get("total") or len(items))
                self._status["current_symbol"] = None
                self._status["finished_at"] = datetime.now().isoformat()
                if all_failed:
                    self._status["error"] = first_error or "所有标的补齐失败"
                    self._status["message"] = (
                        f"后台补齐失败：共 {summary.get('total', 0)} 个标的全部失败。"
                        f"{first_error}"
                    )
                else:
                    self._status["message"] = (
                        f"后台补齐完成：共 {summary.get('total', 0)} 个标的，"
                        f"已最新 {summary.get('up_to_date', 0)} 个，"
                        f"失败 {summary.get('failed', 0)} 个，"
                        f"未获取到数据 {summary.get('no_data', 0)} 个，"
                        f"新增 {int(summary.get('added_rows', 0)):,} 行。"
                    )
                final_status = self._copy_status()
            updated_symbols = [
                str((r.get("result") or {}).get("symbol") or "").strip().upper()
                for r in (self._status.get("results") or [])
                if r.get("ok") and str((r.get("result") or {}).get("status") or "") == "updated"
            ]
            if updated_symbols and not all_failed:
                rebuild_after_backfill(updated_symbols)
            record_job_run_safely(
                "instrument_bulk_backfill", final_status, status=str(final_status.get("status") or "")
            )
        except Exception as exc:
            logger.exception("Instrument bulk backfill job_id=%s failed", job_id)
            with self._lock:
                if self._status.get("job_id") == job_id:
                    self._status["status"] = "failed"
                    self._status["finished_at"] = datetime.now().isoformat()
                    self._status["error"] = str(exc)
                    self._status["message"] = f"后台补齐任务失败：{exc}"
        finally:
            # 默认 factory 返回进程级共享单例（P2-11），一律不 close；
            # 测试注入的私有 fake 由测试自行管理生命周期。
            pass


bulk_backfill_manager = BulkBackfillJobManager()

def _empty_add_status() -> dict:
    return {
        "job_id": None,
        "status": "idle",
        "started_at": None,
        "finished_at": None,
        "progress_current": 0,
        "progress_total": 0,
        "current_symbol": None,
        "message": "空闲。",
        "error": None,
        "summary": {
            "symbol": None,
            "name": None,
            "metadata_saved": 0,
            "config_saved": 0,
            "added_rows": 0,
            "backfill_status": None,
        },
    }


class InstrumentAddJobManager:
    def __init__(
        self,
        data_service_factory: Callable[[list[str] | None], DataService] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._status = _empty_add_status()
        self._pending_symbols: set[str] = set()
        self._data_service_factory = data_service_factory or (
            lambda provider_priority: get_data_service()
        )

    def snapshot(self) -> dict:
        with self._lock:
            return self._copy_status()

    def is_running(self) -> bool:
        with self._lock:
            return self._status.get("status") == "running"

    def is_symbol_pending(self, symbol: str) -> bool:
        normalized = _normalize_symbol(symbol)
        with self._lock:
            return normalized in self._pending_symbols

    def start(
        self,
        *,
        item: dict,
        end_date: date,
        adjust: str,
        provider_priority: list[str] | None,
    ) -> tuple[bool, dict]:
        symbol = _normalize_symbol(item.get("symbol", ""))
        if not symbol:
            raise HTTPException(status_code=400, detail="标的无效")

        now = datetime.now()
        job_id = now.strftime("%Y%m%d%H%M%S%f")
        with self._lock:
            if self._status.get("status") == "running":
                return False, self._copy_status()

            record_job_run_safely(
                "instrument_add",
                {"job_id": job_id, "symbol": str(item.get("symbol") or "")},
                run_date=now.date().isoformat(),
                status="running",
            )
            if symbol in self._pending_symbols:
                return False, self._copy_status()

            self._pending_symbols.add(symbol)
            self._status = {
                "job_id": job_id,
                "status": "running",
                "started_at": now.isoformat(),
                "finished_at": None,
                "progress_current": 0,
                "progress_total": 3,
                "current_symbol": symbol,
                "message": f"新增标的任务已启动：{symbol}。",
                "error": None,
                "summary": {
                    "symbol": symbol,
                    "name": str(item.get("name") or "").strip(),
                    "metadata_saved": 0,
                    "config_saved": 0,
                    "added_rows": 0,
                    "backfill_status": None,
                },
                "result": None,
                "adjust": adjust,
                "requested_end": end_date.isoformat(),
            }
            snapshot = self._copy_status()

        thread = threading.Thread(
            target=self._run,
            kwargs={
                "job_id": job_id,
                "item": {**item, "symbol": symbol},
                "end_date": end_date,
                "adjust": adjust,
                "provider_priority": provider_priority,
            },
            daemon=True,
        )
        thread.start()
        return True, snapshot

    def _copy_status(self) -> dict:
        status = dict(self._status)
        status["summary"] = dict(self._status.get("summary") or {})
        status["result"] = dict(self._status.get("result") or {}) if self._status.get("result") else None
        return status

    def _set_progress(self, job_id: str, current: int, message: str) -> None:
        with self._lock:
            if self._status.get("job_id") != job_id:
                return
            self._status["progress_current"] = current
            self._status["message"] = message

    def _run(
        self,
        *,
        job_id: str,
        item: dict,
        end_date: date,
        adjust: str,
        provider_priority: list[str] | None,
    ) -> None:
        symbol = str(item.get("symbol") or "").strip().upper()
        data_service = self._data_service_factory(provider_priority)
        try:
            self._set_progress(job_id, 0, f"正在写入 {symbol} 的配置与分类信息。")
            record = _build_new_instrument_record(item)
            saved = _append_instrument_config(record)
            with self._lock:
                if self._status.get("job_id") == job_id:
                    summary = self._status["summary"]
                    summary["name"] = record.get("name")
                    summary["metadata_saved"] = saved
                    summary["config_saved"] = saved
                    self._status["progress_current"] = 1
                    self._status["message"] = f"{symbol} 已写入配置，正在补齐历史行情。"

            result = data_service.backfill_daily_history(
                symbol=symbol,
                start_date=_backfill_start_date(),
                end_date=end_date,
                adjust=adjust,
            )
            result["adjust"] = adjust
            result["symbol"] = symbol
            result["request_adjusted_to_earliest_available"] = bool(
                result.get("fetched_start")
                and result.get("requested_start")
                and str(result.get("fetched_start")) > str(result.get("requested_start"))
            )

            with self._lock:
                if self._status.get("job_id") != job_id:
                    return
                summary = self._status["summary"]
                summary["added_rows"] = int(result.get("added_rows") or 0)
                summary["backfill_status"] = str(result.get("status") or "")
                self._status["status"] = "completed"
                self._status["finished_at"] = datetime.now().isoformat()
                self._status["progress_current"] = 3
                self._status["current_symbol"] = None
                self._status["message"] = (
                    f"新增标的完成：{symbol}，新增 "
                    f"{int(result.get('added_rows') or 0):,} 行历史行情。"
                )
                self._status["result"] = result
                final_status = self._copy_status()
            if str(result.get("status") or "") not in ("error", "no_data"):
                rebuild_after_backfill([symbol])
            record_job_run_safely("instrument_add", final_status, status=str(final_status.get("status") or ""))
        except Exception as exc:
            logger.exception("Instrument add job_id=%s failed for %s", job_id, symbol)
            with self._lock:
                if self._status.get("job_id") == job_id:
                    self._status["status"] = "failed"
                    self._status["finished_at"] = datetime.now().isoformat()
                    self._status["error"] = str(exc)
                    self._status["message"] = f"新增标的任务失败：{exc}"
        finally:
            with self._lock:
                self._pending_symbols.discard(symbol)


add_instrument_manager = InstrumentAddJobManager()


def _empty_etf_import_status() -> dict:
    return {
        "job_id": None,
        "status": "idle",
        "started_at": None,
        "finished_at": None,
        "progress_current": 0,
        "progress_total": 0,
        "current_symbol": None,
        "message": "空闲。",
        "error": None,
        "summary": {
            "etf_symbol": None,
            "total": 0,
            "added": [],
            "skipped": [],
            "failed": [],
            "backfill_updated": 0,
            "backfill_failed": 0,
        },
    }


class EtfConstituentImportJobManager:
    """ETF 前十大重仓股一键导入标的池（方案 §6.1）。

    逐股票 resolve_category 自动归类（命中 → 申万类目，未命中 → 待分类，
    后续同步自动回补）；已管理的跳过不覆盖用户配置；重复导入全部 skipped，幂等。
    """

    def __init__(
        self,
        data_service_factory: Callable[[list[str] | None], DataService] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._status = _empty_etf_import_status()
        self._data_service_factory = data_service_factory or (
            lambda provider_priority: get_data_service()
        )

    def snapshot(self) -> dict:
        with self._lock:
            return self._copy_status()

    def _copy_status(self) -> dict:
        status = dict(self._status)
        summary = dict(self._status.get("summary") or {})
        for key in ("added", "skipped", "failed"):
            summary[key] = list(summary.get(key) or [])
        status["summary"] = summary
        return status

    def is_running(self) -> bool:
        with self._lock:
            return self._status.get("status") == "running"

    def start(
        self,
        *,
        etf_symbol: str,
        end_date: date,
        adjust: str,
        provider_priority: list[str] | None,
    ) -> tuple[bool, dict]:
        now = datetime.now()
        job_id = now.strftime("%Y%m%d%H%M%S%f")
        with self._lock:
            if self._status.get("status") == "running":
                return False, self._copy_status()

            record_job_run_safely(
                "etf_constituent_import",
                {"job_id": job_id, "etf_symbol": etf_symbol},
                run_date=now.date().isoformat(),
                status="running",
            )
            self._status = {
                **_empty_etf_import_status(),
                "job_id": job_id,
                "status": "running",
                "started_at": now.isoformat(),
                "message": f"ETF 重仓股导入任务已启动：{etf_symbol}。",
            }
            self._status["summary"]["etf_symbol"] = etf_symbol
            snapshot = self._copy_status()

        thread = threading.Thread(
            target=self._run,
            kwargs={
                "job_id": job_id,
                "etf_symbol": etf_symbol,
                "end_date": end_date,
                "adjust": adjust,
                "provider_priority": provider_priority,
            },
            daemon=True,
        )
        thread.start()
        return True, snapshot

    def _set_progress(self, job_id: str, current: int, total: int, message: str) -> None:
        with self._lock:
            if self._status.get("job_id") != job_id:
                return
            self._status["progress_current"] = current
            self._status["progress_total"] = total
            self._status["message"] = message

    def _run(
        self,
        *,
        job_id: str,
        etf_symbol: str,
        end_date: date,
        adjust: str,
        provider_priority: list[str] | None,
    ) -> None:
        db = get_db()
        data_service = self._data_service_factory(provider_priority)
        try:
            rows = db.list_current_etf_constituents(etf_symbol)
            if not rows:
                raise ValueError(f"{etf_symbol} 暂无重仓股快照，请先运行季度快照脚本")

            known = _known_managed_symbols()
            added: list[dict] = []
            skipped: list[dict] = []
            failed: list[dict] = []
            backfill_items: list[dict] = []

            with self._lock:
                self._status["summary"]["total"] = len(rows)

            for idx, row in enumerate(rows, start=1):
                symbol = str(row.get("stock_symbol") or "").strip().upper()
                name = str(row.get("stock_name") or "").strip() or symbol
                if not is_a_share(symbol):
                    # 港股/美股/北交所等如实保留在快照里，但不纳入管理
                    skipped.append({"symbol": symbol, "name": name, "reason": "not_manageable"})
                    continue
                self._set_progress(job_id, idx, len(rows) + 1, f"正在写入 {symbol} {name} 的标的信息。")
                outcome = add_constituent_stock(symbol, name, known_symbols=known)
                status = outcome["status"]
                if status == "added":
                    known.add(symbol)  # 同一只 ETF 内重复出现时后续直接跳过
                    added.append(
                        {
                            "symbol": symbol,
                            "name": name,
                            "category": outcome.get("category", ""),
                            "hit": bool(outcome.get("hit")),
                        }
                    )
                    backfill_items.append({"symbol": symbol, "start_date": _backfill_start_date()})
                elif status == "skipped":
                    skipped.append({"symbol": symbol, "name": name, "reason": "already_managed"})
                else:
                    failed.append({"symbol": symbol, "name": name, "error": outcome.get("error", "")})

            backfill_updated = 0
            backfill_failed = 0
            updated_symbols: list[str] = []
            if backfill_items:
                self._set_progress(
                    job_id, len(rows), len(rows) + 1,
                    f"正在批量回补 {len(backfill_items)} 只新标的的历史行情。",
                )
                result_payloads = data_service.backfill_daily_histories(
                    items=backfill_items,
                    end_date=end_date,
                    adjust=adjust,
                    max_retries=3,
                    batch_size=100,
                    request_interval_seconds=2.0,
                    retry_delay_seconds=2.0,
                )
                for payload in result_payloads:
                    if payload.get("ok") and str((payload.get("result") or {}).get("status") or "") not in (
                        "error",
                        "no_data",
                    ):
                        backfill_updated += 1
                        updated_symbols.append(
                            str((payload.get("result") or {}).get("symbol") or "").strip().upper()
                        )
                    else:
                        backfill_failed += 1
                        symbol = str((payload.get("result") or {}).get("symbol") or payload.get("symbol") or "")
                        for entry in failed:
                            if entry["symbol"] == symbol:
                                break
                        else:
                            failed.append(
                                {
                                    "symbol": symbol,
                                    "name": "",
                                    "error": str(payload.get("error") or "行情回补失败")[:200],
                                }
                            )

            with self._lock:
                if self._status.get("job_id") != job_id:
                    return
                summary = self._status["summary"]
                summary["added"] = added
                summary["skipped"] = skipped
                summary["failed"] = failed
                summary["backfill_updated"] = backfill_updated
                summary["backfill_failed"] = backfill_failed
                self._status["status"] = "completed"
                self._status["finished_at"] = datetime.now().isoformat()
                self._status["progress_current"] = len(rows) + 1
                self._status["progress_total"] = len(rows) + 1
                self._status["current_symbol"] = None
                pending_note = f"，其中 {sum(1 for a in added if not a['hit'])} 只待分类（后续同步自动回补）" if added else ""
                unmanaged_count = sum(1 for s in skipped if s.get("reason") == "not_manageable")
                managed_count = len(skipped) - unmanaged_count
                unmanaged_note = f"、不纳入管理 {unmanaged_count} 只（港股等非 A 股）" if unmanaged_count else ""
                self._status["message"] = (
                    f"导入完成：新增 {len(added)} 只、已在管理跳过 {managed_count} 只"
                    f"{unmanaged_note}、失败 {len(failed)} 只{pending_note}。"
                )
                final_status = self._copy_status()

            if updated_symbols:
                rebuild_after_backfill(updated_symbols)
            record_job_run_safely(
                "etf_constituent_import", final_status, status=str(final_status.get("status") or "")
            )
        except Exception as exc:
            logger.exception("ETF constituent import job_id=%s failed for %s", job_id, etf_symbol)
            with self._lock:
                if self._status.get("job_id") == job_id:
                    self._status["status"] = "failed"
                    self._status["finished_at"] = datetime.now().isoformat()
                    self._status["error"] = str(exc)
                    self._status["message"] = f"ETF 重仓股导入失败：{exc}"
        finally:
            # 默认 factory 返回进程级共享单例（P2-11），一律不 close；
            # 测试注入的私有 fake 由测试自行管理生命周期。
            pass


etf_constituent_import_manager = EtfConstituentImportJobManager()

# ---------------------------------------------------------------------------
# 进程重启清理（P2-9）
# ---------------------------------------------------------------------------
def mark_interrupted_at_startup(db=None) -> int:
    """启动清扫（P2-9）：job_runs 中停在 running 且无配对终态行的任务
    标记为 interrupted（比照批量回测的 mark_interrupted_batch_runs）。

    任务运行时状态虽在内存，但 start() 会落 status=running 的 job_run；
    进程重启后该行为孤儿——启动时清扫标记，任务历史可追溯。
    返回被标记的行数。
    """
    db = db or get_db()
    return db.mark_interrupted_job_runs(
        ["instrument_bulk_backfill", "instrument_add", "etf_constituent_import"]
    )
