"""Curation edits on Chinook (BUILD_PLAN.md M5.1, ARCHITECTURE.md §5.7): each edit writes
ops.change_log in its own transaction, re-embeds only the semantic-layer texts whose content hash
changed, which rebuilds the BM25 index, and can be undone from the log. A full re-embed, after an
embedding-model change, runs as a tracked job. Compiled with test_compile.py's scripted gateway and
embedded with hash vectors, so it needs the binaries and not the model."""

# The vendored code is untyped where these tests read its results.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from conftest import sql
from fakes import HashEmbeddings, ScriptedGateway
from test_compile import claude

from auto_ontology.dal.session import write_transaction
from auto_ontology.vdb.postgres import PostgresVDB
from ontological.catalog.ingest import ingest
from ontological.compile.split import compile_database, compile_pack
from ontological.curate import edits
from ontological.curate.edits import (
    AlreadyUndone,
    Conflict,
    Creation,
    EditRefused,
    NameTaken,
    NotFound,
    edit_attribute,
    edit_term,
    history,
    undo,
)
from ontological.embed import reembed
from ontological.embed.embedder import LocalEmbedder
from ontological.embed.hybrid import HybridSearch
from ontological.embed.search import semantic_vdb
from ontological.embed.semantic import Synced, sync_database
from ontological.jobs import store as jobs_store
from ontological.jobs.active import ActiveJobs, JobBusy
from ontological.jobs.service import Embeddings, Jobs, NoEmbedder
from ontological.sources.executor import SourceExecutor, SqliteSource
from ontological.store import pg

TIMEOUT = 30.0


class Hashed(LocalEmbedder):
    """The engine's embedder with hash vectors 8 wide, under a model name of the test's choosing,
    and no model to load."""

    dim = 8

    def __init__(self, model: str = "hash-1") -> None:
        self.model = model
        self.hash = HashEmbeddings(self.dim)
        self.fail = False

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("the embedder failed")
        return self.hash.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        return self.hash.embed_query(text)


@dataclass
class Curated:
    store: pg.Layout
    embedder: Hashed
    vdb: PostgresVDB
    search: HybridSearch
    total: int  # vectors in the semantic layer

    def ready(self) -> HybridSearch:
        assert self.search.bm25.wait(TIMEOUT)
        return self.search


@pytest.fixture
def curated(store: pg.Layout, chinook: Path) -> Iterator[Curated]:
    """Chinook ingested with its data layer, compiled, and its semantic layer embedded."""
    embedder = Hashed()
    with SourceExecutor(SqliteSource(chinook)) as executor:
        ingest(executor, embedder=embedder)
        compiled = asyncio.run(
            compile_database("chinook", executor, ScriptedGateway(claude), embedder=embedder)
        )
    assert compiled.vectors is not None
    vdb = semantic_vdb(embedder)
    search = HybridSearch(vdb)
    try:
        yield Curated(store, embedder, vdb, search, compiled.vectors.embedded)
    finally:
        search.close(TIMEOUT)
        vdb.close()


def one(lay: pg.Layout, q: str) -> str:
    return sql(lay, q)


def term_id(lay: pg.Layout, name: str) -> str:
    return one(lay, f"SELECT id FROM term WHERE name = '{name}'")


def attr_id(lay: pg.Layout, table: str, column: str) -> str:
    return one(
        lay,
        "SELECT a.id FROM column_attribute a JOIN catalog_table t ON t.id = a.table_id "
        f"WHERE t.name = '{table}' AND a.source_column = '{column}'",
    )


def attributes_of(lay: pg.Layout, term: str) -> int:
    return int(
        one(
            lay,
            "SELECT count(*) FROM column_attribute__term at JOIN term ON term.id = at.term_id "
            f"WHERE term.name = '{term}'",
        )
    )


