"""L1.5 数据门面层（gateway 包）。"""

from gateway.panel import Panel, PanelRequestError, build_panel
from gateway.service import BoundGateway, Gateway, GatewayViolation
from gateway.tradability import board_limit_pct, compute_tradability

__all__ = [
    "BoundGateway",
    "Gateway",
    "GatewayViolation",
    "Panel",
    "PanelRequestError",
    "board_limit_pct",
    "build_panel",
    "compute_tradability",
]
