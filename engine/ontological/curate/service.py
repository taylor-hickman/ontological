"""What the API asks of the ontology (DESIGN.md §5.3, §5.5; BUILD_PLAN.md M5.2): the reads in
`ontology`, and a person's edits and undos from `edits`, each re-embedding what it changed with
the engine's embedder so the next answer uses it.

The API holds an `OntologyService`, so the engine process tests can serve the routes from a fake.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol, TypeVar

from pydantic import BaseModel

from auto_ontology.vdb.postgres import PostgresVDB
from ontological.curate import edits, ontology
from ontological.curate.ontology import (
    Glossary,
    Graph,
    JoinPath,
    Layer,
    SqlAttributeDetail,
    TableSemantics,
    TermDetail,
)
from ontological.embed.embedder import LocalEmbedder
from ontological.embed.search import semantic_vdb

T = TypeVar("T")


class TermChanges(BaseModel):
    """The fields a person changed; a field left out keeps what it holds."""

    name: str | None = None
    description: str | None = None
    synonyms: list[str] | None = None
    name_certified: bool | None = None
    description_certified: bool | None = None


class Saved(BaseModel):
    edit: int | None  # None when nothing changed
    reembedded: int | None  # vectors embedded; None when the engine has no embedder
    term: TermDetail | None  # the Term the edit changed, as it is now


class OntologyService(Protocol):
    async def glossary(self, source_id: str) -> Glossary: ...
    async def term(self, term_id: str) -> TermDetail: ...
    async def edit_term(self, term_id: str, changes: TermChanges) -> Saved: ...
    async def undo(self, edit_id: int) -> Saved: ...
    async def graph(self, source_id: str, layer: Layer) -> Graph: ...
    async def path(self, source_id: str, from_term: str, to_term: str) -> JoinPath: ...
    async def sql_attribute(self, attr_id: str) -> SqlAttributeDetail: ...
    async def table_semantics(self, table_id: str) -> TableSemantics: ...


def clean_synonyms(synonyms: list[str]) -> list[str]:
    """Trimmed, without blanks, and each once, in the order given."""
    seen: dict[str, None] = {}
    for s in synonyms:
        if (s := s.strip()) and s.lower() not in {k.lower() for k in seen}:
            seen[s] = None
    return list(seen)


class StoreOntology:
    """The ontology in the store, edited through `edits` with the engine's embedder."""

    def __init__(self, embedder: LocalEmbedder | None) -> None:
        self.embedder = embedder

    async def glossary(self, source_id: str) -> Glossary:
        return await asyncio.to_thread(ontology.glossary, source_id)

    async def term(self, term_id: str) -> TermDetail:
        return await asyncio.to_thread(ontology.term, term_id)

    async def graph(self, source_id: str, layer: Layer) -> Graph:
        return await asyncio.to_thread(ontology.graph, source_id, layer)

    async def path(self, source_id: str, from_term: str, to_term: str) -> JoinPath:
        return await asyncio.to_thread(ontology.path, source_id, from_term, to_term)

    async def sql_attribute(self, attr_id: str) -> SqlAttributeDetail:
        return await asyncio.to_thread(ontology.sql_attribute, attr_id)

    async def table_semantics(self, table_id: str) -> TableSemantics:
        return await asyncio.to_thread(ontology.table_semantics, table_id)

    async def edit_term(self, term_id: str, changes: TermChanges) -> Saved:
        def run(vdb: PostgresVDB | None) -> Saved:
            done = edits.edit_term(
                term_id,
                name=changes.name.strip() if changes.name is not None else None,
                description=changes.description,
                synonyms=clean_synonyms(changes.synonyms) if changes.synonyms is not None else None,
                name_certified=changes.name_certified,
                description_certified=changes.description_certified,
                vdb=vdb,
            )
            return _saved(done, ontology.term(term_id))

        return await asyncio.to_thread(self._with_vdb, run)

    async def undo(self, edit_id: int) -> Saved:
        def run(vdb: PostgresVDB | None) -> Saved:
            done = edits.undo(edit_id, vdb=vdb)
            terms = [c.entity_id for c in done.changes if c.entity == "term"]
            return _saved(done, ontology.term(terms[0]) if terms else None)

        return await asyncio.to_thread(self._with_vdb, run)

    def _with_vdb(self, run: Callable[[PostgresVDB | None], T]) -> T:
        """Run with the semantic layer's vector store, so the edit re-embeds what it changed.
        Without an embedder the edit is still logged, and the next sync catches the vectors up."""
        if self.embedder is None:
            return run(None)
        vdb = semantic_vdb(self.embedder)
        try:
            return run(vdb)
        finally:
            vdb.close()


def _saved(done: edits.Edit | None, term: TermDetail | None) -> Saved:
    return Saved(
        edit=done.id if done else None,
        reembedded=done.vectors.embedded if done and done.vectors else None,
        term=term,
    )
