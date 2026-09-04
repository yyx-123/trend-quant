"""标的大盘盘中实时看板：快照单例运行器。

盘中实时计算耗时长（全市场实时报价 + 逐标的指标重算，约 20 秒），
因此采用「定时快照」模式：

- 交易时段内的定时任务（core/scheduler，每 5 分钟一轮）通过
  ``ensure_running`` 触发重算；同一进程任意时刻只允许一个在跑的实时
  计算任务，重复触发直接复用进行中的任务。页面只展示快照，不触发重算。
- 每次计算完成，结果作为最新快照持久化到独立的 ``dashboard_snapshot``
  表（单行替换），页面打开时优先展示该快照。

红线：快照 payload 只服务于看板展示，严禁写入 market_data_* 日K库 ——
日K数据只能由收盘后的补库任务写入稳定值。``build_intraday_dashboard``
本身全程在内存中合成当日K线，不落库；本模块的持久化也仅限快照表。

读取侧（``intraday_dashboard_snapshot``）是 MCP intraday_dashboard 工具的
唯一数据来源：只读定时快照、绝不触发重算，数据最旧约 5 分钟。
"""

from __future__ import annotations

import threading
from datetime import datetime, time
from typing import Any

from audit.app_logger import get_logger
from core.calendar import (
    is_past_market_open,
    is_realtime_available,
    is_trading_day,
    market_now,
)
from core.display import filter_fully_classified
from data.intraday_service import build_intraday_dashboard
from data.service import get_data_service
from data.storage.db import get_db
from services.dashboard import dashboard_lite, trend_dashboard_payload
from services.market_indicators import trend_config

logger = get_logger(__name__)


