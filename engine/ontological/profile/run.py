# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: the engine's half of upstream's profiling in
# auto_ontology/semantic/visit_enter.py. `process_table`'s sampling step and the store writes at the
# end of `calculate_columns_profiling` run here, against the executor's `profile` op, and the
# sampling circuit breaker is per run, not per process.

"""Profile a database's catalog through its source executor, and write what it finds to the
store (ARCHITECTURE.md §1.3 stages 1 and 2, §5.6).

Each table is profiled in the executor's child (`SourceExecutor.profile`), which sends back each
column's sample values, uniqueness and inferred date format. They are written to the catalog's
columns through upstream's DAL, as upstream's profiling wrote them.
"""

# The vendored DAL is untyped.
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from auto_ontology.dal import schema as s
from auto_ontology.dal.datasources import (
    store_column_date_formats,
    store_column_sample_values,
    store_column_uniqueness,
)
from auto_ontology.dal.session import store
from ontological.sources.executor import (
    PROFILE_TIMEOUT_S,
    Cancelled,
    Record,
    SourceError,
    SourceExecutor,
    SourceQueryError,
)

logger = logging.getLogger(__name__)

# Consecutive failed samples after which sampling is abandoned for the run.
MAX_CONSECUTIVE_SAMPLE_FAILURES = 3


@dataclass(frozen=True)
class CatalogTable:
    id: str
    schema: str
    name: str
    columns: list[Record]  # name, data_type


@dataclass
class Profiled:
    tables: int = 0  # profiled and written
    failed: list[Record] = field(default_factory=list[Record])  # schema, table, error
    skipped: int = 0  # not sampled, after sampling was abandoned
    sampling_disabled: bool = False


def catalog_tables(database_name: str) -> list[CatalogTable]:
    """The store's tables for `database_name`, each with its columns in ordinal order."""
    rows = store().query_read(
        select(
            s.catalog_table.c.id,
            s.catalog_schema.c.name.label("schema_name"),
            s.catalog_table.c.name.label("table_name"),
            s.catalog_column.c.name.label("column_name"),
            s.catalog_column.c.data_type,
        )
        .select_from(
            s.catalog_database.join(
                s.catalog_schema, s.catalog_schema.c.database_id == s.catalog_database.c.id
            )
            .join(s.catalog_table, s.catalog_table.c.schema_id == s.catalog_schema.c.id)
            .join(s.catalog_column, s.catalog_column.c.table_id == s.catalog_table.c.id)
        )
        .where(s.catalog_database.c.name == database_name)
        .order_by(
            s.catalog_schema.c.name,
            s.catalog_table.c.name,
            s.catalog_column.c.ordinal_position,
            s.catalog_column.c.id,
        )
    )
    tables: dict[str, CatalogTable] = {}
    columns: defaultdict[str, list[Record]] = defaultdict(list)
    for row in rows:
        table_id = str(row["id"])
        if table_id not in tables:
            tables[table_id] = CatalogTable(
                table_id, row["schema_name"], row["table_name"], columns[table_id]
            )
        columns[table_id].append({"name": row["column_name"], "data_type": row["data_type"]})
    return list(tables.values())


def profile_catalog(
    executor: SourceExecutor,
    database_name: str,
    *,
    probe_distinct_values: bool = True,
    timeout_s: float = PROFILE_TIMEOUT_S,
) -> Profiled:
    """Profile every table of `database_name` through `executor`, and write the sample values,
    uniqueness and date formats to the store.

    A table the source refuses is logged and left out. A sample that times out, or a source that
    can't be reached, counts against the source: after `MAX_CONSECUTIVE_SAMPLE_FAILURES` in a row
    the rest of the run skips sampling and says so once, since a stalled source fails every
    table the same way, a timeout at a time. `Cancelled` is the caller's, and propagates.
    """
    result = Profiled()
    failures = 0
    for table in catalog_tables(database_name):
        if result.sampling_disabled:
            result.skipped += 1
            continue
        try:
            profile = executor.profile(
                table.schema,
                table.name,
                table.columns,
                probe_distinct_values=probe_distinct_values,
                timeout_s=timeout_s,
            )
        except Cancelled:
            raise
        except SourceQueryError as e:
            # Says nothing about the source as a whole: a permission, a type DuckDB can't read.
            logger.warning("[%s] column profiling query failed — skipping: %s", table.name, e)
            result.failed.append(_failure(table, e))
            continue
        except SourceError as e:  # a timeout, a dead child, a source that won't open
            logger.warning("[%s] column profiling failed — skipping: %s", table.name, e)
            result.failed.append(_failure(table, e))
            failures += 1
            if failures >= MAX_CONSECUTIVE_SAMPLE_FAILURES:
                result.sampling_disabled = True
                logger.warning(
                    "Value sampling disabled for %s — %d consecutive sample queries timed out or "
                    "could not reach the source. Profiling continues without sample values.",
                    database_name,
                    MAX_CONSECUTIVE_SAMPLE_FAILURES,
                )
            continue
        failures = 0
        store_column_sample_values(table.id, profile["sample_values"])
        store_column_uniqueness(table.id, profile["uniqueness"])
        store_column_date_formats(table.id, profile["date_formats"])
        result.tables += 1
    return result


def _failure(table: CatalogTable, error: Exception) -> dict[str, Any]:
    return {"table_schema": table.schema, "table_name": table.name, "error": str(error)}
