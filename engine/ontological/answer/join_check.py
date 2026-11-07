# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's
# auto_ontology/retrieval/text_to_sql/db_probe/join_path_check.py at 2a24f5d, for check 4 of the
# answer gate (ARCHITECTURE.md §8.2). The semantic-FK graph is read through a `JoinGraph` bound to
# the question's database, in place of the DAL functions upstream calls with `database_name`
# (`ontological.answer.ontology` implements it with those functions). Probes go through
# `ontological.answer.probe`, and name a table with the schema the SQL gave it. The self-apply
# rewrites (`try_self_apply_wrong_column_fixes`, `try_self_apply_missing_bridge_fixes`) aren't
# ported: the gate reports issues, and the model rewrites its own SQL.

# sqlglot's expression tree is loosely typed.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportAttributeAccessIssue=false
# pyright: reportPrivateImportUsage=false, reportArgumentType=false
# pyright: reportMissingTypeArgument=false, reportUnknownParameterType=false

"""Join-path verification for the repair path.

Sibling of ``literal_check.py``/``jsonb_path_check.py``: those catch a
hallucinated filter value or JSONB key; this catches a hallucinated **join
column** — an ``ON a.col = b.col`` predicate between two tables that aren't
actually related that way. Unlike a bad literal or JSONB key, a fabricated
join is syntactically and semantically valid SQL (both columns exist, types
match) and returns rows, so nothing upstream (parse validation, execution)
ever errors on it — it just silently returns the wrong data.

Ground truth here is the same ``SEMANTIC_FK`` graph already used to build
``attribute_join_paths`` for SQL generation (see
``auto_ontology/dal/attributes.py::find_join_path``), queried directly against the
*actual* join predicates the model wrote, not just the columns it was told
about up front. Three verdicts:

- a **different**, real join path connects the same two tables (either a
  direct edge on different columns, or only via intermediate bridge
  table(s)) — repairable: return the correct hops so reconstruction can be
  told exactly what to use, mirroring the "KNOWN JOIN KEYS" mechanism.
- both columns share a **hub**: no forward path either way, but each holds
  its own real, PK-anchored ``SEMANTIC_FK`` edge to the same attribute (see
  ``find_shared_hub_bridge``) — also repairable, routed through the hub.
- **no** path or shared hub is known in the graph at all. This does not, on
  its own, mean the join is wrong — the graph can be incomplete for
  relationships ingestion never resolved. A live value-overlap probe (same
  idea as ``semantic_fk.py``'s ingestion-time sample-value fallback) is used
  as a secondary, confirmatory signal: near-zero overlap between the two
  join columns' actual values is treated as a fabricated join; substantial
  overlap is treated as "probably a real, just-unmodeled relationship" and
  left alone, to avoid flagging legitimate business joins the graph was
  never meant to cover.

Only equality predicates inside a ``JOIN ... ON`` clause are considered, and
only when at least one side is a column already known to the FK graph in
some capacity (see ``column_participates_in_semantic_fk``) — this keeps the
check scoped to the FK-shaped join-hallucination failure mode it targets,
not arbitrary business-logic joins (date ranges, status matches) the graph
was never meant to model.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol

import sqlglot
from sqlglot import exp

from ontological.answer.literal_check import (
    _as_column,  # pyright: ignore[reportPrivateUsage]
    _candidate_tables,  # pyright: ignore[reportPrivateUsage]
    _table_nodes,  # pyright: ignore[reportPrivateUsage]
)
from ontological.answer.probe import ProbeExecutor

if TYPE_CHECKING:  # the gate imports this module
    from ontological.answer.gate import KeyColumns

logger = logging.getLogger(__name__)


class JoinGraph(Protocol):
    """The question's database's semantic-FK graph, as upstream's DAL reads it."""

    def column_id(self, table: str, column: str) -> str | None:
        """`find_column_id_by_table_and_name`: the column's id, either name in any case."""
        ...

    def join_path(self, col_a_id: str, col_b_id: str) -> list[dict[str, Any]]:
        """`find_join_path`: the SEMANTIC_FK hops from one column to the other, outgoing only."""
        ...

    def in_semantic_fk(self, col_id: str) -> bool:
        """`column_participates_in_semantic_fk`."""
        ...

    def shared_hub(self, col_a_id: str, col_b_id: str) -> dict[str, Any]:
        """`find_shared_hub_bridge`: `hub_table` and `hub_column`, or nothing."""
        ...

    def key_columns(self, table: str) -> KeyColumns:
        """`find_table_key_columns`."""
        ...


