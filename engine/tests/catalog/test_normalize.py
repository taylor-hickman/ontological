# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/catalog/tests/
# test_normalize_nullable.py to test ontological.catalog.normalize, with types.

"""Tests for ``is_nullable`` coercion at the ingest boundary (BUILD_PLAN.md M2.3).

``catalog_column.is_nullable`` is a real boolean. Upstream once stored ``information_schema``'s
``'YES'``/``'NO'`` strings there, and since both are truthy, ``bool(stored)`` reported every
column in the catalog as nullable. These tests pin the conversion so it can't silently regress to
a string.
"""

from __future__ import annotations

import pandas as pd
import pytest

from ontological.catalog.normalize import chunks, coerce_nullable, normalize_columns


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, True),
        (False, False),
        ("YES", True),
        ("NO", False),
        ("yes", True),
        ("no", False),
        (" NO ", False),
        ("TRUE", True),
        ("FALSE", False),
        ("t", True),
        ("f", False),
        ("1", True),
        ("0", False),
        (None, None),
        ("", None),
    ],
)
def test_coercion_covers_what_connectors_actually_return(
    raw: object, expected: bool | None
) -> None:
    assert coerce_nullable(raw) is expected


def test_the_string_no_is_false_not_true() -> None:
    """The whole bug in one assertion: ``bool('NO')`` is ``True``."""
    assert bool("NO") is True
    assert coerce_nullable("NO") is False


def test_an_unrecognised_value_raises_rather_than_guessing() -> None:
    """Defaulting is what made the original bug invisible."""
    with pytest.raises(ValueError, match="must be a boolean"):
        coerce_nullable("maybe")


def test_numpy_bools_from_a_dataframe_round_trip() -> None:
    """Records become DataFrame cells, not Python scalars."""
    series = pd.DataFrame({"x": [True, False]})["x"]
    assert coerce_nullable(series.iloc[0]) is True
    assert coerce_nullable(series.iloc[1]) is False


def test_missing_values_stay_none() -> None:
    for missing in (None, pd.NA, float("nan")):
        assert coerce_nullable(missing) is None


def _columns(**extra: list[object]) -> pd.DataFrame:
    n = len(next(iter(extra.values())))
    names = ["a", "b", "c"][:n]
    return pd.DataFrame(
        {
            "table_schema": ["s"] * n,
            "table_name": ["t"] * n,
            "column_name": names,
            "ordinal_position": list(range(1, n + 1)),
            "data_type": ["int"] * n,
            "description": [None] * n,
            **extra,
        }
    )


def test_normalize_columns_produces_a_nullable_boolean_dtype() -> None:
    """"boolean", not "bool": a source may not be able to determine it."""
    df = normalize_columns(_columns(is_nullable=[True, False, None]))
    assert df["is_nullable"].dtype == "boolean"
    assert df["is_nullable"].tolist()[:2] == [True, False]
    assert pd.isna(df["is_nullable"].iloc[2])


def test_normalize_columns_still_accepts_the_sql_standard_spelling() -> None:
    """A source that returns 'YES'/'NO' must not poison the store."""
    df = normalize_columns(_columns(is_nullable=["NO", "YES"]))
    assert df["is_nullable"].tolist() == [False, True]


def test_a_missing_column_normalizes_to_null_not_false() -> None:
    """Absent means undetermined; callers read that as nullable."""
    df = normalize_columns(_columns(ordinal_position=[1]))
    assert pd.isna(df["is_nullable"].iloc[0])


def test_chunks_splits_in_order_and_keeps_the_remainder() -> None:
    assert list(chunks([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert list(chunks([], 2)) == []
