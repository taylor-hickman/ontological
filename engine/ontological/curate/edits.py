"""Edits to Terms and column attributes, logged in ops.change_log and undone from it
(ARCHITECTURE.md §5.7, BUILD_PLAN.md M5.1).

- **One edit, one transaction.** An edit locks its row, applies the fields that differ from what
  is stored, and writes one change_log row for each, all sharing the edit's id, in one
  transaction. A field set to what it already holds isn't a change, and an edit that changes
  nothing writes nothing and returns None.
- **The vendored DAL applies it.** `update_term` and `update_column_attribute` write the fields,
  so a Term rename also rewrites the `term_name` copy on its attributes, and a person's edit
  stamps `modified`. Synonyms, and a description put back to none by an undo, which the DAL's
  coalescing update can't write, are written here.
- **Then the vectors.** Given the semantic layer's VDB, the edit then syncs the semantic layer of
  each database the entity is in (`sync_database`), which embeds only the texts whose content
  hash changed and deletes the vectors they replace. Those writes rebuild the BM25 index through
  the VDB's change hook, whichever `HybridSearch` serves it. A certification changes no text, so
  it re-embeds nothing: the sync rewrites the vector's `certified` flag in place. Without a VDB,
  the edit is logged and the next sync catches the vectors up.
- **Undo is an edit too.** `undo` writes back each field's old value as a new edit whose rows name
  the one they revert (`undoes`), so history shows it and undoing an undo makes the edit again.
  An edit is undone once, and not while a later edit holds one of its fields: undo that first.
  An edit that created a row isn't undone: there is no name to put back.
- **Compile and import log their writes here too.** They write through the DAL themselves, in
  their own transaction, so they take each row's fields before they write (`fields_of`) and log
  what changed after (`log_write`): one edit per Term, holding its fields and its attributes',
  as actor `compile` or `import`. A row the writer created is logged from `CREATED`, so each
  field it was given is a change from nothing.

Every call blocks; async callers run them in a thread.
"""

# The vendored DAL and vdb are untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

from sqlalchemy import select, update

from auto_ontology.dal import schema as s
from auto_ontology.dal.attributes import update_column_attribute
from auto_ontology.dal.session import store, write_transaction
from auto_ontology.dal.terms import update_term
from auto_ontology.vdb.postgres import PostgresVDB
from ontological.embed.semantic import Synced, sync_database
from ontological.store.local_owner import LOCAL_OWNER_ID

Entity = Literal["term", "column_attribute"]
Actor = Literal["user", "compile", "import"]

# The fields an edit can change, in the order its changes are logged.
FIELDS: Mapping[Entity, tuple[str, ...]] = {
    "term": ("name", "description", "synonyms", "name_certified", "description_certified"),
    "column_attribute": ("name", "description", "certified"),
}


class EditRefused(Exception):
    """The edit or undo was refused, and nothing was written."""


class NotFound(EditRefused):
    """No such Term, attribute or edit."""


class NameTaken(EditRefused):
    """Another Term (or another attribute of the same column and Term) has that name."""


class AlreadyUndone(EditRefused):
    """That edit has been undone already."""


class Conflict(EditRefused):
    """A later edit changed a field this one changed: undo that one first."""


class Creation(EditRefused):
    """That edit created a Term or attribute, which an undo can't take back."""


@dataclass(frozen=True)
class Change:
    """One field of one edit: a change_log row."""

    id: int
    edit: int
    ts: datetime
    entity: Entity
    entity_id: str
    field: str
    old: Any
    new: Any
    actor: Actor
    undoes: int | None


@dataclass(frozen=True)
class Edit:
    id: int
    changes: tuple[Change, ...]
    vectors: Synced | None  # the semantic layer's sync; None without a VDB


# --- edits ------------------------------------------------------------------------------------


def edit_term(
    term_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    synonyms: Sequence[str] | None = None,
    name_certified: bool | None = None,
    description_certified: bool | None = None,
    vdb: PostgresVDB | None = None,
    actor: Actor = "user",
) -> Edit | None:
    """Edit a Term: each field given and different from the stored one. None leaves a field as
    it is. Returns None when nothing changed."""
    values = {
        "name": name,
        "description": description,
        "synonyms": list(synonyms) if synonyms is not None else None,
        "name_certified": name_certified,
        "description_certified": description_certified,
    }
    return _edit("term", term_id, _given(values), vdb=vdb, actor=actor)


