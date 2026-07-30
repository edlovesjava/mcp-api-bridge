"""Entry point: `mcp-api-bridge` speaks MCP over stdio."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .config import ConfigError, load_config
from .server import build_server


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="mcp-api-bridge",
        description="MCP server fronting in-house catalog search APIs.",
    )
    parser.add_argument(
        "--config",
        help="Path to the catalog config. Defaults to $MCP_API_BRIDGE_CONFIG "
        "or ./config/catalog.yaml.",
    )
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "sse", "streamable-http"],
        help="MCP transport (default: stdio).",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("MCP_API_BRIDGE_LOG_LEVEL", "INFO"),
        help="Logging level (default: INFO).",
    )
    args = parser.parse_args()

    # stdout is the MCP transport on stdio — logs must not land there.
    logging.basicConfig(
        level=args.log_level.upper(),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"mcp-api-bridge: {exc}", file=sys.stderr)
        return 2

    logging.getLogger(__name__).info(
        "serving %d catalog(s): %s",
        len(config.apis),
        ", ".join(a.name for a in config.apis),
    )
    build_server(config).run(transport=args.transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
