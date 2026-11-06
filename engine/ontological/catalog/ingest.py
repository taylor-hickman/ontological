# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/catalog/ingest.py, extract.py,
# write.py, parsers/schemas_parser.py and services/schema.py. It reads a source executor's extract
# in place of a connector, writes a first load only (no diff, no query history), sequentially and
# in one transaction, and profiles the catalog before it embeds the data layer.

"""Minimal ingest: a source's catalog, extracted by its executor and written through the DAL,
then profiled through the executor, then embedded as the data layer.

The write is the first-load branch of upstream's `write.populate_db`, then `populate_fks` and
`populate_pks`, over upstream's vendored catalog store. A database the store already holds is
refused: updating it needs the catalog diff, which M2.3 ports. Query history (M2.4) isn't read.

Profiling (`ontological/profile/`) runs before the data layer is built, so each column's text
carries its sample values (§7.1). Upstream embedded the data layer at ingest and profiled at
compile, so its texts never had them.

Upstream writes each schema, the keys and the `pulled` stamp in separate transactions, and a
failure part way is repaired by the next ingest's diff. With no diff a half-written catalog would
be stuck, so here the whole write is one transaction: the catalog lands complete, or not at all.
"""

# The vendored catalog store is untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import pandas as pd

from auto_ontology.catalog.constants import Labels
from auto_ontology.catalog.model.node import CatalogNode
from auto_ontology.catalog.model.schema import Schema
from auto_ontology.catalog.normalize import normalize_columns, normalize_tables
from auto_ontology.catalog.store.db import db_exists, update_node_property
from auto_ontology.catalog.store.schemas import (
    PendingRows,
    add_fks,
    add_pks,
    add_schemas_edge,
    merge_schema_edges,
    merge_schema_nodes,
)
from auto_ontology.dal.session import write_transaction
from ontological.catalog.data_layer import embed_data_layer
from ontological.embed.embedder import LocalEmbedder
from ontological.profile.run import Profiled, profile_catalog
from ontological.sources.executor import Catalog, Record, SourceExecutor

logger = logging.getLogger(__name__)


class AlreadyIngested(Exception):
    """The store already holds this database's catalog. Updating it needs the diff (M2.3)."""


@dataclass(frozen=True)
class Ingested:
    database_name: str
    database_id: str | None  # None when the source has no tables, and nothing was written
    tables: int
    columns: int
    skipped: list[Record]  # relations the executor couldn't read, which weren't written
    profiled: Profiled | None = None  # None when nothing was written
    data_layer_rows: int = 0  # rows embedded into `data_objects_layer`


def ingest(
    executor: SourceExecutor,
    *,
    embedder: LocalEmbedder | None = None,
    timeout_s: float | None = None,
    probe_distinct_values: bool = True,
) -> Ingested:
    """Extract the executor's source, write its catalog to the store, profile it, and, given an
    embedder, embed its data layer from the profiled catalog."""
    ingested = write_catalog(executor.extract(timeout_s=timeout_s))
    if ingested.database_id is None:
        return ingested
    profiled = profile_catalog(
        executor, ingested.database_name, probe_distinct_values=probe_distinct_values
    )
    rows = embed_data_layer(ingested.database_name, embedder) if embedder is not None else 0
    return replace(ingested, profiled=profiled, data_layer_rows=rows)


def write_catalog(catalog: Catalog) -> Ingested:
    """Write an extracted catalog to the store, as a first load."""
    name = catalog.database_name
    for relation in catalog.skipped:
        logger.warning(
            "%s: skipped %s.%s: %s",
            name,
            relation["table_schema"],
            relation["table_name"],
            relation["error"],
        )
    tables = normalize_tables(pd.DataFrame(catalog.tables))
    columns = normalize_columns(pd.DataFrame(catalog.columns))
    if tables.empty:
        logger.warning("%s has no tables; nothing to write", name)
        return Ingested(name, None, 0, 0, catalog.skipped)

    db_node = CatalogNode(
        name=name, label=Labels.DB, props={"name": name}, match_props={"name": name}
    )
    with write_transaction():
        existing_id, loaded = db_exists(db_node)
        if loaded:
            raise AlreadyIngested(f"the store already holds {name}'s catalog")
        if existing_id is not None:
            db_node.replace_id(existing_id)
        pulled = datetime.now(UTC).replace(microsecond=0)
        for schema_name in tables["table_schema"].unique():
            schema = Schema(
                db_node,
                tables.loc[tables["table_schema"] == schema_name].copy(),
                columns.loc[columns["table_schema"] == schema_name].copy(),
            )
            schema.create_schema_node(schema_name)
            _add_schema(schema, pulled)
        add_fks(pd.DataFrame(catalog.fks), pulled, name)
        add_pks(pd.DataFrame(catalog.pks), name)
        database_id = str(db_node.get_id())
        update_node_property("db", database_id, {"pulled": pulled})
    return Ingested(name, database_id, len(tables), len(columns), catalog.skipped)


def _add_schema(schema: Schema, pulled: datetime) -> None:
    """Upstream's `services/schema.add_schema`: the schema row, then its tables and columns.
    Table and column rows wait in `pending` until the edge pass names their parent."""
    pending = PendingRows()
    add_schemas_edge(schema.get_db_schema_edge(), pulled)
    nodes = [
        {"label": [label], "match_props": node["match_props"], "props": node["props"]}
        for label, listed in (
            (Labels.TABLE, schema.get_table_nodes()),
            (Labels.COLUMN, schema.get_column_nodes()),
        )
        for node in listed
    ]
    merge_schema_nodes(nodes, pulled, pending)
    merge_schema_edges(schema.get_schema_to_tables_edges(), Labels.SCHEMA, Labels.TABLE, pending)
    for edges in schema.get_edges_per_table():
        merge_schema_edges(edges, Labels.TABLE, Labels.COLUMN, pending)
