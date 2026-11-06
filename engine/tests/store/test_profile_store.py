"""Profiling written to the store, and the data layer built after it (BUILD_PLAN.md M2.5,
ARCHITECTURE.md §1.3 stages 1 and 2, §7.1), on Chinook through a real executor. The sampling
breaker's tests are upstream's (auto_ontology/semantic/tests/test_visit_enter.py), against a fake
executor."""

# The vendored DAL is untyped where these tests read its results.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import sql

from ontological.catalog.data_layer import data_layer_rows, embed_data_layer
from ontological.catalog.ingest import ingest, write_catalog
from ontological.embed import model
from ontological.embed.embedder import LocalEmbedder
from ontological.embed.search import data_vdb, dense_search
from ontological.profile import run
from ontological.profile.run import profile_catalog
from ontological.profile.sampling import TableProfile
from ontological.sources.executor import (
    Cancelled,
    SourceExecutor,
    SourceQueryError,
    SqliteSource,
    StatementTimeout,
)
from ontological.store import pg

TABLES, COLUMNS = 11, 64


def rows(lay: pg.Layout, q: str) -> list[str]:
    out = sql(lay, q)
    return out.splitlines() if out else []


@pytest.fixture
def executor(chinook: Path) -> Iterator[SourceExecutor]:
    with SourceExecutor(SqliteSource(chinook)) as ex:
        yield ex


def column(lay: pg.Layout, table: str, name: str) -> tuple[str, str, str]:
    """`is_unique`, `format` and `sample_values` as the store holds them."""
    [row] = rows(
        lay,
        "SELECT coalesce(c.is_unique::text, '') || '|' || coalesce(c.format, '') || '|' || "
        "coalesce(c.sample_values, '') FROM catalog_column c "
        f"JOIN catalog_table t ON t.id = c.table_id WHERE t.name = '{table}' AND c.name = '{name}'",
    )
    is_unique, fmt, samples = row.split("|", 2)
    return is_unique, fmt, samples


# Profiling at ingest


def test_ingest_profiles_every_table_into_the_store(
    store: pg.Layout, executor: SourceExecutor
) -> None:
    got = ingest(executor)

    assert got.profiled is not None
    assert (got.profiled.tables, got.profiled.failed, got.profiled.sampling_disabled) == (
        TABLES,
        [],
        False,
    )
    unique, fmt, samples = column(store, "Genre", "Name")
    assert (unique, fmt) == ("true", "")
    assert json.loads(samples) == ["Rock", "Jazz", "Metal", "Alternative & Punk", "Rock And Roll"]
    # Numbers are stored as numbers.
    assert json.loads(column(store, "Track", "UnitPrice")[2]) == [0.99]
    # A date column gets its format, and no samples.
    assert column(store, "Invoice", "InvoiceDate") == ("false", "YYYY-MM-DD HH:MM:SS", "")
    # Every column has its uniqueness.
    assert rows(store, "SELECT count(*) FROM catalog_column WHERE is_unique IS NULL") == ["0"]


def test_the_distinct_probe_can_be_turned_off(store: pg.Layout, executor: SourceExecutor) -> None:
    calls: list[bool] = []
    real = executor.profile

    def spy(*args: Any, probe_distinct_values: bool = True, **kwargs: Any) -> TableProfile:
        calls.append(probe_distinct_values)
        return real(*args, probe_distinct_values=probe_distinct_values, **kwargs)

    executor.profile = spy  # type: ignore[method-assign]
    ingest(executor, probe_distinct_values=False)

    assert calls == [False] * TABLES


# The data layer is built after profiling


def test_data_layer_texts_carry_the_profiled_sample_values(
    store: pg.Layout, executor: SourceExecutor
) -> None:
    ingest(executor)

    built = data_layer_rows("chinook")

    assert len(built) == TABLES + COLUMNS
    by_name = {(r["metadata"]["label"], r["text"].split("table_name: ")[1].split(",")[0],
                r["metadata"]["name"]): r for r in built}  # fmt: skip
    genre_name = by_name[("Column", "Genre", "Name")]
    assert genre_name["text"] == (
        "database_name: chinook, schema_name: main, table_name: Genre, column_name: Name, "
        "data_type: VARCHAR, sample_values: Rock, Jazz, Metal, Alternative & Punk, Rock And Roll"
    )
    # A date column has its format in the store, but no samples in its text.
    assert "sample_values" not in by_name[("Column", "Invoice", "InvoiceDate")]["text"]
    assert by_name[("Table", "Genre", "Genre")]["text"].startswith(
        "database_name: chinook, schema_name: main, table_name: Genre, columns: "
    )


def test_a_database_the_store_lacks_has_no_data_layer(store: pg.Layout) -> None:
    assert data_layer_rows("chinook") == []


