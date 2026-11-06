"""Hybrid search on the bundled Postgres: dense, BM25 and trigram rankings fused by RRF with the
certified bonus, upstream's filters and result contract, and the BM25 index kept current by the
VDB's change hook (ARCHITECTURE.md §7.3, §5.6). Needs the binaries; the tests on the real model
need it too."""

# The vendored code is untyped where these tests read its results.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fakes import HashEmbeddings
from langchain_core.embeddings import Embeddings
from psycopg import sql

from auto_ontology.vdb import get_vdb
from auto_ontology.vdb.postgres import PostgresVDB, on_change
from auto_ontology.vdb.records import embed_rows_into_vdb
from ontological.embed.embedder import LocalEmbedder, Tokens, Vectors
from ontological.embed.hybrid import CERTIFIED_BONUS, HybridSearch, rrf
from ontological.embed.search import dense_search, semantic_vdb
from ontological.store import pg

COLLECTION = "hybrid_test"
TIMEOUT = 30.0

CATALOG = [
    # (name, text, label)
    ("customer", "customer: one row per customer, with name, email and country.", "Table"),
    ("customer.first_name", "customer.first_name: the customer's first name.", "Column"),
    ("invoice", "invoice: one row per invoice billed to a customer.", "Table"),
    ("invoice.total", "invoice.total: the amount billed on the invoice.", "Column"),
    ("invoice_line.unit_price", "invoice_line.unit_price: price of one unit.", "Column"),
    ("track.milliseconds", "track.milliseconds: the length of a track.", "Column"),
    ("employee.hire_date", "employee.hire_date: when the employee started.", "Column"),
    ("billing_master.cust_acct_no", "billing_master.cust_acct_no", "Column"),
    ("Zorblaxian", "Zorblaxian: a made-up warehouse code.", "Table"),
]


def vdb_for(embeddings: Embeddings, dim: int) -> PostgresVDB:
    return get_vdb(collection_name=COLLECTION, embeddings=embeddings, vector_size=dim)


def write(
    vdb: PostgresVDB,
    rows: list[tuple[str, str, str]],
    *,
    database_name: str = "chinook",
    certified: set[str] | None = None,
) -> dict[str, str]:
    """Write catalog-like rows; their metadata ids by name."""
    ids = {name: str(uuid.uuid4()) for name, _, _ in rows}
    embed_rows_into_vdb(
        [
            {
                "text": text,
                "metadata": {
                    "id": ids[name],
                    "label": label,
                    "name": name,
                    "database_name": database_name,
                    **({"certified": True} if name in (certified or set()) else {}),
                },
            }
            for name, text, label in rows
        ],
        vdb.embeddings,
        vdb,
        what="test",
    )
    return ids


@pytest.fixture
def hashed(store: pg.Layout) -> Iterator[PostgresVDB]:
    """A collection embedded with hash vectors: the dense ranking is noise, so what ranks first
    is what the lexical rankings found."""
    vdb = vdb_for(HashEmbeddings(), 8)
    try:
        yield vdb
    finally:
        vdb.close()


@pytest.fixture
def search(hashed: PostgresVDB) -> Iterator[HybridSearch]:
    hybrid = HybridSearch(hashed)
    try:
        yield hybrid
    finally:
        hybrid.close(TIMEOUT)


def ready(hybrid: HybridSearch) -> HybridSearch:
    assert hybrid.bm25.wait(TIMEOUT)
    return hybrid


def names(hits: list[dict[str, Any]]) -> list[str]:
    return [h["metadata"]["name"] for h in hits]


