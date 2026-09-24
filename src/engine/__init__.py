"""L2 交易引擎层（engine 包，纯执行）：一个引擎，A 股规则完备。

只懂"给我订单，我按 A 股规则撮合并记账"；不懂策略、不懂选股、不懂研究。
"""

from engine.engine import Engine, EngineError, new_run_id
from engine.matcher import MatchResult, TradabilityCard
from engine.models import (
    Account,
    ExitOrderIntent,
    Fill,
    OrderIntent,
    Position,
    StopState,
    Unfilled,
)
from engine.profiles import CN_STOCK, MarketProfile, get_profile
from engine.store import ENGINE_VERSION, EngineStore

__all__ = [
    "CN_STOCK",
    "ENGINE_VERSION",
    "Account",
    "Engine",
    "EngineError",
    "EngineStore",
    "ExitOrderIntent",
    "Fill",
    "MarketProfile",
    "MatchResult",
    "OrderIntent",
    "Position",
    "StopState",
    "TradabilityCard",
    "Unfilled",
    "get_profile",
    "new_run_id",
]
