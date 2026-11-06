"""The minimal split compile (BUILD_PLAN.md M1.4) on Chinook: ingested through a real source
executor into the bundled store, profiled through the same executor, then compiled with a
scripted gateway, one call at a time, and given semantic FKs from its declared foreign keys."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import sql
from fakes import NoAnswer, ScriptedGateway

from ontological.catalog.ingest import ingest
from ontological.compile.descriptions import to_term_name
from ontological.compile.semantic_fk import SemanticFks
from ontological.compile.split import Compiled, compile_database
from ontological.sources.executor import SourceExecutor, SqliteSource
from ontological.store import pg

# Upstream's canonical commit order: casefolded schema and table names
ORDER = [
    "Album", "Artist", "Customer", "Employee", "Genre", "Invoice", "InvoiceLine", "MediaType",
    "Playlist", "PlaylistTrack", "Track",
]  # fmt: skip


def table_of(user: str) -> str:
    found = re.search(r"^Table: (?:main\.)?(\w+)$", user, flags=re.M)
    assert found, user
    return found.group(1)


def claude(task: str, user: str) -> dict[str, Any] | NoAnswer | None:
    """What the scripted Claude says to each stage."""
    if task == "table_keys":
        table = table_of(user)
        return {
            "fk_suggestions": [{"column_name": "Composer"}] if table == "Track" else [],
            "is_junction_table": table == "InvoiceLine",
        }
    if task == "column_descriptions":
        names = re.findall(r"^  - (\S+) \(", user, flags=re.M)
        return {"descriptions": [{"column_name": n, "description": f"About {n}."} for n in names]}
    assert task == "table_terms"
    table = table_of(user)
    if table == "Genre":
        return None  # an invalid answer: the fallback Term
    columns = re.findall(r"^  - source_column=(\S+) ", user, flags=re.M)
    return {
        "terms": [
            {
                "name": to_term_name(table),
                "description": f"A {table}.",
                "attributes": [
                    {"source_column": c, "display_name": to_term_name(c)} for c in columns
                ],
            }
        ]
    }


@pytest.fixture
def executor(chinook: Path) -> Iterator[SourceExecutor]:
    with SourceExecutor(SqliteSource(chinook)) as ex:
        yield ex


@pytest.fixture
def compiled(
    store: pg.Layout, executor: SourceExecutor
) -> tuple[Compiled, ScriptedGateway, pg.Layout]:
    ingest(executor)
    gateway = ScriptedGateway(claude)
    return asyncio.run(compile_database("chinook", executor, gateway)), gateway, store


def rows(lay: pg.Layout, q: str) -> list[str]:
    out = sql(lay, q)
    return out.splitlines() if out else []


def test_compile_runs_the_three_stages_per_table_one_call_at_a_time(
    compiled: tuple[Compiled, ScriptedGateway, pg.Layout],
) -> None:
    got, gateway, _ = compiled

    assert [t.name for t in got.tables] == ORDER
    # PlaylistTrack is two declared FKs: nothing to ask, nothing to describe, no Term
    stages = ["table_keys", "column_descriptions", "table_terms"]
    assert gateway.tasks() == [task for t in ORDER if t != "PlaylistTrack" for task in stages]
    assert [table_of(c.user) for c in gateway.calls if c.task != "column_descriptions"] == [
        t for t in ORDER if t != "PlaylistTrack" for _ in range(2)
    ]
    assert gateway.peak == 1
    assert [t.name for t in got.tables if t.skipped] == ["PlaylistTrack"]


def test_profiling_is_stored_and_reaches_the_keys_prompt(
    compiled: tuple[Compiled, ScriptedGateway, pg.Layout],
) -> None:
    _, gateway, store = compiled

    assert rows(
        store,
        "SELECT c.name || '|' || coalesce(c.is_unique::text, '') || '|' || "
        "coalesce(c.format, '') || '|' || coalesce(c.sample_values, '') "
        "FROM catalog_column c JOIN catalog_table t ON t.id = c.table_id "
        "WHERE t.name = 'Invoice' AND c.name IN ('InvoiceId', 'InvoiceDate', 'BillingCountry') "
        "ORDER BY c.ordinal_position",
    ) == [
        "InvoiceId|true||[1, 2, 3, 4, 5]",
        "InvoiceDate|false|YYYY-MM-DD HH:MM:SS|",  # a timestamp keeps no samples
        'BillingCountry|false||["USA", "Canada", "France", "Brazil", "Germany"]',
    ]
    [keys] = [
        c.user for c in gateway.calls if c.task == "table_keys" and "main.Invoice\n" in c.user
    ]
    assert "Primary key columns (exclude from suggestions): InvoiceId\n" in keys
    assert "Known foreign key columns (exclude from suggestions): CustomerId\n" in keys
    assert (
        "  - BillingCountry (VARCHAR) [samples: USA, Canada, France, Brazil, Germany] "
        "[is_unique: false]" in keys
    )


def test_terms_and_attributes_are_written_for_every_non_fk_column(
    compiled: tuple[Compiled, ScriptedGateway, pg.Layout],
) -> None:
    got, _, store = compiled

    terms = rows(
        store,
        "SELECT t.name || '|' || term.name || '|' || term.description FROM table__term tt "
        "JOIN catalog_table t ON t.id = tt.table_id JOIN term ON term.id = tt.term_id "
        'ORDER BY t.name COLLATE "C"',
    )
    assert terms == [
        "Genre|Genre|Business entity represented by table Genre"  # the failed call's fallback
        if t == "Genre"
        else f"{t}|{to_term_name(t)}|A {t}."
        for t in sorted(ORDER)
        if t != "PlaylistTrack"
    ]
    invoice = next(t for t in got.tables if t.name == "Invoice")
    assert invoice.terms == ["Invoice"]
    attributes = rows(
        store,
        "SELECT c.name || '|' || a.name || '|' || coalesce(a.description, '') "
        "FROM column__has_attribute h JOIN column_attribute a ON a.id = h.attribute_id "
        "JOIN catalog_column c ON c.id = h.column_id JOIN catalog_table t ON t.id = c.table_id "
        "WHERE t.name = 'Invoice' ORDER BY c.ordinal_position",
    )
    # CustomerId is a declared FK, and gets a semantic FK instead
    assert [a.split("|")[0] for a in attributes] == [
        "InvoiceId", "InvoiceDate", "BillingAddress", "BillingCity", "BillingState",
        "BillingCountry", "BillingPostalCode", "Total",
    ]  # fmt: skip
    assert "InvoiceDate|Invoice Date|About InvoiceDate. — format: YYYY-MM-DD HH:MM:SS" in attributes
    # The suggested FK is left out too; the junction call is kept
    assert rows(
        store,
        "SELECT count(*) FROM column__has_attribute h JOIN catalog_column c ON c.id = h.column_id "
        "WHERE c.name = 'Composer'",
    ) == ["0"]
    assert rows(store, "SELECT name FROM catalog_table WHERE is_junction_table") == ["InvoiceLine"]


def test_semantic_fks_come_from_declared_fks_only(
    compiled: tuple[Compiled, ScriptedGateway, pg.Layout],
) -> None:
    got, _, store = compiled

    edges = rows(
        store,
        "SELECT st.name || '.' || sc.name || ' -> ' || tt.name || '.' || tc.name || ' ' || "
        "f.use_in_join_paths FROM column__semantic_fk f "
        "JOIN catalog_column sc ON sc.id = f.column_id "
        "JOIN catalog_table st ON st.id = sc.table_id "
        "JOIN column__has_attribute h ON h.attribute_id = f.attribute_id "
        "JOIN catalog_column tc ON tc.id = h.column_id "
        "JOIN catalog_table tt ON tt.id = tc.table_id ORDER BY 1",
    )
    assert edges == [
        "Album.ArtistId -> Artist.ArtistId true",
        "Customer.SupportRepId -> Employee.EmployeeId true",
        "Invoice.CustomerId -> Customer.CustomerId true",
        "InvoiceLine.InvoiceId -> Invoice.InvoiceId true",
        "InvoiceLine.TrackId -> Track.TrackId true",
        "PlaylistTrack.PlaylistId -> Playlist.PlaylistId true",
        "PlaylistTrack.TrackId -> Track.TrackId true",
        "Track.AlbumId -> Album.AlbumId true",
        "Track.GenreId -> Genre.GenreId true",
        "Track.MediaTypeId -> MediaType.MediaTypeId true",
    ]
    # Employee.ReportsTo references its own table; Track.Composer has no declared FK, and
    # inferring one is M3.4's
    assert got.semantic_fks == SemanticFks(
        created=10, self_references=1, no_attribute=0, undeclared=1
    )


def test_a_second_compile_asks_nothing_and_writes_nothing(
    compiled: tuple[Compiled, ScriptedGateway, pg.Layout], executor: SourceExecutor
) -> None:
    _, _, store = compiled
    before = rows(
        store, "SELECT count(*) FROM term UNION ALL SELECT count(*) FROM column__semantic_fk"
    )
    gateway = ScriptedGateway(claude)

    again = asyncio.run(compile_database("chinook", executor, gateway))

    assert [(t.name, t.skipped) for t in again.tables] == [("PlaylistTrack", "no non-FK columns")]
    assert gateway.calls == []
    assert again.semantic_fks is not None and again.semantic_fks.created == 0
    after = rows(
        store, "SELECT count(*) FROM term UNION ALL SELECT count(*) FROM column__semantic_fk"
    )
    assert after == before == ["10", "10"]


def test_a_rate_limit_stops_the_compile_and_the_next_run_resumes_there(
    store: pg.Layout, executor: SourceExecutor
) -> None:
    ingest(executor)

    def limited_at_employee(task: str, user: str) -> dict[str, Any] | NoAnswer | None:
        if task == "table_terms" and table_of(user) == "Employee":
            return NoAnswer("limited", "limited")
        return claude(task, user)

    gateway = ScriptedGateway(limited_at_employee)
    stopped = asyncio.run(compile_database("chinook", executor, gateway))

    assert [t.name for t in stopped.tables] == ["Album", "Artist", "Customer"]
    assert stopped.stopped is not None and stopped.stopped.task == "table_terms"
    assert (stopped.complete, stopped.semantic_fks) == (False, None)
    # Nothing for Employee, no fallback Term anywhere: Employee and the rest stay in the work list
    with_terms = "SELECT t.name FROM table__term tt JOIN catalog_table t ON t.id = tt.table_id"
    assert rows(store, with_terms + " ORDER BY 1") == ["Album", "Artist", "Customer"]
    assert rows(store, "SELECT count(*) FROM term WHERE description LIKE 'Business entity%'") == [
        "0"
    ]
    assert rows(store, "SELECT count(*) FROM column__semantic_fk") == ["0"]

    resumed = ScriptedGateway(claude)
    done = asyncio.run(compile_database("chinook", executor, resumed))

    assert done.complete
    assert [t.name for t in done.tables] == ORDER[3:]
    assert table_of(resumed.calls[0].user) == "Employee"
    counts = "SELECT count(*) FROM term UNION ALL SELECT count(*) FROM column__semantic_fk"
    assert rows(store, counts) == ["10", "10"]
