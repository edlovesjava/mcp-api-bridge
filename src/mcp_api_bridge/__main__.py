"""Entry point.

`mcp-api-bridge` runs the MCP server; `mcp-api-bridge import-openapi` scaffolds
a config from an OpenAPI document.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .openapi import (
    Derivation,
    OpenAPIError,
    derive_api,
    list_operations,
    load_spec,
    render_config,
)
from .server import build_server


def _add_serve_args(parser: argparse.ArgumentParser) -> None:
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-api-bridge",
        description="MCP server fronting in-house catalog search APIs.",
    )
    _add_serve_args(parser)
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Run the MCP server (the default).")
    _add_serve_args(serve)

    imp = sub.add_parser(
        "import-openapi",
        help="Scaffold catalog config from an OpenAPI 3.x document.",
        description=(
            "Derives a config block from a spec. The wire contract is taken from the "
            "spec; roles the spec cannot express are guessed by name and reported, so "
            "review the generated file before serving it."
        ),
    )
    imp.add_argument("spec", help="Path to an OpenAPI 3.x JSON or YAML document.")
    imp.add_argument(
        "--list",
        action="store_true",
        help="List the spec's operations and exit, without generating anything.",
    )
    imp.add_argument(
        "--operation",
        action="append",
        default=[],
        metavar="NAME=operationId",
        help="A catalog to generate, as `name=operationId`. Repeatable.",
    )
    imp.add_argument(
        "--item-operation",
        action="append",
        default=[],
        metavar="NAME=operationId",
        help="Optional get-item operation for a catalog, as `name=operationId`.",
    )
    imp.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="NAME=p1,p2",
        help="Allowlist the filters exposed for a catalog. Strongly recommended on "
        "wide endpoints — every filter enters the query-understanding prompt.",
    )
    imp.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="NAME=p1,p2",
        help="Drop specific parameters from a catalog's filters.",
    )
    imp.add_argument(
        "--max-attributes",
        type=int,
        default=10,
        help="Cap on unmapped response leaves carried through as attributes (default: 10).",
    )
    imp.add_argument("-o", "--out", help="Write to this file instead of stdout.")
    return parser


def _pairs(values: list[str], flag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise SystemExit(f"mcp-api-bridge: {flag} expects NAME=VALUE, got {value!r}")
        name, _, rest = value.partition("=")
        out[name.strip()] = rest.strip()
    return out


def run_import(args: argparse.Namespace) -> int:
    try:
        spec = load_spec(args.spec)
    except OpenAPIError as exc:
        print(f"mcp-api-bridge: {exc}", file=sys.stderr)
        return 2

    if args.list or not args.operation:
        operations = list_operations(spec)
        print(f"{len(operations)} operation(s) in {args.spec}:\n", file=sys.stderr)
        for op in operations:
            print(
                f"  {op['operation_id'] or '(no operationId)':28} "
                f"{op['method']:5} {op['path']:42} {op['parameters']:3} params",
                file=sys.stderr,
            )
            if op["summary"]:
                print(f"  {'':28} {op['summary'][:90]}", file=sys.stderr)
        if not args.operation:
            print(
                "\nPick one or more with --operation NAME=operationId "
                "(and --include NAME=p1,p2 to curate filters).",
                file=sys.stderr,
            )
        return 0

    operations = _pairs(args.operation, "--operation")
    item_ops = _pairs(args.item_operation, "--item-operation")
    includes = {
        k: [p.strip() for p in v.split(",") if p.strip()]
        for k, v in _pairs(args.include, "--include").items()
    }
    excludes = {
        k: [p.strip() for p in v.split(",") if p.strip()]
        for k, v in _pairs(args.exclude, "--exclude").items()
    }

    derivations: list[Derivation] = []
    for name, operation_id in operations.items():
        try:
            derivations.append(
                derive_api(
                    spec,
                    name=name,
                    search_operation=operation_id,
                    item_operation=item_ops.get(name),
                    include=includes.get(name),
                    exclude=excludes.get(name),
                    max_attributes=args.max_attributes,
                )
            )
        except OpenAPIError as exc:
            print(f"mcp-api-bridge: {name}: {exc}", file=sys.stderr)
            return 2

    rendered = render_config(derivations)
    if args.out:
        Path(args.out).write_text(rendered)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(rendered)

    # Provenance goes to stderr so it survives redirection of the config itself.
    for d in derivations:
        print(f"\n{d.config['name']}:", file=sys.stderr)
        filters = d.config["search"]["filters"]
        print(
            f"  {len(filters)} filter(s) imported"
            + (f", {len(d.skipped)} skipped" if d.skipped else ""),
            file=sys.stderr,
        )
        for note in d.notes:
            print(f"  note: {note}", file=sys.stderr)
        for gap in d.gaps:
            print(f"  TODO: {gap}", file=sys.stderr)
    return 0


def run_serve(args: argparse.Namespace) -> int:
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


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "import-openapi":
        return run_import(args)
    return run_serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