def edit_attribute(
    attribute_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    certified: bool | None = None,
    vdb: PostgresVDB | None = None,
    actor: Actor = "user",
) -> Edit | None:
    """Edit a column attribute: each field given and different from the stored one. Returns None
    when nothing changed."""
    values = {"name": name, "description": description, "certified": certified}
    return _edit("column_attribute", attribute_id, _given(values), vdb=vdb, actor=actor)


def undo(edit_id: int, *, vdb: PostgresVDB | None = None, actor: Actor = "user") -> Edit:
    """Put back what edit `edit_id` changed, as a new edit that names it."""
    with write_transaction():
        changes = _changes("WHERE edit = :edit ORDER BY id FOR UPDATE", {"edit": edit_id})
        if not changes:
            raise NotFound(f"no edit {edit_id}")
        if any(created(c) for c in changes):
            raise Creation(f"edit {edit_id} created what it changed, so it can't be undone")
        if store().query_read(
            "SELECT 1 FROM ops.change_log WHERE undoes = :edit LIMIT 1", {"edit": edit_id}
        ):
            raise AlreadyUndone(f"edit {edit_id} is undone already")
        by_entity: dict[tuple[Entity, str], list[Change]] = {}
        for c in changes:
            by_entity.setdefault((c.entity, c.entity_id), []).append(c)
        new_edit = _next_edit()
        written: list[Change] = []
        databases: set[str] = set()
        for (entity, entity_id), entity_changes in by_entity.items():
            done = _write(
                entity,
                entity_id,
                {c.field: c.old for c in entity_changes},
                expected={c.field: c.new for c in entity_changes},
                edit=new_edit,
                actor=actor,
                undoes=edit_id,
            )
            if done is not None:
                written.extend(done[0])
                databases |= done[1]
    return Edit(new_edit, tuple(written), _sync(vdb, databases))


# --- history ----------------------------------------------------------------------------------


def history(entity: Entity, entity_id: str) -> list[Change]:
    """Every change to one Term or attribute, newest first."""
    return _changes(
        "WHERE entity = :entity AND entity_id = :id ORDER BY id DESC",
        {"entity": entity, "id": entity_id},
    )


def created(change: Change) -> bool:
    """Whether the change is a row's creation, logged from `CREATED`: an undo can't revert it."""
    return change.field == "name" and change.old is None


# --- compile's and import's writes ------------------------------------------------------------

# A row before it exists: what a writer's new rows are logged from, so each field it was given is
# a change and each it left unset isn't.
CREATED: Mapping[Entity, Mapping[str, Any]] = {
    "term": {
        "name": None,
        "description": None,
        "synonyms": [],
        "name_certified": False,
        "description_certified": False,
    },
    "column_attribute": {"name": None, "description": None, "certified": False},
}

Row = tuple[Entity, str]


def fields_of(entity: Entity, ids: Collection[str]) -> dict[Row, dict[str, Any]]:
    """The logged fields of each of these rows there is, locked until the transaction ends: what a
    writer outside this module takes before it writes, for `log_write` to compare against."""
    if not ids:
        return {}
    table = s.term if entity == "term" else s.column_attribute
    rows = store().query_read(
        select(table.c.id, *(table.c[f] for f in FIELDS[entity]))
        .where(table.c.id.in_(sorted(ids)))
        .order_by(table.c.id)
        .with_for_update()
    )
    out: dict[Row, dict[str, Any]] = {}
    for row in rows:
        fields = {f: row[f] for f in FIELDS[entity]}
        if entity == "term":
            fields["synonyms"] = list(fields["synonyms"] or [])
        out[(entity, str(row["id"]))] = fields
    return out


