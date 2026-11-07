"""Sources in the bundled store (catalog/sources.py): adding Chinook through a real executor, the
summaries, the catalog tree and a table's detail, then removing it."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import sql

from ontological.catalog.sources import (
    AddFiles,
    AddServer,
    NotFound,
    SourceConflict,
    StoreSources,
)
from ontological.jobs.active import ActiveJobs
from ontological.sources.keychain import KeychainRef
from ontological.sources.spec import Source
from ontological.store import pg


@pytest.fixture
def sources(store: pg.Layout) -> StoreSources:
    return StoreSources(jobs=ActiveJobs())


def test_adding_chinook_reads_its_structure(
    store: pg.Layout, sources: StoreSources, chinook: Path
) -> None:
    added = sources.add(AddFiles(kind="file", paths=[str(chinook)]))

    got = added.source
    assert (got.name, got.kind, got.label) == ("chinook", "sqlite", str(chinook))
    assert (got.schemas, got.tables, got.columns) == (1, 11, 64)
    assert got.size_bytes == chinook.stat().st_size
    assert got.pulled is not None
    assert added.skipped == 0
    assert sources.list() == [got]

    with pytest.raises(SourceConflict):
        sources.add(AddFiles(kind="file", paths=[str(chinook)]))


def test_the_catalog_tree_and_a_table_s_detail(
    store: pg.Layout, sources: StoreSources, chinook: Path
) -> None:
    source = sources.add(AddFiles(kind="file", paths=[str(chinook)])).source
    catalog = sources.catalog(source.id)
    assert [s.name for s in catalog.schemas] == ["main"]
    tables = {t.name: t for t in catalog.schemas[0].tables}
    assert len(tables) == 11
    assert (tables["Track"].columns, tables["Track"].pk) == (9, ["TrackId"])

    track = tables["Track"].id
    tag_id = sql(store, "INSERT INTO tag (id, name) VALUES ('t-pii', 'PII') RETURNING id")
    column_id = sql(
        store, f"SELECT id FROM catalog_column WHERE table_id = '{track}' AND name = 'Composer'"
    )
    sql(
        store,
        f"INSERT INTO tag_target (id, tag_id, column_id) VALUES ('tt', '{tag_id}', '{column_id}')",
    )

    detail = sources.table(track)
    assert (detail.source, detail.schema_name, detail.name) == ("chinook", "main", "Track")
    columns = {c.name: c for c in detail.columns}
    assert detail.columns[0].name == "TrackId"  # in ordinal order
    assert columns["TrackId"].pk and not columns["AlbumId"].pk
    assert columns["AlbumId"].references == ["main.Album.AlbumId"]
    assert columns["Name"].is_nullable is False
    assert columns["Name"].sample_values  # profiled at ingest (M2.5)
    assert (columns["Composer"].tags, columns["Composer"].pii) == (["PII"], True)
    assert not columns["Name"].pii

    with pytest.raises(NotFound):
        sources.table("no-such-table")


def test_removing_a_source_deletes_its_catalog(
    store: pg.Layout, sources: StoreSources, chinook: Path
) -> None:
    source = sources.add(AddFiles(kind="file", paths=[str(chinook)])).source
    sources.remove(source.id)
    assert sources.list() == []
    assert sql(store, "SELECT count(*) FROM catalog_table") == "0"
    with pytest.raises(NotFound):
        sources.remove(source.id)


def test_a_server_source_that_fails_forgets_its_password(store: pg.Layout) -> None:
    filed: list[str] = []
    forgotten: list[KeychainRef] = []

    def refuse(source: Source) -> object:
        raise ConnectionError("server is down")

    sources = StoreSources(
        jobs=ActiveJobs(),
        open_executor=refuse,  # type: ignore[arg-type]
        save=lambda account, password: filed.append(account) or KeychainRef(account),
        forget=forgotten.append,
    )
    request = AddServer(kind="postgres", host="db", database="shop", user="ro", password="pw")
    with pytest.raises(ConnectionError):
        sources.add(request)
    assert filed == ["postgres://ro@db:5432/shop"]
    assert forgotten == [KeychainRef("postgres://ro@db:5432/shop")]
    assert sources.list() == []
