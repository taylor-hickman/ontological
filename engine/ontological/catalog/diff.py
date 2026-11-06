# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/catalog/diff.py. The diff is
# upstream's; the code is reorganized into one function per kind of change, and a failure keeps
# its type, chained, instead of becoming a bare Exception.

"""What changed in one schema since the last ingest, and the store writes that apply it.

Pandas over two frames, one read back from the store and one built from a fresh extract, plus calls
back into upstream's vendored catalog store to apply the result:

- tables only in the extract are added with their columns; tables only in the store are deleted,
  and their columns go with them by cascade;
- tables in both get the facts the source owns refreshed (`table_type`). Their `description` is
  curated, and survives;
- columns are matched on schema, table and name. Those only in the store are deleted, those only in
  the extract are added, and those in both whose `data_type` or `is_nullable` changed are updated
  in place, keeping their id and any curated description. A rename is a drop and an add.

The store is imported inside the functions, as upstream does: `catalog/store/db.py` re-exports this
module's functions, so a top-level import would close a circle.
"""

# The vendored catalog store and model are untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
# pyright: reportUnknownParameterType=false, reportMissingParameterType=false

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import pandas as pd

from auto_ontology.catalog.constants import Labels
from auto_ontology.catalog.model.schema import Schema
from ontological.catalog.normalize import chunks

logger = logging.getLogger(__name__)

# Upstream once merged on ["database", "schema", "table_name", "column_name"]: neither frame has a
# "schema" column, so every merge raised and the column diff never ran. A schema belongs to one
# database, so the database isn't needed either.
_COLUMN_MERGE_KEYS = ["table_schema", "table_name", "column_name"]
# What the source owns, and a re-ingest refreshes. `description` is curated, and isn't here.
_TABLE_PROPS = ("table_type", "pk")
_COLUMN_PROPS = ("data_type", "is_nullable")
_UPDATE_BATCH = 1000


def accumulate_added_column_props(
    added_column: Any, edges_to_add: list[dict[str, Any]], new_schema: Schema
) -> None:
    """The table-to-column edge that adds `added_column`, a row of the column merge."""
    edges_to_add.append(
        {
            "from_label": Labels.TABLE,
            "from_identProps": new_schema.get_table_node_match_props(added_column.table_name),
            "v_props": new_schema.get_table_node_props(added_column.table_name),
            "to_label": Labels.COLUMN,
            "to_identProps": added_column.match_props_files,
            "u_props": added_column.props_files,
            "optional_edge_props": {},
        }
    )


def accumulate_updated_column(
    column_in_intersection: Any, items_to_update_in_graph: list[dict[str, Any]], new_schema: Schema
) -> None:
    """The update that gives a stored column, under its stored id, the extract's props."""
    column_id = column_in_intersection.props_graph["id"]
    props = {**column_in_intersection.props_files, "id": column_id}
    items_to_update_in_graph.append({"id": column_id, "label": Labels.COLUMN, "props": props})


def update_diff_from_existing_schema(new_schema: Schema, latest_timestamp: datetime) -> None:
    """Bring the stored copy of `new_schema`'s schema in line with it. A schema the store doesn't
    hold is left alone: adding one is the caller's job."""
    from auto_ontology.catalog.store.schemas import load_schema_from_graph

    schema_name = new_schema.get_schema_name()
    try:
        existing = load_schema_from_graph(new_schema.get_database_name(), schema_name)
        if existing is None:
            return
        old_tables = set(existing.tables_df.table_name.unique())
        new_tables = set(new_schema.tables_df.table_name.unique())
        _add_tables(existing, new_schema, new_tables - old_tables, latest_timestamp)
        _refresh_tables(existing, new_schema, new_tables & old_tables)
        _delete_tables(existing, old_tables - new_tables)
        _diff_columns(existing, new_schema, latest_timestamp)
    except Exception as err:
        raise RuntimeError(f"diffing schema {schema_name!r}: {err}") from err


