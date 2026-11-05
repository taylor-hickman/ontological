"""Upstream's vendored DAL, catalog store and vector store on the bundled Postgres, over the socket
with no password, as the engine runs them (ARCHITECTURE.md §5.3, §5.4).

M2.1 runs upstream's full DAL suite against the golden seed. These check the wiring: that
`dal.connect` points both upstream connection paths (SQLAlchemy + psycopg for the DAL, asyncpg for
langchain-postgres) at the store, and that the edits M1.1 made work on the real server.
"""

# The vendored code is untyped where these tests read its results.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import keyring
import pytest
from conftest import sql
from fakes import HashEmbeddings, MemoryKeyring

from auto_ontology.catalog.constants import Labels
from auto_ontology.catalog.model.node import CatalogNode
from auto_ontology.catalog.store import db as catalog_db
from auto_ontology.catalog.store.rows import upsert_row
from auto_ontology.dal import connections, datasources, owners, pql_analyses, terms
from auto_ontology.dal import schema as s
from auto_ontology.infra.keychain import KEYRING_SERVICE
from auto_ontology.vdb import SEMANTIC_VDB_COLLECTION, VDB_SCHEMA, get_semantic_vdb
from ontological.store import local_owner, pg

DIM = 8


@pytest.fixture
def keychain() -> Iterator[MemoryKeyring]:
    before = keyring.get_keyring()
    ring = MemoryKeyring()
    keyring.set_keyring(ring)
    try:
        yield ring
    finally:
        keyring.set_keyring(before)


def test_the_dal_reads_and_writes_the_catalog(store: pg.Layout, keychain: MemoryKeyring) -> None:
    stored = {"type": "sqlite", "database_name": "chinook", "path": "/data/chinook.db"}
    connections.insert_connection(connection=json.dumps(stored), database_name="chinook")
    connections.insert_connection(connection="", database_name="pagila")
    secret = {"type": "postgres", "database_name": "pagila", "password": "pw"}
    keychain.items[(KEYRING_SERVICE, "pagila")] = json.dumps(secret)

    assert connections.list_connections() == [stored, secret]  # the keychain's, for pagila
    connections.verify_connectivity()


def test_the_catalog_store_writes_rows_with_the_local_owner(store: pg.Layout) -> None:
    db_id = upsert_row(Labels.DB, {"name": "chinook"}, {"name": "chinook"})
    node = CatalogNode(name="chinook", label=Labels.DB, props={"name": "chinook"})
    assert catalog_db.db_exists(node) == (db_id, False)
    upsert_row(
        Labels.SCHEMA, {"database_name": "chinook", "name": "main"}, {"name": "main"},
        parent_id=db_id,
    )  # fmt: skip
    assert catalog_db.db_exists(node) == (db_id, True)

    owners.assign_owner(s.catalog_database, db_id, local_owner.LOCAL_OWNER_ID)
    subject = owners.edit_subject(db_id)
    assert subject is not None and subject.owner_id == local_owner.LOCAL_OWNER_ID
    assert local_owner.can_edit_entity(subject=subject)

    [database] = datasources.fetch_databases(zone_ids=None)  # None: the whole catalog
    assert (database["name"], database["owner_id"]) == ("chinook", local_owner.LOCAL_OWNER_ID)
    assert datasources.fetch_databases(zone_ids=[]) == []  # an empty list grants nothing
    assert terms.count_terms(zone_ids=None) == 0


def test_analyses_embed_into_pgvector_at_the_models_dimension(store: pg.Layout) -> None:
    emb = HashEmbeddings(dim=DIM)
    pql_analyses.upsert_pql_analysis_node(
        str(uuid.uuid4()), "Monthly churn", "Customers lost per month", "PREDICT churn"
    )
    target = str(uuid.uuid4())
    pql_analyses.upsert_pql_analysis_node(target, "Revenue", "Invoice totals", "PREDICT sum")
    vdb = get_semantic_vdb(embeddings=emb, vector_size=DIM)
    try:
        pql_analyses.embed_pql_analyses(emb, vdb, database_name="chinook")
        [hits] = vdb.retrieval([emb.embed_query("Revenue: Invoice totals")], top_k=1)
        assert [h["metadata"]["id"] for h in hits] == [target]
        assert hits[0]["_distance"] == pytest.approx(0, abs=1e-6)
        assert vdb.delete_by_database("chinook") and vdb.retrieval([[1.0] * DIM]) == [[]]
    finally:
        vdb.close()

    table = f"{VDB_SCHEMA}.{SEMANTIC_VDB_COLLECTION}"
    column = sql(store, f"SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                 f"WHERE attrelid = '{table}'::regclass AND attname = 'embedding'")  # fmt: skip
    assert column == f"vector({DIM})"  # no halfvec: the dimension is within HNSW's limit
    index = sql(store, f"SELECT indexdef FROM pg_indexes WHERE schemaname = '{VDB_SCHEMA}' "
                f"AND indexdef LIKE '%hnsw%'")  # fmt: skip
    assert "vector_cosine_ops" in index
