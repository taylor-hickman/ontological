"""Query-history ingest (BUILD_PLAN.md M2.4): parsed in the parse pool, written to the bundled
store against Chinook's catalog, with each statement's months counted (ARCHITECTURE.md §1.3), and
the API answering while a large history ingests (§5.6)."""

from __future__ import annotations

import random
import socket
import statistics
import threading
import time
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import uvicorn
from conftest import sql

from auto_ontology.dal.session import write_transaction
from ontological.api.app import create_app
from ontological.catalog import query_history
from ontological.catalog.ingest import ingest
from ontological.catalog.parse_pool import ParsePool
from ontological.catalog.query_history import (
    HistoryRow,
    NotInCatalog,
    ingest_query_history,
    month_counts,
)
from ontological.catalog.query_stats import recent_usage
from ontological.engine.lifetime import Lifetime
from ontological.sources.executor import SourceExecutor, SqliteSource
from ontological.store import pg

DIALECTS = ["sqlite"]
JOIN = (
    "SELECT a.Title, r.Name FROM Album a JOIN Artist r ON a.ArtistId = r.ArtistId "
    "WHERE r.Name = 'AC/DC'"
)
UNION = "SELECT Name FROM Artist UNION SELECT Title FROM Album"


def at(year: int, month: int, day: int = 15) -> datetime:
    return datetime(year, month, day, 12, tzinfo=UTC)


@pytest.fixture
def chinook_store(store: pg.Layout, chinook: Path) -> pg.Layout:
    with SourceExecutor(SqliteSource(chinook)) as ex:
        ingest(ex)
    return store


@pytest.fixture(scope="module")
def pool() -> Iterator[ParsePool]:
    with ParsePool(workers=2) as p:
        yield p


def rows(lay: pg.Layout, q: str) -> list[str]:
    out = sql(lay, q)
    return out.splitlines() if out else []


def statements(lay: pg.Layout) -> list[str]:
    return rows(
        lay,
        "SELECT sql_full_query || ' | ' || total_counter || ' | ' "
        "|| to_char(last_query_timestamp AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI') "
        "FROM sql_query ORDER BY sql_full_query",
    )


def test_history_is_written_with_its_links_counts_and_months(
    chinook_store: pg.Layout, pool: ParsePool
) -> None:
    history = [
        HistoryRow(JOIN, at(2025, 12)),
        HistoryRow(JOIN, at(2026, 1), count=3),
        HistoryRow(JOIN.replace("AC/DC", "Queen"), at(2026, 1, 20)),  # the same, other literal
        HistoryRow(UNION, at(2026, 2)),
        HistoryRow("SELECT * FROM nowhere", at(2026, 2)),
        HistoryRow("SELEC oops FROM", at(2026, 2)),
    ]

    got = ingest_query_history("chinook", history, dialects=DIALECTS, pool=pool)

    assert (got.rows, got.added, got.merged, got.unmatched, got.failed) == (6, 2, 0, 1, 1)
    assert statements(chinook_store) == [
        f"{UNION} | 1 | 2026-02-15 12:00",
        f"{JOIN} | 5 | 2026-01-20 12:00",
    ]
    assert rows(
        chinook_store,
        "SELECT q.total_counter || ' ' || string_agg(t.name, ',' ORDER BY t.name) "
        "FROM sql_query q JOIN sql_query__table qt ON qt.sql_query_id = q.id "
        "JOIN catalog_table t ON t.id = qt.table_id GROUP BY q.id ORDER BY 1",
    ) == ["1 Album,Artist", "5 Album,Artist"]
    assert rows(
        chinook_store,
        "SELECT string_agg(t.name || '.' || c.name, ',' ORDER BY t.name, c.name) "
        "FROM sql_query q JOIN sql_query__column qc ON qc.sql_query_id = q.id "
        "JOIN catalog_column c ON c.id = qc.column_id JOIN catalog_table t ON t.id = c.table_id "
        "WHERE q.sql_full_query = $$" + JOIN + "$$",
    ) == ["Album.ArtistId,Album.Title,Artist.ArtistId,Artist.Name"]
    join_id = rows(
        chinook_store, "SELECT id FROM sql_query WHERE sql_full_query = $$" + JOIN + "$$"
    )[0]
    assert rows(
        chinook_store,
        "SELECT s.name || '->' || t.name || ' ' || array_to_string(j.refs, ',') "
        "FROM column__join j JOIN catalog_column s ON s.id = j.source_column_id "
        "JOIN catalog_column t ON t.id = j.target_column_id",
    ) == [f"ArtistId->ArtistId {join_id}|album.artistid = artist.artistid"]
    assert rows(chinook_store, "SELECT count(*) FROM column__union") == ["1"]
    assert month_counts([join_id]) == {join_id: {date(2025, 12, 1): 1, date(2026, 1, 1): 4}}