def test_ingest_embeds_the_data_layer_from_the_profiled_catalog(
    store: pg.Layout, executor: SourceExecutor, embedder: LocalEmbedder
) -> None:
    got = ingest(executor, embedder=embedder)

    assert got.data_layer_rows == TABLES + COLUMNS
    vdb = data_vdb(embedder)
    try:
        hits = dense_search(vdb, "Alternative & Punk", k=3, where={"label": "Column"})
    finally:
        vdb.close()
    texts = [h["text"] for h in hits]
    assert any("table_name: Genre, column_name: Name" in t for t in texts), texts
    genre = next(h for h in hits if "table_name: Genre, column_name: Name" in h["text"])
    assert "sample_values: Rock, Jazz" in genre["text"]
    assert genre["metadata"]["model"] == model.MODEL_ID
    assert genre["metadata"]["database_name"] == "chinook"


def test_embedding_the_data_layer_again_replaces_its_rows(
    store: pg.Layout, executor: SourceExecutor, embedder: LocalEmbedder
) -> None:
    ingest(executor, embedder=embedder)
    count = "SELECT count(*) FROM vdb.data_objects_layer WHERE database_name = 'chinook'"
    assert rows(store, count) == [str(TABLES + COLUMNS)]

    assert embed_data_layer("chinook", embedder) == TABLES + COLUMNS
    assert rows(store, count) == [str(TABLES + COLUMNS)]


# The sampling breaker


class FakeExecutor:
    """Answers each `profile` with the next outcome: an exception to raise, or a profile."""

    def __init__(self, *outcomes: Exception | None) -> None:
        self.outcomes = list(outcomes)
        self.profiled: list[str] = []

    def profile(self, schema: str, table: str, columns: list[Any], **_: Any) -> TableProfile:
        self.profiled.append(table)
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome
        return {
            "columns": {},
            "sample_values": {columns[0]["name"]: ["x"]},
            "uniqueness": {columns[0]["name"]: True},
            "date_formats": {},
        }


@pytest.fixture
def written(store: pg.Layout, executor: SourceExecutor) -> pg.Layout:
    """Chinook's catalog, written and not profiled."""
    write_catalog(executor.extract())
    return store


def timeout() -> StatementTimeout:
    return StatementTimeout("the query ran longer than its 120s timeout")


def test_repeated_sample_timeouts_disable_sampling_for_the_run(written: pg.Layout) -> None:
    fake = FakeExecutor(timeout(), timeout(), timeout())

    got = profile_catalog(fake, "chinook")  # type: ignore[arg-type]

    assert len(fake.profiled) == run.MAX_CONSECUTIVE_SAMPLE_FAILURES
    assert got.sampling_disabled
    assert (got.tables, len(got.failed), got.skipped) == (0, 3, TABLES - 3)
    assert rows(written, "SELECT count(*) FROM catalog_column WHERE is_unique IS NOT NULL") == ["0"]


def test_a_sql_error_on_one_table_does_not_disable_sampling(written: pg.Layout) -> None:
    fake = FakeExecutor(*(SourceQueryError("permission denied") for _ in range(TABLES - 1)))

    got = profile_catalog(fake, "chinook")  # type: ignore[arg-type]

    assert len(fake.profiled) == TABLES
    assert not got.sampling_disabled
    assert (got.tables, len(got.failed)) == (1, TABLES - 1)


def test_one_success_clears_the_failure_streak(written: pg.Layout) -> None:
    fake = FakeExecutor(timeout(), timeout(), None, timeout(), timeout())

    got = profile_catalog(fake, "chinook")  # type: ignore[arg-type]

    assert len(fake.profiled) == TABLES
    assert not got.sampling_disabled
    assert (got.tables, len(got.failed)) == (TABLES - 4, 4)
    assert rows(written, "SELECT count(*) FROM catalog_column WHERE is_unique") == [str(TABLES - 4)]


def test_a_cancel_stops_profiling(written: pg.Layout) -> None:
    fake = FakeExecutor(None, Cancelled("the operation was cancelled"))

    with pytest.raises(Cancelled):
        profile_catalog(fake, "chinook")  # type: ignore[arg-type]
    assert len(fake.profiled) == 2


def test_each_run_starts_with_sampling_on(written: pg.Layout) -> None:
    profile_catalog(FakeExecutor(timeout(), timeout(), timeout()), "chinook")  # type: ignore[arg-type]

    again = FakeExecutor()
    got = profile_catalog(again, "chinook")  # type: ignore[arg-type]

    assert (len(again.profiled), got.tables, got.sampling_disabled) == (TABLES, TABLES, False)
    assert sql(written, "SELECT count(*) FROM catalog_column WHERE is_unique") == str(TABLES)
