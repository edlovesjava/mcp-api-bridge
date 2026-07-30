"""MCP bridge for in-house catalog search APIs, with Bedrock query understanding."""

from .config import BridgeConfig, ConfigError, load_config
from .models import CatalogItem, QueryPlan, SearchResult, UnderstoodQuery
from .server import build_server

__all__ = [
    "BridgeConfig",
    "CatalogItem",
    "ConfigError",
    "QueryPlan",
    "SearchResult",
    "UnderstoodQuery",
    "build_server",
    "load_config",
]

__version__ = "0.1.0"
