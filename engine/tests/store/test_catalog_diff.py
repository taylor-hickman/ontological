# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/catalog/tests/
# test_incremental_ingest.py and catalog_inspector.py. The source is read through a source
# executor (DuckDB's postgres_scanner), so column types are DuckDB's; the tests after
# "Ontological's" below are new.

"""Incremental re-ingest: what changes in the catalog when the source changes (BUILD_PLAN.md
M2.3, ARCHITECTURE.md §1.2).

The diff decides which tables and columns are added, dropped and updated on every re-ingest, so
a wrong answer either loses catalog entries users were querying or leaves ghosts pointing at
columns that no longer exist. A first load exercises only the empty-store case, where diffing has
nothing to do, and re-ingest is where upstream's bugs were.

Each test owns a throwaway source database in the store's own cluster, so it can add and drop
tables freely. Upstream's `test_newly_denied_table_is_removed_from_the_catalog` isn't ported:
Ontological's sources have no table allow/deny filter (the scope picker scopes compile instead).
"""

# The vendored DAL is untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import text

from auto_ontology.dal.session import store as dal_store
from ontological.catalog import ingest as ingest_mod
from ontological.catalog.ingest import Ingested, ingest
from ontological.sources.executor import PostgresSource, SourceExecutor
from ontological.store import pg


@pytest.fixture
def source_db(store: pg.Layout) -> str:
    """A throwaway source database this test may freely mutate: upstream's `shop` fixture."""
    name = f"difftest_{uuid.uuid4().hex[:12]}"
    pg.psql(store, f'CREATE DATABASE "{name}"', db="postgres")
    _mutate(
        store,
        name,
        "CREATE SCHEMA shop",
        "CREATE TABLE shop.customer ("
        " customer_id integer PRIMARY KEY,"
        " name varchar(100),"
        " email varchar(200))",
        "CREATE TABLE shop.orders ("
        " order_id integer PRIMARY KEY,"
        " customer_id integer REFERENCES shop.customer(customer_id),"
        " total numeric(10,2))",
        "INSERT INTO shop.customer VALUES (1, 'ada', 'a@b.c')",
        "INSERT INTO shop.orders VALUES (1, 1, 9.99)",
    )
    return name


@pytest.fixture
def executor(store: pg.Layout, source_db: str) -> Iterator[SourceExecutor]:
    """One executor for the whole test, as the engine keeps one warm per source: every extract
    has to see the source as it is now."""
    source = PostgresSource(str(store.pgsocket), source_db, pg.USER, port=pg.PORT)
    with SourceExecutor(source) as ex:
        yield ex


def _mutate(store: pg.Layout, database_name: str, *statements: str) -> None:
    for statement in statements:
        pg.psql(store, statement, db=database_name)


# --- the catalog, read back in its own vocabulary (upstream's catalog_inspector.py) ---------------


def _q(sql: str, **params: Any) -> list[dict[str, Any]]:
    return list(dal_store().query_read(text(sql), params))


_UNDER_DB = (
    "JOIN catalog_schema s ON s.id = t.schema_id "
    "JOIN catalog_database d ON d.id = s.database_id WHERE d.name = :name"
)


def tables(database_name: str) -> set[str]:
    rows = _q(f"SELECT t.name AS name FROM catalog_table t {_UNDER_DB}", name=database_name)
    return {r["name"] for r in rows}


def columns(database_name: str, table: str) -> set[str]:
    rows = _q(
        "SELECT c.name AS name FROM catalog_column c JOIN catalog_table t ON t.id = c.table_id "
        f"{_UNDER_DB} AND t.name = :table",
        name=database_name,
        table=table,
    )
    return {r["name"] for r in rows}


def column_property(database_name: str, table: str, column: str, prop: str) -> Any:
    """One column property: a column of catalog_column."""
    rows = _q(
        f"SELECT c.{prop} AS value FROM catalog_column c JOIN catalog_table t ON t.id = c.table_id "
        f"{_UNDER_DB} AND t.name = :table AND c.name = :column",
        name=database_name,
        table=table,
        column=column,
    )
    return rows[0]["value"] if rows else None


def table_property(database_name: str, table: str, prop: str) -> Any:
    rows = _q(
        f"SELECT t.{prop} AS value FROM catalog_table t {_UNDER_DB} AND t.name = :table",
        name=database_name,
        table=table,
    )
    return rows[0]["value"] if rows else None