def owners_named(lay: pg.Layout, term: str) -> list[str]:
    """The `term_name` copies on a Term's attributes."""
    return one(
        lay,
        "SELECT DISTINCT a.term_name FROM column_attribute a JOIN column_attribute__term at "
        f"ON at.attribute_id = a.id WHERE at.term_id = '{term}'",
    ).splitlines()


def change_log(lay: pg.Layout) -> list[str]:
    out = one(
        lay,
        "SELECT entity || ' ' || field || ' ' || old::text || ' -> ' || new::text || ' ' || actor "
        "FROM ops.change_log ORDER BY id",
    )
    return out.splitlines() if out else []


def found(hits: list[dict[str, Any]], label: str, name: str) -> bool:
    return any(h["metadata"]["label"] == label and h["metadata"]["name"] == name for h in hits)


def vector_metadata(lay: pg.Layout, metadata_id: str, key: str) -> str:
    return one(
        lay,
        f"SELECT langchain_metadata ->> '{key}' FROM vdb.semantic_layer "
        f"WHERE langchain_metadata ->> 'id' = '{metadata_id}'",
    )


# --- the change log ---------------------------------------------------------------------------


def test_an_edit_logs_one_change_per_field_it_changed(curated: Curated) -> None:
    lay = curated.store
    album = term_id(lay, "Album")
    edit = edit_term(
        album, description="A record release.", synonyms=["record", "LP"], vdb=curated.vdb
    )
    assert edit is not None
    assert [(c.entity, c.entity_id, c.field) for c in edit.changes] == [
        ("term", album, "description"),
        ("term", album, "synonyms"),
    ]
    assert {c.edit for c in edit.changes} == {edit.id}
    assert change_log(lay) == [
        'term description "A Album." -> "A record release." user',
        'term synonyms [] -> ["record", "LP"] user',
    ]
    assert one(lay, f"SELECT description FROM term WHERE id = '{album}'") == "A record release."
    assert one(lay, f"SELECT array_to_string(synonyms, ',') FROM term WHERE id = '{album}'") == (
        "record,LP"
    )
    # A person's edit stamps the Term as modified; an import's doesn't look like one
    assert one(lay, f"SELECT modified IS NOT NULL FROM term WHERE id = '{album}'") == "t"
    artist = term_id(lay, "Artist")
    assert edit_term(artist, description="Who made it.", actor="import") is not None
    assert one(lay, f"SELECT modified IS NULL FROM term WHERE id = '{artist}'") == "t"
    assert change_log(lay)[-1] == 'term description "A Artist." -> "Who made it." import'


def test_an_edit_that_changes_nothing_writes_nothing(curated: Curated) -> None:
    album = term_id(curated.store, "Album")
    assert edit_term(album, description="A Album.", vdb=curated.vdb) is None
    assert edit_term(album, vdb=curated.vdb) is None
    assert change_log(curated.store) == []


def test_editing_what_isnt_there_is_refused(curated: Curated) -> None:
    with pytest.raises(NotFound):
        edit_term("no-such-term", description="x")
    with pytest.raises(NotFound):
        edit_attribute("no-such-attribute", description="x")
    with pytest.raises(NotFound):
        undo(12345)
    assert change_log(curated.store) == []


def test_history_lists_an_entity_s_changes_newest_first(curated: Curated) -> None:
    album = term_id(curated.store, "Album")
    first = edit_term(album, description="One.")
    second = edit_term(album, description="Two.")
    edit_term(term_id(curated.store, "Artist"), description="Elsewhere.")
    assert first is not None and second is not None
    assert [(c.edit, c.new) for c in history("term", album)] == [
        (second.id, "Two."),
        (first.id, "One."),
    ]


# --- re-embedding -----------------------------------------------------------------------------