def _add_tables(
    existing: Schema, new_schema: Schema, names: set[str], latest_timestamp: datetime
) -> None:
    from auto_ontology.catalog.store.schemas import add_schemas_edge

    logger.info("Tables to add in schema %s: %d", existing.get_schema_name(), len(names))
    schema_node = existing.get_schema_node()
    for table_name in names:
        table_node = new_schema.get_table_node(table_name)
        add_schemas_edge([schema_node, table_node, {}], latest_timestamp)
        for column_name in new_schema.get_table_columns_by_table_name(table_name):
            column_node = new_schema.get_column_node(column_name, table_name)
            add_schemas_edge([table_node, column_node, {}], latest_timestamp)


def _refresh_tables(existing: Schema, new_schema: Schema, names: set[str]) -> None:
    """Upstream once handled only additions and deletions, so a table that changed shape kept its
    first values: a view dropped and re-created as a table still read 'view'."""
    from auto_ontology.catalog.store.db import update_properties_in_graph_batch

    updates: list[dict[str, Any]] = []
    for table_name in names:
        old = existing.get_table_node_props(table_name)
        new = new_schema.get_table_node_props(table_name)
        changed = {k: new.get(k) for k in _TABLE_PROPS if k in new and new.get(k) != old.get(k)}
        if changed:
            updates.append({"id": old["id"], "label": Labels.TABLE, "props": changed})
    if updates:
        logger.info("Tables to update in schema %s: %d", existing.get_schema_name(), len(updates))
        update_properties_in_graph_batch(updates)


def _delete_tables(existing: Schema, names: set[str]) -> None:
    from auto_ontology.catalog.store.db import delete_table

    logger.info("Tables to delete in schema %s: %d", existing.get_schema_name(), len(names))
    for table_name in names:
        delete_table(existing.get_table_node_props(table_name)["id"])


def _diff_columns(existing: Schema, new_schema: Schema, latest_timestamp: datetime) -> None:
    from auto_ontology.catalog.store.db import (
        add_schemas_edge_batch,
        delete_columns_batch,
        update_properties_in_graph_batch,
    )

    schema_name = existing.get_schema_name()

    def merge(how: str) -> pd.DataFrame:
        return pd.merge(
            existing.columns_df,
            new_schema.columns_df,
            on=_COLUMN_MERGE_KEYS,
            how=how,  # pyright: ignore[reportArgumentType]
            suffixes=("_graph", "_files"),
        )

    # In the store, not the extract. A dropped table's columns are among them, already gone by
    # cascade, so deleting them again does nothing.
    left = merge("left")
    deleted = left.loc[left["column_name_lower_files"].isnull()]
    logger.info("Columns to delete in schema %s: %d", schema_name, len(deleted))
    delete_columns_batch([props["id"] for props in deleted["props_graph"]])

    # In the extract, not the store. An added table's columns are among them, already added with
    # it; the edge is an upsert by id, so adding them again does nothing.
    right = merge("right")
    added = right.loc[right["column_name_lower_graph"].isnull()]
    logger.info("Columns to add in schema %s: %d", schema_name, len(added))
    edges: list[dict[str, Any]] = []
    for row in added.itertuples(index=False):
        accumulate_added_column_props(row, edges, new_schema)
    add_schemas_edge_batch(edges, created=latest_timestamp)

    # In both, with a source-owned fact changed.
    both = merge("inner")
    changed = pd.Series(False, index=both.index)
    for prop in _COLUMN_PROPS:
        changed |= both[f"{prop}_graph"].astype(str) != both[f"{prop}_files"].astype(str)
    updates: list[dict[str, Any]] = []
    for row in both.loc[changed].itertuples(index=False):
        accumulate_updated_column(row, updates, new_schema)
    batches = list(chunks(updates, _UPDATE_BATCH))
    for i, batch in enumerate(batches, 1):
        logger.info("Updating columns in schema %s: batch %d/%d", schema_name, i, len(batches))
        update_properties_in_graph_batch(batch)
