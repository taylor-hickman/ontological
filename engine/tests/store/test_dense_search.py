"""Dense search with the local embedder through upstream's PostgresVDB, on the bundled Postgres
(ARCHITECTURE.md §5.4, §7.1). Needs the model and the binaries."""

# The vendored code is untyped where these tests read its results.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import insert

from auto_ontology.dal import custom_analyses, pql_analyses
from auto_ontology.dal import schema as s
from auto_ontology.dal.session import store as dal_store
from ontological.embed import model
from ontological.embed.embedder import LocalEmbedder
from ontological.embed.search import dense_search, semantic_vdb
from ontological.store import pg

PQL = {
    "Monthly churn": "Customers who cancelled their subscription each month",
    "Revenue by country": "Invoice totals summed per billing country",
    "Longest tracks": "Tracks ordered by their running time",
}


def add_custom_analysis(name: str, description: str, sql_text: str) -> str:
    analysis_id, sql_id = str(uuid.uuid4()), str(uuid.uuid4())
    dal_store().query_write(
        insert(s.custom_analysis).values(id=analysis_id, name=name, description=description)
    )
    dal_store().query_write(insert(s.sql_query).values(id=sql_id, sql_full_query=sql_text))
    dal_store().query_write(
        insert(s.custom_analysis__sql).values(analysis_id=analysis_id, sql_query_id=sql_id)
    )
    return analysis_id


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_dense_search_finds_the_nearest_analysis(store: pg.Layout, embedder: LocalEmbedder) -> None:
    ids = {name: str(uuid.uuid4()) for name in PQL}
    for name, description in PQL.items():
        pql_analyses.upsert_pql_analysis_node(ids[name], name, description, "PREDICT x")
    top_staff = add_custom_analysis(
        "Top sales staff",
        "Employees ranked by the invoice totals of the customers they support",
        "SELECT e.last_name, sum(i.total) FROM employee e JOIN customer c ON "
        "c.support_rep_id = e.employee_id JOIN invoice i USING (customer_id) GROUP BY 1",
    )

    vdb = semantic_vdb(embedder)
    try:
        pql_analyses.embed_pql_analyses(embedder, vdb, database_name="chinook")
        custom_analyses.embed_custom_analyses(embedder, vdb, database_name="chinook")

        [hit] = dense_search(vdb, "which countries bring in the most money", k=1)
        assert hit["metadata"]["id"] == ids["Revenue by country"]

        [hit] = dense_search(vdb, "which employees sell the most", k=1)
        assert hit["metadata"]["id"] == top_staff

        hits = dense_search(vdb, "song duration", k=4)
        assert hits[0]["metadata"]["id"] == ids["Longest tracks"]
        assert [h["_distance"] for h in hits] == sorted(h["_distance"] for h in hits)

        scoped = dense_search(vdb, "song duration", k=4, where={"label": "CustomAnalysis"})
        assert [h["metadata"]["id"] for h in scoped] == [top_staff]
    finally:
        vdb.close()


def test_each_vector_names_its_model_and_the_hash_of_its_text(
    store: pg.Layout, embedder: LocalEmbedder
) -> None:
    pql_analyses.upsert_pql_analysis_node(str(uuid.uuid4()), "Revenue", "Invoice totals", "P")
    vdb = semantic_vdb(embedder)
    try:
        pql_analyses.embed_pql_analyses(embedder, vdb)
        [hit] = dense_search(vdb, "revenue", k=1)
    finally:
        vdb.close()
    assert hit["text"] == "Revenue: Invoice totals"
    assert hit["metadata"]["model"] == model.MODEL_ID
    assert hit["metadata"]["content_hash"] == sha256("Revenue: Invoice totals")
