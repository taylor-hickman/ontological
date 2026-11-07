"""Compile the eval databases in the eval store (BUILD_PLAN.md M3.9), so the retrieval benchmark
has a semantic layer to search and the eval runner a semantic layer to answer from.

It runs the engine's own compile (`ontological.compile.split.compile_database`) through the real
gateway, on the user's Claude subscription, then syncs the Terms and attributes into the semantic
layer with the default embedding model. Every call is logged in the store's `ops.llm_call` with
its API-equivalent cost, and answers are cached there, so a second run of a finished compile makes
no call. A compile Claude stops (a rate limit it can't wait out, a logged-out `claude`) is resumed
by the next run, as the engine resumes one.

Like `ingest`, it refuses while an engine serves the store, and starts the eval cluster (for WWI)
and the store's Postgres if they aren't running, stopping whichever it started.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ontological.compile.split import Compiled, compile_database
from ontological.embed import model as models
from ontological.embed.embedder import LocalEmbedder
from ontological.engine import discovery
from ontological.llm.calls import PgCallLog
from ontological.llm.gateway import ClaudeGateway
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


def usage(lay: pg.Layout) -> tuple[int, float]:
    """Calls that reached Claude (a cache hit isn't logged), and their API-equivalent cost in
    USD, over the store's whole log."""
    out = pg.psql(lay, "SELECT count(*), coalesce(sum(cost_usd), 0) FROM ops.llm_call")
    calls, cost = out.split("|")
    return int(calls), float(cost)


def run(
    home: Path,
    pg_bin: Path,
    names: Sequence[str],
    *,
    progress: Callable[[str], None] = lambda _: None,
) -> list[Result]:
    """Compile each named database, in order; stop at the first that Claude stopped."""
    model_dir = models_dir(home) / models.DEFAULT.key
    models.fetch(model_dir)
    lay = ingest.layout(home, pg_bin)
    with discovery.engine_lock(lay.app_data):
        if (live := discovery.find_engine(lay.app_data)) is not None:
            raise CompileError(
                f"an engine (pid {live.pid}) serves {lay.app_data}: stop it and compile again"
            )
        wwi_cluster = cluster.up(home, pg_bin) if ingest.WWI in names else None
        try:
            store = pg.ensure_running(lay)
            try:
                pg.migrate(lay)
                dal.connect(lay)
                try:
                    return asyncio.run(_compile_all(home, pg_bin, lay, names, model_dir, progress))
                finally:
                    dal.close()
            finally:
                if store.action != "adopt":
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
) -> list[Result]:
    gateway = ClaudeGateway(calls=PgCallLog(pg.url(lay)), sandbox=lay.app_data / "claude-sandbox")
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
        with SourceExecutor(source) as executor:
            database = source.database_name
            compiled = await compile_database(database, executor, gateway, embedder=embedder)
        results.append(Result(name, compiled))
        if not compiled.complete:
            break
    return results
