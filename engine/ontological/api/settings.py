"""The routes Settings reads and writes the person's settings through (§9.1).

    GET /settings          every key with its value, a default for a key never set
    PUT /settings/{key}    {"value": ...} sets one; answers with every key, as GET does

An unknown key is 404, and a value of another type than the key's default is 422. An engine with
no store answers 503.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ontological.settings import BadValue, SettingsService, UnknownSetting


class SettingValue(BaseModel):
    value: Any


def add_settings_routes(app: FastAPI, *, settings: SettingsService | None) -> None:
    def need() -> SettingsService:
        if settings is None:
            raise HTTPException(503, "this engine has no store to keep settings in")
        return settings

    @app.get("/settings")
    async def read_settings() -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        return await need().every()

    @app.put("/settings/{key}")
    async def write_setting(key: str, body: SettingValue) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        service = need()
        try:
            await service.put(key, body.value)
        except UnknownSetting:
            raise HTTPException(404, f"no setting {key}") from None
        except BadValue as e:
            raise HTTPException(422, str(e)) from None
        return await service.every()
