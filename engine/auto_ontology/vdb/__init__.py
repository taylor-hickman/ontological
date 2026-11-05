# SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: the factories pass an embeddings and a vector_size through.

"""VDB initialization — data layer and semantic layer collections."""

from langchain_core.embeddings import Embeddings

from auto_ontology.infra.postgres import get_postgres_connection_string
from auto_ontology.vdb.postgres import PostgresVDB

DATA_VDB_COLLECTION: str = "data_objects_layer"
SEMANTIC_VDB_COLLECTION: str = "semantic_layer"
VDB_SCHEMA: str = "vdb"


def get_vdb(
    *,
    database_name: str | None = None,
    collection_name: str = DATA_VDB_COLLECTION,
    reset: bool = False,
    embeddings: Embeddings | None = None,
    vector_size: int | None = None,
) -> PostgresVDB:
    """Build a PostgresVDB pointed at the local pgvector-enabled Postgres.

    When database_name is provided, the VDB will use it to reset old embeddings for the given database.
    """
    connection_string = get_postgres_connection_string()
    kwargs: dict = {
        "connection_string": connection_string,
        "collection_name": collection_name,
        "schema_name": VDB_SCHEMA,
    }
    if database_name:
        kwargs["database_name"] = database_name
    if embeddings is not None:
        kwargs["embeddings"] = embeddings
    if vector_size is not None:
        kwargs["vector_size"] = vector_size
    return PostgresVDB(reset=reset, **kwargs)


def get_data_vdb(
    *,
    database_name: str | None = None,
    reset: bool = False,
    embeddings: Embeddings | None = None,
    vector_size: int | None = None,
) -> PostgresVDB:
    """Backward-compatible alias for :func:`get_vdb` (tabular / data layer)."""
    return get_vdb(
        database_name=database_name,
        reset=reset,
        embeddings=embeddings,
        vector_size=vector_size,
    )


def get_semantic_vdb(
    *,
    database_name: str | None = None,
    reset: bool = False,
    embeddings: Embeddings | None = None,
    vector_size: int | None = None,
) -> PostgresVDB:
    """Build a PostgresVDB for the semantic layer."""
    return get_vdb(
        database_name=database_name,
        collection_name=SEMANTIC_VDB_COLLECTION,
        reset=reset,
        embeddings=embeddings,
        vector_size=vector_size,
    )
