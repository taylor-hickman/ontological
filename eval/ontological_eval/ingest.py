"""Ingest the eval's five databases into a store of the eval's own (BUILD_PLAN.md M2.9).

WWI is read from the eval cluster as a Postgres source, through DuckDB's `postgres_scanner`, and
each BIRD database is attached as a SQLite file. Both go through the engine's own source executor
and `ontological.catalog.ingest`, the code path a user's source takes. BIRD's
`database_description/*.csv` files aren't read, so the score measures Ontological's own compile.

The store is <home>/app, an engine <appData> that only the eval uses, so the eval's catalogs
never mix with the user's own and can be thrown away with the rest of the eval home. Ingest
writes to the store directly, under `engine.lock`, and refuses while an engine serves it: a
running engine wouldn't know the catalog had changed under it.

Ingest is a first load only (M1.3): a database the store already holds is reported and left as
it is, since updating it needs the catalog diff (M2.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ontological.catalog.ingest import AlreadyIngested, Ingested, ingest
from ontological.engine import discovery
from ontological.sources.executor import SourceExecutor
from ontological.sources.spec import PostgresSource, Source, SqliteSource
from ontological.store import dal, pg

from . import cluster
from .home import app_data_dir, bird_dir
from .questions import BIRD_DATABASES

WWI = "wwi"
DATABASES = (WWI, *BIRD_DATABASES)
# A first ingest of the largest, WWI, takes under a second; this only stops a wedged source.
EXTRACT_TIMEOUT_S = 600.0


class IngestError(RuntimeError):
    pass


@dataclass(frozen=True)
class Result:
    name: str
    ingested: Ingested | None  # None when the store already held it


def layout(home: Path, pg_bin: Path) -> pg.Layout:
    return pg.Layout(app_data_dir(home), pg_bin)


def source(home: Path, pg_bin: Path, name: str) -> Source:
    """WWI as a Postgres source on the eval cluster's socket; a BIRD database as its file."""
    if name == WWI:
        return PostgresSource(
            host=str(cluster.layout(home, pg_bin).pgsocket),
            database=cluster.DATABASE,
            user=pg.USER,
            port=pg.PORT,
        )
    if name not in BIRD_DATABASES:
        raise IngestError(f"no eval database {name!r}: choose from {', '.join(DATABASES)}")
    path = bird_dir(home) / f"{name}.sqlite"
    if not path.is_file():
        raise IngestError(f"{path} is missing: run `fetch bird-databases` first")
    return SqliteSource(path)


def run(home: Path, pg_bin: Path, names: list[str]) -> list[Result]:
    """Ingest each named database into the eval store, starting the eval cluster for WWI and the
    store's Postgres as needed, and stopping whichever this started."""
    sources = {name: source(home, pg_bin, name) for name in dict.fromkeys(names)}
    lay = layout(home, pg_bin)
    lay.app_data.mkdir(parents=True, exist_ok=True)
    with discovery.engine_lock(lay.app_data):
        if (live := discovery.find_engine(lay.app_data)) is not None:
            raise IngestError(
                f"an engine (pid {live.pid}) serves {lay.app_data}: stop it and ingest again"
            )
        wwi_cluster = cluster.up(home, pg_bin) if WWI in sources else None
        try:
            store = pg.ensure_running(lay)
            try:
                pg.migrate(lay)
                dal.connect(lay)
                try:
                    return [_ingest(name, s) for name, s in sources.items()]
                finally:
                    dal.close()
            finally:
                if store.action != "adopt":
                    pg.stop(lay)
        finally:
            if wwi_cluster is not None and wwi_cluster.action != "adopt":
                cluster.down(home, pg_bin)


def _ingest(name: str, s: Source) -> Result:
    with SourceExecutor(s) as executor:
        try:
            return Result(name, ingest(executor, timeout_s=EXTRACT_TIMEOUT_S))
        except AlreadyIngested:
            return Result(name, None)
