"""The grounding pack as the answer agent reads it (ARCHITECTURE.md §8.1): in its system prompt,
and in what `plan` returns for the entities the agent extracted."""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import Any

from ontological.answer.pack import GroundingPack


def render_pack(pack: GroundingPack, *, shown_tables: Collection[str] = ()) -> str:
    """The pack's tables, attributes, SQL attributes, Terms and join paths. A table whose id is
    in `shown_tables` is named, not listed again with its columns."""
    tables = [_table(t) for t in pack.tables if str(t["id"]) not in shown_tables]
    again = [_name(t) for t in pack.tables if str(t["id"]) in shown_tables]
    if again:
        tables.append(f"Also these, shown already: {', '.join(again)}")
    parts = [
        "### Tables\n" + ("\n\n".join(tables) or "(none found)"),
        "### Matched attributes\n"
        + ("\n".join(_attribute(a, pack.term_synonyms) for a in pack.attributes) or "(none found)"),
    ]
    if pack.sql_attributes:
        parts.append(
            "### SQL attributes\nMeasures and filters the ontology defines in SQL. When one is "
            "what the question asks for, use its SQL.\n"
            + "\n".join(_sql_attribute(a) for a in pack.sql_attributes)
        )
    parts.append(
        "### Terms\n"
        + ("\n".join(f"- {t.get('text') or t.get('name')}" for t in pack.terms) or "(none)")
    )
    if paths := render_join_paths(pack):
        parts.append(f"### Join paths\n{paths}")
    return "\n\n".join(parts)


def render_coverage(pack: GroundingPack) -> str:
    """The databases retrieval found the question in, best first, for a question with no
    scope."""
    lines = [
        f"- {c.database}: {len(c.entities)} of the question's searches covered, {c.hits} hit(s)"
        for c in pack.coverage
    ]
    return "\n".join(lines)


def render_join_paths(pack: GroundingPack) -> str:
    """Upstream's `attribute_join_paths` from the anchor, one line per attribute."""
    if pack.anchor is None or not pack.join_paths:
        return ""
    anchor = pack.anchor
    lines = [f"From the anchor {anchor['table_name']}.{anchor['col_name']}:"]
    for entry in pack.join_paths:
        place = f"{entry['table_name']}.{entry['col_name']}"
        hops: list[dict[str, str]] = entry.get("path") or []
        if not hops:
            via = "same table" if entry["table_name"] == anchor["table_name"] else "no path"
        else:
            via = render_hops(hops)
        lines.append(f"- {place}: {via}")
    return "\n".join(lines)


def render_hops(hops: Iterable[dict[str, str]]) -> str:
    return " → ".join(
        f"{h['source_table']}.{h['source_column']} = {h['target_table']}.{h['target_column']}"
        for h in hops
    )


def _name(table: dict[str, Any]) -> str:
    return f"{table.get('schema_name') or 'main'}.{table['name']}"


def _table(table: dict[str, Any]) -> str:
    head = f"{_name(table)} (table id {table['id']})"
    if table.get("description"):
        head += f": {table['description']}"
    if table.get("pk"):
        head += f"\n  primary key: {', '.join(table['pk'])}"
    lines = [head]
    columns: list[dict[str, Any]] = table.get("columns") or []
    for column in columns:
        line = f"  - {column['name']}"
        if column.get("data_type"):
            line += f" {column['data_type']}"
        if column.get("description"):
            line += f": {column['description']}"
        if samples := column.get("sample_values"):
            line += f" (e.g. {_samples(samples)})"
        if column.get("semantic_fk"):
            line += f" → {column['semantic_fk']}"
        lines.append(line)
    return "\n".join(lines)


def _attribute(attribute: dict[str, Any], synonyms: dict[str, list[str]]) -> str:
    line = (
        f"- {attribute['table_name']}.{attribute['col_name']} (attribute id {attribute['id']}): "
        f"{attribute.get('attr_name') or ''}"
    )
    if attribute.get("attr_description"):
        line += f", {attribute['attr_description']}"
    if term := attribute.get("term_name"):
        line += f" [Term {term}"
        if synonyms.get(term):
            line += f"; also {', '.join(synonyms[term])}"
        line += "]"
    return line


def _sql_attribute(attribute: dict[str, Any]) -> str:
    line = f"- {attribute['name']}"
    if attribute.get("description"):
        line += f": {attribute['description']}"
    if attribute.get("term_name"):
        line += f" [Term {attribute['term_name']}]"
    return f"{line}\n  SQL: {attribute.get('sql') or attribute.get('expression')}"


def _samples(values: Iterable[Any]) -> str:
    return ", ".join(str(v) for v in list(values)[:5])