# How much value-overlap between two join columns counts as "probably a real
# relationship, just not modeled in the graph" — below this, treat the join
# as fabricated. Deliberately generous: this only fires when the graph has
# nothing to say either way, so a false "leave it alone" is far cheaper than
# a false "flag a legitimate join".
_OVERLAP_KEEP_THRESHOLD = 0.2


def _find_join_path_either_direction(graph: JoinGraph, col_a_id: str, col_b_id: str) -> list[dict]:
    """``find_join_path``, tried in both column orders.

    ``find_join_path`` walks ``SEMANTIC_FK`` outgoing-only (child FK ->
    parent attribute — see its docstring), so a real, single-hop edge is
    only found when the *child* column is passed as the anchor. Every
    caller here derives ``col_a``/``col_b`` from a written predicate's
    left/right order, which has nothing to do with which side is the FK
    child — a real edge written with the parent on the left (e.g.
    ``parent.id = child.parent_id``) would otherwise come back with 0 hops
    purely because of how the model happened to order the equality, and
    fall through to "unverified" even though it's correct. Trying the
    reverse order costs one extra graph query only when the forward one
    finds nothing.
    """
    hops = graph.join_path(col_a_id, col_b_id)
    if hops:
        return hops
    return graph.join_path(col_b_id, col_a_id)


def _join_equalities(tree: exp.Expression) -> list[tuple[exp.Column, exp.Column]]:
    """Column-to-column equality predicates that express a join, wherever written.

    Two syntactic shapes carry the same join topology as an explicit
    ``JOIN ... ON``, and are collected here too:

    - a ``WHERE`` clause connecting two tables the old, implicit way
      (``FROM a, b WHERE a.col = b.col``);
    - a correlated subquery's ``WHERE`` comparing an outer-query column to an
      inner-query column (e.g. ``(SELECT ... FROM b WHERE b.col = a.col)`` in
      the outer query's projection or filter) — this is exactly as capable of
      encoding a hallucinated relationship as a real ``JOIN``, but was
      previously invisible to this check entirely.

    ``tree.find_all(exp.Where)`` walks every nesting level, so a correlated
    subquery's own ``WHERE`` is picked up alongside the top-level one without
    extra recursion here. Predicates already inside a ``JOIN ... ON`` are not
    re-found via ``Where`` (``ON`` is a distinct clause), so nothing is
    double-counted between the two loops below.

    Only predicates where *both* sides are bare columns are considered — a
    join condition compares two identity columns, not a column against a
    literal or an expression (those are filters, not join topology). Same-
    table and unresolvable-alias predicates are filtered out by the caller
    (see ``find_join_path_mismatches``), so a same-table ``WHERE`` filter
    (e.g. ``a.status = a.other_col``) is never mistaken for a join here.
    """
    out: list[tuple[exp.Column, exp.Column]] = []
    for join in tree.find_all(exp.Join):
        on = join.args.get("on")
        if on is None:
            continue
        for eq in on.find_all(exp.EQ):
            left = _as_column(eq.this)
            right = _as_column(eq.expression)
            if left is not None and right is not None:
                out.append((left, right))
    for where in tree.find_all(exp.Where):
        for eq in where.find_all(exp.EQ):
            left = _as_column(eq.this)
            right = _as_column(eq.expression)
            if left is not None and right is not None:
                out.append((left, right))
    return out


def _resolve_table(
    col: exp.Column, all_nodes: list[exp.Table], by_key: dict[str, exp.Table]
) -> exp.Table | None:
    """The table node a column belongs to, per the SQL's own aliasing."""
    candidates = _candidate_tables(col, all_nodes, by_key)
    if len(candidates) == 1:
        return candidates[0]
    return None