def test_a_second_ingest_adds_to_what_the_store_holds(
    chinook_store: pg.Layout, pool: ParsePool
) -> None:
    ingest_query_history(
        "chinook", [HistoryRow(JOIN, at(2026, 1)), HistoryRow(UNION, at(2026, 1))],
        dialects=DIALECTS, pool=pool,
    )  # fmt: skip
    join_id = rows(
        chinook_store, "SELECT id FROM sql_query WHERE sql_full_query = $$" + JOIN + "$$"
    )

    got = ingest_query_history(
        "chinook",
        [
            HistoryRow(JOIN, at(2025, 11)),  # by text; an older sighting keeps the later time
            HistoryRow(JOIN.replace("AC/DC", "Queen"), at(2026, 2)),  # by shape
            HistoryRow(UNION, at(2026, 1)),
        ],
        dialects=DIALECTS,
        pool=pool,
    )

    assert (got.added, got.merged) == (0, 2)
    assert statements(chinook_store) == [
        f"{UNION} | 2 | 2026-01-15 12:00",
        f"{JOIN} | 3 | 2026-02-15 12:00",
    ]
    assert month_counts(join_id) == {
        join_id[0]: {date(2025, 11, 1): 1, date(2026, 1, 1): 1, date(2026, 2, 1): 1}
    }
    assert rows(chinook_store, "SELECT cardinality(refs) FROM column__join") == ["1"]


def test_months_are_read_back_newest_first(chinook_store: pg.Layout, pool: ParsePool) -> None:
    # The month-counter bug's case, end to end: upstream stored no month a reader could parse,
    # and would have ranked December 2025 above the months of 2026.
    history = [HistoryRow(JOIN, at(2025, 12), count=100)] + [
        HistoryRow(JOIN, at(2026, m), count=m) for m in (1, 2, 3)
    ]
    ingest_query_history("chinook", history, dialects=DIALECTS, pool=pool)
    [join_id] = rows(chinook_store, "SELECT id FROM sql_query")

    assert recent_usage(month_counts([join_id])[join_id]) == 6


def test_history_for_a_database_the_catalog_lacks_is_refused(
    chinook_store: pg.Layout, pool: ParsePool
) -> None:
    with pytest.raises(NotInCatalog, match="pagila"):
        ingest_query_history(
            "pagila", [HistoryRow(JOIN, at(2026, 1))], dialects=DIALECTS, pool=pool
        )


# --- responsiveness -------------------------------------------------------------------------------

TOKEN = "t" * 43
TRACK = [
    "TrackId",
    "Name",
    "AlbumId",
    "MediaTypeId",
    "GenreId",
    "Composer",
    "Milliseconds",
    "Bytes",
]
#: Statements in the large history. Each is distinct text, and most are distinct statements.
LARGE = 8000
#: How much slower the API may answer while the history ingests than when idle (p95), and its
#: slowest acceptable answer. Parsing on an engine thread adds 50-70 ms to the p95.
P95_ADDED_S = 0.025
MAX_S = 0.5