def test_a_description_edit_re_embeds_only_its_own_text(curated: Curated) -> None:
    lay = curated.store
    edit = edit_term(term_id(lay, "Album"), description="A record release.", vdb=curated.vdb)
    assert edit is not None
    assert edit.vectors == Synced(embedded=1, deleted=1, current=curated.total - 1)
    assert (
        one(lay, "SELECT count(*) FROM vdb.semantic_layer WHERE content = 'Term: Album. A record "
            "release.'")
        == "1"
    )  # fmt: skip

    invoice_country = attr_id(lay, "Invoice", "BillingCountry")
    edit = edit_attribute(invoice_country, description="Where it was billed.", vdb=curated.vdb)
    assert edit is not None
    assert [(c.entity, c.field) for c in edit.changes] == [("column_attribute", "description")]
    assert edit.vectors == Synced(embedded=1, deleted=1, current=curated.total - 1)


def test_a_synonym_edit_re_embeds_the_term_s_attributes(curated: Curated) -> None:
    # The synonyms are in each attribute's text ("of Term Album (record)"), not the Term's
    lay = curated.store
    edit = edit_term(term_id(lay, "Album"), synonyms=["record"], vdb=curated.vdb)
    assert edit is not None
    n = attributes_of(lay, "Album")
    assert edit.vectors == Synced(embedded=n, deleted=n, current=curated.total - n)


def test_a_rename_reaches_the_attributes_and_their_vectors(curated: Curated) -> None:
    lay = curated.store
    album = term_id(lay, "Album")
    edit = edit_term(album, name="Release", vdb=curated.vdb)
    assert edit is not None
    n = attributes_of(lay, "Release")
    assert edit.vectors == Synced(embedded=1 + n, deleted=1 + n, current=curated.total - 1 - n)
    assert owners_named(lay, album) == ["Release"]
    assert found(curated.ready().search("Release", k=10), "Term", "Release")

    undone = undo(edit.id, vdb=curated.vdb)
    assert undone.vectors == Synced(embedded=1 + n, deleted=1 + n, current=curated.total - 1 - n)
    assert one(lay, f"SELECT name FROM term WHERE id = '{album}'") == "Album"
    assert owners_named(lay, album) == ["Album"]


def test_a_rename_onto_a_name_already_taken_is_refused(curated: Curated) -> None:
    lay = curated.store
    with pytest.raises(NameTaken):
        edit_term(term_id(lay, "Album"), name="Artist", description="Changed.", vdb=curated.vdb)
    with pytest.raises(EditRefused):
        edit_term(term_id(lay, "Album"), name="  ")
    assert one(lay, "SELECT description FROM term WHERE name = 'Album'") == "A Album."
    assert change_log(lay) == []

    country = attr_id(lay, "Invoice", "BillingCountry")
    taken = one(lay, f"SELECT name FROM column_attribute WHERE id = '{country}'")
    other = attr_id(lay, "Invoice", "BillingCity")
    # Another column's attribute may share a name: the merge key includes the column
    assert edit_attribute(other, name=taken) is not None


def test_certifying_marks_the_vectors_without_re_embedding_them(curated: Curated) -> None:
    lay = curated.store
    country = attr_id(lay, "Invoice", "BillingCountry")
    assert vector_metadata(lay, country, "certified") == "false"
    edit = edit_attribute(country, certified=True, vdb=curated.vdb)
    assert edit is not None
    assert change_log(lay) == ["column_attribute certified false -> true user"]
    assert edit.vectors == Synced(embedded=0, deleted=0, current=curated.total, retagged=1)
    assert vector_metadata(lay, country, "certified") == "true"
    hits = curated.ready().search("billing country", k=50)
    [hit] = [h for h in hits if h["metadata"].get("id") == country]
    assert hit["metadata"]["certified"] is True

    # A Term's vector is certified when its name and its description both are
    album = term_id(lay, "Album")
    edit_term(album, description_certified=True, vdb=curated.vdb)
    assert vector_metadata(lay, album, "certified") == "false"
    edit_term(album, name_certified=True, vdb=curated.vdb)
    assert vector_metadata(lay, album, "certified") == "true"