def _table_ref(node: exp.Table, dialect: str | None, *, quoted: bool) -> str:
    """*node* for a probe's FROM: its schema and name as the SQL wrote them, with no alias."""
    return exp.table_(node.name, db=node.db or None, quoted=quoted).sql(dialect=dialect or None)


def _value_overlap(
    executor: ProbeExecutor,
    dialect: str | None,
    table_a: exp.Table,
    col_a: str,
    table_b: exp.Table,
    col_b: str,
) -> float | None:
    """Fraction of *table_a*'s distinct ``col_a`` values also present in *table_b*'s ``col_b``.

    ``None`` when the probe couldn't run (no connector, budget exhausted,
    query failure) — callers must treat that as "can't confirm", not as a
    verdict either way.

    Identifiers are always quoted: *table_a*/*col_a*/etc. are already the
    real, correctly-cased names resolved from schema metadata by the
    caller, and an unquoted reference here would let the engine's own
    case-folding rules silently rewrite it (e.g. Postgres lowercases an
    unquoted ``ArtifactsCore`` to ``artifactscore``, which then doesn't
    exist) — producing a query-execution failure that this function can
    only see as "probe couldn't run," so the mismatch falls through to
    "unverified" even when the join is actually correct. Quoting the
    already-correct case is a no-op for lowercase-only identifiers in every
    dialect this pipeline targets, so this can't regress a case that works
    today — it only fixes the mixed-case ones that silently didn't.
    """
    d = dialect or None
    a_ref = exp.column(col_a, quoted=True).sql(dialect=d)
    b_ref = exp.column(col_b, quoted=True).sql(dialect=d)
    a_table_ref = _table_ref(table_a, d, quoted=True)
    b_table_ref = _table_ref(table_b, d, quoted=True)
    sql = (
        f"SELECT "
        f"COUNT(DISTINCT a.{a_ref}) AS total, "
        f"COUNT(DISTINCT CASE WHEN b.{b_ref} IS NOT NULL THEN a.{a_ref} END) AS matched "
        f"FROM {a_table_ref} a "
        f"LEFT JOIN {b_table_ref} b ON a.{a_ref} = b.{b_ref} "
        f"WHERE a.{a_ref} IS NOT NULL"
    )
    res = executor.run(sql, purpose="join_path_check_overlap")
    if not res["ok"] or not res["rows"]:
        return None
    row = res["rows"][0]
    total = row.get("total") or 0
    matched = row.get("matched") or 0
    if not total:
        return None
    return matched / total


def _overlap(
    executor: ProbeExecutor,
    dialect: str | None,
    a: tuple[exp.Table, str],
    b: tuple[exp.Table, str],
) -> float | None:
    """``_value_overlap`` of *a*'s values in *b*, or else of *b*'s in *a*; ``None`` when neither
    probe could run."""
    if not executor.budget_left:
        return None
    overlap = _value_overlap(executor, dialect, a[0], a[1], b[0], b[1])
    if overlap is None:
        overlap = _value_overlap(executor, dialect, b[0], b[1], a[0], a[1])
    return overlap


