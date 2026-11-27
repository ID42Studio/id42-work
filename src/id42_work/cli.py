"""Command line entry point.

``mcp serve``, ``approvals``, ``reconcile`` and ``web serve`` load their extras
themselves. Importing this module does not import those extras.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from id42_work.approvals.commands import format_decision
from id42_work.approvals.console import ApprovalResumeError
from id42_work.exceptions import WorkError
from id42_work.reconciliation.console import ReconciliationResumeError


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
    approvals = commands.add_parser(
        "approvals",
        help="List and decide approvals as an operator. Not an MCP tool.",
        description="List and decide approvals as an operator. Not an MCP tool.",
    )
    sub = approvals.add_subparsers(dest="approvals_command", required=True)
    factory = argparse.ArgumentParser(add_help=False)
    factory.add_argument(
        "--factory",
        required=True,
        help="module:callable that returns a WorkMcpServer, the same contract as mcp serve",
    )
    listing = sub.add_parser(
        "list", parents=[factory], help="List approvals that are still waiting"
    )
    listing.set_defaults(handler=_approvals)
    show = sub.add_parser(
        "show", parents=[factory], help="Show one approval, including its preview"
    )
    show.add_argument("approval_id", help="Approval UUID")
    show.set_defaults(handler=_approvals)
    approve = sub.add_parser(
        "approve",
        parents=[factory],
        help="Grant an approval. Resume only when --resume is set",
    )
    approve.add_argument("approval_id", help="Approval UUID")
    approve.add_argument(
        "--actor",
        required=True,
        help="Operator actor id. Sealed with only the approval's reviewer grant",
    )
    approve.add_argument("--reason", default=None, help="Reason recorded on the decision")
    approve.add_argument(
        "--resume",
        action="store_true",
        help="After the grant, resume as the factory's host actor, not as the operator",
    )
    approve.set_defaults(handler=_approvals)
    reject = sub.add_parser(
        "reject",
        parents=[factory],
        help="Reject an approval. The execution ends REJECTED",
    )
    reject.add_argument("approval_id", help="Approval UUID")
    reject.add_argument(
        "--actor",
        required=True,
        help="Operator actor id. Sealed with only the approval's reviewer grant",
    )
    reject.add_argument("--reason", required=True, help="Why the approval was rejected")
    reject.set_defaults(handler=_approvals)
    _add_reconcile(commands)
    _add_web(commands)
    return parser


def _add_reconcile(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    reconcile = commands.add_parser(
        "reconcile",
        help="Reconcile uncertain capability calls as an operator. Not an MCP tool.",
        description="Reconcile uncertain capability calls as an operator. Not an MCP tool.",
    )
    sub = reconcile.add_subparsers(dest="reconcile_command", required=True)
    factory = argparse.ArgumentParser(add_help=False)
    factory.add_argument(
        "--factory",
        required=True,
        help="module:callable that returns a WorkMcpServer, the same contract as mcp serve",
    )
    listing = sub.add_parser(
        "list", parents=[factory], help="List capability calls that are still UNCERTAIN"
    )
    listing.set_defaults(handler=_reconcile)
    target = argparse.ArgumentParser(add_help=False)
    target.add_argument("execution_id", help="Execution UUID")
    target.add_argument("action_id", help="Journal action id")
    show = sub.add_parser(
        "show", parents=[factory, target], help="Show one journaled capability call"
    )
    show.set_defaults(handler=_reconcile)
    actor = argparse.ArgumentParser(add_help=False)
    actor.add_argument(
        "--actor",
        required=True,
        help="Operator actor id. Sealed with only reconcile:<work_id>",
    )
    actor.add_argument("--reason", required=True, help="Why this decision was made")
    resume = argparse.ArgumentParser(add_help=False)
    resume.add_argument(
        "--resume",
        action="store_true",
        help="After the decision, resume as the factory's host actor, not as the operator",
    )
    applied = sub.add_parser(
        "applied",
        parents=[factory, target, actor, resume],
        help="The effect happened. Record the outcome and allow replay",
    )
    applied.add_argument("--result", required=True, help="JSON outcome confirmed for the call")
    applied.set_defaults(handler=_reconcile)
    not_applied = sub.add_parser(
        "not-applied",
        parents=[factory, target, actor, resume],
        help="The effect did not happen. The same operation id may be retried",
    )
    not_applied.set_defaults(handler=_reconcile)
    abandon = sub.add_parser(
        "abandon",
        parents=[factory, target, actor],
        help="Close the uncertainty. The execution stays failed",
    )
    abandon.set_defaults(handler=_reconcile)


_DEFAULT_HTTP_PORT = 8766
_MIN_PORT = 1
_MAX_PORT = 65535
_WEB_HOST = "127.0.0.1"
_WEB_PORT = 8767


def _add_web(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    web = commands.add_parser(
        "web",
        help="Operator Web UI for approvals and reconciliation. Not an MCP tool.",
        description=(
            "Serve a loopback UI that decides through the approval and "
            "reconciliation consoles. Not an MCP tool."
        ),
    )
    sub = web.add_subparsers(dest="web_command", required=True)
    serve = sub.add_parser(
        "serve",
        help="Serve the operator UI on a loopback address (default 127.0.0.1)",
    )
    serve.add_argument(
        "--factory",
        required=True,
        help="module:callable that returns a WorkMcpServer, the same contract as mcp serve",
    )
    serve.add_argument(
        "--host",
        default=_WEB_HOST,
        help="Loopback IP address to bind (default 127.0.0.1). Other addresses are refused",
    )
    serve.add_argument(
        "--port",
        type=int,
        default=_WEB_PORT,
        help=f"TCP port (default {_WEB_PORT})",
    )
    serve.add_argument(
        "--token-file",
        default=None,
        help="Mode 0600 file containing the operator token. Overrides the token environment",
    )
    serve.add_argument(
        "--credential-auth",
        action="store_true",
        help="Also accept a TrustedActor credential verified by the factory authority",
    )
    serve.set_defaults(handler=_web_serve)


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


def _approvals(args: argparse.Namespace) -> int:
    try:
        from id42_work.mcp._dependency import require_mcp  # noqa: PLC0415

        require_mcp()
        from id42_work.mcp.server import load_factory  # noqa: PLC0415
    except ImportError as exc:
        print(exc, file=sys.stderr)
        return 1
    try:
        server = load_factory(args.factory)
    except (ImportError, AttributeError, ValueError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    from id42_work.approvals.commands import run_approval  # noqa: PLC0415

    try:
        asyncio.run(run_approval(server, args))
    except ApprovalResumeError as exc:
        print(format_decision("approved", exc.decision))
        print(f"id42-work: {exc}", file=sys.stderr)
        return 1
    except WorkError as exc:
        print(f"id42-work: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"id42-work: {exc}", file=sys.stderr)
        return 2
    return 0


def _reconcile(args: argparse.Namespace) -> int:
    try:
        from id42_work.mcp._dependency import require_mcp  # noqa: PLC0415

        require_mcp()
        from id42_work.mcp.server import load_factory  # noqa: PLC0415
    except ImportError as exc:
        print(exc, file=sys.stderr)
        return 1
    try:
        server = load_factory(args.factory)
    except (ImportError, AttributeError, ValueError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    from id42_work.reconciliation.commands import format_decision as format_reconcile  # noqa: PLC0415
    from id42_work.reconciliation.commands import run_reconcile  # noqa: PLC0415

    try:
        asyncio.run(run_reconcile(server, args))
    except ReconciliationResumeError as exc:
        kind = "applied" if exc.decision.entry.status.value == "SUCCEEDED" else "not-applied"
        print(format_reconcile(kind, exc.decision))
        print(f"id42-work: {exc}", file=sys.stderr)
        return 1
    except WorkError as exc:
        print(f"id42-work: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"id42-work: {exc}", file=sys.stderr)
        return 2
    return 0


def _web_serve(args: argparse.Namespace) -> int:
    try:
        from id42_work.web._dependency import require_web  # noqa: PLC0415

        require_web()
    except ImportError as exc:
        print(exc, file=sys.stderr)
        return 1
    try:
        from id42_work.mcp._dependency import require_mcp  # noqa: PLC0415

        require_mcp()
        from id42_work.mcp.server import load_factory  # noqa: PLC0415
    except ImportError as exc:
        print(exc, file=sys.stderr)
        return 1
    try:
        server = load_factory(args.factory)
    except (ImportError, AttributeError, ValueError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    from id42_work.web.serve import serve_web  # noqa: PLC0415

    try:
        serve_web(
            server,
            host=args.host,
            port=args.port,
            token_file=args.token_file,
            credential_auth=args.credential_auth,
            factory=args.factory,
        )
    except (TypeError, ValueError) as exc:
        print(f"id42-work: {exc}", file=sys.stderr)
        return 2
    return 0