class IntradaySnapshotRunner:
    """进程级单例：管理盘中看板重算任务与最新快照。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._percent = 0.0
        self._message = ""
        self._last_error: str | None = None
        self._last_finished_at: str | None = None
        self._snapshot: dict | None = None
        self._snapshot_loaded = False

    # ------------------------------------------------------------------
    # 快照读取（内存优先，首次访问时从 DB 懒加载，进程重启后仍可展示）
    # ------------------------------------------------------------------
    def latest_snapshot(self) -> dict | None:
        """Return the latest snapshot dict (kind/as_of/computed_at/payload)."""
        with self._lock:
            if not self._snapshot_loaded:
                try:
                    self._snapshot = get_db().load_dashboard_snapshot()
                    # 仅在读取成功后才置位：首次失败（如 DB 短暂不可用）不永久
                    # 缓存 None，下次访问重试（旧实现先置位导致进程重启前快照
                    # 展示失效）。
                    self._snapshot_loaded = True
                except Exception:
                    logger.exception("Failed to load dashboard snapshot from DB")
            return self._snapshot

    # ------------------------------------------------------------------
    # 状态查询（前端轮询用）
    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "percent": self._percent,
                "message": self._message,
                "last_error": self._last_error,
                "last_finished_at": self._last_finished_at,
                "snapshot_ts": (self._snapshot or {}).get("computed_at"),
            }

    # ------------------------------------------------------------------
    # 触发重算（定时任务唯一入口，单例保证不并发）
    # ------------------------------------------------------------------
    def ensure_running(self, trigger: str) -> dict[str, Any]:
        """Start a recompute unless one is already running or not applicable.

        Returns {"status": "started" | "running" | "skipped", ...}.
        """
        now = market_now()
        today = now.date()
        with self._lock:
            if self._running:
                return {"status": "running", "percent": self._percent, "message": self._message}
            if not is_trading_day(today):
                return {"status": "skipped", "reason": "non_trading_day"}
            # 午间休盘与收盘后（日K补库落库前）同样允许：报价分别是上午
            # 快照与当日收盘快照。
            if not is_past_market_open(now):
                return {"status": "skipped", "reason": "pre_open"}
            try:
                revision = get_db().get_market_dashboard_revision()
            except Exception:
                logger.exception("Failed to read dashboard revision")
                return {"status": "skipped", "reason": "revision_unavailable"}
            max_bar = str(revision[0])[:10] if revision and revision[0] else ""
            if max_bar >= today.isoformat():
                # 今日日K已落库，EOD 看板即为盘后确认值，无需实时估算。
                return {"status": "skipped", "reason": "eod_current"}
            self._running = True
            self._percent = 0.0
            self._message = "正在准备…"
            self._last_error = None

        thread = threading.Thread(target=self._run, args=(trigger,), daemon=True)
        thread.start()
        return {"status": "started"}

    # ------------------------------------------------------------------
    def _run(self, trigger: str) -> None:
        try:
            db = get_db()
            symbols = db.list_market_symbols(price_mode="qfq")
            if not symbols:
                raise RuntimeError("本地无日K数据")
            metadata_map = db.get_instrument_metadata_map()
            classified = filter_fully_classified(symbols, metadata_map)
            if not classified:
                raise RuntimeError("无完整分类的标的")

            data_service = get_data_service()
            payload = build_intraday_dashboard(
                classified,
                db=db,
                data_service=data_service,
                trend_config=trend_config(),
                progress_callback=self._on_progress,
            )

            computed_at = db.save_dashboard_snapshot("intraday", payload.get("as_of"), payload)
            snapshot = {
                "kind": "intraday",
                "as_of": payload.get("as_of"),
                "computed_at": computed_at,
                "payload": payload,
            }
            with self._lock:
                self._snapshot = snapshot
                self._snapshot_loaded = True
                self._last_finished_at = computed_at
                self._percent = 1.0
                self._message = "完成"
            logger.info(
                "Intraday dashboard snapshot saved (trigger=%s, as_of=%s)",
                trigger,
                snapshot["as_of"],
            )
        except Exception as exc:
            logger.exception("Intraday dashboard recompute failed (trigger=%s)", trigger)
            with self._lock:
                self._last_error = str(exc)
        finally:
            with self._lock:
                self._running = False

    def _on_progress(self, update: dict) -> None:
        with self._lock:
            self._percent = float(update.get("percent", 0))
            self._message = str(update.get("message", ""))


snapshot_runner = IntradaySnapshotRunner()

# ---------------------------------------------------------------------------
# 快照读取口径（MCP dashboard 工具 intraday 口径的数据来源）
# ---------------------------------------------------------------------------

# 新鲜度校验与 core/scheduler.INTRADAY_SNAPSHOT_CRONS 对齐：调度在
# 9:35~11:30 / 13:00~15:00 每 5 分钟一轮（单轮约 20 秒）。「调度活跃窗口」
# 内要求快照 computed_at 不旧于 _SNAPSHOT_STALE_SECONDS（一轮间隔 + 耗时 +
# 一轮 misfire 余量）；窗口外（午间休盘、15:05 收盘后）快照本就不再更新，
# 只要 as_of 是当日即有效——收盘快照会一直服役到当日日K落库后由 EOD 口径
# 接管（dashboard_payload 的 auto 路由负责切换）。
_SNAPSHOT_ACTIVE_WINDOWS: tuple[tuple[time, time], ...] = (
    (time(9, 30), time(11, 35)),
    (time(13, 0), time(15, 5)),
)
_SNAPSHOT_STALE_SECONDS = 600.0


def _in_snapshot_active_window(now: datetime) -> bool:
    current = now.time()
    return any(start <= current <= end for start, end in _SNAPSHOT_ACTIVE_WINDOWS)


def _filter_dashboard_tree(payload: dict, keyword: str) -> dict | None:
    """按分类关键词（匹配 L1/L2/L3 任一级，大小写不敏感）裁剪看板树。

    命中上级类目则保留整个子树；只命中 L3 时保留路径上的 L1/L2 节点。
    返回裁剪后的新 payload（不改原对象）；无任何匹配时返回 None。
    """
    kw = keyword.lower()

    def _hit(node: dict, key: str) -> bool:
        return kw in str(node.get(key) or "").lower()

    groups_out: list[dict] = []
    for group in payload.get("groups") or []:
        if _hit(group, "category_l1"):
            groups_out.append(group)
            continue
        items_out: list[dict] = []
        for item in group.get("items") or []:
            if _hit(item, "category_l2"):
                items_out.append(item)
                continue
            children = [c for c in item.get("children") or [] if _hit(c, "category_l3")]
            if children:
                kept = dict(item)
                kept["children"] = children
                kept["child_count"] = len(children)
                items_out.append(kept)
        if items_out:
            kept_group = dict(group)
            kept_group["items"] = items_out
            kept_group["count"] = len(items_out)
            groups_out.append(kept_group)

    if not groups_out:
        return None

    out = dict(payload)
    out["groups"] = groups_out
    out["secondary_count"] = sum(len(g["items"]) for g in groups_out)
    out["category_count"] = sum(
        len(item["children"]) for g in groups_out for item in g["items"]
    )
    out["instrument_count"] = sum(
        len(l3.get("children") or [])
        for g in groups_out
        for item in g["items"]
        for l3 in item["children"]
    )
    return out


def intraday_dashboard_snapshot(
    category: str = "", detail: str = "full", db=None
) -> dict:
    """盘中实时看板：只读 5 分钟定时快照，绝不触发重算。

    数据新鲜度：调度活跃时段最旧约 5 分钟（一轮间隔），午间休盘为上午
    收盘快照，15:00 后为当日收盘快照。门控与运行器一致——非交易日 /
    开盘前 / 今日快照未生成 / 快照过期（调度疑似停摆）返回 ok=False。

    ``category`` 在快照树上做 L1/L2/L3 过滤（快照是全市场，过滤零成本）；
    ``detail="lite"`` 返回瘦身副本（见 services.dashboard.dashboard_lite）。
    """
    db = db or get_db()
    now = market_now()
    if not is_trading_day(now.date()):
        return {
            "ok": False,
            "error": "今日非交易日，无盘中快照；请用 dashboard（mode=auto 默认即为 EOD 日K口径）",
        }
    if not is_past_market_open(now):
        return {
            "ok": False,
            "error": "今日尚未开盘（需 9:30 之后）；请用 dashboard（mode=auto 默认即为 EOD 日K口径）",
        }

    snapshot = db.load_dashboard_snapshot()
    today = now.date().isoformat()
    if not snapshot or str(snapshot.get("as_of") or "")[:10] != today:
        return {
            "ok": False,
            "error": "今日盘中快照尚未生成（首轮约 9:35 完成）；请稍后重试，或用 dashboard（mode=auto 自动回退 EOD 口径）",
        }

    computed_at_raw = str(snapshot.get("computed_at") or "")
    try:
        computed_at = datetime.fromisoformat(computed_at_raw)
    except ValueError:
        computed_at = None
    now_naive = now.replace(tzinfo=None)
    if (
        computed_at is not None
        and _in_snapshot_active_window(now_naive)
        and (now_naive - computed_at).total_seconds() > _SNAPSHOT_STALE_SECONDS
    ):
        return {
            "ok": False,
            "error": f"盘中快照过期（最后更新 {computed_at_raw}，定时快照任务疑似未运行）；请检查服务状态",
        }

    category = category.strip()
    payload: dict = dict(snapshot.get("payload") or {})
    if category:
        filtered = _filter_dashboard_tree(payload, category)
        if filtered is None:
            return {"ok": False, "error": f"无匹配「{category}」的分类"}
        payload = filtered

    payload["ok"] = True
    payload["source"] = "snapshot"
    payload["snapshot_ts"] = computed_at_raw
    payload["post_close"] = not is_realtime_available(now)
    payload["requested_category"] = category
    if detail == "lite":
        lite = dashboard_lite(payload)
        lite["detail"] = "lite"
        return lite
    return payload


# ---------------------------------------------------------------------------
# 合并看板入口（MCP dashboard 工具的唯一实现）：按时段自动选择数据口径
# ---------------------------------------------------------------------------

def _eod_current(db, now: datetime) -> bool:
    """今日日K已落库（收盘补库完成）——与运行器 ensure_running 的跳过口径一致。"""
    try:
        revision = db.get_market_dashboard_revision()
    except Exception:
        logger.exception("Failed to read dashboard revision")
        return False
    max_bar = str(revision[0])[:10] if revision and revision[0] else ""
    return bool(max_bar) and max_bar >= now.date().isoformat()


def _eod_dashboard_response(category: str, detail: str) -> dict:
    """EOD 口径响应：RevisionCache 全量 payload → category 过滤 → lite 瘦身。

    注意缓存里的 full payload 是共享对象：加响应标记前必须浅拷贝，
    category 过滤 / lite 瘦身本身都构建新结构，不污染缓存。
    """
    payload = dict(trend_dashboard_payload(detail="full"))
    if category:
        filtered = _filter_dashboard_tree(payload, category)
        if filtered is None:
            return {"ok": False, "error": f"无匹配「{category}」的分类"}
        payload = filtered
    payload["ok"] = True
    payload["data_mode"] = "eod"
    payload["requested_category"] = category
    if detail == "lite":
        lite = dashboard_lite(payload)
        lite["detail"] = "lite"
        return lite
    return payload


def dashboard_payload(
    category: str = "", detail: str = "full", mode: str = "auto", db=None
) -> dict:
    """标的看板统一入口：交易时段取 5 分钟定时快照，其余时段取 EOD 日K。

    ``mode``：
    - ``auto``（默认）：交易日 9:30 后且今日日K未落库时优先快照；快照
      不可得（首轮未生成 / 任务停摆）或非交易时段自动回退 EOD；
      今日日K落库后 EOD 即为含当日的确认值，比快照更权威，直接用 EOD。
    - ``eod``：强制日K口径（盘中也不含今日形成中的K线，供只要已确认
      历史信号的消费方使用）。
    - ``intraday``：强制快照口径；不可得时返回 ok=False（错误语义与
      ``intraday_dashboard_snapshot`` 一致）。

    响应统一带 ``data_mode``（``intraday_snapshot`` / ``eod``）与 ``as_of``，
    快照口径另带 ``snapshot_ts`` / ``post_close``。
    """
    db = db or get_db()
    category = category.strip()
    mode = (mode or "auto").strip().lower()
    if mode not in ("auto", "eod", "intraday"):
        return {"ok": False, "error": f"无效的 mode：{mode}（可选 auto / eod / intraday）"}

    if mode == "intraday":
        result = intraday_dashboard_snapshot(category=category, detail=detail, db=db)
        if result.get("ok"):
            result["data_mode"] = "intraday_snapshot"
        return result

    if mode == "auto":
        now = market_now()
        if (
            is_trading_day(now.date())
            and is_past_market_open(now)
            and not _eod_current(db, now)
        ):
            result = intraday_dashboard_snapshot(category=category, detail=detail, db=db)
            if result.get("ok"):
                result["data_mode"] = "intraday_snapshot"
                return result
            # 快照不可得时降级 EOD（data_mode 会如实标记口径）。

    return _eod_dashboard_response(category, detail)
