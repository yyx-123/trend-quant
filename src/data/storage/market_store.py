from __future__ import annotations

import pandas as pd


class MarketStore:
    def __init__(self, db=None, price_mode: str = "qfq") -> None:
        self._db = db
        self.price_mode = price_mode

    def _get_db(self):
        # 每次现取（P2-12）：首次调用即永久缓存会把测试补丁窗口内的
        # get_db 替身固化进生产路径（main.py:59-63 自警过的模式）。
        if self._db is not None:
            return self._db
        from data.storage.db import get_db

        return get_db()

    def save_history(self, symbol: str, df: pd.DataFrame) -> str:
        self._get_db().save_market_data(symbol, df, price_mode=self.price_mode)
        return f"sqlite/{self.price_mode}/{symbol}"

    def replace_history(self, symbol: str, df: pd.DataFrame) -> int:
        """全量重写一个标的（同事务删除+插入），返回写入行数。"""
        return self._get_db().replace_market_data(symbol, df, price_mode=self.price_mode)

    def load_history(self, symbol: str) -> pd.DataFrame:
        return self._get_db().load_market_data(symbol, price_mode=self.price_mode)

    def list_stored_symbols(self) -> list[str]:
        return self._get_db().list_market_symbols(price_mode=self.price_mode)