def test_an_edit_without_a_vdb_is_caught_up_by_the_next_sync(curated: Curated) -> None:
    edit = edit_term(term_id(curated.store, "Album"), description="Later.")
    assert edit is not None and edit.vectors is None
    assert sync_database(curated.vdb, "chinook") == Synced(
        embedded=1, deleted=1, current=curated.total - 1
    )


# --- retrieval and undo -----------------------------------------------------------------------


def test_retrieval_sees_an_edit_and_its_undo(curated: Curated) -> None:
    lay = curated.store
    query = "zorblax pressings"
    assert not found(curated.ready().search(query, k=5), "Term", "Album")

    album = term_id(lay, "Album")
    edit = edit_term(album, description="Zorblax pressings on vinyl.", vdb=curated.vdb)
    assert edit is not None
    assert found(curated.ready().search(query, k=1), "Term", "Album")

    undone = undo(edit.id, vdb=curated.vdb)
    assert one(lay, f"SELECT description FROM term WHERE id = '{album}'") == "A Album."
    assert not found(curated.ready().search(query, k=5), "Term", "Album")
    assert undone.vectors == Synced(embedded=1, deleted=1, current=curated.total - 1)


def test_undo_reverts_every_field_of_the_edit_and_is_logged(curated: Curated) -> None:
    lay = curated.store
    album = term_id(lay, "Album")
    edit = edit_term(album, description="A record release.", synonyms=["record"])
    assert edit is not None
    undone = undo(edit.id)
    assert {c.undoes for c in undone.changes} == {edit.id}
    assert [(c.field, c.old, c.new) for c in undone.changes] == [
        ("description", "A record release.", "A Album."),
        ("synonyms", ["record"], []),
    ]
    assert one(lay, f"SELECT description FROM term WHERE id = '{album}'") == "A Album."
    assert one(lay, f"SELECT cardinality(synonyms) FROM term WHERE id = '{album}'") == "0"
    assert [c.edit for c in history("term", album)] == [undone.id, undone.id, edit.id, edit.id]


def test_an_edit_is_undone_once(curated: Curated) -> None:
    edit = edit_term(term_id(curated.store, "Album"), description="Once.")
    assert edit is not None
    undo(edit.id)
    with pytest.raises(AlreadyUndone):
        undo(edit.id)


def test_undo_is_refused_while_a_later_edit_holds_the_field(curated: Curated) -> None:
    lay = curated.store
    album = term_id(lay, "Album")
    first = edit_term(album, description="First.")
    second = edit_term(album, description="Second.")
    assert first is not None and second is not None
    with pytest.raises(Conflict):
        undo(first.id)
    assert one(lay, f"SELECT description FROM term WHERE id = '{album}'") == "Second."
    undo(second.id)
    undo(first.id)
    assert one(lay, f"SELECT description FROM term WHERE id = '{album}'") == "A Album."


def test_undoing_an_undo_makes_the_edit_again(curated: Curated) -> None:
    lay = curated.store
    country = attr_id(lay, "Invoice", "BillingCountry")
    edit = edit_attribute(country, description="Where it was billed.", certified=True)
    assert edit is not None
    undone = undo(edit.id)
    redo = undo(undone.id)
    assert {c.undoes for c in redo.changes} == {undone.id}
    assert one(lay, f"SELECT description || ' ' || certified FROM column_attribute "
               f"WHERE id = '{country}'") == "Where it was billed. true"  # fmt: skip


# --- what compile changes ---------------------------------------------------------------------


def recompile(lay: pg.Layout, chinook: Path, *names: str) -> None:
    """Compile these tables again, as "recompile with current models" does, through the same
    scripted Claude."""
    tables = [
        {
            "id": one(lay, f"SELECT id FROM catalog_table WHERE name = '{name}'"),
            "name": name,
            "schema_name": "main",
            "description": one(lay, f"SELECT description FROM catalog_table WHERE name = '{name}'")
            or None,
        }
        for name in names
    ]
    with SourceExecutor(SqliteSource(chinook)) as executor:
        asyncio.run(compile_pack([(t, None) for t in tables], executor, ScriptedGateway(claude)))


