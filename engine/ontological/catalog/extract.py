# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/catalog/extract.py. It reads a
# source executor's `Catalog` in place of a connector, so there is no table filter (Ontological's
# sources have none) and no query history (M2.4).

"""A source's extract as the frames ingest writes: upstream's step 1 of the catalog write path.

The executor does the reading (`ontological/sources/`). What is left here is upstream's fail-closed
check and the normalizing.
"""

# pandas has no stubs here.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ontological.catalog.normalize import normalize_columns, normalize_tables
from ontological.sources.executor import Catalog


class IncompleteCatalogExtractionError(RuntimeError):
    """A listed relation has no columns."""


@dataclass(frozen=True)
class Extracted:
    database_name: str
    tables: pd.DataFrame  # normalized
    columns: pd.DataFrame  # normalized
    views: pd.DataFrame
    pks: pd.DataFrame
    fks: pd.DataFrame


def relation_keys(frame: pd.DataFrame | None) -> set[tuple[str, str]]:
    """`(schema, table)` for each row that names a table. A missing schema reads as ''."""
    if frame is None or frame.empty:
        return set()
    keys: set[tuple[str, str]] = set()
    for raw_schema, raw_table in zip(frame["table_schema"], frame["table_name"], strict=True):
        schema = "" if pd.isna(raw_schema) else str(raw_schema)
        table = "" if pd.isna(raw_table) else str(raw_table)
        if table:
            keys.add((schema, table))
    return keys


def _validate_relation_column_coverage(tables: pd.DataFrame, columns: pd.DataFrame) -> None:
    """Fail closed when an extract lists relations it couldn't describe.

    A table with zero columns is what a partial extraction looks like from here, and it can't be
    told from a complete one: the catalog lands, retrieval offers the table, and every query
    against it fails. The executor keeps such a relation out of `tables` (it goes in `skipped`),
    so this is the backstop for an extract that gets that wrong.
    """
    listed = relation_keys(tables)
    if not listed:
        return
    missing = sorted(listed - relation_keys(columns))
    if not missing:
        return
    preview = ", ".join(
        f"{schema + '.' if schema else ''}{table}" for schema, table in missing[:10]
    )
    suffix = "" if len(missing) <= 10 else f" (+{len(missing) - 10} more)"
    raise IncompleteCatalogExtractionError(
        f"Catalog extraction listed {len(listed)} relation(s), but "
        f"{len(missing)} had no described columns: {preview}{suffix}"
    )


def create_dataframe(
    catalog: Catalog,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """The extract's raw `tables`, `columns`, `views`, `pks` and `fks` frames, checked for
    coverage."""
    tables = pd.DataFrame(catalog.tables)
    columns = pd.DataFrame(catalog.columns)
    _validate_relation_column_coverage(tables, columns)
    return (
        tables,
        columns,
        pd.DataFrame(catalog.views),
        pd.DataFrame(catalog.pks),
        pd.DataFrame(catalog.fks),
    )


def extract(catalog: Catalog) -> Extracted:
    """The extract, checked and normalized for the write."""
    tables, columns, views, pks, fks = create_dataframe(catalog)
    return Extracted(
        catalog.database_name,
        normalize_tables(tables),
        normalize_columns(columns),
        views,
        pks,
        fks,
    )