def find_join_path_mismatches(
    executor: ProbeExecutor,
    dialect: str | None,
    sql: str,
    graph: JoinGraph,
) -> list[dict[str, Any]]:
    """Return join predicates that don't correspond to a known FK relationship.

    Each entry: ``{"table_a", "col_a", "table_b", "col_b", "verdict", "hops",
    "bridge_tables"}``. ``verdict`` is one of:

    - ``"wrong_column"``: same two tables, a different real edge exists — use
      ``hops[0]`` instead.
    - ``"missing_bridge"``: these two tables should not be joined directly at
      all — route through ``hops`` (which cross ``bridge_tables``).
    - ``"unverified"``: no known path either way, and (when a live probe was
      possible) value overlap was low — likely fabricated, but there is no
      known repair; ``hops``/``bridge_tables`` are empty.

    Empty list means every checked predicate matched a known edge, or (when
    the graph had nothing to say and no probe could run) nothing could be
    confirmed as wrong — never treated as "the whole query is fine" beyond
    what was actually checked.
    """
    try:
        tree = sqlglot.parse_one(sql, read=dialect or None)
    except Exception as exc:
        logger.info("join_path_check: could not parse SQL (%s)", exc)
        return []

    all_nodes, by_key = _table_nodes(tree)
    if not all_nodes:
        return []

    mismatches: list[dict[str, Any]] = []
    checked_pairs: set[tuple[str, str, str, str]] = set()

    for left, right in _join_equalities(tree):
        node_a = _resolve_table(left, all_nodes, by_key)
        node_b = _resolve_table(right, all_nodes, by_key)
        if node_a is None or node_b is None:
            continue  # unresolvable alias — not this check's job
        table_a, table_b = node_a.name, node_b.name
        if not table_a or not table_b or table_a.lower() == table_b.lower():
            continue  # self-join — not this check's job

        pair_key = tuple(
            sorted(
                [
                    (table_a.lower(), left.name.lower()),
                    (table_b.lower(), right.name.lower()),
                ]
            )
        )
        flat_key = (pair_key[0][0], pair_key[0][1], pair_key[1][0], pair_key[1][1])
        if flat_key in checked_pairs:
            continue
        checked_pairs.add(flat_key)

        col_a_id = graph.column_id(table_a, left.name)
        col_b_id = graph.column_id(table_b, right.name)
        if not col_a_id or not col_b_id:
            continue  # can't resolve — never flag on missing information

        if not (graph.in_semantic_fk(col_a_id) or graph.in_semantic_fk(col_b_id)):
            continue  # neither side is FK-shaped — out of scope for this check

        hops = _find_join_path_either_direction(graph, col_a_id, col_b_id)

        if len(hops) == 1:
            hop = hops[0]
            hop_pair = {
                (hop["source_table"].lower(), hop["source_column"].lower()),
                (hop["target_table"].lower(), hop["target_column"].lower()),
            }
            written_pair = {
                (table_a.lower(), left.name.lower()),
                (table_b.lower(), right.name.lower()),
            }
            if hop_pair == written_pair:
                continue  # verified — exactly what the graph says

            # The graph knows *a* direct relationship between these two
            # tables — but two tables can legitimately have more than one
            # (e.g. a treatments table with both prescribing_clinician_id
            # and reviewing_clinician_id, each a real FK to clinicians). The
            # graph having learned one doesn't mean the model's different,
            # also-real column pair is wrong — same reasoning as the
            # missing_bridge case above, so the same live check applies
            # before concluding it's fabricated.
            overlap = _overlap(executor, dialect, (node_a, left.name), (node_b, right.name))
            if overlap is not None and overlap >= _OVERLAP_KEEP_THRESHOLD:
                logger.info(
                    "join_path_check: graph knows a different column pair for "
                    "%s <-> %s, but live overlap %.2f >= threshold on the "
                    "written %s.%s = %s.%s — treating it as a real, "
                    "additional relationship and leaving it alone",
                    table_a,
                    table_b,
                    overlap,
                    table_a,
                    left.name,
                    table_b,
                    right.name,
                )
                continue

            mismatches.append(
                {
                    "table_a": table_a,
                    "col_a": left.name,
                    "table_b": table_b,
                    "col_b": right.name,
                    "verdict": "wrong_column",
                    "hops": [hop],
                    "bridge_tables": [],
                }
            )
            continue

        if len(hops) > 1:
            # The graph knows *an* indirect path — but unlike the "no path
            # at all" branch below, this used to trust that as proof the
            # model's direct join is wrong, with no live check. That's only
            # true when the direct columns aren't *also* a real relationship
            # the graph simply never modeled (e.g. an undeclared FK-shaped
            # column with no formal constraint) — the graph having *an*
            # answer doesn't mean it has the *only* answer. Apply the same
            # value-overlap safety net used below before concluding the
            # direct join is fabricated, so a correct-but-unmodeled direct
            # join isn't torn out in favor of a technically-known but wrong
            # indirect route.
            overlap = _overlap(executor, dialect, (node_a, left.name), (node_b, right.name))
            if overlap is not None and overlap >= _OVERLAP_KEEP_THRESHOLD:
                logger.info(
                    "join_path_check: graph only knows an indirect path for "
                    "%s.%s = %s.%s, but live overlap %.2f >= threshold — "
                    "treating the direct join as real and leaving it alone",
                    table_a,
                    left.name,
                    table_b,
                    right.name,
                    overlap,
                )
                continue

            bridge_tables = sorted(
                {h[side] for h in hops for side in ("source_table", "target_table")}
                - {table_a, table_b}
            )
            mismatches.append(
                {
                    "table_a": table_a,
                    "col_a": left.name,
                    "table_b": table_b,
                    "col_b": right.name,
                    "verdict": "missing_bridge",
                    "hops": hops,
                    "bridge_tables": bridge_tables,
                }
            )
            continue

        # No path at all via the forward-only traversal. Before falling
        # back to a live probe, check for a shared-identity hub: two
        # columns can each hold a real, PK-anchored forward SEMANTIC_FK to
        # the same attribute without either being reachable from the
        # other (find_join_path's guard against fabricating a join between
        # two coincidentally-shared-target columns also hides this
        # legitimate case). Trusted at the same confidence as the
        # multi-hop `missing_bridge` branch above — both sides already
        # have a real, ingested FK edge, so no extra probe is needed here.
        hub = graph.shared_hub(col_a_id, col_b_id)
        if hub.get("hub_table") and hub.get("hub_column"):
            # A direct join between two hub-shared FK columns is only worth
            # flagging when NEITHER side is itself unique/PK-backed on its
            # own table. When one side is (e.g. a satellite table's PK is
            # also its FK to the hub), that column is guaranteed 1:1 with
            # the hub row, so `A.col = B.col` directly is mathematically
            # identical to routing `A -> hub <- B` through the bridge — just
            # without the extra join.
            keys_a = graph.key_columns(table_a)
            keys_b = graph.key_columns(table_b)
            col_a_is_identity = left.name.lower() in (keys_a.pk + keys_a.unique)
            col_b_is_identity = right.name.lower() in (keys_b.pk + keys_b.unique)
            if col_a_is_identity or col_b_is_identity:
                continue  # direct join is provably equivalent — not a mismatch
            mismatches.append(
                {
                    "table_a": table_a,
                    "col_a": left.name,
                    "table_b": table_b,
                    "col_b": right.name,
                    "verdict": "missing_bridge",
                    "hops": [
                        {
                            "source_schema": "",
                            "source_table": table_a,
                            "source_column": left.name,
                            "target_schema": "",
                            "target_table": hub["hub_table"],
                            "target_column": hub["hub_column"],
                        },
                        {
                            "source_schema": "",
                            "source_table": table_b,
                            "source_column": right.name,
                            "target_schema": "",
                            "target_table": hub["hub_table"],
                            "target_column": hub["hub_column"],
                        },
                    ],
                    "bridge_tables": [hub["hub_table"]],
                }
            )
            continue

        # No shared hub either — the graph has nothing to confirm or deny
        # this join. Fall back to a live value-overlap probe as a
        # secondary signal before flagging, to avoid false-positiving on a
        # real relationship the graph never happened to model at all.
        overlap = _overlap(executor, dialect, (node_a, left.name), (node_b, right.name))
        if overlap is not None and overlap >= _OVERLAP_KEEP_THRESHOLD:
            logger.info(
                "join_path_check: no graph edge for %s.%s = %s.%s, but live "
                "overlap %.2f >= threshold — treating as a real, unmodeled "
                "relationship and leaving it alone",
                table_a,
                left.name,
                table_b,
                right.name,
                overlap,
            )
            continue

        mismatches.append(
            {
                "table_a": table_a,
                "col_a": left.name,
                "table_b": table_b,
                "col_b": right.name,
                "verdict": "unverified",
                "hops": [],
                "bridge_tables": [],
                "overlap": overlap,
            }
        )

    return mismatches


