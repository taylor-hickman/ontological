"""An OntologyService from a scenario file, for the app's end-to-end tests (apps/desktop/test/e2e).

<appData>/ontology.json, when it exists, holds what the Terms and Ontology screens should show:

    {"glossaries": {"<source id>": Glossary}, "terms": {"<term id>": TermDetail},
     "graphs": {"<source id>": {"semantic": Graph, "data": Graph}},
     "paths": {"<from term id>:<to term id>": JoinPath},
     "sql_attributes": {"<attribute id>": SqlAttributeDetail},
     "tables": {"<table id>": TableSemantics}}

Edits and undos change the Terms in memory as `curate.edits` changes their rows: each changed
field is logged in the Term's history under a new edit id, a rename onto a taken name is refused,
and an undo writes the old values back as an edit that names the one it reverts. The glossary and
the semantic graph follow. Each edit and undo is appended to <appData>/ontology.log.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ontological.curate.edits import FIELDS, AlreadyUndone, EditRefused, NameTaken
from ontological.curate.ontology import (
    ChangeOut,
    Glossary,
    Graph,
    JoinPath,
    Layer,
    NotFound,
    SqlAttributeDetail,
    TableSemantics,
    TermDetail,
)
from ontological.curate.service import Saved, TermChanges, clean_synonyms


class FakeOntology:
    def __init__(self, app_data: Path) -> None:
        self._log = app_data / "ontology.log"
        path = app_data / "ontology.json"
        scenario: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}
        self._glossaries = {
            k: Glossary.model_validate(v) for k, v in scenario.get("glossaries", {}).items()
        }
        self._terms = {
            k: TermDetail.model_validate(v) for k, v in scenario.get("terms", {}).items()
        }
        self._graphs = {
            k: {layer: Graph.model_validate(g) for layer, g in v.items()}
            for k, v in scenario.get("graphs", {}).items()
        }
        self._paths = {k: JoinPath.model_validate(v) for k, v in scenario.get("paths", {}).items()}
        self._sql_attributes = {
            k: SqlAttributeDetail.model_validate(v)
            for k, v in scenario.get("sql_attributes", {}).items()
        }
        self._tables = {
            k: TableSemantics.model_validate(v) for k, v in scenario.get("tables", {}).items()
        }
        self._next_change = 1 + max(
            (c.id for t in self._terms.values() for c in t.history), default=0
        )
        self._next_edit = 1 + max(
            (c.edit for t in self._terms.values() for c in t.history), default=0
        )

    def _note(self, line: str) -> None:
        with open(self._log, "a") as f:
            f.write(line + "\n")

    async def glossary(self, source_id: str) -> Glossary:
        if source_id not in self._glossaries:
            raise NotFound(f"no source {source_id}")
        return self._glossaries[source_id]

    async def term(self, term_id: str) -> TermDetail:
        if term_id not in self._terms:
            raise NotFound(f"no Term {term_id}")
        return self._terms[term_id]

    async def graph(self, source_id: str, layer: Layer) -> Graph:
        if source_id not in self._graphs:
            raise NotFound(f"no source {source_id}")
        return self._graphs[source_id][layer]

    async def path(self, source_id: str, from_term: str, to_term: str) -> JoinPath:
        return self._paths.get(f"{from_term}:{to_term}", JoinPath(terms=[], hops=[], tables=[]))

    async def sql_attribute(self, attr_id: str) -> SqlAttributeDetail:
        if attr_id not in self._sql_attributes:
            raise NotFound(f"no SQL attribute {attr_id}")
        return self._sql_attributes[attr_id]

    async def table_semantics(self, table_id: str) -> TableSemantics:
        if table_id not in self._tables:
            raise NotFound(f"no table {table_id}")
        return self._tables[table_id]

    async def edit_term(self, term_id: str, changes: TermChanges) -> Saved:
        term = await self.term(term_id)
        values = changes.model_dump(exclude_none=True)
        if "synonyms" in values:
            values["synonyms"] = clean_synonyms(values["synonyms"])
        if "name" in values:
            values["name"] = values["name"].strip()
            if not values["name"]:
                raise EditRefused("a name can't be empty")
            if any(t.name == values["name"] and t.id != term_id for t in self._terms.values()):
                raise NameTaken(f"a Term named {values['name']!r} exists")
        edit = self._apply(term, values, undoes=None)
        self._note(f"edit {term_id} {json.dumps(values, sort_keys=True)}")
        return Saved(edit=edit, reembedded=0 if edit else None, term=self._terms[term_id])

    async def undo(self, edit_id: int) -> Saved:
        for term in self._terms.values():
            changes = [c for c in term.history if c.edit == edit_id]
            if not changes:
                continue
            if any(c.undoes == edit_id for c in term.history):
                raise AlreadyUndone(f"edit {edit_id} is undone already")
            edit = self._apply(term, {c.field: c.old for c in changes}, undoes=edit_id)
            self._note(f"undo {edit_id}")
            return Saved(edit=edit, reembedded=0, term=self._terms[term.id])
        raise NotFound(f"no edit {edit_id}")

    def _apply(self, term: TermDetail, values: dict[str, Any], *, undoes: int | None) -> int | None:
        current = term.model_dump()
        changed = {f: values[f] for f in FIELDS["term"] if f in values and values[f] != current[f]}
        if not changed:
            return None
        edit = self._next_edit
        self._next_edit += 1
        now = datetime.now(UTC)
        logged: list[ChangeOut] = []
        for field, new in changed.items():
            logged.append(
                ChangeOut(
                    id=self._next_change,
                    edit=edit,
                    ts=now,
                    field=field,
                    old=current[field],
                    new=new,
                    actor="user",
                    undoes=undoes,
                )
            )
            self._next_change += 1
        updated = term.model_copy(
            update={**changed, "history": logged[::-1] + term.history, "undoable": edit}
        )
        self._terms[term.id] = updated
        certified = updated.name_certified and updated.description_certified
        for glossary in self._glossaries.values():
            rows = [
                r.model_copy(update={**changed, "certified": certified, "changed_at": now})
                if r.id == term.id
                else r
                for r in glossary.terms
            ]
            if rows != glossary.terms:
                glossary.terms = rows
                glossary.certified = sum(r.certified for r in rows)
        for graphs in self._graphs.values():
            semantic = graphs.get("semantic")
            if semantic is None:
                continue
            semantic.nodes = [
                n.model_copy(update={"name": updated.name, "certified": certified})
                if n.id == term.id
                else n
                for n in semantic.nodes
            ]
        return edit