def langchain_id(vdb: PostgresVDB, metadata_id: str) -> str:
    """The row id the BM25 index keys documents by."""
    with psycopg.connect(vdb.connection_string) as conn:
        row = conn.execute(
            sql.SQL(
                "SELECT langchain_id::text FROM {} WHERE langchain_metadata ->> 'id' = %s"
            ).format(sql.Identifier(vdb.schema_name, vdb.collection_name)),
            (metadata_id,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_rrf_sums_reciprocal_ranks() -> None:
    scores = rrf([["a", "b"], ["b", "c"], []], k=60)
    assert scores == pytest.approx({"a": 1 / 61, "b": 1 / 62 + 1 / 61, "c": 1 / 62})


def test_an_empty_collection_finds_nothing(search: HybridSearch) -> None:
    assert ready(search).search("invoice") == []


def test_an_identifier_query_finds_its_column(hashed: PostgresVDB, search: HybridSearch) -> None:
    ids = write(hashed, CATALOG)
    column = "billing_master.cust_acct_no"
    # BM25 and the trigram ranking both put it first.
    assert names(ready(search).search("cust_acct_no", k=3))[0] == column
    # BM25 puts it first for the words and the CamelCase spelling too. The fusion can rank it
    # lower here: the customer rows are in the trigram ranking as well, and the hash vectors give
    # every row a dense rank.
    for query in ("customer account number", "CustAcctNo"):
        [(first, _), *_] = search.bm25.search(query, 5)
        assert first == langchain_id(hashed, ids[column])


def test_a_misspelt_name_finds_its_row_by_trigrams(
    hashed: PostgresVDB, search: HybridSearch
) -> None:
    write(hashed, CATALOG)
    # No token in common, so only the trigram ranking (and noise from the dense one) has it.
    assert ready(search).bm25.search("zorblaxan", 5) == []
    assert names(search.search("zorblaxan", k=3))[0] == "Zorblaxian"


def test_hits_keep_upstreams_contract(store: pg.Layout, embedder: LocalEmbedder) -> None:
    """The same {text, metadata, _distance} as dense search, with the same cosine distance."""
    vdb = semantic_vdb(embedder)
    hybrid = HybridSearch(vdb)
    try:
        write(vdb, CATALOG)
        query = "how long is a song"
        hits = ready(hybrid).search(query, k=len(CATALOG))
        dense = {h["metadata"]["id"]: h for h in dense_search(vdb, query, k=len(CATALOG))}
        assert len(hits) == len(CATALOG)
        for hit in hits:
            assert set(hit) == {"text", "metadata", "_distance", "_score"}
            same = dense[hit["metadata"]["id"]]
            assert hit["text"] == same["text"]
            assert hit["metadata"] == same["metadata"]
            assert hit["_distance"] == pytest.approx(same["_distance"], abs=1e-6)
        assert names(hits)[0] == "track.milliseconds"
        assert [h["_score"] for h in hits] == sorted((h["_score"] for h in hits), reverse=True)
    finally:
        hybrid.close(TIMEOUT)
        vdb.close()


def test_filters_hold_for_every_ranking(hashed: PostgresVDB, search: HybridSearch) -> None:
    write(hashed, CATALOG)
    write(hashed, [("pagila.invoice", "invoice: a pagila invoice.", "Table")], database_name="pg")
    ready(search)

    tables = search.search("invoice", k=20, where={"label": "Table"})
    assert {h["metadata"]["label"] for h in tables} == {"Table"}
    assert "invoice" in names(tables) and "invoice.total" not in names(tables)

    pagila = search.search("invoice", k=20, where={"database_name": "pg"})
    assert names(pagila) == ["pagila.invoice"]

    both = search.search("invoice", k=20, where={"database_name": ["pg", "chinook"]})
    assert {"invoice", "pagila.invoice", "invoice.total"} <= set(names(both))

    assert search.search("invoice", where={"label": {"$nin": ["Table", "Column"]}}) == []
    with pytest.raises(ValueError):
        search.search("invoice", where={"name": "invoice"})


def test_a_certified_row_wins_a_tie(hashed: PostgresVDB, search: HybridSearch) -> None:
    text = "invoice.total: the amount billed on the invoice."
    rows = [("invoice.total", text, "Column"), ("invoice.total ", text, "Column")]
    for certified in ("invoice.total", "invoice.total "):
        hashed.delete_all()
        write(hashed, rows, certified={certified})
        hits = ready(search).search("invoice total", k=2)
        assert names(hits)[0] == certified  # whichever the tie-breaks favour
        assert hits[0]["_score"] - hits[1]["_score"] <= CERTIFIED_BONUS + 0.001


def test_every_change_rebuilds_the_index_whichever_vdb_made_it(
    hashed: PostgresVDB, search: HybridSearch
) -> None:
    ready(search)
    other = vdb_for(HashEmbeddings(), 8)  # another instance on the same collection
    try:
        before = search.bm25.rebuilds
        ids = write(other, CATALOG)
        assert names(ready(search).search("cust_acct_no", k=1)) == ["billing_master.cust_acct_no"]
        assert search.bm25.rebuilds > before

        assert len(search.bm25.snapshot.ids) == len(CATALOG)
        other.delete_by_id(ids["billing_master.cust_acct_no"])
        assert len(ready(search).bm25.snapshot.ids) == len(CATALOG) - 1

        other.delete_by_database("chinook")
        assert ready(search).search("invoice") == []
    finally:
        other.close()


def test_a_deleted_row_is_gone_before_the_rebuild_finishes(
    hashed: PostgresVDB, search: HybridSearch, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = write(hashed, CATALOG)
    ready(search)
    monkeypatch.setattr(search.bm25, "invalidate", lambda: None)  # the index stays a write behind
    hashed.delete_by_id(ids["billing_master.cust_acct_no"])
    assert len(search.bm25.snapshot.ids) == len(CATALOG)
    assert "billing_master.cust_acct_no" not in names(search.search("cust_acct_no"))


def test_a_listener_hears_writes_and_deletes(hashed: PostgresVDB) -> None:
    heard: list[tuple[str, str]] = []
    stop = on_change(lambda schema, collection: heard.append((schema, collection)))
    try:
        ids = write(hashed, CATALOG[:2])
        hashed.delete_by_id(ids["customer"])
        hashed.delete_by_id(ids["customer"])  # nothing left to delete: no change
        hashed.delete_all()
    finally:
        stop()
    write(hashed, CATALOG[:1])
    assert heard == [("vdb", COLLECTION)] * 3


def test_a_search_does_not_wait_for_a_bulk_batch_in_flight(
    store: pg.Layout, embedder: LocalEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The query embed calls the session beside a running bulk batch, with no queue (§5.6, S7)."""
    vdb = semantic_vdb(embedder)
    hybrid = HybridSearch(vdb)
    try:
        write(vdb, CATALOG)
        ready(hybrid)
        running, bulk_done = threading.Event(), threading.Event()
        original = embedder.run

        def spy(ids: Tokens, mask: Tokens) -> Vectors:
            if ids.shape[0] > 1:
                running.set()
            return original(ids, mask)

        # One batch of seconds of work, so the search lands inside it (as in test_embedder.py).
        monkeypatch.setattr(embedder, "cap_tokens", 64 * 512)
        monkeypatch.setattr(embedder, "run", spy)

        def bulk() -> None:
            embedder.embed_documents([" ".join(["revenue"] * 600)] * 64)
            bulk_done.set()

        thread = threading.Thread(target=bulk)
        thread.start()
        try:
            assert running.wait(TIMEOUT)
            assert names(hybrid.search("song length", k=1)) == ["track.milliseconds"]
            assert not bulk_done.is_set(), "the search waited for the bulk batch"
        finally:
            thread.join()
    finally:
        hybrid.close(TIMEOUT)
        vdb.close()
