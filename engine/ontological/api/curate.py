"""The routes the Terms and Ontology screens read and edit through (DESIGN.md §5.3, §5.5).

    GET   /sources/{id}/terms                 the glossary: the source's Terms and how many are
                                              certified
    GET   /sources/{id}/graph?layer=          the semantic (Terms) or data (tables) layer
    GET   /sources/{id}/path?from_term=&to_term=   a shortest join path between two Terms
    GET   /terms/{id}                         one Term with its attributes and history
    PATCH /terms/{id}                         edit, rename or certify it; re-embeds what changed
    POST  /edits/{id}/undo                    put back what an edit changed, as a new edit

None of them use Claude. An engine with no store answers 503.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, HTTPException, Query

from ontological.curate import edits
from ontological.curate.ontology import Glossary, Graph, JoinPath, Layer, NotFound, TermDetail
from ontological.curate.service import OntologyService, Saved, TermChanges


def add_curate_routes(app: FastAPI, *, ontology: OntologyService | None) -> None:
    def need() -> OntologyService:
        if ontology is None:
            raise HTTPException(503, "this engine has no store")
        return ontology

    @app.get("/sources/{source_id}/terms")
    async def glossary(source_id: str) -> Glossary:  # pyright: ignore[reportUnusedFunction]
        try:
            return await need().glossary(source_id)
        except NotFound as e:
            raise HTTPException(404, str(e)) from None

    @app.get("/sources/{source_id}/graph")
    async def graph(source_id: str, layer: Layer = "semantic") -> Graph:  # pyright: ignore[reportUnusedFunction]
        try:
            return await need().graph(source_id, layer)
        except NotFound as e:
            raise HTTPException(404, str(e)) from None

    @app.get("/sources/{source_id}/path")
    async def join_path(  # pyright: ignore[reportUnusedFunction]
        source_id: str, from_term: Annotated[str, Query()], to_term: Annotated[str, Query()]
    ) -> JoinPath:
        try:
            return await need().path(source_id, from_term, to_term)
        except NotFound as e:
            raise HTTPException(404, str(e)) from None

    @app.get("/terms/{term_id}")
    async def term(term_id: str) -> TermDetail:  # pyright: ignore[reportUnusedFunction]
        try:
            return await need().term(term_id)
        except NotFound as e:
            raise HTTPException(404, str(e)) from None

    @app.patch("/terms/{term_id}")
    async def edit_term(term_id: str, changes: TermChanges) -> Saved:  # pyright: ignore[reportUnusedFunction]
        try:
            return await need().edit_term(term_id, changes)
        except (NotFound, edits.EditRefused) as e:
            raise _refused(e) from None

    @app.post("/edits/{edit_id}/undo")
    async def undo(edit_id: int) -> Saved:  # pyright: ignore[reportUnusedFunction]
        try:
            return await need().undo(edit_id)
        except (NotFound, edits.EditRefused) as e:
            raise _refused(e) from None


def _refused(e: NotFound | edits.EditRefused) -> HTTPException:
    """An edit's refusal, as the API answers it."""
    if isinstance(e, NotFound | edits.NotFound):
        return HTTPException(404, str(e))
    if isinstance(e, edits.NameTaken | edits.Conflict | edits.AlreadyUndone | edits.Creation):
        return HTTPException(409, str(e))
    return HTTPException(400, str(e))
