# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/catalog/normalize.py, with types.
# `flat_list_recursive`, which nothing here calls, is left out.

"""Normalize a source's raw tables and columns into the frames the writer and the diff expect.

Both sides of the diff pass through here: the frames built from a fresh extract, and the frames
the store reads back (`catalog/store/schemas.py`), so the two compare cell for cell.
"""

# pandas has no stubs here.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC
from typing import Any

import numpy as np
import pandas as pd

from auto_ontology.catalog.constants import TableTypes

_TABLE_TYPES = {
    "table_schema": "category",
    "table_name": "string",
    "table_type": "category",
    "created": "string",
    "description": "string",
}
_COLUMN_TYPES = {
    "table_schema": "category",
    "table_name": "category",
    "column_name": "string",
    "ordinal_position": "Int16",
    "data_type": "category",
    # "boolean", not "bool": the nullable dtype, because a source that can't determine
    # nullability leaves it NULL.
    "is_nullable": "boolean",
    "description": "string",
}
_NULLABLE_TRUE = {"YES", "TRUE", "T", "Y", "1"}
_NULLABLE_FALSE = {"NO", "FALSE", "F", "N", "0"}


def chunks[T](items: Sequence[T], n: int) -> Iterator[Sequence[T]]:
    """Successive `n`-sized slices of `items`, the last one shorter."""
    for i in range(0, len(items), n):
        yield items[i : i + n]


def normalize_tables(df: pd.DataFrame | None) -> pd.DataFrame:
    """Type a tables frame. Rows come from an extract or from the store's reload
    (`get_schema_tables`); the store's `id` and `database` columns are kept, and a connector's
    `owner` is dropped."""
    if df is None or df.empty:
        return pd.DataFrame(columns=pd.Index(list(_TABLE_TYPES)))
    df = df.copy()
    for key in _TABLE_TYPES:
        if key not in df.columns:
            df[key] = pd.NA
    df["table_type"] = df["table_type"].fillna(TableTypes.BASE_TABLE)
    df = df.astype(dtype=_TABLE_TYPES)
    created = pd.to_datetime(df["created"], utc=True, format="mixed")
    df["created"] = created.apply(_to_utc_second)
    if "owner" in df.columns:
        df = df.drop(columns=["owner"])
    return df


def _to_utc_second(x: Any) -> Any:
    return x.tz_convert(UTC).replace(microsecond=0) if pd.notna(x) else x


def coerce_nullable(value: object) -> bool | None:
    """One `is_nullable` value as a boolean, or None when the source didn't say.

    Executors return real booleans. This is for what still arrives as text: a source that hands
    back `information_schema`'s `'YES'`/`'NO'` verbatim. Both spellings are truthy as strings,
    which is what made `bool(stored)` report a whole catalog as nullable upstream, so an
    unrecognised value raises rather than defaulting.
    """
    if value is None or value is pd.NA or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, bool | np.bool_):
        return bool(value)
    spelled = str(value).strip().upper()
    if spelled in _NULLABLE_TRUE:
        return True
    if spelled in _NULLABLE_FALSE:
        return False
    if spelled == "":
        return None
    raise ValueError(
        f"is_nullable must be a boolean (or 'YES'/'NO'); got {value!r}. "
        "Convert it in the source's extract."
    )


def normalize_columns(df: pd.DataFrame | None) -> pd.DataFrame:
    """Type a columns frame, from an extract or from the store's reload (`get_schema_columns`)."""
    if df is None or df.empty:
        return pd.DataFrame(columns=pd.Index(list(_COLUMN_TYPES)))
    df = df.copy()
    for key in _COLUMN_TYPES:
        if key not in df.columns:
            df[key] = pd.NA
    df["ordinal_position"] = pd.to_numeric(df["ordinal_position"])
    df["is_nullable"] = df["is_nullable"].map(coerce_nullable)
    return df.astype(dtype=_COLUMN_TYPES)