def build_join_path_repair_error(mismatches: list[dict[str, Any]]) -> str:
    """Render mismatches into a targeted reconstruction instruction."""
    lines = []
    for m in mismatches:
        written = f"{m['table_a']}.{m['col_a']} = {m['table_b']}.{m['col_b']}"
        if m["verdict"] == "wrong_column":
            hop = m["hops"][0]
            correct = (
                f"{hop['source_table']}.{hop['source_column']} = "
                f"{hop['target_table']}.{hop['target_column']}"
            )
            lines.append(
                f"- You joined {written}, but the real relationship between "
                f"these tables uses {correct}. Use the correct column pair."
            )
        elif m["verdict"] == "missing_bridge":
            chain = " -> ".join(
                f"{h['source_table']}.{h['source_column']} = "
                f"{h['target_table']}.{h['target_column']}"
                for h in m["hops"]
            )
            lines.append(
                f"- You joined {written} directly, but these two tables are "
                f"not directly related. Route the join through "
                f"{', '.join(m['bridge_tables'])} instead, using: {chain}."
            )
        else:
            lines.append(
                f"- You joined {written}, but no relationship between these "
                f"columns could be verified and their actual values barely "
                f"overlap. This join is very likely wrong. Before looking for "
                f"a way to connect these two tables, first check whether the "
                f"value you need is already available on a table you're "
                f"already joined to — you may not need this join at all. If "
                f"it genuinely isn't available anywhere in scope, the value "
                f"likely comes from a different table entirely, not just a "
                f"different column pair between these same two tables."
            )
    body = "\n".join(lines)
    # "unverified" mismatches have no known correct join — the real fix may
    # be dropping the join and sourcing the value from elsewhere entirely
    # (see the per-item message above), which the blanket "change only the
    # join" instruction below would otherwise forbid.
    has_unverified = any(m["verdict"] == "unverified" for m in mismatches)
    scope_instruction = (
        "Rewrite the SQL using the correct join condition(s) above. Change "
        "ONLY the mismatched join(s); keep all other joins, columns, "
        "grouping, and filters exactly as they are."
        if not has_unverified
        else "Rewrite the SQL to fix the join(s) flagged above. For any "
        "flagged as unverified, you may need to change which table/column "
        "supplies that value entirely (including dropping the join and "
        "reading it from a table already in scope), not just swap the join "
        "predicate. Leave every other join, column, grouping, and filter "
        "exactly as they are."
    )
    return (
        "One or more JOIN conditions in the generated SQL do not match a "
        "real relationship between the tables, so the query silently joins "
        "the wrong rows instead of erroring:\n"
        f"{body}\n\n"
        f"{scope_instruction}"
    )


