"""A SettingsService in <appData>/settings.json, for the app's end-to-end tests
(apps/desktop/test/e2e).

The file holds the keys that were set, as app.setting holds their rows: a key that isn't there has
its default. A put checks the key and value as the real service does, then writes the file, so a
test can read what the app set and a relaunched engine reads it back.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ontological import settings


class FakeSettings:
    def __init__(self, app_data: Path) -> None:
        self._path = app_data / "settings.json"

    def _stored(self) -> dict[str, Any]:
        return json.loads(self._path.read_text()) if self._path.exists() else {}

    async def every(self) -> dict[str, Any]:
        stored = self._stored()
        return {key: stored.get(key, default) for key, default in settings.DEFAULTS.items()}

    async def put(self, key: str, value: Any) -> None:
        settings.check(key, value)
        self._path.write_text(json.dumps({**self._stored(), key: value}))
