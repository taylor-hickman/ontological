"""The compile plan: the order tables compile in, and the packs small tables share (BUILD_PLAN.md
M3.1; ARCHITECTURE.md §6.4).

A pack's tables go to Claude in one call per stage, so a pack's prompt, and its cache key, is
the set of tables in it. The plan is a function of the tables alone, never of the order they
were fetched in, and a job stores it in `ops.job_step` when it starts, so a paused or crashed
compile resumes on the packs it began with rather than repacking what is left. Two more things
keep packs stable from one run to the next:

- A pack holds tables of one schema, so a change in one schema doesn't move another's packs.
- A wide table is a pack of its own, outside the run of small ones, so adding or dropping one
  doesn't move any small table.

Inside a schema, small tables fill packs greedily in canonical order up to `PACK_TOKEN_BUDGET`.
Sizes are estimated from column counts, since the plan is made before profiling; M3.2, which
sends the packed calls, calibrates the estimate against real prompts.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

SMALL_TABLE_COLUMNS = 6  # §6.4: tables with at most this many columns are packed
PACK_TOKEN_BUDGET = 8_000  # §6.4: about 8k tokens of input to a pack
TABLE_TOKENS = 150  # a table's heading and description in a prompt, estimated
COLUMN_TOKENS = 100  # a column's line with its type and samples, estimated


@dataclass(frozen=True)
class PlannedTable:
    id: str
    schema_name: str
    name: str
    columns: int


@dataclass(frozen=True)
class PlannedStep:
    position: int  # in canonical order, from 0
    table: PlannedTable
    pack: int  # packs are numbered from 0, in the order of their first table


def commit_key(
    database_name: str, schema_name: str, name: str, table_id: str = ""
) -> tuple[str, ...]:
    """Upstream's canonical table order, which decides who keeps an unqualified Term name when
    two tables propose the same one: casefolded names, then exact names, then the id."""
    parts = (database_name, schema_name, name)
    return (*[p.casefold() for p in parts], *parts, table_id)


def estimated_tokens(table: PlannedTable) -> int:
    return TABLE_TOKENS + COLUMN_TOKENS * table.columns


def is_small(table: PlannedTable) -> bool:
    return table.columns <= SMALL_TABLE_COLUMNS


def plan(database_name: str, tables: Iterable[PlannedTable]) -> list[PlannedStep]:
    ordered = sorted(tables, key=lambda t: commit_key(database_name, t.schema_name, t.name, t.id))
    groups: list[list[int]] = []  # positions, one list per pack
    open_pack: dict[str, tuple[list[int], int]] = {}  # schema -> its filling pack and size
    for position, table in enumerate(ordered):
        if not is_small(table):
            groups.append([position])
            continue
        size = estimated_tokens(table)
        current = open_pack.get(table.schema_name)
        if current is not None and current[1] + size <= PACK_TOKEN_BUDGET:
            current[0].append(position)
            open_pack[table.schema_name] = (current[0], current[1] + size)
            continue
        members = [position]
        groups.append(members)
        open_pack[table.schema_name] = (members, size)
    pack_of = {p: number for number, members in enumerate(groups) for p in members}
    return [PlannedStep(p, t, pack_of[p]) for p, t in enumerate(ordered)]