def test_a_first_compile_logs_nothing_and_a_recompile_logs_what_it_changes(
    curated: Curated, chinook: Path
) -> None:
    lay = curated.store
    # What a compile creates isn't a change; the Term's `created` says when it was compiled
    assert change_log(lay) == []
    # The first recompile reads each generated description back as its column's own and
    # appends the samples the prompt showed, so it changes every sampled attribute once
    recompile(lay, chinook, "Customer", "Invoice")
    first = change_log(lay)
    assert first and all(
        line.startswith('column_attribute description "About ')
        and " — samples: " in line
        and line.endswith(" compile")
        for line in first
    ), first
    # The next answers the same, so it changes nothing and logs nothing
    recompile(lay, chinook, "Customer", "Invoice")
    assert change_log(lay) == first


def test_a_recompile_logs_each_field_it_changes_as_actor_compile(
    curated: Curated, chinook: Path
) -> None:
    lay = curated.store
    customer = term_id(lay, "Customer")
    city = attr_id(lay, "Invoice", "BillingCity")
    # Once, so the samples are on every description, and the log emptied
    recompile(lay, chinook, "Customer", "Invoice")
    sql(lay, "DELETE FROM ops.change_log")
    # Written behind the log's back, so the recompile has something to change
    sql(
        lay,
        "UPDATE term SET description = 'Stale.' WHERE name = 'Customer';"
        f"UPDATE column_attribute SET description = 'Stale city.' WHERE id = '{city}'",
    )

    recompile(lay, chinook, "Customer", "Invoice")

    [term_change] = history("term", customer)
    assert (term_change.field, term_change.old, term_change.new, term_change.actor) == (
        "description",
        "Stale.",
        "A Customer.",
        "compile",
    )
    [city_change] = history("column_attribute", city)
    assert (city_change.field, city_change.old, city_change.actor) == (
        "description",
        "Stale city.",
        "compile",
    )
    assert city_change.new == one(
        lay, f"SELECT description FROM column_attribute WHERE id = '{city}'"
    )
    # An edit per Term: Customer's and Invoice's are undone apart
    assert city_change.edit != term_change.edit
    assert [line.rsplit(" ", 1)[1] for line in change_log(lay)] == ["compile", "compile"]

    # A person can take back what compile wrote
    undone = undo(term_change.edit)
    assert [(c.old, c.new, c.actor) for c in undone.changes] == [("A Customer.", "Stale.", "user")]
    assert one(lay, f"SELECT description FROM term WHERE id = '{customer}'") == "Stale."


def test_an_edit_that_created_what_it_changed_isnt_undone(curated: Curated) -> None:
    lay = curated.store
    album = term_id(lay, "Album")
    # Logged as an import logs the Terms it creates: each field it holds, from nothing
    with write_transaction():
        changes = edits.log_write(
            album, [], {("term", album): edits.CREATED["term"]}, actor="import"
        )
    assert [(c.field, c.old, c.new) for c in changes] == [
        ("name", None, "Album"),
        ("description", None, "A Album."),
    ]
    with pytest.raises(Creation):
        undo(changes[0].edit)
    assert one(lay, f"SELECT name FROM term WHERE id = '{album}'") == "Album"


# --- a full re-embed, as a job ----------------------------------------------------------------


def jobs(lay: pg.Layout) -> list[str]:
    out = one(lay, "SELECT kind || ' ' || database_name || ' ' || state FROM ops.job ORDER BY id")
    return out.splitlines() if out else []


