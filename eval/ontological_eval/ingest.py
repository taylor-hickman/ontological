"""Ingest the eval's five databases into a store of the eval's own (BUILD_PLAN.md M2.9).

WWI is read from the eval cluster as a Postgres source, through DuckDB's `postgres_scanner`, and
each BIRD database is attached as a SQLite file. Both go through the engine's own source executor
and `ontological.catalog.ingest`, the code path a user's source takes. BIRD's
`database_description/*.csv` files aren't read, so the score measures Ontological's own compile.

The store is <home>/app, an engine <appData> that only the eval uses, so the eval's catalogs
never mix with the user's own and can be thrown away with the rest of the eval home. Ingest
writes to the store directly, under `engine.lock`, and refuses while an engine serves it: a
running engine wouldn't know the catalog had changed under it.

`pagila` is ingested from the eval cluster too, for the compile-quality review (M3.10), which
names a store of its own for each config it compares (`store`); it isn't one of the five.

A database the store already holds is re-ingested through the catalog diff (M2.3): an unchanged
source leaves its catalog as it is, ids included.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ontological.catalog.ingest import Ingested, ingest
from ontological.engine import discovery
from ontological.sources.executor import SourceExecutor
from ontological.sources.spec import PostgresSource, Source, SqliteSource
from ontological.store import dal, pg

from . import cluster
from .home import app_data_dir, bird_dir
from .questions import BIRD_DATABASES

WWI = "wwi"
PAGILA = "pagila"
DATABASES = (WWI, *BIRD_DATABASES)
CLUSTER_DATABASES = {WWI: cluster.DATABASE, PAGILA: cluster.PAGILA}
# A first ingest of the largest, WWI, takes under a second; this only stops a wedged source.
EXTRACT_TIMEOUT_S = 600.0


class IngestError(RuntimeError):
    pass


@dataclass(frozen=True)
class Result:
    name: str
    ingested: Ingested


def layout(home: Path, pg_bin: Path, store: str | None = None) -> pg.Layout:
    return pg.Layout(app_data_dir(home, store), pg_bin)


def source(home: Path, pg_bin: Path, name: str) -> Source:
    """WWI or Pagila as a Postgres source on the eval cluster's socket; a BIRD database as its
    file."""
    if name in CLUSTER_DATABASES:
        return PostgresSource(
            host=str(cluster.layout(home, pg_bin).pgsocket),
            database=CLUSTER_DATABASES[name],
            user=pg.USER,
            port=pg.PORT,
        )
    if name not in BIRD_DATABASES:
        known = ", ".join((*DATABASES, PAGILA))
        raise IngestError(f"no eval database {name!r}: choose from {known}")
    path = bird_dir(home) / f"{name}.sqlite"
    if not path.is_file():
        raise IngestError(f"{path} is missing: run `fetch bird-databases` first")
    return SqliteSource(path)


def run(home: Path, pg_bin: Path, names: list[str], *, store: str | None = None) -> list[Result]:
    """Ingest each named database into the eval store, or the review store `store`, starting
    the eval cluster for WWI and Pagila and the store's Postgres as needed, and stopping
    whichever this started."""
    sources = {name: source(home, pg_bin, name) for name in dict.fromkeys(names)}
    lay = layout(home, pg_bin, store)
    lay.app_data.mkdir(parents=True, exist_ok=True)
    with discovery.engine_lock(lay.app_data):
        if (live := discovery.find_engine(lay.app_data)) is not None:
            raise IngestError(
                f"an engine (pid {live.pid}) serves {lay.app_data}: stop it and ingest again"
            )
        wwi_cluster = up_cluster(home, pg_bin, sources)
        try:
            server = pg.ensure_running(lay)
            try:
                pg.migrate(lay)
                dal.connect(lay)
                try:
                    return [_ingest(name, s) for name, s in sources.items()]
                finally:
                    dal.close()
            finally:
                if server.action != "adopt":
                    pg.stop(lay)
        finally:
            if wwi_cluster is not None and wwi_cluster.action != "adopt":
                cluster.down(home, pg_bin)


def up_cluster(home: Path, pg_bin: Path, names: Iterable[str]) -> pg.Server | None:
    """Start or adopt the eval cluster when any of `names` lives there, loading Pagila if it is
    one of them and isn't loaded yet; None when none does."""
    names = set(names)
    if not names & CLUSTER_DATABASES.keys():
        return None
    server = cluster.up(home, pg_bin)
    if PAGILA in names:
        cluster.ensure_pagila(home, pg_bin)
    return server


def _ingest(name: str, s: Source) -> Result:
    with SourceExecutor(s) as executor:
        return Result(name, ingest(executor, timeout_s=EXTRACT_TIMEOUT_S))
