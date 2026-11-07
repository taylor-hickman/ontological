"""The routes onboarding and the catalog screen read (ARCHITECTURE.md §9.1; DESIGN.md §5.1, §5.5).

    GET    /claude                 the user's Claude Code: installed, version, logged in, and the
                                   auth variables Ontological ignores. No Claude usage.
    POST   /claude/test            one `ping` on Haiku, timed: a tiny amount of Claude usage.
    GET    /sources                the sources added, with their counts
    POST   /sources                add one and read its structure: no Claude usage
    DELETE /sources/{id}           remove it and its catalog
    GET    /sources/{id}/catalog   its schemas and tables
    GET    /tables/{id}            one table's columns, keys, tags and profile

Reading a source blocks, so those routes run on a thread. An engine without a store or a gateway
answers 503 on the routes that need it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException, Response
from pydantic import BaseModel

from ontological.catalog.sources import (
    Added,
    AddSource,
    NotFound,
    SourceCatalog,
    SourceConflict,
    SourceError,
    SourceOut,
    Sources,
    TableDetail,
)
from ontological.jobs.active import JobBusy
from ontological.llm.install import Claude, InstallState

log = logging.getLogger(__name__)


class ClaudeOut(BaseModel):
    state: InstallState
    cli_path: str | None
    version: str | None
    min_version: str
    logged_in: bool | None
    ignored_variables: list[str]


class PingOut(BaseModel):
    ok: bool
    seconds: float | None
    state: str
    error: str | None


def add_setup_routes(app: FastAPI, *, claude: Claude | None, sources: Sources | None) -> None:
    def need_claude() -> Claude:
        if claude is None:
            raise HTTPException(503, "this engine has no Claude gateway")
        return claude

    def need_sources() -> Sources:
        if sources is None:
            raise HTTPException(503, "this engine has no store")
        return sources

    @app.get("/claude")
    async def claude_status() -> ClaudeOut:  # pyright: ignore[reportUnusedFunction]
        got = await need_claude().detect()
        return ClaudeOut(
            state=got.state,
            cli_path=str(got.cli_path) if got.cli_path else None,
            version=got.version,
            min_version=got.min_version,
            logged_in=got.logged_in,
            ignored_variables=got.ignored_variables,
        )

    @app.post("/claude/test")
    async def claude_test() -> PingOut:  # pyright: ignore[reportUnusedFunction]
        got = await need_claude().test_call()
        return PingOut(ok=got.ok, seconds=got.seconds, state=got.state, error=got.error)

    @app.get("/sources")
    async def list_sources() -> list[SourceOut]:  # pyright: ignore[reportUnusedFunction]
        return await asyncio.to_thread(need_sources().list)

    @app.post("/sources", status_code=201)
    async def add_source(  # pyright: ignore[reportUnusedFunction]
        request: Annotated[AddSource, Body(discriminator="kind")],
    ) -> Added:
        store = need_sources()
        try:
            return await asyncio.to_thread(store.add, request)
        except SourceError as e:
            raise HTTPException(400, str(e)) from None
        except (SourceConflict, JobBusy) as e:
            raise HTTPException(409, str(e)) from None
        except Exception as e:
            # A server that is down, a wrong password, a file the executor can't open.
            log.warning("adding a source failed", exc_info=True)
            raise HTTPException(422, f"couldn't read the source: {e}") from None

    @app.delete("/sources/{source_id}", status_code=204)
    async def remove_source(source_id: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        try:
            await asyncio.to_thread(need_sources().remove, source_id)
        except NotFound as e:
            raise HTTPException(404, str(e)) from None
        return Response(status_code=204)

    @app.get("/sources/{source_id}/catalog")
    async def source_catalog(source_id: str) -> SourceCatalog:  # pyright: ignore[reportUnusedFunction]
        try:
            return await asyncio.to_thread(need_sources().catalog, source_id)
        except NotFound as e:
            raise HTTPException(404, str(e)) from None

    @app.get("/tables/{table_id}")
    async def table_detail(table_id: str) -> TableDetail:  # pyright: ignore[reportUnusedFunction]
        try:
            return await asyncio.to_thread(need_sources().table, table_id)
        except NotFound as e:
            raise HTTPException(404, str(e)) from None
