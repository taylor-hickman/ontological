"""The person's settings, kept in app.setting (ARCHITECTURE.md §5.2, Settings in §9.1).

Each setting is a key with a JSON value. A key with no row has its default here, so a store that
has never been asked keeps every default without a row for each.
"""

from __future__ import annotations

import json
from typing import Any

from auto_ontology.dal.session import store

# A staleness refresh that finds new tables starts their database's compile, instead of asking
# (§9.1). Off by default: compiling spends Claude quota.
AUTO_COMPILE = "compile.auto_compile"

DEFAULTS: dict[str, Any] = {AUTO_COMPILE: False}


def get(key: str) -> Any:
    rows = store().query_read("SELECT value FROM app.setting WHERE key = :key", {"key": key})
    return rows[0]["value"] if rows else DEFAULTS[key]


def put(key: str, value: Any) -> None:
    if key not in DEFAULTS:
        raise KeyError(key)
    store().query_write(
        "INSERT INTO app.setting (key, value) VALUES (:key, CAST(:value AS jsonb)) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        {"key": key, "value": json.dumps(value)},
    )


def auto_compile() -> bool:
    return get(AUTO_COMPILE) is True
