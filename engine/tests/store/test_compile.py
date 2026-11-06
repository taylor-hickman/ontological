"""The split compile (BUILD_PLAN.md M1.4, M3.2) on Chinook: ingested through a real source
executor into the bundled store, profiled through the same executor, then compiled with a
scripted gateway, one call at a time, the small tables sharing their pack's calls, and given
semantic FKs from its declared foreign keys."""

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
from ontological.compile.split import Compiled, compile_database, compile_pack
from ontological.sources.executor import SourceExecutor, SqliteSource
from ontological.store import pg

# Upstream's canonical commit order: casefolded schema and table names
ORDER = [
    "Album", "Artist", "Customer", "Employee", "Genre", "Invoice", "InvoiceLine", "MediaType",
    "Playlist", "PlaylistTrack", "Track",
]  # fmt: skip


# The order packs run in: the small tables' pack, then each wide one (test_packing.py)
PACK_ORDER = [
    "Album", "Artist", "Genre", "InvoiceLine", "MediaType", "Playlist", "PlaylistTrack",
    "Customer", "Employee", "Invoice", "Track",
]  # fmt: skip
SMALL = PACK_ORDER[:7]


def table_of(user: str) -> str:
    found = re.search(r"^Table: (?:main\.)?(\w+)$", user, flags=re.M)
    assert found, user
    return found.group(1)


def tables_of(user: str) -> list[str]:
    """Every table a prompt asks about, packed or not."""
    return re.findall(r"^Table: (?:main\.)?(\w+)$", user, flags=re.M)


def sections(user: str) -> list[tuple[str, str]]:
    """A packed prompt's tables: each heading, and its prompt."""
    parts = re.split(r"^(?=Table: )", user, flags=re.M)
    return [
        (p.split("\n", 1)[0].removeprefix("Table: "), p) for p in parts if p.startswith("Table: ")
    ]


def answer_table(task: str, table: str, user: str) -> dict[str, Any] | None:
    """What the scripted Claude says about one table."""
    if task == "table_keys":
        return {
            "fk_suggestions": [{"column_name": "Composer"}] if table == "Track" else [],
            "is_junction_table": table == "InvoiceLine",
        }
    if task == "column_descriptions":
        names = re.findall(r"^  - (\S+) \(", user, flags=re.M)
        return {"descriptions": [{"column_name": n, "description": f"About {n}."} for n in names]}
    if task == "table_synonyms":
        written = re.findall(r"\(([A-Z]+)\)", user)
        return {"synonyms": written}
    assert task == "table_terms"
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


def claude(task: str, user: str) -> dict[str, Any] | NoAnswer | None:
    """What the scripted Claude says to each stage: an entry per table to a packed call, which
    leaves out a table whose own answer would be invalid."""
    found = sections(user)
    if len(found) > 1 or (found and task in ("column_descriptions", "table_synonyms")):
        entries = [
            {"table": heading, **answer}
            for heading, part in found
            if (answer := answer_table(task, heading.removeprefix("main."), part)) is not None
        ]
        return {"tables": entries}
    return answer_table(task, table_of(user) if found else "", user)


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


def test_compile_runs_each_stage_once_a_pack_one_call_at_a_time(
    compiled: tuple[Compiled, ScriptedGateway, pg.Layout],
) -> None:
    got, gateway, _ = compiled

    assert [t.name for t in got.tables] == PACK_ORDER
    # The small tables share a call per stage. PlaylistTrack is two declared FKs: nothing to
    # ask, nothing to describe, no Term. Genre's terms are left out of the pack's answer, so it
    # is asked alone. No Chinook table has a description, so no synonyms are asked for.
    asked = [(c.task, tables_of(c.user)) for c in gateway.calls]
    in_pack = [t for t in SMALL if t != "PlaylistTrack"]
    assert asked == [
        ("table_keys", in_pack),
        ("column_descriptions", in_pack),
        ("table_terms", in_pack),
        ("table_terms", ["Genre"]),
        *[
            (task, [] if task == "column_descriptions" else [t])
            for t in PACK_ORDER[7:]
            for task in ("table_keys", "column_descriptions", "table_terms")
        ],
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


def test_synonyms_written_in_a_table_s_description_go_on_its_terms(
    store: pg.Layout, executor: SourceExecutor
) -> None:
    ingest(executor)
    sql(
        store,
        "UPDATE catalog_table SET description = CASE name "
        "WHEN 'Artist' THEN 'Recording artists (ART).' WHEN 'Genre' THEN 'Music genres (GN).' "
        "WHEN 'Customer' THEN 'Buyers, also known as clients (CL).' END "
        "WHERE name IN ('Artist', 'Genre', 'Customer')",
    )
    gateway = ScriptedGateway(claude)

    asyncio.run(compile_database("chinook", executor, gateway))

    # The pack's two described tables share a call; Customer is a pack of its own
    asked = [(tables_of(c.user), c.user) for c in gateway.calls if c.task == "table_synonyms"]
    assert asked == [
        (
            ["Artist", "Genre"],
            "Table: Artist\nDescription: Recording artists (ART).\n\n"
            "Table: Genre\nDescription: Music genres (GN).\n",
        ),
        ([], "Description: Buyers, also known as clients (CL)."),
    ]
    # Genre's Term is the fallback, and still gets them
    assert rows(
        store,
        "SELECT name || '|' || array_to_string(synonyms, ',') FROM term "
        "WHERE cardinality(synonyms) > 0 ORDER BY name",
    ) == ["Artist|ART", "Customer|CL", "Genre|GN"]


def test_a_recompile_keeps_certified_descriptions(
    compiled: tuple[Compiled, ScriptedGateway, pg.Layout], executor: SourceExecutor
) -> None:
    """ARCHITECTURE.md §1.3: upstream let a compile overwrite a description a person certified."""
    _, _, store = compiled
    sql(
        store,
        "UPDATE term SET description = 'Curated.', description_certified = true "
        "WHERE name = 'Invoice';"
        "UPDATE term SET description = 'Stale.' WHERE name = 'Customer';"
        "UPDATE column_attribute SET description = 'Curated total.', certified = true "
        "WHERE term_name = 'Invoice' AND source_column = 'Total';"
        "UPDATE column_attribute SET description = 'Stale city.' "
        "WHERE term_name = 'Invoice' AND source_column = 'BillingCity'",
    )
    tables = [
        {"id": rows(store, f"SELECT id FROM catalog_table WHERE name = '{name}'")[0],
         "name": name, "schema_name": "main", "description": None}
        for name in ("Customer", "Invoice")
    ]  # fmt: skip

    asyncio.run(compile_pack([(t, None) for t in tables], executor, ScriptedGateway(claude)))

    assert rows(
        store,
        "SELECT name || '|' || description FROM term WHERE name IN ('Customer', 'Invoice')"
        " ORDER BY name",
    ) == ["Customer|A Customer.", "Invoice|Curated."]
    assert rows(
        store,
        "SELECT source_column || '|' || description FROM column_attribute "
        "WHERE term_name = 'Invoice' AND source_column IN ('BillingCity', 'Total') ORDER BY 1",
    ) == ["BillingCity|About BillingCity.", "Total|Curated total."]
