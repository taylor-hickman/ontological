"""Compile the eval databases in the eval store (BUILD_PLAN.md M3.9), so the retrieval benchmark
has a semantic layer to search and the eval runner a semantic layer to answer from.

It runs the engine's own compile (`ontological.compile.split.compile_database`) through the real
gateway, on the user's Claude subscription, then syncs the Terms and attributes into the semantic
layer with the default embedding model. Every call is logged in the store's `ops.llm_call` with
its API-equivalent cost, and answers are cached there, so a second run of a finished compile makes
no call. A compile Claude stops (a rate limit it can't wait out, a logged-out `claude`) is resumed
by the next run, as the engine resumes one.

The compile-quality review (BUILD_PLAN.md M3.10) runs it with a `store` of each config's own, the
config's `table_calls` and `routes`, and reads each compile's quota and time from its `Result`.

Like `ingest`, it refuses while an engine serves the store, and starts the eval cluster (for WWI)
and the store's Postgres if they aren't running, stopping whichever it started.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ontological.compile.packing import TableCalls
from ontological.compile.split import Compiled, compile_database
from ontological.embed import model as models
from ontological.embed.embedder import LocalEmbedder
from ontological.engine import discovery
from ontological.llm.calls import PgCallLog
from ontological.llm.gateway import ClaudeGateway
from ontological.llm.routes import Route
from ontological.sources.executor import SourceExecutor
from ontological.store import dal, pg

from . import cluster, ingest
from .home import models_dir


class CompileError(RuntimeError):
    pass


@dataclass(frozen=True)
class Result:
    name: str
    compiled: Compiled
    seconds: float = 0.0  # wall time of this run's compile, embedding included
    # The latest per-window utilization the gateway saw before and after it (§6.5), when Claude
    # sent one: {"five_hour": 0.12, ...}. Coarse, and shared with anything else on the account.
    utilization_before: dict[str, float] = field(default_factory=dict[str, float])
    utilization_after: dict[str, float] = field(default_factory=dict[str, float])


def usage(lay: pg.Layout) -> tuple[int, float]:
    """Calls that reached Claude, cache hits left out, and their API-equivalent cost in USD,
    over the store's whole log."""
    out = pg.psql(
        lay, "SELECT count(*), coalesce(sum(cost_usd), 0) FROM ops.llm_call WHERE NOT cached"
    )
    calls, cost = out.split("|")
    return int(calls), float(cost)


def run(
    home: Path,
    pg_bin: Path,
    names: Sequence[str],
    *,
    store: str | None = None,
    table_calls: TableCalls = "split",
    routes: Mapping[str, Route] | None = None,
    progress: Callable[[str], None] = lambda _: None,
) -> list[Result]:
    """Compile each named database, in order, in the eval store or the review store `store`;
    stop at the first that Claude stopped."""
    model_dir = models_dir(home) / models.DEFAULT.key
    models.fetch(model_dir)
    lay = ingest.layout(home, pg_bin, store)
    with discovery.engine_lock(lay.app_data):
        if (live := discovery.find_engine(lay.app_data)) is not None:
            raise CompileError(
                f"an engine (pid {live.pid}) serves {lay.app_data}: stop it and compile again"
            )
        wwi_cluster = ingest.up_cluster(home, pg_bin, names)
        try:
            server = pg.ensure_running(lay)
            try:
                pg.migrate(lay)
                dal.connect(lay)
                try:
                    return asyncio.run(
                        _compile_all(
                            home, pg_bin, lay, names, model_dir, progress, table_calls, routes
                        )
                    )
                finally:
                    dal.close()
            finally:
                if server.action != "adopt":
                    pg.stop(lay)
        finally:
            if wwi_cluster is not None and wwi_cluster.action != "adopt":
                cluster.down(home, pg_bin)


async def _compile_all(
    home: Path,
    pg_bin: Path,
    lay: pg.Layout,
    names: Sequence[str],
    model_dir: Path,
    progress: Callable[[str], None],
    table_calls: TableCalls,
    routes: Mapping[str, Route] | None,
) -> list[Result]:
    gateway = ClaudeGateway(
        calls=PgCallLog(pg.url(lay)), sandbox=lay.app_data / "claude-sandbox", routes=routes
    )
    gateway.sandbox.mkdir(parents=True, exist_ok=True)
    status = await gateway.check()
    if status.state != "ready":
        raise CompileError(f"Claude isn't ready ({status.state}): {status.error or ''}".strip())
    embedder = LocalEmbedder(model_dir)
    embedder.calibrate()
    results: list[Result] = []
    for name in dict.fromkeys(names):
        progress(f"{name}: compiling")
        source = ingest.source(home, pg_bin, name)
        before = _utilization(gateway)
        started = time.monotonic()
        with SourceExecutor(source) as executor:
            database = source.database_name
            compiled = await compile_database(
                database, executor, gateway, embedder=embedder, table_calls=table_calls
            )
        seconds = time.monotonic() - started
        results.append(Result(name, compiled, seconds, before, _utilization(gateway)))
        if not compiled.complete:
            break
    return results


def _utilization(gateway: ClaudeGateway) -> dict[str, float]:
    limit = gateway.status.rate_limit
    return dict(limit.utilization) if limit else {}
