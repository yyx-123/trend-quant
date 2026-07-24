"""手工交易记录 — 用户凭据校验、交易录入 / 清仓 / 列表聚合。

鉴权为极简无状态方案：每个请求携带 username + password，逐次查 users
表校验（密码明文存储，内部小工具口径）。admin 用户可查看 / 操作所有
用户的交易记录。

持仓与止损指标完全复用 ``services/manual_trade.compute_manual_trade``
（未清仓：intraday 实时口径；已清仓：``end_date`` 截断口径），本模块只做
持久化、权限与列表聚合，不重复实现任何计算逻辑。
"""

from __future__ import annotations

import logging

import pandas as pd

from core.display import load_instrument_name_map
from core.symbols import normalize_symbol
from core.trend import safe_float
from data.storage.db import get_db
from services import stop_loss as sl
from services.manual_trade import compute_manual_trade

logger = logging.getLogger(__name__)

__all__ = [
    "TradeAuthError",
    "TradePermissionError",
    "TradeRecordError",
    "authenticate",
    "list_users",
    "create_trade",
    "close_trade",
    "list_trades",
]


class TradeAuthError(ValueError):
    """用户名或密码错误。"""


class TradePermissionError(ValueError):
    """已登录但无权访问目标资源。"""


class TradeRecordError(ValueError):
    """交易记录业务错误（记录不存在 / 状态非法 / 输入不合法）。"""


# ------------------------------------------------------------------
# 凭据与用户
# ------------------------------------------------------------------
def authenticate(username: str, password: str, db=None) -> dict:
    """校验用户名 + 密码，返回 ``{id, username, is_admin}``；失败抛 TradeAuthError。"""
    db = db or get_db()
    user = db.get_user_by_username(str(username))
    if user is None or user["password"] != str(password):
        raise TradeAuthError("用户名或密码错误")
    return {"id": user["id"], "username": user["username"], "is_admin": user["is_admin"]}


def list_users(username: str, password: str, db=None) -> list[dict]:
    """用户列表（仅 admin，供前端切换查看）。"""
    user = authenticate(username, password, db=db)
    if not user["is_admin"]:
        raise TradePermissionError("仅管理员可查看用户列表")
    db = db or get_db()
    return [
        {"id": u["id"], "username": u["username"], "is_admin": u["is_admin"]}
        for u in db.list_users()
    ]


def _resolve_target_user(user: dict, user_id: int | None, db) -> dict:
    """确定列表接口的目标用户：默认自己；admin 可指定任意用户。"""
    if user_id is None or int(user_id) == user["id"]:
        return user
    if not user["is_admin"]:
        raise TradePermissionError("只能查看自己的交易记录")
    target = db.get_user(int(user_id))
    if target is None:
        raise TradeRecordError(f"用户不存在: {user_id}")
    return {"id": target["id"], "username": target["username"], "is_admin": target["is_admin"]}


# ------------------------------------------------------------------
# 交易录入 / 清仓
# ------------------------------------------------------------------
def create_trade(
    username: str,
    password: str,
    *,
    symbol: str,
    buy_date: str,
    buy_price: float,
    shares: float,
    db=None,
) -> dict:
    """录入一笔交易。买入价复用 ``compute_stop_loss`` 的当日区间校验。"""
    user = authenticate(username, password, db=db)
    db = db or get_db()
    shares = float(shares)
    if shares <= 0:
        raise TradeRecordError("买入份数必须大于 0")
    # 复用止损计算的校验链：标的有效 / 有数据 / 买入价落在当日 [low, high] 区间
    sl.compute_stop_loss(symbol, str(buy_date), float(buy_price), db=db, intraday=False)
    return db.create_manual_trade(
        user["id"], normalize_symbol(symbol), str(buy_date), float(buy_price), shares
    )


def _validate_price_in_day_range(df: pd.DataFrame, date_str: str, price: float, label: str) -> None:
    """价格必须落在当日K线 [low, high] 区间内；非交易日（无当根K线）跳过，
    与 ``compute_stop_loss`` 的买入价校验同口径。"""
    day = df[df["time"].dt.normalize() == pd.Timestamp(date_str)]
    if day.empty:
        return
    day_low = safe_float(pd.to_numeric(day["low"], errors="coerce").iloc[0], 0.0)
    day_high = safe_float(pd.to_numeric(day["high"], errors="coerce").iloc[0], 0.0)
    eps = max(1e-4, abs(day_high) * 1e-6)
    if day_low > 0 and day_high > 0 and not (day_low - eps <= price <= day_high + eps):
        raise TradeRecordError(
            f"{label}价格 {price} 超出 {date_str} 当日价格区间 "
            f"[{round(day_low, 4)}, {round(day_high, 4)}]"
        )