def log_write(
    term_id: str,
    attribute_ids: Collection[str],
    before: Mapping[Row, Mapping[str, Any]],
    *,
    actor: Actor,
) -> tuple[Change, ...]:
    """Log, as one edit by `actor` inside the writer's transaction, each field a write changed on
    the Term and its attributes since `before`. A row `before` doesn't hold isn't logged: give a
    row the writer created as `CREATED`. Returns the changes, none when nothing changed."""
    term: list[Row] = [("term", term_id)]
    attributes: list[Row] = [("column_attribute", a) for a in attribute_ids]
    rows = [r for r in dict.fromkeys(term + attributes) if r in before]
    after = fields_of("term", [i for e, i in rows if e == "term"]) | fields_of(
        "column_attribute", [i for e, i in rows if e == "column_attribute"]
    )
    edit: int | None = None
    changes: list[Change] = []
    for entity, entity_id in rows:
        now = after.get((entity, entity_id))
        if now is None:
            continue
        old = before[(entity, entity_id)]
        for field in FIELDS[entity]:
            if now[field] == old[field]:
                continue
            edit = edit if edit is not None else _next_edit()
            changes.append(
                _log(
                    edit, entity, entity_id, field, old[field], now[field], actor=actor, undoes=None
                )
            )
    return tuple(changes)


# --- the work ---------------------------------------------------------------------------------


def _given(values: Mapping[str, Any]) -> dict[str, Any]:
    return {f: v for f, v in values.items() if v is not None}


def _edit(
    entity: Entity,
    entity_id: str,
    values: dict[str, Any],
    *,
    vdb: PostgresVDB | None,
    actor: Actor,
) -> Edit | None:
    if "name" in values and not str(values["name"]).strip():
        raise EditRefused("a name can't be empty")
    with write_transaction():
        done = _write(entity, entity_id, values, edit=None, actor=actor, undoes=None)
    if done is None:
        return None
    changes, databases = done
    return Edit(changes[0].edit, tuple(changes), _sync(vdb, databases))


def _write(
    entity: Entity,
    entity_id: str,
    values: Mapping[str, Any],
    *,
    edit: int | None,
    actor: Actor,
    undoes: int | None,
    expected: Mapping[str, Any] | None = None,
) -> tuple[list[Change], set[str]] | None:
    """Apply `values` to one locked row and log them, inside the caller's transaction. Returns
    the changes and the databases whose vectors they touch, or None when nothing changed."""
    current = _current(entity, entity_id)
    if current is None:
        raise NotFound(f"no {entity.replace('_', ' ')} {entity_id}")
    for field, value in (expected or {}).items():
        if current[field] != value:
            raise Conflict(
                f"a later edit changed the {field} of {entity.replace('_', ' ')} {entity_id}"
            )
    changed = {f: values[f] for f in FIELDS[entity] if f in values and values[f] != current[f]}
    if not changed:
        return None
    if "name" in changed:
        _check_name(entity, entity_id, str(changed["name"]), current)
    touch = actor == "user"
    if entity == "term":
        _apply_term(entity_id, changed, touch=touch)
    else:
        _apply_attribute(entity_id, str(current["term_id"]), changed, touch=touch)
    edit = edit if edit is not None else _next_edit()
    changes = [
        _log(edit, entity, entity_id, f, current[f], v, actor=actor, undoes=undoes)
        for f, v in changed.items()
    ]
    return changes, _databases(entity, entity_id)


def _current(entity: Entity, entity_id: str) -> dict[str, Any] | None:
    """The row's editable fields, locked until the transaction ends."""
    if entity == "term":
        rows = store().query_read(
            "SELECT name, description, coalesce(synonyms, '{}') AS synonyms, name_certified, "
            "description_certified, source FROM term WHERE id = :id FOR UPDATE",
            {"id": entity_id},
        )
    else:
        rows = store().query_read(
            "SELECT a.name, a.description, a.certified, a.source_column, a.term_name, "
            "a.table_id, a.source, at.term_id FROM column_attribute a "
            "JOIN column_attribute__term at ON at.attribute_id = a.id "
            "WHERE a.id = :id ORDER BY at.term_id LIMIT 1 FOR UPDATE OF a",
            {"id": entity_id},
        )
    if not rows:
        return None
    row = dict(rows[0])
    row["synonyms"] = list(row.get("synonyms") or [])
    return row


def _check_name(entity: Entity, entity_id: str, name: str, current: Mapping[str, Any]) -> None:
    if entity == "term":
        taken = store().query_read(
            "SELECT 1 FROM term WHERE name = :name AND source = :source AND id <> :id",
            {"name": name, "source": current["source"], "id": entity_id},
        )
        what = f"a Term named {name!r} exists"
    else:
        # The attribute merge key: name, column, Term, table and source
        taken = store().query_read(
            "SELECT 1 FROM column_attribute WHERE name = :name AND source_column = :column "
            "AND term_name = :term AND table_id = :table AND source = :source AND id <> :id",
            {
                "name": name,
                "column": current["source_column"],
                "term": current["term_name"],
                "table": current["table_id"],
                "source": current["source"],
                "id": entity_id,
            },
        )
        what = f"this column's {current['term_name']} already has an attribute named {name!r}"
    if taken:
        raise NameTaken(what)