def entity_counts(database_name: str) -> dict[str, int]:
    """How many of each catalog entity a database holds. A duplicate-creating bug keeps every
    name, so counts are what a no-op re-ingest has to be checked against."""
    row = _q(
        "SELECT"
        " (SELECT count(*) FROM catalog_schema s"
        "   JOIN catalog_database d ON d.id = s.database_id WHERE d.name = :name) AS schemas,"
        f" (SELECT count(*) FROM catalog_table t {_UNDER_DB}) AS tables,"
        " (SELECT count(*) FROM catalog_column c JOIN catalog_table t ON t.id = c.table_id"
        f"   {_UNDER_DB}) AS columns",
        name=database_name,
    )[0]
    return {"Schema": row["schemas"], "Table": row["tables"], "Column": row["columns"]}


def foreign_keys(database_name: str) -> set[tuple[str, str]]:
    """`(source column name, target column name)` for each foreign key."""
    rows = _q(
        "SELECT sc.name AS src, tc.name AS dst FROM column__foreign_key fk "
        "JOIN catalog_column sc ON sc.id = fk.source_column_id "
        "JOIN catalog_column tc ON tc.id = fk.target_column_id "
        f"JOIN catalog_table t ON t.id = sc.table_id {_UNDER_DB}",
        name=database_name,
    )
    return {(r["src"], r["dst"]) for r in rows}


def snapshot(database_name: str) -> list[tuple[Any, ...]]:
    """Every catalog row under the database with the facts the source owns, ids included, and
    its foreign keys: what an unchanged re-ingest must leave exactly as it was."""
    rows = _q(
        "SELECT s.id AS schema_id, s.name AS schema, t.id AS table_id, t.name AS table,"
        " t.table_type, t.pk, c.id AS column_id, c.name AS column, c.data_type, c.is_nullable,"
        " c.ordinal_position, c.description"
        " FROM catalog_column c JOIN catalog_table t ON t.id = c.table_id "
        f"{_UNDER_DB} ORDER BY s.name, t.name, c.ordinal_position",
        name=database_name,
    )
    keys = sorted(foreign_keys(database_name))
    return [tuple(r.values()) for r in rows] + [("fk", *k) for k in keys]


# --- upstream's cases -----------------------------------------------------------------------------


def test_first_ingest_creates_the_catalog(source_db: str, executor: SourceExecutor) -> None:
    ingest(executor)
    assert tables(source_db) == {"customer", "orders"}
    assert columns(source_db, "customer") == {"customer_id", "name", "email"}


def test_reingest_without_changes_is_a_no_op(source_db: str, executor: SourceExecutor) -> None:
    """An unchanged source must not churn. Compared on counts, as upstream does, since a
    duplicate-creating bug keeps every name; and on every row with its id, which a
    delete-and-recreate bug changes."""
    ingest(executor)
    before, rows_before = entity_counts(source_db), snapshot(source_db)

    ingest(executor)

    assert entity_counts(source_db) == before
    assert snapshot(source_db) == rows_before


def test_added_table_appears_with_its_columns(
    store: pg.Layout, source_db: str, executor: SourceExecutor
) -> None:
    ingest(executor)
    _mutate(
        store,
        source_db,
        "CREATE TABLE shop.invoice (invoice_id integer PRIMARY KEY, amount numeric)",
    )
    ingest(executor)

    assert "invoice" in tables(source_db)
    assert columns(source_db, "invoice") == {"invoice_id", "amount"}


def test_dropped_table_is_removed(
    store: pg.Layout, source_db: str, executor: SourceExecutor
) -> None:
    """A ghost table is worse than a missing one: the agent is offered it, and then writes
    queries against a relation that is gone."""
    ingest(executor)
    _mutate(store, source_db, "DROP TABLE shop.orders")
    ingest(executor)

    assert tables(source_db) == {"customer"}


def test_dropped_table_takes_its_columns_with_it(
    store: pg.Layout, source_db: str, executor: SourceExecutor
) -> None:
    ingest(executor)
    _mutate(store, source_db, "DROP TABLE shop.orders")
    ingest(executor)

    assert columns(source_db, "orders") == set()


def test_added_column_appears(store: pg.Layout, source_db: str, executor: SourceExecutor) -> None:
    ingest(executor)
    _mutate(store, source_db, "ALTER TABLE shop.customer ADD COLUMN phone varchar(40)")
    ingest(executor)

    assert "phone" in columns(source_db, "customer")


def test_dropped_column_is_removed(
    store: pg.Layout, source_db: str, executor: SourceExecutor
) -> None:
    ingest(executor)
    _mutate(store, source_db, "ALTER TABLE shop.customer DROP COLUMN email")
    ingest(executor)

    assert columns(source_db, "customer") == {"customer_id", "name"}


