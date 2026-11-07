"""ontological-mcp, the stdio MCP server for external agents (ARCHITECTURE.md §9.2).

    ontological-mcp [--app-data DIR] [the engine's options, such as --pg-bin DIR]

Add it to Claude Code with `claude mcp add ontological -- ontological-mcp`. It passes
`--app-data` and every other option to the engine it starts.

Its tools are upstream's curated nine plus `validate_sql` and `run_validated_sql`
(`ontological.mcp.tools`). It answers MCP `initialize` at once and attaches to an engine in the
background, starting one
headless if none is running, because Postgres start-up and model loading take longer than MCP
clients wait for a handshake. It holds a lease on the engine while the client is connected, and
releases it when the client closes stdin or sends SIGTERM or SIGINT. Nothing but MCP goes to
stdout; the log goes to stderr.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

import anyio

import ontological
from ontological.engine import main as engine_main
from ontological.mcp.link import EngineLink, hold_engine
from ontological.mcp.tools import build_server

log = logging.getLogger("ontological.mcp")


async def _exit_on_signal(link: EngineLink) -> None:
    """The stdio transport reads stdin in a worker thread that a cancel can't interrupt, so on a
    signal the shim releases its lease and exits at once."""
    with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
        async for signum in signals:
            log.info("%s: releasing the lease and exiting", signal.Signals(signum).name)
            await link.release()
            sys.stderr.flush()
            os._exit(0)


async def serve(link: EngineLink) -> None:
    server = build_server(link, version=ontological.__version__)
    async with anyio.create_task_group() as tg:
        tg.start_soon(_exit_on_signal, link)
        await server.run_stdio_async()
        tg.cancel_scope.cancel()


def main(
    argv: Sequence[str] | None = None,
    *,
    engine: Sequence[str] = (sys.executable, "-m", "ontological.engine"),
) -> int:
    """`engine` is the command that runs the engine, without arguments."""
    ap = argparse.ArgumentParser(
        prog="ontological-mcp",
        description="Ontological's MCP server. Other options go to the engine it starts.",
    )
    ap.add_argument("--app-data", type=Path, default=None, help="<appData>/Ontological")
    known, engine_args = ap.parse_known_args(sys.argv[1:] if argv is None else argv)
    app_data = (known.app_data or engine_main.default_app_data()).absolute()
    start = [*engine, "--app-data", str(app_data), *engine_args, "--headless"]
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s %(message)s"
    )

    async def run() -> None:
        async with hold_engine(app_data, start) as link:
            await serve(link)

    anyio.run(run)
    return 0


def cli() -> None:
    """The `ontological-mcp` console script."""
    sys.exit(main())
