"""The routes for model import and export (ARCHITECTURE.md §9.3; BUILD_PLAN.md M6.4).

    POST /model/export    {databases, format, include_ontology}: the catalog and semantic model
                          of those databases (none: every one) as an attachment, native YAML or
                          Apache Ossie, or with include_ontology a zip of Ossie's semantic model,
                          ontology and mapping
    POST /model/import?replace=   a native or Ossie YAML document as the request body, applied in
                          one transaction; then each database it wrote is re-embedded as a
                          `reembed` job. `replace` (on by default) drops the semantics of those
                          databases that the document leaves out

A document that doesn't validate, can't be converted or names what it doesn't carry is 422, and a
Term whose name another Term holds is 409. Upstream also took a multipart upload; the app sends
the file's text as the body. None of them use Claude. An engine with no store answers 503.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from auto_ontology.dal.model_interchange import ModelInterchangeError
from auto_ontology.server.sql_errors import SqlParseError
from ontological.interchange.schemas import ExportRequest
from ontological.interchange.service import AutoOntologyConversionError
from ontological.interchange.store import Imported, InterchangeService


def add_interchange_routes(app: FastAPI, *, interchange: InterchangeService | None) -> None:
    def need() -> InterchangeService:
        if interchange is None:
            raise HTTPException(503, "this engine has no store")
        return interchange

    @app.post("/model/export")
    async def export_model(body: ExportRequest) -> Response:  # pyright: ignore[reportUnusedFunction]
        try:
            done = await need().export(body)
        except ModelInterchangeError as e:
            raise HTTPException(422, str(e)) from None
        except AutoOntologyConversionError as e:
            detail = f"Can't express this model as Apache Ossie YAML: {e}"
            raise HTTPException(422, detail) from None
        return Response(
            content=done.content,
            media_type=done.media_type,
            headers={"Content-Disposition": f'attachment; filename="{done.filename}"'},
        )

    @app.post("/model/import")
    async def import_model(  # pyright: ignore[reportUnusedFunction]
        request: Request, replace: Annotated[bool, Query()] = True
    ) -> Imported:
        raw = await request.body()
        if not raw:
            raise HTTPException(422, "send the YAML document as the request body")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(422, "the YAML document must be UTF-8") from None
        try:
            return await need().import_model(text, replace=replace)
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False)) from None
        except AutoOntologyConversionError as e:
            raise HTTPException(422, f"Can't read this file as Apache Ossie YAML: {e}") from None
        except IntegrityError as e:
            raise HTTPException(409, f"the document conflicts with the store: {e.orig}") from None
        except (ModelInterchangeError, SqlParseError, ValueError) as e:
            raise HTTPException(422, str(e)) from None