def _apply_term(term_id: str, changed: Mapping[str, Any], *, touch: bool) -> None:
    dal_fields = {
        f: changed[f]
        for f in ("name", "description", "name_certified", "description_certified")
        if f in changed and changed[f] is not None
    }
    if dal_fields or touch:
        update_term(term_id, modified_by=LOCAL_OWNER_ID, touch=touch, **dal_fields)
    direct: dict[str, Any] = {}
    if "synonyms" in changed:
        direct["synonyms"] = list(changed["synonyms"])
    if "description" in changed and changed["description"] is None:
        direct["description"] = None
    if direct:
        store().query_write(update(s.term).where(s.term.c.id == term_id).values(**direct))


def _apply_attribute(
    attribute_id: str, term_id: str, changed: Mapping[str, Any], *, touch: bool
) -> None:
    dal_fields = {
        f: changed[f]
        for f in ("name", "description", "certified")
        if f in changed and changed[f] is not None
    }
    update_column_attribute(
        attribute_id, term_id, modified_by=LOCAL_OWNER_ID, touch=touch, **dal_fields
    )
    if "description" in changed and changed["description"] is None:
        store().query_write(
            update(s.column_attribute)
            .where(s.column_attribute.c.id == attribute_id)
            .values(description=None)
        )


def _next_edit() -> int:
    [row] = store().query_write("SELECT nextval('ops.change_log_edit_seq') AS edit")
    return int(row["edit"])


_CHANGE = (
    "SELECT id, edit, ts, entity, entity_id, field, old, new, actor, undoes FROM ops.change_log"
)


def _changes(where: str, params: dict[str, Any]) -> list[Change]:
    return [Change(**row) for row in store().query_read(f"{_CHANGE} {where}", params)]


def _log(
    edit: int,
    entity: Entity,
    entity_id: str,
    field: str,
    old: Any,
    new: Any,
    *,
    actor: Actor,
    undoes: int | None,
) -> Change:
    [row] = store().query_write(
        "INSERT INTO ops.change_log (edit, entity, entity_id, field, old, new, actor, undoes) "
        "VALUES (:edit, :entity, :id, :field, CAST(:old AS jsonb), CAST(:new AS jsonb), "
        ":actor, :undoes) RETURNING id, ts",
        {
            "edit": edit,
            "entity": entity,
            "id": entity_id,
            "field": field,
            "old": json.dumps(old),
            "new": json.dumps(new),
            "actor": actor,
            "undoes": undoes,
        },
    )
    return Change(
        id=int(row["id"]),
        edit=edit,
        ts=cast(datetime, row["ts"]),
        entity=entity,
        entity_id=entity_id,
        field=field,
        old=old,
        new=new,
        actor=actor,
        undoes=undoes,
    )


def _databases(entity: Entity, entity_id: str) -> set[str]:
    """The databases whose semantic layer holds this Term's or attribute's vectors."""
    tables = (
        "JOIN catalog_table t ON t.id = {table_id} "
        "JOIN catalog_schema sc ON sc.id = t.schema_id "
        "JOIN catalog_database d ON d.id = sc.database_id"
    )
    if entity == "term":
        query = (
            "SELECT DISTINCT d.name FROM table__term tt "
            + tables.format(table_id="tt.table_id")
            + " WHERE tt.term_id = :id"
        )
    else:
        query = (
            "SELECT DISTINCT d.name FROM column_attribute a "
            + tables.format(table_id="a.table_id")
            + " WHERE a.id = :id"
        )
    return {str(r["name"]) for r in store().query_read(query, {"id": entity_id})}


def _sync(vdb: PostgresVDB | None, databases: set[str]) -> Synced | None:
    if vdb is None:
        return None
    total = Synced(embedded=0, deleted=0, current=0)
    for database_name in sorted(databases):
        done = sync_database(vdb, database_name)
        total = Synced(
            embedded=total.embedded + done.embedded,
            deleted=total.deleted + done.deleted,
            current=total.current + done.current,
            retagged=total.retagged + done.retagged,
        )
    return total
