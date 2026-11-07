"""The settings in app.setting (ARCHITECTURE.md §5.2, §9.1), read and written as the settings
routes do, through `StoreSettings`.

Needs ONTOLOGICAL_PG_BIN, like the rest of tests/store.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import sql

from ontological import settings
from ontological.settings import BadValue, StoreSettings, UnknownSetting
from ontological.store import pg


def test_a_store_never_asked_has_every_default_and_no_rows(store: pg.Layout) -> None:
    assert asyncio.run(StoreSettings().every()) == {"compile.auto_compile": False}
    assert sql(store, "SELECT count(*) FROM app.setting") == "0"


def test_auto_compile_is_written_and_read_back(store: pg.Layout) -> None:
    service = StoreSettings()
    asyncio.run(service.put(settings.AUTO_COMPILE, True))
    assert asyncio.run(service.every()) == {"compile.auto_compile": True}
    assert settings.auto_compile() is True, "the staleness check reads the same row"

    asyncio.run(service.put(settings.AUTO_COMPILE, False))
    assert settings.auto_compile() is False
    assert sql(store, "SELECT value FROM app.setting") == "false", "one row, updated"


def test_an_unknown_key_or_a_value_of_another_type_writes_nothing(store: pg.Layout) -> None:
    service = StoreSettings()
    with pytest.raises(UnknownSetting):
        asyncio.run(service.put("compile.nope", True))
    for wrong in (1, "true", None, [True]):
        with pytest.raises(BadValue):
            asyncio.run(service.put(settings.AUTO_COMPILE, wrong))
    assert sql(store, "SELECT count(*) FROM app.setting") == "0"
