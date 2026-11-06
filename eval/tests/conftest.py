"""Fixtures shared by the tests that drive the bundled Postgres: the eval cluster's and the eval
store's. They need ONTOLOGICAL_PG_BIN, the bundled tree's bin/ (engine/README.md says how to get
one), and skip without it unless ONTOLOGICAL_PG_REQUIRED=1, as the store's lifecycle tests do.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from ontological.store import pg

from ontological_eval import cluster
from ontological_eval.home import app_data_dir, wwi_dir

DDL = {
    "01_schemas.sql": "CREATE SCHEMA IF NOT EXISTS Sales;",
    "02_sequences.sql": "CREATE SEQUENCE IF NOT EXISTS Sales.OrderID START WITH 3;",
    "03_tables.sql": """
        CREATE TABLE Sales.Orders (OrderID integer NOT NULL, Comments text, PRIMARY KEY (OrderID));
        CREATE TABLE Sales.OrderLines (OrderLineID integer NOT NULL, OrderID integer NOT NULL);
    """,
    "04_indexes.sql": "CREATE INDEX ix_lines_order ON Sales.OrderLines (OrderID);",
    "05_fkeys.sql": """ALTER TABLE Sales.OrderLines ADD CONSTRAINT fk_order
        FOREIGN KEY (OrderID) REFERENCES Sales.Orders (OrderID);""",
    "06_views.sql": "CREATE VIEW Sales.OrderSizes AS SELECT OrderID, count(*) AS n "
    "FROM Sales.OrderLines GROUP BY OrderID;",
}
# A quoted newline, a quote, a NULL as \N, and a field over the csv module's default limit.
ORDERS = 'OrderID,Comments\n1,"two\nlines, ""quoted"""\n2,\\N\n3,' + "x" * 200_000 + "\n"
LINES = "OrderLineID,OrderID\n10,1\n11,1\n12,2\n"


@pytest.fixture(scope="session")
def pg_bin() -> Path:
    value = os.environ.get("ONTOLOGICAL_PG_BIN")
    if not value:
        if os.environ.get("ONTOLOGICAL_PG_REQUIRED") == "1":
            pytest.fail("ONTOLOGICAL_PG_BIN is not set and ONTOLOGICAL_PG_REQUIRED=1")
        pytest.skip("set ONTOLOGICAL_PG_BIN to the bundled Postgres's bin/ directory")
    return Path(value).resolve()


@pytest.fixture
def home(pg_bin: Path) -> Iterator[Path]:
    """An eval home with the WWI-shaped dataset in it, under a short root with a space in its
    path. The eval cluster and the eval store are stopped afterwards."""
    root = Path(tempfile.mkdtemp(prefix="ev-", dir="/tmp"))
    home = root / "Eval Home"
    wwi = wwi_dir(home)
    (wwi / "ddl").mkdir(parents=True)
    (wwi / "data").mkdir()
    for name, sql in DDL.items():
        (wwi / "ddl" / name).write_text(sql)
    (wwi / "data" / "Sales.Orders.csv").write_text(ORDERS)
    (wwi / "data" / "Sales.OrderLines.csv").write_text(LINES)
    try:
        yield home
    finally:
        cluster.down(home, pg_bin)
        pg.stop(pg.Layout(app_data_dir(home), pg_bin), quit_dump=False)
        shutil.rmtree(root, ignore_errors=True)
