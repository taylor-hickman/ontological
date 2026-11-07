"""The data layer: one embedded text per table and per column, in the `data_objects_layer`
collection (ARCHITECTURE.md §1.2 step 5, §7.1).

The texts are upstream's (`auto_ontology/utils/embedding_rows.py`, vendored unchanged), built
from the store, so they are built after profiling has written `sample_values` and each column's
text carries them. Upstream built them from ingest's frames, before compile profiled anything,
and its data layer never saw a sample value. Ingest embeds them, and compile and a re-embed embed
them again when they've changed since, such as once compile has written the descriptions.
"""

# The vendored DAL and embedding rows are untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from auto_ontology.dal import schema as s
from auto_ontology.dal.datasources import fetch_tables_and_columns_by_node_ids
from auto_ontology.dal.session import store
from auto_ontology.utils.embedding_rows import CatalogEmbeddingRowsOp
from auto_ontology.vdb.records import content_hash, write_embedded_rows
from ontological.embed.embedder import LocalEmbedder
from ontological.embed.search import data_vdb

logger = logging.getLogger(__name__)


def data_layer_rows(database_name: str) -> list[dict[str, Any]]:
    """Upstream's embedding rows for every table and column of `database_name`, from the store
    as it is now: `text`, `path`, `page_number` and `metadata` (the node's `id`, `label`,
    `name`, `schema_name`, `database_name`)."""
    table_ids = [
        str(row["id"])
        for row in store().query_read(
            select(s.catalog_table.c.id)
            .select_from(
                s.catalog_table.join(
                    s.catalog_schema, s.catalog_schema.c.id == s.catalog_table.c.schema_id
                ).join(
                    s.catalog_database, s.catalog_database.c.id == s.catalog_schema.c.database_id
                )
            )
            .where(s.catalog_database.c.name == database_name)
        )
    ]
    if not table_ids:
        return []
    tables, columns, _ = fetch_tables_and_columns_by_node_ids(table_ids)
    frame = CatalogEmbeddingRowsOp(database_name=database_name)((tables, columns))
    return frame.to_dict(orient="records")


def embed_data_layer(database_name: str, embedder: LocalEmbedder) -> int:
    """Embed `database_name`'s data layer and replace its rows in `data_objects_layer` with
    them. Returns the number of rows written.

    Everything is embedded before the old rows are deleted, as upstream does, so an embed that
    fails leaves retrieval the previous rows rather than none. Each row's metadata gets `model`
    and `content_hash` (§7.1)."""
    rows = data_layer_rows(database_name)
    if not rows:
        return 0
    texts = [row["text"] for row in rows]
    vectors = embedder.embed_documents(texts)
    embedded = [
        {
            **row,
            "metadata": {
                **row["metadata"],
                "embedding": vector,
                "model": embedder.model,
                "content_hash": content_hash(text),
            },
        }
        for row, text, vector in zip(rows, texts, vectors, strict=True)
    ]
    vdb = data_vdb(embedder, database_name=database_name, reset=True)
    try:
        written = write_embedded_rows(vdb, embedded)
    finally:
        vdb.close()
    logger.info("%s: %d data-layer rows embedded", database_name, written)
    return written


def data_layer_current(database_name: str, model: str) -> bool:
    """Whether the database's data-layer rows are this model's, one per table and column of its
    catalog, each with its text as the catalog would build it now."""
    wanted = sorted(
        (str(row["metadata"].get("id")), content_hash(row["text"]))
        for row in data_layer_rows(database_name)
    )
    [exists] = store().query_read("SELECT to_regclass(:t) AS t", {"t": "vdb.data_objects_layer"})
    if exists["t"] is None:
        return not wanted
    stored = store().query_read(
        "SELECT langchain_metadata ->> 'id' AS id, langchain_metadata ->> 'content_hash' AS hash, "
        "langchain_metadata ->> 'model' AS model FROM vdb.data_objects_layer "
        "WHERE database_name = :db",
        {"db": database_name},
    )
    return all(r["model"] == model for r in stored) and wanted == sorted(
        (str(r["id"]), str(r["hash"])) for r in stored
    )


def refresh_data_layer(database_name: str, embedder: LocalEmbedder) -> int:
    """Embed `database_name`'s data layer again unless it is current. Returns the number of rows
    written, 0 when it was."""
    if data_layer_current(database_name, embedder.model):
        return 0
    return embed_data_layer(database_name, embedder)
