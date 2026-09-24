"""L1.5 数据门面层（gateway 包）：全系统唯一数据咽喉。

- as-of 强制：请求方声明决策时刻 t，门面物理上不返回 t 之后的信息——
  PIT 卡控从"消费方自觉"变成"门面强制"；
- 可交易性标注：逐日逐标的停牌/涨停/跌停推导；
- 面板读取 + 元数据 + 请求留痕；
- 受限句柄（BoundGateway，详设 §6.7）：as_of 与 data_version 在句柄创建
  时绑定，模块不可覆写、不可另行传参；越权调用被拒绝并记入 gateway_audit。

L1.5 永不回调上层；任何层不得绕过本包直连 L1 的行情读取。
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from gateway.audit import AuditBuffer
from gateway.metadata import MetadataService
from gateway.panel import Panel, build_panel
from gateway.tradability import compute_tradability


class GatewayViolation(PermissionError):
    """越权数据访问（试图绕过已绑定的 as_of / 访问未授权方法）。"""


class Gateway:
    def __init__(self, db, live_overlay=None) -> None:
        self._db = db
        self.audit = AuditBuffer(db)
        self.metadata = MetadataService(db)
        # 实盘运行器注入的盘中合成 bar 提供者：(symbols, as_of) -> {symbol: bar}
        self._live_overlay = live_overlay

    def data_version(self, adjust: str = "qfq") -> int:
        table = "market_data_qfq" if adjust == "qfq" else "market_data_raw"
        return self._db.get_data_version(table)

    def get_panel(
        self,
        *,
        symbols: list[str],
        start,
        end,
        fields: list[str],
        adjust: str = "qfq",
        as_of: datetime | date,
        mode: str = "historical",
        caller_layer: str,
        run_id: str | None = None,
    ) -> Panel:
        panel = build_panel(
            self._db,
            symbols=symbols,
            start=start,
            end=end,
            fields=fields,
            adjust=adjust,
            as_of=as_of,
            mode=mode,
            live_overlay=self._live_overlay if mode == "live" else None,
        )
        self.audit.record(
            caller_layer=caller_layer,
            run_id=run_id,
            method="get_panel",
            as_of=str(as_of),
            symbols_count=len(panel.symbols),
            date_start=str(start) if start else None,
            date_end=str(end) if end else None,
            fields=",".join(fields),
            adjust=adjust,
            mode=mode,
            data_version=self.data_version(adjust),
        )
        return panel

    def get_tradability(
        self,
        *,
        symbols: list[str],
        dates: list[date],
        as_of: datetime | date,
        caller_layer: str,
        run_id: str | None = None,
    ) -> pd.DataFrame:
        as_of_day = as_of.date() if isinstance(as_of, datetime) else as_of
        clipped = [d for d in dates if pd.Timestamp(d).date() <= as_of_day]
        frame = compute_tradability(self._db, symbols=symbols, dates=clipped)
        self.audit.record(
            caller_layer=caller_layer,
            run_id=run_id,
            method="get_tradability",
            as_of=str(as_of),
            symbols_count=len(symbols),
            date_start=str(min(clipped)) if clipped else None,
            date_end=str(max(clipped)) if clipped else None,
            data_version=self.data_version("raw"),
        )
        return frame

    def get_production_indicator(
        self,
        *,
        symbols: list[str],
        name: str,
        since,
        as_of: datetime | date,
        caller_layer: str,
        run_id: str | None = None,
    ) -> dict[str, pd.Series]:
        """生产指标直取（详设 §2.2：少数参数稳定的生产指标继续走既有缓存）。

        当前支持 name="trend_score"（trend_daily 缓存，default 参数集、
        最新 formula_version）；as_of 之后的数据一律不返回。
        """
        if name != "trend_score":
            raise ValueError(f"unsupported production indicator: {name}")
        as_of_day = as_of.date() if isinstance(as_of, datetime) else as_of
        rows = self._db.load_trend_daily_bulk(str(since), param_set="default")
        wanted = {str(s).strip().upper() for s in symbols}
        out: dict[str, pd.Series] = {}
        values: dict[str, list] = {}
        for r in rows:
            symbol = r["symbol"]
            if symbol not in wanted:
                continue
            day = pd.Timestamp(r["time"]).date()
            if day > as_of_day:
                continue
            values.setdefault(symbol, []).append((day, r["trend_score"]))
        for symbol, items in values.items():
            items.sort(key=lambda x: x[0])
            out[symbol] = pd.Series(
                [v for _, v in items], index=[d for d, _ in items], dtype=float
            )
        self.audit.record(
            caller_layer=caller_layer,
            run_id=run_id,
            method=f"get_production_indicator:{name}",
            as_of=str(as_of),
            symbols_count=len(symbols),
            date_start=str(since),
            data_version=self.data_version("qfq"),
        )
        return out

    def bind(
        self,
        *,
        as_of: datetime | date,
        caller_layer: str,
        run_id: str | None = None,
    ) -> BoundGateway:
        """创建受限句柄（as_of 在绑定后不可变）。"""
        return BoundGateway(
            self, as_of=as_of, caller_layer=caller_layer, run_id=run_id
        )

    def flush_audit(self) -> int:
        return self.audit.flush()


class BoundGateway:
    """运行注入的受限句柄：模块拿数据的唯一途径（详设 §5.2 DayContext）。

    as_of 与 data_version 在句柄创建时绑定——**模块不可覆写、不可另行传参**；
    任何带 as_of/data_version 的调用被视为越权：记入 gateway_audit 并拒绝。
    """

    # 信任边界（评审 DS-P2-11）：__gateway 名称改写只是摩擦；真 PIT 保障 =
    # 门面 as-of 强制 + 前缀稳定性探针 + 越权调用记 audit（见 _reject_violation）。
    __slots__ = ("_as_of", "_caller_layer", "_data_version", "__gateway", "_run_id")

    def __init__(self, gateway: Gateway, *, as_of, caller_layer: str, run_id: str | None) -> None:
        self.__gateway = gateway
        self._as_of = as_of
        self._caller_layer = caller_layer
        self._run_id = run_id
        self._data_version = gateway.data_version("qfq")

    @property
    def as_of(self):
        return self._as_of

    @property
    def data_version(self) -> int:
        """句柄绑定时锚定的行情内容版本（血缘）。"""
        return self._data_version

    @property
    def metadata(self) -> MetadataService:
        return self.__gateway.metadata

    def _reject_violation(self, method: str, kwargs: dict) -> None:
        self.__gateway.audit.record(
            caller_layer=self._caller_layer,
            run_id=self._run_id,
            method=f"violation:{method}",
            as_of=str(kwargs.get("as_of") or self._as_of),
            data_version=self._data_version,
        )
        raise GatewayViolation(
            f"{method}: as_of/data_version are bound to this handle and cannot be overridden"
        )

    def get_panel(
        self,
        *,
        symbols: list[str],
        start,
        end,
        fields: list[str],
        adjust: str = "qfq",
        mode: str = "historical",
        **kwargs,
    ) -> Panel:
        if "as_of" in kwargs or "data_version" in kwargs:
            self._reject_violation("get_panel", kwargs)
        return self.__gateway.get_panel(
            symbols=symbols,
            start=start,
            end=end,
            fields=fields,
            adjust=adjust,
            as_of=self._as_of,
            mode=mode,
            caller_layer=self._caller_layer,
            run_id=self._run_id,
        )

    def get_tradability(self, *, symbols: list[str], dates: list[date], **kwargs) -> pd.DataFrame:
        if "as_of" in kwargs or "data_version" in kwargs:
            self._reject_violation("get_tradability", kwargs)
        return self.__gateway.get_tradability(
            symbols=symbols,
            dates=dates,
            as_of=self._as_of,
            caller_layer=self._caller_layer,
            run_id=self._run_id,
        )

    def get_production_indicator(self, *, symbols: list[str], name: str, since, **kwargs):
        if "as_of" in kwargs or "data_version" in kwargs:
            self._reject_violation("get_production_indicator", kwargs)
        return self.__gateway.get_production_indicator(
            symbols=symbols, name=name, since=since, as_of=self._as_of,
            caller_layer=self._caller_layer, run_id=self._run_id,
        )

    def trading_days(self, start, end) -> list[date]:
        """截至 as_of 的交易日（历史口径不返回未来）。"""
        as_of_day = self._as_of.date() if isinstance(self._as_of, datetime) else self._as_of
        end_day = pd.Timestamp(end).date()
        end_day = min(end_day, as_of_day)
        return self.__gateway.metadata.trading_days(start, end_day)