def close_trade(
    username: str,
    password: str,
    *,
    trade_id: int,
    sell_date: str,
    sell_price: float,
    db=None,
) -> dict:
    """清仓一笔交易（全仓清出）。本人或 admin 可操作。"""
    user = authenticate(username, password, db=db)
    db = db or get_db()
    trade = db.get_manual_trade(int(trade_id))
    if trade is None:
        raise TradeRecordError("交易记录不存在")
    if trade["user_id"] != user["id"] and not user["is_admin"]:
        raise TradePermissionError("只能操作自己的交易记录")
    if trade["status"] != "open":
        raise TradeRecordError("该交易已清仓")
    sell_price = float(sell_price)
    if sell_price <= 0:
        raise TradeRecordError("清仓价格必须大于 0")
    if pd.Timestamp(sell_date) < pd.Timestamp(trade["buy_date"]):
        raise TradeRecordError(f"清仓日期 {sell_date} 早于买入日期 {trade['buy_date']}")

    df = db.load_market_data(trade["symbol"])
    if df.empty:
        raise TradeRecordError(f"未找到 {trade['symbol']} 的数据")
    df = df.copy()
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    _validate_price_in_day_range(df, str(sell_date), sell_price, "清仓")

    closed = db.close_manual_trade(trade["id"], str(sell_date), sell_price)
    if closed is None:  # 并发下已被清仓
        raise TradeRecordError("该交易已清仓")
    return closed


# ------------------------------------------------------------------
# 列表聚合
# ------------------------------------------------------------------
def _base_item(row: dict, name_map: dict[str, str]) -> dict:
    return {
        "id": row["id"],
        "symbol": row["symbol"],
        "name": name_map.get(row["symbol"], ""),
        "status": row["status"],
        "buy_date": row["buy_date"],
        "buy_price": row["buy_price"],
        "shares": row["shares"],
        "sell_date": row["sell_date"],
        "sell_price": row["sell_price"],
    }


def _open_item(row: dict, result: dict, name_map: dict[str, str]) -> dict:
    shares = float(row["shares"])
    latest = float(result["stops"]["latest_price"])
    buy_price = float(row["buy_price"])
    item = _base_item(row, name_map)
    item.update(
        {
            "latest_price": latest,
            "position_value": round(shares * latest, 2),
            "pnl_amount": round((latest - buy_price) * shares, 2),
            "is_intraday": bool(result.get("is_intraday")),
            "intraday_ts": result.get("intraday_ts"),
            "stops": result["stops"],
            "holding": result["holding"],
        }
    )
    return item


def _closed_item(row: dict, result: dict, name_map: dict[str, str]) -> dict:
    shares = float(row["shares"])
    buy_price = float(row["buy_price"])
    sell_price = float(row["sell_price"])
    item = _base_item(row, name_map)
    item.update(
        {
            "realized_pnl": round((sell_price - buy_price) * shares, 2),
            "realized_pnl_pct": round((sell_price / buy_price - 1) * 100, 2)
            if buy_price > 0
            else 0.0,
            "stops": result["stops"],
            "holding": result["holding"],
        }
    )
    return item


def list_trades(
    username: str,
    password: str,
    user_id: int | None = None,
    db=None,
    intraday: bool = True,
) -> dict:
    """某用户的全部交易记录 + 实时/截止口径指标。

    排序：未清仓按持仓金额（份数 × 最新价）降序在前；已清仓排最后，
    按清仓日倒序。单笔计算失败不拖垮整个列表，以 ``error`` 字段返回。
    """
    user = authenticate(username, password, db=db)
    db = db or get_db()
    target = _resolve_target_user(user, user_id, db)
    rows = db.list_manual_trades(target["id"])
    name_map = load_instrument_name_map()  # DB 不可用时返回 {}（单测环境）

    # 同 symbol 实时报价请求级去重：一次列表内每个 symbol 只拉一次报价
    prefetched: dict[str, dict | None] = {}
    if intraday:
        for row in rows:
            if row["status"] == "open" and row["symbol"] not in prefetched:
                df = db.load_market_data(row["symbol"])
                prefetched[row["symbol"]] = (
                    None if df.empty else sl.fetch_intraday_bar(row["symbol"], df)
                )

    open_items: list[dict] = []
    closed_items: list[dict] = []
    for row in rows:
        item = _base_item(row, name_map)
        try:
            if row["status"] == "open":
                result = compute_manual_trade(
                    row["symbol"],
                    row["buy_date"],
                    row["buy_price"],
                    db=db,
                    intraday=intraday,
                    intraday_bar=(
                        prefetched.get(row["symbol"]) if intraday else sl.UNSET_INTRADAY_BAR
                    ),
                )
                open_items.append(_open_item(row, result, name_map))
            else:
                result = compute_manual_trade(
                    row["symbol"],
                    row["buy_date"],
                    row["buy_price"],
                    db=db,
                    intraday=False,
                    end_date=row["sell_date"],
                )
                closed_items.append(_closed_item(row, result, name_map))
        except Exception as exc:
            logger.warning("trade %s metric compute failed: %s", row["id"], exc)
            item["error"] = str(exc)
            item["position_value"] = 0.0
            (open_items if row["status"] == "open" else closed_items).append(item)

    open_items.sort(key=lambda t: t.get("position_value", 0.0), reverse=True)
    closed_items.sort(key=lambda t: (t.get("sell_date") or "", t["id"]), reverse=True)

    return {
        "user": user,
        "viewing": target,
        "trades": open_items + closed_items,
    }
