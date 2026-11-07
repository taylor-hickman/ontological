"""The parse pool: query-history parsing off the engine's GIL (ARCHITECTURE.md §5.6).

sqlglot is pure Python and holds the GIL, so parsing a large history in the engine would stall
the API and chat for as long as it ran. The pool's workers are processes, started with `spawn`
so they inherit none of the engine's threads, connections or locks, and there are at most
`min(cores - 1, 4)` of them, leaving a core to the engine. They run at a lower priority
(`NICE`), so on a machine with few cores the scheduler still runs the engine's API threads, and
the app's, as soon as they wake. They get statement texts and a
`CatalogIndex`, and return `ParsedQuery` structures and normalized SQL. They never connect to the
store; the engine writes what they return (`query_history.py`).

ProcessPoolExecutor starts its workers as work arrives, so a pool nobody uses costs nothing.
"""

from __future__ import annotations

import multiprocessing
import os
from collections.abc import Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor
from itertools import repeat

from ontological.catalog.parsers.catalog_index import CatalogIndex
from ontological.catalog.sql_parse import ChunkResult, normalize_many, parse_chunk
from ontological.compile.fragments import extract_many

#: Upstream's chunk of history: a task's worth of statements.
CHUNK = 500
#: How much a worker's priority is lowered: parsing is background work.
NICE = 10


def default_workers() -> int:
    return max(1, min((os.cpu_count() or 2) - 1, 4))


def _lower_priority() -> None:
    os.nice(NICE)


def chunked[T](items: Sequence[T], workers: int, most: int = CHUNK) -> list[Sequence[T]]:
    """At most `most` items a chunk, and enough chunks to keep every worker busy."""
    if not items:
        return []
    size = max(1, min(most, -(-len(items) // workers)))
    return [items[i : i + size] for i in range(0, len(items), size)]


class ParsePool:
    def __init__(self, workers: int | None = None) -> None:
        self.workers = workers or default_workers()
        self._executor = ProcessPoolExecutor(
            max_workers=self.workers,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_lower_priority,
        )

    def __enter__(self) -> ParsePool:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def parse(
        self, texts: Sequence[str], catalog: CatalogIndex, dialects: Sequence[str]
    ) -> Iterator[ChunkResult]:
        """Each chunk's result, in the order of `texts`. Every chunk is queued at once."""
        chunks = chunked(texts, self.workers)
        return self._executor.map(parse_chunk, chunks, repeat(catalog), repeat(tuple(dialects)))

    def normalize(self, sqls: Sequence[str], dialects: Sequence[str]) -> list[str | None]:
        """Each statement's normalized SQL (`sql_parse.normalize`), in order."""
        chunks = chunked(sqls, self.workers)
        out: list[str | None] = []
        for part in self._executor.map(normalize_many, chunks, repeat(tuple(dialects))):
            out.extend(part)
        return out

    def fragments(self, texts: Sequence[str]) -> list[list[str]]:
        """Each statement's SQL fragments (`compile.fragments.extract_expressions`), in order:
        the SQL-attribute stage's sqlglot work, off the engine's GIL as parsing is."""
        out: list[list[str]] = []
        for part in self._executor.map(extract_many, chunked(texts, self.workers)):
            out.extend(part)
        return out

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)