def test_renamed_column_is_add_plus_drop(
    store: pg.Layout, source_db: str, executor: SourceExecutor
) -> None:
    """Rename is indistinguishable from drop and add at the catalog level. Worth pinning: any
    curation attached to the old column does not follow the rename."""
    ingest(executor)
    _mutate(store, source_db, "ALTER TABLE shop.customer RENAME COLUMN email TO contact")
    ingest(executor)

    names = columns(source_db, "customer")
    assert "contact" in names
    assert "email" not in names


def test_changed_column_type_is_updated_in_place(
    store: pg.Layout, source_db: str, executor: SourceExecutor
) -> None:
    """The column keeps its identity; only data_type changes. Upstream changes varchar to text,
    which DuckDB reads as VARCHAR both times, so this changes a numeric to a double."""
    ingest(executor)
    before_id = column_property(source_db, "orders", "total", "id")
    assert column_property(source_db, "orders", "total", "data_type") == "DECIMAL(10,2)"

    _mutate(store, source_db, "ALTER TABLE shop.orders ALTER COLUMN total TYPE double precision")
    ingest(executor)

    assert column_property(source_db, "orders", "total", "data_type") == "DOUBLE"
    assert column_property(source_db, "orders", "total", "id") == before_id, (
        "the column was recreated rather than updated; anything referencing its id would dangle"
    )


def test_added_then_dropped_table_leaves_no_trace(
    store: pg.Layout, source_db: str, executor: SourceExecutor
) -> None:
    ingest(executor)
    baseline = entity_counts(source_db)

    _mutate(store, source_db, "CREATE TABLE shop.temp_thing (id integer PRIMARY KEY)")
    ingest(executor)
    assert "temp_thing" in tables(source_db)

    _mutate(store, source_db, "DROP TABLE shop.temp_thing")
    ingest(executor)

    assert "temp_thing" not in tables(source_db)
    assert entity_counts(source_db) == baseline


def test_new_schema_is_picked_up(
    store: pg.Layout, source_db: str, executor: SourceExecutor
) -> None:
    ingest(executor)
    _mutate(
        store,
        source_db,
        "CREATE SCHEMA warehouse",
        "CREATE TABLE warehouse.stock (sku text PRIMARY KEY, qty integer)",
    )
    ingest(executor)

    assert "stock" in tables(source_db)


def test_foreign_key_survives_reingest(source_db: str, executor: SourceExecutor) -> None:
    ingest(executor)
    ingest(executor)

    assert foreign_keys(source_db) == {("customer_id", "customer_id")}


# --- Ontological's --------------------------------------------------------------------------------


def test_dropped_schema_is_removed(
    store: pg.Layout, source_db: str, executor: SourceExecutor
) -> None:
    _mutate(
        store,
        source_db,
        "CREATE SCHEMA warehouse",
        "CREATE TABLE warehouse.stock (sku text PRIMARY KEY, qty integer)",
    )
    ingest(executor)
    _mutate(store, source_db, "DROP SCHEMA warehouse CASCADE")
    ingest(executor)

    assert tables(source_db) == {"customer", "orders"}
    assert entity_counts(source_db)["Schema"] == 1


def test_changed_nullability_is_updated_in_place(
    store: pg.Layout, source_db: str, executor: SourceExecutor
) -> None:
    ingest(executor)
    before_id = column_property(source_db, "customer", "name", "id")
    assert column_property(source_db, "customer", "name", "is_nullable") is True

    _mutate(store, source_db, "ALTER TABLE shop.customer ALTER COLUMN name SET NOT NULL")
    ingest(executor)

    assert column_property(source_db, "customer", "name", "is_nullable") is False
    assert column_property(source_db, "customer", "name", "id") == before_id


def test_a_view_recreated_as_a_table_is_retyped_in_place(
    store: pg.Layout, source_db: str, executor: SourceExecutor
) -> None:
    """A table that changes shape refreshes the facts the source owns. Left as 'view', the agent
    would be told a queryable table is not one."""
    _mutate(store, source_db, "CREATE VIEW shop.big_orders AS SELECT order_id FROM shop.orders")
    ingest(executor)
    before_id = table_property(source_db, "big_orders", "id")
    assert table_property(source_db, "big_orders", "table_type") == "view"

    _mutate(
        store,
        source_db,
        "DROP VIEW shop.big_orders",
        "CREATE TABLE shop.big_orders (order_id integer)",
    )
    ingest(executor)

    assert table_property(source_db, "big_orders", "table_type") == "base table"
    assert table_property(source_db, "big_orders", "id") == before_id


