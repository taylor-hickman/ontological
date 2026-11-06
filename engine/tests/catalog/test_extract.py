# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/catalog/tests/test_extract.py to
# test ontological.catalog.extract, which reads a source executor's Catalog, not a connector.

"""Fail-closed catalog extraction (BUILD_PLAN.md M2.3).

The executor already keeps an undescribable relation out of `tables` and puts it in `skipped`
(`ontological/sources/session.py`). This is upstream's backstop for an extract that gets that
wrong: a listed relation with no columns fails before anything is written.
"""

from __future__ import annotations

import pandas as pd
import pytest

from ontological.catalog.extract import (
    IncompleteCatalogExtractionError,
    create_dataframe,
    extract,
    relation_keys,
)
from ontological.sources.executor import Catalog, Record


def _catalog(
    tables: list[Record],
    columns: list[Record],
    *,
    pks: list[Record] | None = None,
    fks: list[Record] | None = None,
) -> Catalog:
    return Catalog(
        database_name="db",
        tables=tables,
        columns=columns,
        views=[],
        pks=pks or [],
        fks=fks or [],
        skipped=[],
    )


def test_empty_catalog_is_valid() -> None:
    tables, columns, *_rest = create_dataframe(_catalog([], []))

    assert tables.empty
    assert columns.empty


def test_nullable_relation_identifiers_are_normalized_without_boolean_coercion() -> None:
    frame = pd.DataFrame(
        [
            {"table_schema": pd.NA, "table_name": "orders"},
            {"table_schema": "sales", "table_name": pd.NA},
        ]
    )

    assert relation_keys(frame) == {("", "orders")}


def test_every_listed_relation_with_columns_is_valid() -> None:
    catalog = _catalog(
        [
            {"table_schema": "sales", "table_name": "orders"},
            {"table_schema": "sales", "table_name": "customers"},
        ],
        [
            {"table_schema": "sales", "table_name": "orders", "column_name": "id"},
            {"table_schema": "sales", "table_name": "customers", "column_name": "id"},
        ],
    )

    tables, columns, *_rest = create_dataframe(catalog)

    assert len(tables) == 2
    assert len(columns) == 2


def test_listed_relation_without_columns_fails_before_catalog_write() -> None:
    catalog = _catalog(
        [
            {"table_schema": "sales", "table_name": "orders"},
            {"table_schema": "private", "table_name": "customers"},
        ],
        [{"table_schema": "sales", "table_name": "orders", "column_name": "id"}],
    )

    with pytest.raises(IncompleteCatalogExtractionError) as exc_info:
        create_dataframe(catalog)

    message = str(exc_info.value)
    assert "2 relation(s)" in message
    assert "1 had no described columns" in message
    assert "private.customers" in message


def test_extract_normalizes_the_frames_and_keeps_the_keys() -> None:
    catalog = _catalog(
        [{"table_schema": "sales", "table_name": "orders", "table_type": "base table"}],
        [
            {"table_schema": "sales", "table_name": "orders", "column_name": "id",
             "data_type": "INTEGER", "is_nullable": False, "ordinal_position": 1},
            {"table_schema": "sales", "table_name": "orders", "column_name": "note",
             "data_type": "VARCHAR", "is_nullable": True, "ordinal_position": 2},
        ],  # fmt: skip
        pks=[{"table_schema": "sales", "table_name": "orders", "column_name": "id",
              "ordinal_position": 1}],  # fmt: skip
    )

    got = extract(catalog)

    assert got.database_name == "db"
    assert got.tables["table_name"].tolist() == ["orders"]
    assert got.columns["is_nullable"].dtype == "boolean"
    assert got.columns["is_nullable"].tolist() == [False, True]
    assert got.columns["ordinal_position"].dtype == "Int16"
    assert got.pks["column_name"].tolist() == ["id"]
    assert got.fks.empty