def synthetic_history(n: int) -> list[HistoryRow]:
    """`n` statements over Chinook: a subset of Track's columns, optionally joined to Album and
    Artist, filtered on one column; each with its own literal."""
    rng = random.Random(4)
    out: list[HistoryRow] = []
    for i in range(n):
        cols = ", ".join(f"t.{c}" for c in rng.sample(TRACK, rng.randint(1, 5)))
        joins = ""
        if rng.random() < 0.5:
            cols += ", a.Title"
            joins = " JOIN Album a ON a.AlbumId = t.AlbumId"
            if rng.random() < 0.5:
                cols += ", r.Name"
                joins += " JOIN Artist r ON r.ArtistId = a.ArtistId"
        text = f"SELECT {cols} FROM Track t{joins} WHERE t.{rng.choice(TRACK)} > {i}"
        out.append(HistoryRow(text, at(2026, 1 + i % 12)))
    return out


@contextmanager
def serving(lifetime: Lifetime) -> Generator[str]:
    """The engine's API on a thread, as the engine runs it beside a job."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    app = create_app(token=TOKEN, lifetime=lifetime, pid=1, version="0")
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "the API didn't start"
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()


def test_the_api_stays_responsive_while_a_large_history_ingests(
    chinook_store: pg.Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = synthetic_history(LARGE)
    failed: list[BaseException] = []
    done = threading.Event()
    writing: list[float] = []  # when the ingest's write began, to split the report by phase

    @contextmanager
    def timed_write() -> Generator[None]:
        writing.append(time.perf_counter())
        with write_transaction():
            yield

    monkeypatch.setattr(query_history, "write_transaction", timed_write)

    def run(pool: ParsePool) -> None:
        try:
            got = ingest_query_history("chinook", history, dialects=DIALECTS, pool=pool)
            assert got.added > LARGE // 10 and got.failed == 0
        except BaseException as err:
            failed.append(err)
        finally:
            done.set()

    def timed(client: httpx.Client) -> float:
        t0 = time.perf_counter()
        assert client.get("/health").status_code == 200
        return time.perf_counter() - t0

    latencies: list[tuple[float, float]] = []  # (when sent, how long)
    # Without TCP_NODELAY, Linux's delayed ACK can add 40 ms to every request, idle or not.
    nodelay = httpx.HTTPTransport(socket_options=[(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)])
    with (
        serving(Lifetime()) as base,
        httpx.Client(
            base_url=base, headers={"Authorization": f"Bearer {TOKEN}"}, transport=nodelay
        ) as client,
        ParsePool() as pool,
    ):
        idle: list[float] = []
        for _ in range(50):
            idle.append(timed(client))
            time.sleep(0.01)
        job = threading.Thread(target=run, args=(pool,))
        started = time.perf_counter()
        job.start()
        while not done.is_set():
            latencies.append((time.perf_counter(), timed(client)))
            time.sleep(0.01)
        job.join()
        took = time.perf_counter() - started

    assert not failed, failed
    every = [d for _, d in latencies]
    p95 = statistics.quantiles(every, n=20)[-1]
    idle_p95 = statistics.quantiles(idle, n=20)[-1]
    report = (
        f"idle: {summary(idle)}; {len(every)} requests over a {took:.1f} s ingest: {summary(every)}"
    )
    for phase, ds in (
        ("parsing", [d for t, d in latencies if t < writing[0]]),
        ("writing", [d for t, d in latencies if t >= writing[0]]),
    ):
        report += f"; while {phase}, {len(ds)}: {summary(ds)}"
    print(report)
    assert len(every) >= 50, f"too short an ingest to measure: {report}"
    assert p95 < idle_p95 + P95_ADDED_S and max(every) < MAX_S, report


def summary(ds: list[float]) -> str:
    if len(ds) < 2:
        return "-"
    p95 = statistics.quantiles(ds, n=20)[-1]
    return (
        f"median {statistics.median(ds) * 1000:.1f} ms, p95 {p95 * 1000:.1f} ms, "
        f"max {max(ds) * 1000:.1f} ms"
    )
