"""A source spec as the JSON the store keeps (`spec.to_record`, `spec.from_record`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ontological.sources.keychain import KeychainRef
from ontological.sources.spec import (
    DuckDBSource,
    FileSource,
    MySQLSource,
    PostgresSource,
    Source,
    SqliteSource,
    from_record,
    to_record,
)

SOURCES: list[Source] = [
    SqliteSource(Path("/data/chinook.sqlite")),
    DuckDBSource(Path("/data/warehouse.duckdb")),
    FileSource.of("/data/sales/orders.parquet", "/data/sales/items.csv.gz"),
    PostgresSource("/tmp/pgsocket", "wwi", "reader", 5433, KeychainRef("wwi-reader")),
    PostgresSource("db.local", "shop", "app"),
    MySQLSource("127.0.0.1", "shop", "root", password=KeychainRef("shop-root")),
]


@pytest.mark.parametrize("source", SOURCES, ids=lambda s: type(s).__name__)
def test_a_source_survives_its_record(source: Source) -> None:
    assert from_record(json.loads(json.dumps(to_record(source)))) == source


def test_a_record_holds_the_keychain_reference_not_a_password() -> None:
    record = to_record(PostgresSource("h", "d", "u", password=KeychainRef("acct")))
    assert record["password"] == "acct"


@pytest.mark.parametrize(
    "record", [{}, {"kind": "oracle"}, {"kind": "sqlite"}, {"kind": "files", "name": "x"}]
)
def test_a_record_it_cant_read_is_a_value_error(record: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="not a source record"):
        from_record(record)
