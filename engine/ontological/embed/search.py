"""Dense search through upstream's PostgresVDB with the local embedder (ARCHITECTURE.md §5.4, §7.1).

The VDBs are built with the embedder and its dimension, so the vector column is `vector(768)`
and every write and query goes through the same model. Hybrid search (§7.3,
ontological.embed.hybrid) fuses this ranking with BM25 and trigram ones; this is the dense one
alone.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from auto_ontology.vdb import get_data_vdb, get_semantic_vdb
from auto_ontology.vdb.postgres import PostgresVDB
from ontological.embed.embedder import LocalEmbedder

# PostgresVDB.retrieval, typed: query vectors in, one list of `{text, metadata, _distance}` each.
Retrieval = Callable[..., list[list[dict[str, Any]]]]


def data_vdb(
    embedder: LocalEmbedder, *, database_name: str | None = None, reset: bool = False
) -> PostgresVDB:
    """The data layer: tables and columns."""
    return get_data_vdb(
        database_name=database_name, reset=reset, embeddings=embedder, vector_size=embedder.dim
    )


def semantic_vdb(
    embedder: LocalEmbedder, *, database_name: str | None = None, reset: bool = False
) -> PostgresVDB:
    """The semantic layer: terms, attributes and saved analyses."""
    return get_semantic_vdb(
        database_name=database_name, reset=reset, embeddings=embedder, vector_size=embedder.dim
    )


def dense_search(
    vdb: PostgresVDB, query: str, *, k: int = 10, where: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """The `k` nearest rows to `query` by cosine distance, nearest first, as upstream's
    `{text, metadata, _distance}`. `where` filters on the metadata columns (`label`,
    `database_name`). The query is embedded alone, by the VDB's embedder."""
    vector = vdb.embeddings.embed_query(query)
    retrieval = cast(Retrieval, vdb.retrieval)  # pyright: ignore[reportUnknownMemberType]
    [hits] = retrieval([vector], top_k=k, where=where)
    return hits
