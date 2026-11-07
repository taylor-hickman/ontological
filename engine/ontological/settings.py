"""The person's settings, kept in app.setting (ARCHITECTURE.md §5.2, Settings in §9.1).

Each setting is a key with a JSON value. A key with no row has its default here, so a store that
has never been asked keeps every default without a row for each. A value must have its default's
type: `compile.auto_compile` is true or false.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Protocol

from auto_ontology.dal.session import store

# A staleness refresh that finds new tables starts their database's compile, instead of asking
# (§9.1). Off by default: compiling spends Claude quota.
AUTO_COMPILE = "compile.auto_compile"

DEFAULTS: dict[str, Any] = {AUTO_COMPILE: False}


class UnknownSetting(KeyError):
    """A key with no default here."""


class BadValue(ValueError):
    """A value of another type than the key's default."""


def get(key: str) -> Any:
    rows = store().query_read("SELECT value FROM app.setting WHERE key = :key", {"key": key})
    return rows[0]["value"] if rows else DEFAULTS[key]


def every() -> dict[str, Any]:
    """Every key, with its stored value or its default."""
    rows = store().query_read("SELECT key, value FROM app.setting")
    stored = {row["key"]: row["value"] for row in rows}
    return {key: stored.get(key, default) for key, default in DEFAULTS.items()}


def check(key: str, value: Any) -> None:
    """Refuse a key with no default, or a value of another type than its default."""
    if key not in DEFAULTS:
        raise UnknownSetting(key)
    # type(), not isinstance: True is an int, and 1 isn't a bool
    if type(value) is not type(DEFAULTS[key]):
        raise BadValue(f"{key} takes a {type(DEFAULTS[key]).__name__}, not {value!r}")


def put(key: str, value: Any) -> None:
    check(key, value)
    store().query_write(
        "INSERT INTO app.setting (key, value) VALUES (:key, CAST(:value AS jsonb)) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        {"key": key, "value": json.dumps(value)},
    )


def auto_compile() -> bool:
    return get(AUTO_COMPILE) is True


class SettingsService(Protocol):
    """What the settings routes read and write through."""

    async def every(self) -> dict[str, Any]: ...
    async def put(self, key: str, value: Any) -> None: ...


class StoreSettings:
    """The settings in the bundled store, off the event loop."""

    async def every(self) -> dict[str, Any]:
        return await asyncio.to_thread(every)

    async def put(self, key: str, value: Any) -> None:
        await asyncio.to_thread(put, key, value)
