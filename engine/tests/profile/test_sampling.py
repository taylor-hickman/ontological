# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: upstream's profiling tests from auto_ontology/semantic/tests/
# test_visit_enter.py, against the port in ontological/profile/sampling.py. A recording fake
# stands in for MagicMock connectors, and the assertions on the mocked store writes read the
# returned profile instead. The breaker's tests moved to tests/store/test_profile_store.py with
# the breaker, and process_table's wait for the compile port.

# The frames are pandas', which pyright can't see into.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

"""Upstream's column profiling, against a fake connector."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal

import duckdb
import numpy as np
import pandas as pd
import pytest

from ontological.profile.sampling import (
    _json_ready_sample,  # pyright: ignore[reportPrivateUsage]
    _keep_persisted_sample,  # pyright: ignore[reportPrivateUsage]
    _sample_key,  # pyright: ignore[reportPrivateUsage]
    calculate_columns_profiling,
)

Frames = Callable[[str], pd.DataFrame]


class FakeConnector:
    """Qualifies and quotes as DuckDB does, records every query, and answers from `frames`: a
    callable from the SQL, or a list served in order."""

    def __init__(self, frames: Frames | list[pd.DataFrame] | pd.DataFrame) -> None:
        self.executed: list[str] = []
        if isinstance(frames, pd.DataFrame):
            frame = frames
            self._frames: Frames = lambda _sql: frame
        elif isinstance(frames, list):
            queue = list(frames)
            self._frames = lambda _sql: queue.pop(0)
        else:
            self._frames = frames

    def qualify(self, schema_name: str | None, table_name: str) -> str:
        return f'"src"."{schema_name}"."{table_name}"'

    def quote(self, name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def execute(self, sql: str) -> pd.DataFrame:
        self.executed.append(sql)
        return self._frames(sql)


def distinct_from(df: pd.DataFrame) -> Frames:
    """The table for the row sample, and the one queried column for a DISTINCT probe: a real
    source returns only that column, and `df.iloc[:, 0]` would otherwise read the first."""

    def answer(sql: str) -> pd.DataFrame:
        match = re.search(r'SELECT DISTINCT "([^"]+)"', sql)
        if not match:
            return df
        col = match.group(1)
        return pd.DataFrame({col: df[col].dropna().unique().tolist()})

    return answer


TABLE = {"name": "orders", "schema_name": "public"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (np.int64(7), 7),
        (np.float64(1.5), 1.5),
        (np.bool_(True), True),
        (Decimal("10"), 10),
        (Decimal("10.5"), 10.5),
        (datetime(2020, 1, 2, 3, 4, 5), "2020-01-02T03:04:05"),
        (date(2020, 1, 2), "2020-01-02"),
        (b"bytes", "bytes"),
        (["a", np.int64(1)], ["a", 1]),
        ({"k": Decimal("2")}, {"k": 2}),
        ("plain", "plain"),
        (None, None),
    ],
)
def test_json_ready_sample_converts_driver_types(raw: object, expected: object) -> None:
    converted = _json_ready_sample(raw)
    assert converted == expected
    # numpy scalars compare equal to their Python counterparts, so identity of
    # the type is the only thing that shows the unwrap actually happened.
    assert type(converted) is type(expected)


def test_sample_key_separates_values_a_str_key_would_merge() -> None:
    # str() would render all three as "1"/"True" collisions across types.
    assert len({_sample_key(1), _sample_key("1"), _sample_key(True)}) == 3
    # Unhashable values still get a key, and equal ones share it.
    assert _sample_key(["a", "b"]) == _sample_key(["a", "b"])
    assert _sample_key({"k": 1}) != _sample_key({"k": 2})


def test_keep_persisted_sample_caps_text_only() -> None:
    assert _keep_persisted_sample("short") is True
    assert _keep_persisted_sample("x" * 31) is False
    assert _keep_persisted_sample(10**40) is True
    assert _keep_persisted_sample(1.5) is True
    assert _keep_persisted_sample(False) is True
    # Non-scalars are judged by how long they render.
    assert _keep_persisted_sample(["a", "b"]) is True
    assert _keep_persisted_sample(["x" * 40]) is False


def test_calculate_columns_profiling_quotes_name_with_space() -> None:
    # Unquoted "main.Sales Orders" parses as table main.Sales, so the probe
    # raised "no such table" and every column of the table was dropped.
    connector = FakeConnector(pd.DataFrame({"OrderDate": ["5/31/18", "6/1/18", "12/4/18"]}))

    result = calculate_columns_profiling(
        {"name": "Sales Orders", "schema_name": "main"},
        [{"name": "OrderDate", "data_type": "TEXT"}],
        connector,
    )

    assert '"main"."Sales Orders"' in connector.executed[0]
    assert result["columns"]["OrderDate"]["format"] == "M/D/YY"


def test_profiling_probe_uses_the_connectors_qualification() -> None:
    connector = FakeConnector(pd.DataFrame({"id": [1, 2, 3]}))

    calculate_columns_profiling(
        {"name": "tbl_Click", "schema_name": "nvapp_client"},
        [{"name": "id", "data_type": "BIGINT"}],
        connector,
    )

    assert connector.executed == ['SELECT * FROM "src"."nvapp_client"."tbl_Click" LIMIT 1000']


def test_calculate_columns_profiling_unhashable_values() -> None:
    # Postgres array / JSON columns come back as Python lists/dicts, which are
    # unhashable — profiling must not crash on them.
    df = pd.DataFrame(
        {"id": [1, 2], "tags": [["a", "b"], ["a", "b"]], "meta": [{"k": 1}, {"k": 2}]}
    )
    columns = [
        {"name": "id", "data_type": "integer"},
        {"name": "tags", "data_type": "ARRAY"},
        {"name": "meta", "data_type": "jsonb"},
    ]

    result = calculate_columns_profiling(TABLE, columns, FakeConnector(df))

    profiled = result["columns"]
    assert set(profiled) == {"id", "tags", "meta"}
    assert profiled["tags"]["is_unique"] is False  # ["a","b"] repeated
    assert profiled["meta"]["is_unique"] is True
    assert profiled["id"]["is_unique"] is True

    # Containers stay containers rather than being flattened to their repr.
    assert profiled["tags"]["sample_values"] == [["a", "b"]]
    assert profiled["meta"]["sample_values"] == [{"k": 1}, {"k": 2}]

    stored = result["sample_values"]
    assert stored["id"] == [1, 2]
    assert stored["tags"] == [["a", "b"]]


def test_calculate_columns_profiling_text_typed_array_column() -> None:
    """A ClickHouse ``Array(String)`` column must not abort the table.

    The declared-type gate matches on substrings, so ``Array(String)`` counts
    as text and its values reach the date-shape scan — but they are lists, not
    strings. That used to raise TypeError out of ``re.match``.
    """
    df = pd.DataFrame(
        {"id": [1, 2], "labels": [["a", "b"], ["c"]], "created": ["2012-08-24", "2014/07/09"]}
    )
    columns = [
        {"name": "id", "data_type": "Int64"},
        {"name": "labels", "data_type": "Array(String)"},
        {"name": "created", "data_type": "String"},
    ]

    result = calculate_columns_profiling(TABLE, columns, FakeConnector(df))

    assert set(result["columns"]) == {"id", "labels", "created"}
    assert result["columns"]["labels"]["sample_values"] == [["a", "b"], ["c"]]
    # The genuinely-text column is still scanned, so the mixed-format warning
    # survives the fix rather than being lost with the crash.
    assert any(
        isinstance(v, str) and "mixed date formats observed" in v
        for v in result["sample_values"]["created"]
    )


def test_calculate_columns_profiling() -> None:
    df = pd.DataFrame(
        {
            "id": [1, 2, 3, 4],
            "status": ["open", "open", "closed", "open"],
            "created_at": ["2020-01-01", "2020-01-02", "2020-01-03", "2020-01-04"],
            "token": ["a" * 40, "b" * 40, "a" * 40, "c" * 40],
        }
    )
    connector = FakeConnector(distinct_from(df))
    columns = [
        {"name": "id", "data_type": "integer"},
        {"name": "status", "data_type": "text"},
        {"name": "created_at", "data_type": "timestamp"},
        {"name": "token", "data_type": "text"},
    ]

    result = calculate_columns_profiling(TABLE, columns, connector)

    assert '"public"."orders"' in connector.executed[0]
    assert "LIMIT 1000" in connector.executed[0]
    assert len(connector.executed) == 3  # the sample, and DISTINCT on status and token

    # Every column (dates, long strings included), each with its top-5 sample
    # values and is_unique flag.
    profiled = result["columns"]
    assert set(profiled) == {"id", "status", "created_at", "token"}
    assert profiled["status"]["sample_values"][0] == "open"
    assert len(profiled["created_at"]["sample_values"]) == 4
    assert profiled["id"]["is_unique"] is True
    assert profiled["status"]["is_unique"] is False
    assert profiled["created_at"]["is_unique"] is True
    assert profiled["token"]["is_unique"] is False
    assert profiled["created_at"]["format"] == "YYYY-MM-DD"
    assert result["date_formats"] == {"created_at": "YYYY-MM-DD"}

    # Uniqueness for every column.
    assert result["uniqueness"] == {
        "id": True,
        "status": False,
        "created_at": True,
        "token": False,
    }

    # Sample values to store exclude the date column and the >30-char tokens.
    stored = result["sample_values"]
    assert "created_at" not in stored
    assert "token" not in stored
    assert stored["status"][0] == "open"
    assert stored["id"] == [1, 2, 3, 4]
    assert all(type(v) is int for v in stored["id"])


def test_calculate_columns_profiling_preserves_scalar_types() -> None:
    """Numbers and booleans profile as themselves, not as their str() form."""
    df = pd.DataFrame(
        {
            "amount": [10, 20, 10, 30],
            "ratio": [1.5, 2.5, 1.5, 3.5],
            "active": [True, False, True, True],
            # Wider than the 30-char cap once rendered — the cap is about prose,
            # so a number is kept however many digits it has.
            "big": [10**40, 10**41, 10**40, 10**42],
            "label": ["a" * 40, "b", "a" * 40, "c"],
        }
    )
    # Only `label` is text with fewer than 5 distinct values, so it is the one
    # column that triggers a DISTINCT probe; return empty to keep the top-N set.
    connector = FakeConnector([df, pd.DataFrame({"label": []})])
    columns = [
        {"name": "amount", "data_type": "integer"},
        {"name": "ratio", "data_type": "numeric"},
        {"name": "active", "data_type": "boolean"},
        {"name": "big", "data_type": "numeric"},
        {"name": "label", "data_type": "text"},
    ]

    result = calculate_columns_profiling(TABLE, columns, connector)

    profiled = result["columns"]
    assert profiled["amount"]["sample_values"] == [10, 20, 30]
    assert profiled["ratio"]["sample_values"] == [1.5, 2.5, 3.5]
    assert profiled["active"]["sample_values"] == [True, False]
    assert all(isinstance(v, int) for v in profiled["amount"]["sample_values"])
    assert all(isinstance(v, float) for v in profiled["ratio"]["sample_values"])
    assert all(isinstance(v, bool) for v in profiled["active"]["sample_values"])

    stored = result["sample_values"]
    assert stored["amount"] == [10, 20, 30]
    assert stored["ratio"] == [1.5, 2.5, 3.5]
    assert stored["active"] == [True, False]
    # The length cap applies to text only: the huge numbers survive, the
    # 40-char string does not.
    assert stored["big"] == [10**40, 10**41, 10**42]
    assert stored["label"] == ["b", "c"]


def test_calculate_columns_profiling_infers_date_format() -> None:
    df = pd.DataFrame(
        {"id": [1, 2, 3, 4], "created_at": ["2020-01-01", "2020-01-02", "2020-01-03", "2020-01-04"]}
    )
    columns = [
        {"name": "id", "data_type": "integer"},
        {"name": "created_at", "data_type": "timestamp"},
    ]

    result = calculate_columns_profiling(TABLE, columns, FakeConnector(df))

    assert result["columns"]["created_at"]["format"] == "YYYY-MM-DD"
    assert result["date_formats"] == {"created_at": "YYYY-MM-DD"}
    # Declared date/timestamp type: sample_values stay excluded regardless of
    # the format inference.
    assert "created_at" not in result["sample_values"]


def test_text_column_with_date_format_keeps_sample_values() -> None:
    """A TEXT-typed date column, unlike a declared date/timestamp type, keeps
    its sample_values alongside the inferred format."""
    df = pd.DataFrame(
        {
            "id": [1, 2, 3, 4],
            "signup_date": ["2020-01-01", "2020-01-02", "2020-01-03", "2020-01-04"],
        }
    )
    columns = [
        {"name": "id", "data_type": "integer"},
        {"name": "signup_date", "data_type": "text"},
    ]

    result = calculate_columns_profiling(TABLE, columns, FakeConnector(df))

    assert result["columns"]["signup_date"]["format"] == "YYYY-MM-DD"
    assert set(result["sample_values"]["signup_date"]) == {
        "2020-01-01",
        "2020-01-02",
        "2020-01-03",
        "2020-01-04",
    }


def test_calculate_columns_profiling_distinct_only_when_under_top_n() -> None:
    """DISTINCT probes run only for text columns with fewer than 5 sample values."""
    sample_df = pd.DataFrame(
        {
            # 5+ distinct values in the sample → no DISTINCT probe.
            "city": ["a", "b", "c", "d", "e", "a"],
            # Fewer than 5 distinct values → DISTINCT probe for rare enums.
            "status": ["open", "open", "closed", "open", "open", "open"],
        }
    )
    status_distinct_df = pd.DataFrame({"status": ["open", "closed", "banned"]})
    connector = FakeConnector([sample_df, status_distinct_df])
    columns = [
        {"name": "city", "data_type": "string"},
        {"name": "status", "data_type": "text"},
    ]

    result = calculate_columns_profiling(TABLE, columns, connector)

    assert len(connector.executed) == 2
    assert "SELECT DISTINCT" in connector.executed[1]
    assert '"status"' in connector.executed[1]
    assert '"city"' not in connector.executed[1]

    assert result["columns"]["city"]["sample_values"] == ["a", "b", "c", "d", "e"]
    assert {"banned", "open", "closed"} <= set(result["columns"]["status"]["sample_values"])


def test_distinct_probe_is_gated_but_the_row_sample_is_not() -> None:
    """The setting governs the DISTINCT probes only."""
    df = pd.DataFrame({"status": ["open", "open", "closed"]})
    columns = [{"name": "status", "data_type": "text"}]

    off = FakeConnector(df)
    calculate_columns_profiling(TABLE, columns, off, probe_distinct_values=False)
    on = FakeConnector([df, pd.DataFrame({"status": ["banned"]})])
    calculate_columns_profiling(TABLE, columns, on, probe_distinct_values=True)

    assert sum("SELECT * FROM" in s for s in off.executed) == 1
    assert sum("SELECT * FROM" in s for s in on.executed) == 1
    assert not any("SELECT DISTINCT" in s for s in off.executed)
    assert sum("SELECT DISTINCT" in s for s in on.executed) == 1


# Ontological's edits


def test_a_failed_row_sample_raises_for_the_engine_to_count() -> None:
    def fail(_sql: str) -> pd.DataFrame:
        raise duckdb.CatalogException("Table with name orders does not exist!")

    with pytest.raises(duckdb.CatalogException):
        calculate_columns_profiling(TABLE, [], FakeConnector(fail))


def test_a_failed_distinct_probe_falls_back_to_the_row_sample() -> None:
    df = pd.DataFrame({"status": ["open", "open", "closed"]})

    def answer(sql: str) -> pd.DataFrame:
        if "DISTINCT" in sql:
            raise duckdb.IOException("connection reset")
        return df

    result = calculate_columns_profiling(
        TABLE, [{"name": "status", "data_type": "VARCHAR"}], FakeConnector(answer)
    )

    assert result["columns"]["status"]["sample_values"] == ["open", "closed"]


def test_a_cancelled_distinct_probe_stops_the_table() -> None:
    df = pd.DataFrame({"status": ["open", "open", "closed"]})

    def answer(sql: str) -> pd.DataFrame:
        if "DISTINCT" in sql:
            raise duckdb.InterruptException("INTERRUPT Error: Interrupted!")
        return df

    with pytest.raises(duckdb.InterruptException):
        calculate_columns_profiling(
            TABLE, [{"name": "status", "data_type": "VARCHAR"}], FakeConnector(answer)
        )


def test_an_empty_table_profiles_as_nothing() -> None:
    result = calculate_columns_profiling(
        TABLE, [{"name": "id", "data_type": "BIGINT"}], FakeConnector(pd.DataFrame({"id": []}))
    )

    assert result == {"columns": {}, "sample_values": {}, "uniqueness": {}, "date_formats": {}}


def test_json_keys_are_sampled_with_their_examples_and_units() -> None:
    df = pd.DataFrame(
        {
            "payload": [
                {"tier": "gold", "salary": {"value": 45000, "unit": "annual"}},
                {"tier": "silver", "visit": {"visit_date": "2024-01-02"}},
            ]
        }
    )

    result = calculate_columns_profiling(
        TABLE, [{"name": "payload", "data_type": "JSON"}], FakeConnector(df)
    )

    assert result["sample_values"]["payload"] == [
        "salary.value [unit: salary.unit]",
        "salary.unit",
        "visit.visit_date [date format: YYYY-MM-DD]",
        "tier [e.g. 'gold', 'silver']",
    ]