def _group_join_equalities_by_table_pair(
    tree: exp.Expression,
    all_nodes: list[exp.Table],
    by_key: dict[str, exp.Table],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Group column-equality join predicates by the (table_a, table_b) they connect.

    Each group entry is ``{"left", "right", "table_left", "table_right"}`` —
    the raw predicate kept oriented exactly as written, plus its resolved
    (real, unaliased) table names. Both orientations of the same two tables
    (``a.col = b.col`` and ``b.col = a.col``) land in one group, keyed by the
    table names sorted case-insensitively.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for left, right in _join_equalities(tree):
        node_left = _resolve_table(left, all_nodes, by_key)
        node_right = _resolve_table(right, all_nodes, by_key)
        if node_left is None or node_right is None:
            continue
        table_left, table_right = node_left.name, node_right.name
        if not table_left or not table_right or table_left.lower() == table_right.lower():
            continue
        key = tuple(sorted([table_left.lower(), table_right.lower()]))
        groups.setdefault(key, []).append(
            {
                "left": left,
                "right": right,
                "table_left": table_left,
                "table_right": table_right,
                "node_left": node_left,
                "node_right": node_right,
            }
        )
    return groups


def _is_verified_edge(predicate: dict[str, Any], graph: JoinGraph) -> bool:
    """Whether *predicate* is exactly the graph-known relationship between its two tables."""
    left, right = predicate["left"], predicate["right"]
    table_left, table_right = predicate["table_left"], predicate["table_right"]
    col_a_id = graph.column_id(table_left, left.name)
    col_b_id = graph.column_id(table_right, right.name)
    if not col_a_id or not col_b_id:
        return False
    hops = _find_join_path_either_direction(graph, col_a_id, col_b_id)
    if len(hops) != 1:
        return False
    hop = hops[0]
    hop_pair = {
        (hop["source_table"].lower(), hop["source_column"].lower()),
        (hop["target_table"].lower(), hop["target_column"].lower()),
    }
    written_pair = {
        (table_left.lower(), left.name.lower()),
        (table_right.lower(), right.name.lower()),
    }
    return hop_pair == written_pair


def _oriented_columns(predicate: dict[str, Any], table_a: str) -> tuple[str, str]:
    """``(a_col, b_col)`` for *predicate*, remapped onto the fixed ``a``/``b`` aliases."""
    if predicate["table_left"].lower() == table_a.lower():
        return predicate["left"].name, predicate["right"].name
    return predicate["right"].name, predicate["left"].name


def _count_case_dirty_rejects(
    executor: ProbeExecutor,
    dialect: str | None,
    table_a: exp.Table,
    table_b: exp.Table,
    core: list[dict[str, Any]],
    extra: list[dict[str, Any]],
) -> tuple[int, int] | None:
    """``(total_rejects, casing_only_rejects)`` among core-matched rows the extra
    predicate(s) reject.

    Rather than just comparing row counts with/without the extra predicate
    (which can't tell "dirty data" apart from "correctly excluding a
    different real-world row"), this inspects the rejected rows directly:
    ``casing_only_rejects`` counts how many of them would pass if the extra
    predicate's columns were compared via ``LOWER(TRIM(...))`` instead of raw
    equality — i.e. the two sides agree in substance and differ only in
    casing/whitespace. ``None`` on probe failure.
    """
    d = dialect or None
    a_ref = _table_ref(table_a, d, quoted=False)
    b_ref = _table_ref(table_b, d, quoted=False)

    def _eq(a_col: str, b_col: str, normalized: bool) -> str:
        a_ref_col = f"a.{exp.column(a_col).sql(dialect=d)}"
        b_ref_col = f"b.{exp.column(b_col).sql(dialect=d)}"
        if normalized:
            return f"LOWER(TRIM({a_ref_col})) = LOWER(TRIM({b_ref_col}))"
        return f"{a_ref_col} = {b_ref_col}"

    core_on = " AND ".join(_eq(*_oriented_columns(p, table_a.name), False) for p in core)
    extra_raw = " AND ".join(_eq(*_oriented_columns(p, table_a.name), False) for p in extra)
    extra_normalized = " AND ".join(_eq(*_oriented_columns(p, table_a.name), True) for p in extra)

    # SUM(CASE WHEN ... THEN 1 ELSE 0 END) instead of COUNT(*) FILTER (WHERE ...):
    # FILTER is Postgres/standard-SQL syntax, not supported by MySQL, so this
    # keeps the probe portable across every dialect this module supports.
    sql = (
        f"SELECT "
        f"SUM(CASE WHEN NOT ({extra_raw}) THEN 1 ELSE 0 END) AS total_rejects, "
        f"SUM(CASE WHEN NOT ({extra_raw}) AND ({extra_normalized}) THEN 1 ELSE 0 END) "
        f"AS casing_only_rejects "
        f"FROM {a_ref} a JOIN {b_ref} b ON {core_on}"
    )
    res = executor.run(sql, purpose="join_case_dirty_rejects")
    if not res["ok"] or not res["rows"]:
        return None
    row = res["rows"][0]
    try:
        return int(row.get("total_rejects") or 0), int(row.get("casing_only_rejects") or 0)
    except (TypeError, ValueError):
        return None


def find_case_dirty_join_mismatches(
    executor: ProbeExecutor,
    dialect: str | None,
    sql: str,
    graph: JoinGraph,
) -> list[dict[str, Any]]:
    """Return compound joins where an extra predicate rejects real matches
    purely due to casing/whitespace, not a real difference.

    When a query joins two tables on more than one equality predicate and one
    of them is the graph-verified relationship, the extra predicate(s) are
    usually added as defensive "belt and suspenders." That's only safe to
    remove if it's actually dirty data — an extra predicate can also be doing
    its job, correctly excluding a genuinely different real-world row that
    happens to share the verified key. A plain row-count comparison can't
    tell those apart, so this inspects the *rejected* rows directly: among
    rows the verified predicate alone matches but the extra predicate
    rejects, are all of them cases where the extra predicate's columns agree
    once ``LOWER(TRIM(...))``'d? Only then is it flagged — a real semantic
    difference (even one rejected row) leaves the check silent.

    Each entry: ``{"table_a", "table_b", "verified_columns", "extra_columns",
    "rejected_rows", "verdict": "case_dirty_join"}``.
    """
    try:
        tree = sqlglot.parse_one(sql, read=dialect or None)
    except Exception as exc:
        logger.info("case_dirty_join_check: could not parse SQL (%s)", exc)
        return []

    all_nodes, by_key = _table_nodes(tree)
    if not all_nodes:
        return []

    groups = _group_join_equalities_by_table_pair(tree, all_nodes, by_key)
    mismatches: list[dict[str, Any]] = []

    for predicates in groups.values():
        if len(predicates) < 2 or not executor.budget_left:
            continue

        verified = [p for p in predicates if _is_verified_edge(p, graph)]
        verified_ids = {id(p) for p in verified}
        extra = [p for p in predicates if id(p) not in verified_ids]
        if not verified or not extra:
            continue  # need a known-good core to test the extra predicate against

        table_a, table_b = predicates[0]["table_left"], predicates[0]["table_right"]
        counts = _count_case_dirty_rejects(
            executor,
            dialect,
            predicates[0]["node_left"],
            predicates[0]["node_right"],
            verified,
            extra,
        )
        if counts is None:
            continue  # probe failed
        total_rejects, casing_only_rejects = counts

        if total_rejects > 0 and total_rejects == casing_only_rejects:
            # Every rejected row agrees once normalized — a confirmed casing
            # artifact, not a real difference.
            mismatches.append(
                {
                    "table_a": table_a,
                    "table_b": table_b,
                    "verified_columns": [
                        f"{p['table_left']}.{p['left'].name} = {p['table_right']}.{p['right'].name}"
                        for p in verified
                    ],
                    "extra_columns": [
                        f"{p['table_left']}.{p['left'].name} = {p['table_right']}.{p['right'].name}"
                        for p in extra
                    ],
                    "rejected_rows": total_rejects,
                    "verdict": "case_dirty_join",
                }
            )

    return mismatches


def build_case_dirty_join_repair_error(mismatches: list[dict[str, Any]]) -> str:
    """Render case-dirty-join mismatches into a targeted reconstruction instruction."""
    lines = []
    for m in mismatches:
        core = ", ".join(m["verified_columns"])
        extra = ", ".join(m["extra_columns"])
        lines.append(
            f"- Joining {m['table_a']} to {m['table_b']} on {core} AND {extra} "
            f"silently rejects {m['rejected_rows']} row(s) that the verified "
            f"relationship ({core}) alone matches — and in every one of them, "
            f"{extra} agree once both sides are lower-cased and trimmed. This "
            f"is dirty data (casing/whitespace), not a real mismatch."
        )
    body = "\n".join(lines)
    return (
        "One or more JOINs use an extra condition that rejects real matches "
        "purely due to inconsistent casing/whitespace between the two tables:\n"
        f"{body}\n\n"
        "Wrap that condition's columns in LOWER(TRIM(...)) on both sides "
        "instead of comparing them raw. Do NOT drop the condition — it may "
        "still be needed to correctly exclude other, genuinely different "
        "rows. Change ONLY the affected condition; keep all other joins, "
        "columns, grouping, and filters exactly as they are."
    )


__all__ = [
    "JoinGraph",
    "build_case_dirty_join_repair_error",
    "build_join_path_repair_error",
    "find_case_dirty_join_mismatches",
    "find_join_path_mismatches",
]
