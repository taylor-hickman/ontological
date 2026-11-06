"""The staleness check on engine start (ARCHITECTURE.md §9.1).

Upstream's scheduler re-ingests every source every 24 hours, but a desktop app is often closed.
So when the engine starts, it re-ingests the metadata of each source last ingested more than
`STALE_AFTER` ago: a source whose catalog database row keeps its spec (`ingest.py` writes it) and
whose `pulled` stamp is older than that, or missing. Ingest reads the source's catalog through its
executor and diffs it into the store (M2.3), then adds the source's recent query history, parsed
in one parse pool the check's sources share; the check spends no Claude quota.

It runs on a thread of its own once the engine serves, one source at a time, and counts as a
running job, so the engine doesn't idle out under it. A source that fails, such as a server that
is down or a file that moved, is logged and left for the next start; the others still run. The
engine cancels the check before it stops the store.

A refresh that finds new tables doesn't compile them: Jobs shows "N new tables, compile?" and
waits (M3.8). Auto-compile, a setting that is off by default, would start the compile job (M3.1)
for them; it waits for `app.setting`.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select

from auto_ontology.dal import schema as s
from auto_ontology.dal.session import store
from ontological.catalog.ingest import ingest
from ontological.catalog.parse_pool import ParsePool
from ontological.sources.executor import Cancelled, SourceExecutor
from ontological.sources.spec import Source, from_record

log = logging.getLogger(__name__)

STALE_AFTER = timedelta(hours=24)
# Only stops a wedged source; a large catalog extracts in seconds.
EXTRACT_TIMEOUT_S = 600.0
JOIN_TIMEOUT_S = 10.0  # a cancelled extract stops within the executor's kill delay, 2 s


@dataclass(frozen=True)
class Refreshed:
    database_name: str
    new_tables: list[str]  # `schema.table`, for Jobs' "N new tables, compile?"
    error: str | None = None  # why the refresh failed; the catalog stays as it was


def stale_sources(now: datetime, stale_after: timedelta = STALE_AFTER) -> list[Source]:
    """The sources the store keeps whose last ingest is older than `stale_after`, by name."""
    db = s.catalog_database
    rows = store().query_read(
        select(db.c.name, db.c.connection)
        .where(db.c.connection.is_not(None))
        .where(or_(db.c.pulled.is_(None), db.c.pulled < now - stale_after))
        .order_by(db.c.name)
    )
    sources: list[Source] = []
    for row in rows:
        try:
            sources.append(from_record(row["connection"]))
        except ValueError:
            log.warning("staleness: %s keeps a connection this engine can't read", row["name"])
    return sources


class StalenessCheck:
    """Re-ingests the stale sources, once. `start` runs it on a thread; `run` runs it here."""

    def __init__(
        self,
        *,
        find: Callable[[], list[Source]] = lambda: stale_sources(datetime.now(UTC)),
        open_executor: Callable[[Source], SourceExecutor] = SourceExecutor,
        extract_timeout_s: float = EXTRACT_TIMEOUT_S,
    ) -> None:
        self._find = find
        self._open = open_executor
        self._extract_timeout_s = extract_timeout_s
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._executor: SourceExecutor | None = None
        self._thread: threading.Thread | None = None
        self.results: list[Refreshed] = []

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_logged, name="staleness", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        """Stop after the source being refreshed, cancelling its extract if it is still running."""
        self._cancelled.set()
        with self._lock:
            if self._executor is not None:
                self._executor.cancel()

    def join(self, timeout_s: float = JOIN_TIMEOUT_S) -> None:
        if self._thread is not None:
            self._thread.join(timeout_s)
            if self._thread.is_alive():
                log.warning("staleness: still running %gs after it was cancelled", timeout_s)

    def run(self) -> list[Refreshed]:
        sources = self._find()
        if sources:
            log.info("staleness: %d source(s) last ingested over a day ago", len(sources))
        with ParsePool() as pool:  # its workers start only if a source has history
            for source in sources:
                if self._cancelled.is_set():
                    break
                self.results.append(self._refresh(source, pool))
        return self.results

    def _run_logged(self) -> None:
        try:
            self.run()
        except Exception:
            log.exception("staleness check failed")

    def _refresh(self, source: Source, pool: ParsePool) -> Refreshed:
        name = source.database_name
        try:
            with self._open(source) as executor:
                with self._lock:
                    self._executor = executor
                if self._cancelled.is_set():  # cancelled before the extract could be
                    raise Cancelled("the check was cancelled")
                got = ingest(executor, timeout_s=self._extract_timeout_s, pool=pool)
        except Exception as e:
            log.warning("staleness: %s (%s) wasn't refreshed: %s", name, source.label, e)
            return Refreshed(name, [], f"{type(e).__name__}: {e}")
        finally:
            with self._lock:
                self._executor = None
        if got.new_tables:
            log.info("staleness: %s has %d new table(s)", name, len(got.new_tables))
        else:
            log.info("staleness: %s refreshed, no new tables", name)
        return Refreshed(name, got.new_tables)