def test_a_dropped_foreign_key_is_swept(
    store: pg.Layout, source_db: str, executor: SourceExecutor
) -> None:
    ingest(executor)
    _mutate(store, source_db, "ALTER TABLE shop.orders DROP CONSTRAINT orders_customer_id_fkey")
    ingest(executor)

    assert foreign_keys(source_db) == set()
    assert columns(source_db, "orders") == {"order_id", "customer_id", "total"}


def test_a_changed_primary_key_is_rewritten(
    store: pg.Layout, source_db: str, executor: SourceExecutor
) -> None:
    ingest(executor)
    assert table_property(source_db, "orders", "pk") == ["order_id"]

    _mutate(
        store,
        source_db,
        "ALTER TABLE shop.orders DROP CONSTRAINT orders_pkey",
        "ALTER TABLE shop.orders ADD PRIMARY KEY (customer_id, order_id)",
    )
    ingest(executor)

    assert table_property(source_db, "orders", "pk") == ["customer_id", "order_id"]
    assert table_property(source_db, "customer", "pk") == ["customer_id"]


def test_curated_descriptions_survive_a_reingest(
    store: pg.Layout, source_db: str, executor: SourceExecutor
) -> None:
    """Descriptions are curated in the UI (ARCHITECTURE.md §1.2). A re-ingest that updates the
    column must not overwrite them with what the source reports, which is usually nothing."""
    ingest(executor)
    table_id = table_property(source_db, "orders", "id")
    column_id = column_property(source_db, "orders", "total", "id")
    dal_store().query_write(
        text("UPDATE catalog_table SET description = 'One sale' WHERE id = :id"), {"id": table_id}
    )
    dal_store().query_write(
        text("UPDATE catalog_column SET description = 'Gross, in USD' WHERE id = :id"),
        {"id": column_id},
    )

    _mutate(store, source_db, "ALTER TABLE shop.orders ALTER COLUMN total TYPE double precision")
    ingest(executor)

    assert table_property(source_db, "orders", "description") == "One sale"
    assert column_property(source_db, "orders", "total", "description") == "Gross, in USD"
    assert column_property(source_db, "orders", "total", "data_type") == "DOUBLE"


def test_mixed_case_names_reingest_as_a_no_op(
    store: pg.Layout, source_db: str, executor: SourceExecutor
) -> None:
    """Upstream keys schemas by lower-cased name and the store by the name as written. A
    mismatch between the two would drop and re-create the schema on every ingest."""
    _mutate(
        store,
        source_db,
        'CREATE SCHEMA "Sales"',
        'CREATE TABLE "Sales"."Invoice" ("InvoiceId" integer PRIMARY KEY, "Total" numeric)',
    )
    ingest(executor)
    before = snapshot(source_db)

    ingest(executor)

    assert snapshot(source_db) == before
    assert columns(source_db, "Invoice") == {"InvoiceId", "Total"}


def test_a_reingest_stamps_pulled(
    store: pg.Layout, source_db: str, executor: SourceExecutor
) -> None:
    """`pulled` is when the catalog was last read from the source; the staleness check on engine
    start reads it (M2.6)."""
    ingest(executor)
    dal_store().query_write(
        text("UPDATE catalog_database SET pulled = '2000-01-01T00:00:00Z' WHERE name = :name"),
        {"name": source_db},
    )

    ingest(executor)

    pulled = _q("SELECT pulled FROM catalog_database WHERE name = :name", name=source_db)[0]
    assert pulled["pulled"].year > 2000


def test_a_failed_reingest_leaves_the_previous_catalog(
    store: pg.Layout,
    source_db: str,
    executor: SourceExecutor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The re-ingest is one transaction, as the first load is: a failure part way leaves the
    catalog as the last ingest wrote it, not half-diffed."""
    ingest(executor)
    before = snapshot(source_db)
    _mutate(
        store,
        source_db,
        "DROP TABLE shop.orders",
        "ALTER TABLE shop.customer ADD COLUMN phone varchar(40)",
        "CREATE TABLE shop.invoice (invoice_id integer PRIMARY KEY)",
    )

    def fail(*_: object) -> None:
        raise RuntimeError("the store went away")

    with monkeypatch.context() as patch:
        patch.setattr(ingest_mod, "add_pks", fail)  # the last write before `pulled`
        with pytest.raises(RuntimeError, match="went away"):
            ingest(executor)

    assert snapshot(source_db) == before
    got: Ingested = ingest(executor)
    assert (got.tables, tables(source_db)) == (2, {"customer", "invoice"})
