"""Command line entry point. MCP support loads only for ``mcp serve``."""

import argparse
import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> None:
    """Run the ``id42-work`` command and exit with its status."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    raise SystemExit(args.handler(args))


def build_parser() -> argparse.ArgumentParser:
    """Parser for ``id42-work``. Subcommands that need an extra import it themselves."""
    parser = argparse.ArgumentParser(prog="id42-work")
    commands = parser.add_subparsers(dest="command", required=True)
    mcp = commands.add_parser("mcp", help="Model Context Protocol")
    mcp_commands = mcp.add_subparsers(dest="mcp_command", required=True)
    serve = mcp_commands.add_parser(
        "serve",
        help="Expose governed execution to an MCP client (stdio by default)",
    )
    serve.add_argument(
        "--factory",
        required=True,
        help="module:callable that returns a WorkMcpServer, for example myapp.governed:server",
    )
    serve.add_argument(
        "--http",
        action="store_true",
        help="Serve streamable HTTP on 127.0.0.1 instead of stdio",
    )
    serve.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port for --http (default 8766). Refused without --http",
    )
    serve.set_defaults(handler=_serve)
    return parser


_DEFAULT_HTTP_PORT = 8766
_MIN_PORT = 1
_MAX_PORT = 65535


def _serve(args: argparse.Namespace) -> int:
    if args.port is not None and not args.http:
        print("--port requires --http", file=sys.stderr)
        return 2
    port = _DEFAULT_HTTP_PORT if args.port is None else args.port
    if args.http and not _MIN_PORT <= port <= _MAX_PORT:
        print(f"port must be an integer from {_MIN_PORT} to {_MAX_PORT}", file=sys.stderr)
        return 2
    try:
        from id42_work.mcp._dependency import require_mcp  # noqa: PLC0415

        require_mcp()
        from id42_work.mcp.server import load_factory, serve  # noqa: PLC0415
    except ImportError as exc:
        print(exc, file=sys.stderr)
        return 1
    try:
        server = load_factory(args.factory)
    except (ImportError, AttributeError, ValueError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    serve(server, http=args.http, port=port)
    return 0
