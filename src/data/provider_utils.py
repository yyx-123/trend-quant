from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from core.trend import safe_float  # noqa: F401  # 公开再导出（provider_* 共用入口）


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [str(c).strip() for c in out.columns]
    return out

def _first_existing(columns: Iterable[str], aliases: list[str]) -> str | None:
    lower_map = {c.lower(): c for c in columns}
    for alias in aliases:
        key = alias.lower()
        if key in lower_map:
            return lower_map[key]
    return None

def standardize_ohlcv(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume", "amount", "symbol"])

    data = _normalize_columns(df)
    cols = list(data.columns)

    time_col = _first_existing(cols, ["time", "datetime", "date", "日期", "时间"])
    open_col = _first_existing(cols, ["open", "开盘"])
    high_col = _first_existing(cols, ["high", "最高"])
    low_col = _first_existing(cols, ["low", "最低"])
    close_col = _first_existing(cols, ["close", "收盘", "最新价", "最新"])
    volume_col = _first_existing(cols, ["volume", "成交量"])
    amount_col = _first_existing(cols, ["amount", "成交额"])

    normalized = pd.DataFrame()
    if time_col:
        normalized["time"] = pd.to_datetime(data[time_col], errors="coerce")
        # 时区统一东八区（全项目约定：数据与处理逻辑一律 Asia/Shanghai 墙钟、
        # A 股交易时段）：vendor 返回的带时区时间（如 UTC ISO 串）在此统一
        # 转换并去 tz，与 compact 路径（provider_tickflow）及库内既有格式一致。
        if isinstance(normalized["time"].dtype, pd.DatetimeTZDtype):
            normalized["time"] = (
                normalized["time"].dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
            )
    else:
        raise ValueError(
            f"行情数据缺少时间列（time/datetime/date/日期/时间），symbol={symbol}，columns={cols}"
        )

    for out_col, in_col in [
        ("open", open_col),
        ("high", high_col),
        ("low", low_col),
        ("close", close_col),
        ("volume", volume_col),
        ("amount", amount_col),
    ]:
        if in_col:
            normalized[out_col] = pd.to_numeric(data[in_col], errors="coerce")
        else:
            normalized[out_col] = pd.NA

    normalized["symbol"] = symbol
    normalized = normalized.dropna(subset=["time"]).drop_duplicates(subset=["time"]).sort_values("time")
    normalized = normalized.reset_index(drop=True)
    return normalized
