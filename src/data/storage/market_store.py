from __future__ import annotations

import pandas as pd


class MarketStore:
    def __init__(self, db=None, price_mode: str = "qfq", period: str = "1d") -> None:
        self._db = db
        self.price_mode = price_mode
        # 周期（1d/1w/1M，别名经 core.bars.normalize_period 归一）：一个
        # store 实例绑定一张周期表，日/周/月各持一个实例，调用方不必每次
        # 重复传周期。
        self.period = period

    def _get_db(self):
        # 每次现取（P2-12）：首次调用即永久缓存会把测试补丁窗口内的
        # get_db 替身固化进生产路径（main.py:59-63 自警过的模式）。
        if self._db is not None:
            return self._db
        from data.storage.db import get_db

        return get_db()

    def save_history(self, symbol: str, df: pd.DataFrame) -> str:
        self._get_db().save_market_data(
            symbol, df, price_mode=self.price_mode, period=self.period
        )
        return f"sqlite/{self.price_mode}/{self.period}/{symbol}"

    def save_history_many(self, items: list[tuple[str, pd.DataFrame]]) -> dict[str, int]:
        """批量写多个标的（单连接单事务）：[(symbol, df)] → {symbol: 行数}。

        日更要为 800+ 标的各写 raw/qfq，逐条新建连接的开销是主要成本，批量
        入口是这条路径的快路径；调用方可据此拿到实际落库行数（空表或整段
        被防御性丢弃的标的不出现在返回 dict 中）。
        """
        return self._get_db().save_market_data_many(
            items, price_mode=self.price_mode, period=self.period
        )

    def replace_history(self, symbol: str, df: pd.DataFrame) -> int:
        """全量重写一个标的（同事务删除+插入），返回写入行数。"""
        return self._get_db().replace_market_data(
            symbol, df, price_mode=self.price_mode, period=self.period
        )

    def load_history(self, symbol: str) -> pd.DataFrame:
        return self._get_db().load_market_data(
            symbol, price_mode=self.price_mode, period=self.period
        )

    def list_stored_symbols(self) -> list[str]:
        return self._get_db().list_market_symbols(
            price_mode=self.price_mode, period=self.period
        )
