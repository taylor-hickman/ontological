# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/catalog/ingest.py, write.py,
# parsers/schemas_parser.py and services/schema.py. It reads a source executor's extract in place
# of a connector, and writes sequentially and in one transaction. No query history (M2.4).

"""Ingest: a source's catalog, extracted by its executor and written through the DAL.

Upstream's `write.populate_db`, then `populate_fks` and `populate_pks`, over upstream's vendored
catalog store. A database the store doesn't hold yet is loaded whole. One it holds is diffed, schema
by schema (`diff.py`): new schemas are added, schemas gone from the source are deleted, and the rest
are brought in line, keeping every unchanged row's id. Foreign keys are upserted with this ingest's
`last_seen` and the ones it didn't see are swept; primary keys are reset and written again.
An ingest through an executor also keeps the executor's source on the database row
(`catalog_database.connection`), so the staleness check can open it again (`jobs/staleness.py`).

Upstream writes each schema, the keys and the `pulled` stamp in separate transactions, and runs
the schema diffs on a thread pool. A failure part way leaves a half-written catalog until the next
ingest repairs it. Here the whole write is one transaction: the catalog lands complete, or stays as
the last ingest left it.
"""

# The vendored catalog store and model are untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pandas as pd
from sqlalchemy import update

from auto_ontology.catalog.constants import Labels
from auto_ontology.catalog.model.node import CatalogNode
from auto_ontology.catalog.model.schema import Schema
from auto_ontology.catalog.store.db import db_exists, delete_schema, update_node_property
from auto_ontology.catalog.store.schemas import (
    PendingRows,
    add_fks,
    add_pks,
    add_schemas_edge,
    delete_old_fks,
    get_column_ids,
    get_schemas_ids_and_names,
    get_table_ids,
    merge_schema_edges,
    merge_schema_nodes,
    reset_pks,
)
from auto_ontology.dal import schema as s
from auto_ontology.dal.session import store, write_transaction
from ontological.catalog.diff import update_diff_from_existing_schema
from ontological.catalog.extract import extract
from ontological.sources.executor import Catalog, Record, SourceExecutor
from ontological.sources.spec import Source, to_record

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Ingested:
    database_name: str
    database_id: str | None  # None when the source has no tables, and nothing was written
    tables: int
    columns: int
    skipped: list[Record]  # relations the executor couldn't read, which weren't written
    # `schema.table` for each table the store didn't hold before: every table, on a first load
    new_tables: list[str] = field(default_factory=list[str])


def ingest(executor: SourceExecutor, *, timeout_s: float | None = None) -> Ingested:
    """Extract the executor's source and write its catalog to the store."""
    return write_catalog(executor.extract(timeout_s=timeout_s), source=executor.source)


def write_catalog(catalog: Catalog, *, source: Source | None = None) -> Ingested:
    """Write an extracted catalog to the store: a first load, or a diff against what it holds.
    With `source`, the database row keeps it, replacing any it kept before."""
    name = catalog.database_name
    for relation in catalog.skipped:
        logger.warning(
            "%s: skipped %s.%s: %s",
            name,
            relation["table_schema"],
            relation["table_name"],
            relation["error"],
        )
    data = extract(catalog)
    if data.tables.empty:
        logger.warning("%s has no tables; nothing to write", name)
        return Ingested(name, None, 0, 0, catalog.skipped)

    with write_transaction():
        new_tables = _new_tables(data.tables, name)
        schemas, db_node = _parse(data.tables, data.columns, name)
        existing_id, loaded = db_exists(db_node)
        if existing_id is not None:
            db_node.replace_id(existing_id)
        pulled = datetime.now(UTC).replace(microsecond=0)
        if loaded:
            _update_schemas(schemas, str(existing_id), pulled)
        else:
            for schema in schemas.values():
                _add_schema(schema, pulled)
        # Upstream's own clock for `last_seen`, finer than `pulled`: two ingests in one second
        # would otherwise share it, and the second couldn't sweep what the first saw.
        last_seen = datetime.now(UTC)
        add_fks(data.fks, last_seen, name)
        delete_old_fks(last_seen, name)
        reset_pks(name)
        add_pks(data.pks, name)
        database_id = str(db_node.get_id())
        update_node_property("db", database_id, {"pulled": pulled})
        if source is not None:
            store().query_write(
                update(s.catalog_database)
                .where(s.catalog_database.c.id == database_id)
                .values(connection=to_record(source))
            )
    return Ingested(
        name, database_id, len(data.tables), len(data.columns), catalog.skipped, new_tables
    )


def _new_tables(tables: pd.DataFrame, database_name: str) -> list[str]:
    """`schema.table` for each extracted table the store holds no row for."""
    known = get_table_ids(tables, database_name)
    new = known if "id" not in known else known[known["id"].isna()]
    return sorted(
        f"{schema}.{table}"
        for schema, table in zip(new["table_schema"], new["table_name"], strict=True)
    )


def _parse(
    tables: pd.DataFrame, columns: pd.DataFrame, database_name: str
) -> tuple[dict[str, Schema], CatalogNode]:
    """Upstream's `schemas_parser.parse_df`: one `Schema` per schema, keyed by its lower-cased
    name, with every table and column the store already holds under its stored id."""
    db_node = CatalogNode(
        name=database_name,
        label=Labels.DB,
        props={"name": database_name},
        match_props={"name": database_name},
    )
    tables = get_table_ids(tables, database_name)
    columns = get_column_ids(columns, database_name)
    schemas: dict[str, Schema] = {}
    for schema_name in tables["table_schema"].unique():
        schema = Schema(
            db_node,
            tables.loc[tables["table_schema"] == schema_name].copy(),
            columns.loc[columns["table_schema"] == schema_name].copy(),
        )
        schema.create_schema_node(schema_name)
        schemas[str(schema_name).lower()] = schema
    return schemas, db_node


def _update_schemas(schemas: dict[str, Schema], database_id: str, pulled: datetime) -> None:
    """Upstream's `populate_db` for a database the store holds: add, diff and delete schemas."""
    stored = {
        row["schema_name"].lower(): row["schema_id"]
        for row in get_schemas_ids_and_names(database_id)
    }
    for key, schema in schemas.items():
        if key in stored:
            update_diff_from_existing_schema(schema, pulled)
            logger.info("Updated schema %s.", schema.get_schema_name())
        else:
            _add_schema(schema, pulled)
            logger.info("Added schema %s.", schema.get_schema_name())
    for key, schema_id in stored.items():
        if key not in schemas:
            logger.info("Deleting schema %s.", key)
            delete_schema(schema_id)


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