def test_a_model_change_re_embeds_both_layers_as_a_tracked_job(curated: Curated) -> None:
    lay = curated.store
    data_rows = int(one(lay, "SELECT count(*) FROM vdb.data_objects_layer"))
    assert data_rows > 0
    assert reembed.stale_databases(curated.embedder) == []

    changed = Hashed("hash-2")
    assert reembed.stale_databases(changed) == ["chinook"]
    active = ActiveJobs()
    done = reembed.reembed_database("chinook", changed, jobs=active)
    assert done.semantic == Synced(embedded=curated.total, deleted=curated.total, current=0)
    assert done.data_layer == data_rows
    assert active.count() == 0
    assert jobs(lay)[-1] == "reembed chinook done"
    for table in ("semantic_layer", "data_objects_layer"):
        assert one(lay, f"SELECT DISTINCT langchain_metadata ->> 'model' FROM vdb.{table}") == (
            "hash-2"
        )
    assert reembed.stale_databases(changed) == []


def test_a_re_embed_resumes_the_job_a_crash_left_running(curated: Curated) -> None:
    crashed = jobs_store.start_job("reembed", "chinook", [])
    done = reembed.reembed_database("chinook", Hashed("hash-2"))
    assert done.job_id == crashed.id
    assert jobs(curated.store)[-1] == "reembed chinook done"


def test_a_re_embed_that_fails_stops_its_job_with_the_error(curated: Curated) -> None:
    broken = Hashed("hash-2")
    broken.fail = True
    with pytest.raises(RuntimeError, match="the embedder failed"):
        reembed.reembed_database("chinook", broken)
    [job] = [j for j in jobs(curated.store) if j.startswith("reembed")]
    assert job == "reembed chinook stopped"
    assert "the embedder failed" in one(
        curated.store, "SELECT error FROM ops.job WHERE kind = 'reembed'"
    )


def test_one_re_embed_of_a_database_runs_at_a_time(curated: Curated) -> None:
    active = ActiveJobs()
    with active.track("reembed", "chinook"), pytest.raises(JobBusy):
        reembed.reembed_database("chinook", Hashed("hash-2"), jobs=active)


def test_the_engine_re_embeds_each_stale_database_as_its_own_job(curated: Curated) -> None:
    changed = Hashed("hash-2")

    async def run() -> None:
        active = ActiveJobs()
        engine_jobs = Jobs(gateway=ScriptedGateway(claude), active=active, embedder=changed)
        assert await engine_jobs.embeddings() == Embeddings(model="hash-2", stale=["chinook"])
        assert await engine_jobs.reembed_stale() == ["chinook"]
        assert await engine_jobs.reembed_stale() == ["chinook"], "still stale while it runs"
        await asyncio.to_thread(wait_for, curated.store, "reembed chinook done")
        assert active.count() == 0
        assert await engine_jobs.embeddings() == Embeddings(model="hash-2", stale=[])
        assert await engine_jobs.reembed_stale() == []
        await engine_jobs.stop()

    asyncio.run(run())
    assert [j for j in jobs(curated.store) if j.startswith("reembed")] == ["reembed chinook done"]


def test_an_engine_with_no_embedder_has_nothing_to_re_embed_with(curated: Curated) -> None:
    async def run() -> None:
        engine_jobs = Jobs(gateway=ScriptedGateway(claude), active=ActiveJobs())
        assert await engine_jobs.embeddings() == Embeddings(model=None, stale=[])
        with pytest.raises(NoEmbedder):
            await engine_jobs.reembed_stale()

    asyncio.run(run())


def wait_for(lay: pg.Layout, line: str) -> None:
    deadline = time.monotonic() + TIMEOUT
    while line not in jobs(lay):
        assert time.monotonic() < deadline, jobs(lay)
        time.sleep(0.1)


def test_edits_module_names_its_fields() -> None:
    # The fields an edit can change are the ones undo knows how to put back
    assert edits.FIELDS == {
        "term": ("name", "description", "synonyms", "name_certified", "description_certified"),
        "column_attribute": ("name", "description", "certified"),
    }
