"""What the API asks of model import and export (ARCHITECTURE.md §9.3; BUILD_PLAN.md M6.4).

An export reads the store. An import validates the document, applies it in one transaction, and
then starts a `reembed` job for each database it wrote, through the engine's jobs, so the vectors
catch up in the background and a failure shows on the Jobs screen, where Resume retries it. The
import answers once it has committed, before the re-embeds end.

The API holds an `InterchangeService`, so the engine process tests can serve the routes from a
fake.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel

from ontological.interchange import service
from ontological.interchange.embed import ImportEmbedBuffer
from ontological.interchange.schemas import ExportRequest, ModelFormat
from ontological.jobs.service import JobsService, NoEmbedder

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Exported:
    content: bytes
    media_type: str
    filename: str


class Imported(BaseModel):
    summary: dict[str, Any]  # what was created and what was already there, by kind
    reembedding: list[str]  # the databases whose re-embed job started
    reembed_error: str | None = None  # why none started, such as an engine with no embedder


class InterchangeService(Protocol):
    async def export(self, request: ExportRequest) -> Exported: ...
    async def import_model(self, yaml_text: str, *, replace: bool) -> Imported: ...


class StoreInterchange:
    """Import and export against the store, re-embedding through `jobs`."""

    def __init__(self, jobs: JobsService) -> None:
        self.jobs = jobs

    async def export(self, request: ExportRequest) -> Exported:
        if request.include_ontology:
            content = await asyncio.to_thread(service.export_ossie_documents, request)
            return Exported(content, "application/zip", "ossie-model.zip")
        text = await asyncio.to_thread(service.export_model, request)
        name = "ossie" if request.format is ModelFormat.OSSIE else "ontological"
        return Exported(text.encode(), "application/x-yaml", f"{name}-model.yaml")

    async def import_model(self, yaml_text: str, *, replace: bool) -> Imported:
        buffer = ImportEmbedBuffer()
        summary = await asyncio.to_thread(
            service.import_model, yaml_text, replace=replace, embed_buffer=buffer
        )
        started: list[str] = []
        for name in buffer.database_names:
            try:
                await self.jobs.reembed(name)
            except NoEmbedder:
                log.warning("imported into %s, but this engine has no embedder", name)
                return Imported(
                    summary=summary,
                    reembedding=started,
                    reembed_error="this engine has no embedding model; the next compile embeds",
                )
            started.append(name)
        return Imported(summary=summary, reembedding=started)
