"""面板读取（as-of 强制，详设 §3.1）。

- `as_of` 必填，无默认值——调用方必须显式声明"我站在哪个时刻做决策"；
- `historical` 模式：返回数据严格 ≤ as_of（日 K 粒度按交易日比较；盘中
  合成 bar、provisional 标记一律不出现）；
- `live` 模式（仅供实盘运行器）：允许含截至 as_of 的盘中合成 bar，
  面板上带 `provisional` 标记列；
- 每次调用留痕（audit.py）。

返回的 Panel 是按 (date, symbol) 对齐的内存面板（numpy），字段按数据集
声明（K 线数据集 {open, high, low, close, volume, amount, pre_close}）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

BAR_FIELDS: tuple[str, ...] = ("open", "high", "low", "close", "volume", "amount", "pre_close")


class PanelRequestError(ValueError):
    pass


@dataclass(frozen=True)
class Panel:
    """(T, N) 对齐的只读面板。

    dates: 升序交易日（date）；symbols: 列顺序；data: {field: (T,N) float64，
    缺数据为 NaN}；provisional: (T,N) bool（live 模式的盘中合成行标记）。
    """

    dates: tuple[date, ...]
    symbols: tuple[str, ...]
    data: dict[str, np.ndarray]
    provisional: np.ndarray

    @property
    def shape(self) -> tuple[int, int]:
        return (len(self.dates), len(self.symbols))

    def field(self, name: str) -> np.ndarray:
        return self.data[name]

    def date_pos(self, day: date) -> int | None:
        try:
            return self._date_index[day]
        except KeyError:
            return None

    @property
    def _date_index(self) -> dict[date, int]:
        idx = getattr(self, "_di", None)
        if idx is None:
            idx = {d: i for i, d in enumerate(self.dates)}
            object.__setattr__(self, "_di", idx)
        return idx

    def symbol_pos(self, symbol: str) -> int | None:
        try:
            return self._symbol_index[symbol]
        except KeyError:
            return None

    @property
    def _symbol_index(self) -> dict[str, int]:
        idx = getattr(self, "_si", None)
        if idx is None:
            idx = {s: i for i, s in enumerate(self.symbols)}
            object.__setattr__(self, "_si", idx)
        return idx

    def series_upto(self, field_name: str, symbol: str, day: date) -> np.ndarray:
        """某标的截至 day（含）的字段序列（前段切片视图；PIT 消费的常用形）。"""
        col = self._symbol_index.get(symbol)
        row = self.date_pos(day)
        if col is None or row is None:
            return np.empty(0)
        return self.data[field_name][: row + 1, col]

    def bar_at(self, symbol: str, day: date) -> dict | None:
        """某标的某日的 OHLC 字典；无 bar（NaN）时返回 None。

        只含本面板加载过的字段（fields 之外的列不补 NaN 占位）。
        """
        col = self._symbol_index.get(symbol)
        row = self.date_pos(day)
        if col is None or row is None:
            return None
        close_arr = self.data.get("close")
        if close_arr is None:
            return None
        close = float(close_arr[row, col])
        if not np.isfinite(close):
            return None
        out = {"close": close, "provisional": bool(self.provisional[row, col])}
        for f in ("open", "high", "low", "volume", "amount", "pre_close"):
            arr = self.data.get(f)
            if arr is not None and np.isfinite(arr[row, col]):
                out[f] = float(arr[row, col])
        return out


def _as_of_day(as_of: datetime | date) -> date:
    if isinstance(as_of, datetime):
        return as_of.date()
    if isinstance(as_of, date):
        return as_of
    raise PanelRequestError(f"as_of must be date/datetime, got {type(as_of).__name__}")


def build_panel(
    db,
    *,
    symbols: Iterable[str],
    start: date | str | None,
    end: date | str | None,
    fields: Iterable[str],
    adjust: str = "qfq",
    as_of: datetime | date | None = None,
    mode: str = "historical",
    live_overlay: Any = None,
) -> Panel:
    """从 L1 行情表构建对齐面板（as-of 强制在此一处实现）。

    live_overlay: 可选 callable(symbols, as_of) -> {symbol: bar_dict}，
    由实盘运行器注入（intraday_service 合成）；historical 模式忽略之。
    """
    if as_of is None:
        raise PanelRequestError("as_of is required (declare your decision time)")
    as_of_day = _as_of_day(as_of)
    if mode not in ("historical", "live"):
        raise PanelRequestError(f"unknown panel mode: {mode}")
    if adjust not in ("qfq", "raw"):
        raise PanelRequestError(f"unknown adjust: {adjust}")

    symbols = [str(s or "").strip().upper() for s in symbols if str(s or "").strip()]
    symbols = list(dict.fromkeys(symbols))
    if not symbols:
        raise PanelRequestError("symbols must be non-empty")

    fields = [str(f) for f in fields]
    unknown = set(fields) - set(BAR_FIELDS)
    if unknown:
        raise PanelRequestError(f"unknown bar fields: {sorted(unknown)}")
    if not fields:
        raise PanelRequestError("fields must be non-empty")

    start_day = pd.Timestamp(start).date() if start is not None else None
    end_day = pd.Timestamp(end).date() if end is not None else None
    # as-of 强制：end 截到 as_of（历史模式物理上不返回 as_of 之后的行）
    if end_day is None or end_day > as_of_day:
        end_day = as_of_day

    frames = db.load_market_data_window_many(
        symbols, start_day, end_day, price_mode=adjust, period="1d"
    )

    # 交易日轴：窗口内所有标的 bar 日期的并集（天然跳过节假日/周末）。
    all_days: set[date] = set()
    per_symbol: dict[str, pd.DataFrame] = {}
    for symbol, df in frames.items():
        if df is None or df.empty:
            continue
        days = pd.to_datetime(df["time"], errors="coerce").dt.date
        df = df.assign(_day=days)
        df = df[df["_day"].notna()]
        if start_day is not None:
            df = df[df["_day"] >= start_day]
        df = df[df["_day"] <= end_day]
        if df.empty:
            continue
        per_symbol[symbol] = df
        all_days.update(df["_day"].tolist())

    # live 模式：并入截至 as_of 的盘中合成 bar（provisional 标记）
    provisional_rows: dict[str, dict] = {}
    if mode == "live" and live_overlay is not None:
        overlays = live_overlay(symbols, as_of) or {}
        for symbol, bar in overlays.items():
            if not bar:
                continue
            day = pd.Timestamp(bar.get("time") or bar.get("date") or as_of_day).date()
            if day != as_of_day:
                continue  # 合成 bar 只代表 as_of 当日
            if start_day is not None and day < start_day:
                continue
            provisional_rows[symbol] = {
                "_day": day,
                "open": bar.get("open"),
                "high": bar.get("high"),
                "low": bar.get("low"),
                "close": bar.get("close"),
                "volume": bar.get("volume"),
                "amount": bar.get("amount"),
            }
            all_days.add(day)

    dates = tuple(sorted(all_days))
    n_days, n_syms = len(dates), len(symbols)
    data: dict[str, np.ndarray] = {
        f: np.full((n_days, n_syms), np.nan, dtype=np.float64) for f in fields
    }
    provisional = np.zeros((n_days, n_syms), dtype=bool)
    date_index = {d: i for i, d in enumerate(dates)}

    for col, symbol in enumerate(symbols):
        df = per_symbol.get(symbol)
        if df is None:
            continue
        rows = df["_day"].map(date_index).to_numpy()
        for f in fields:
            if f == "pre_close":
                continue
            if f in df.columns:
                data[f][rows, col] = pd.to_numeric(df[f], errors="coerce").to_numpy(dtype=float)
        # pre_close 由面板自身 close 推导（前一交易日收盘；首行 NaN）
        if "pre_close" in fields and "close" in df.columns:
            closes = pd.to_numeric(df["close"], errors="coerce").to_numpy(dtype=float)
            pc = np.empty_like(closes)
            pc[0] = np.nan
            pc[1:] = closes[:-1]
            data["pre_close"][rows, col] = pc

    for symbol, bar in provisional_rows.items():
        col = symbols.index(symbol) if symbol in symbols else None
        if col is None:
            continue
        row = date_index[bar["_day"]]
        for f in fields:
            if f == "pre_close":
                continue
            value = bar.get(f)
            if value is not None:
                try:
                    data[f][row, col] = float(value)
                except (TypeError, ValueError):
                    pass
        provisional[row, col] = True

    return Panel(dates=dates, symbols=tuple(symbols), data=data, provisional=provisional)
