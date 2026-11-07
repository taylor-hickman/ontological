"""The pre-compile estimate (ARCHITECTURE.md §6.5, BUILD_PLAN.md M3.7): before a compile spends
quota, the calls, the quota and the time it would take with `split` and with `merged`, so the
person can scope it, or pick merged for a large run.

The calls are counted the way compile makes them, from the same plan (`compile.packing`) and the
catalog: per pack, split asks `table_keys` once, `column_descriptions` once a batch of 15
undescribed columns (`descriptions._batches`), `table_terms` once (a table wider than 40
candidate columns in chunks of 40) and `table_synonyms` once when a table has a description.
Merged asks one `table_semantics` call a table and leaves to split the tables it doesn't suit.
After the tables, both make the same post-stage calls: semantic-FK picks, 25 columns to a call,
and a `bridge_sql` call for each pure-FK bridge table. Two things are guessed before compiling,
since only Claude's answers settle them: which columns are FKs nobody declared (a column named
like a key, `…id`), and how many answers come back invalid and are asked again, which the
calls' range allows for (`RETRY_SHARE`).

The quota is the calls' cost at API prices in USD, the unit `ops.llm_call.cost_usd` logs, which
weights each model the way a subscription's usage limit does. The time is the calls one after
another, as compile makes them: each call's turn plus S2's measured 0.53 s to start and close
the `claude` process (BUILD_PLAN.md, spike S2). A call's turn and cost are a base plus an amount
per table and per column, from M3.9's 256 logged calls (`RATES`). Merged has no measurement of
its own yet (M3.10 makes one): its call is priced as the four stages' parts on Sonnet with one
call's base. Each figure has a range, `LOW` to `HIGH` times the point estimate, for the spread
between tables and runs.
"""

# The vendored DAL is untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from auto_ontology.dal.datasources import (
    fetch_all_tables_without_term,
    fetch_bridge_table_candidates,
)
from auto_ontology.dal.session import store
from ontological.compile import descriptions, packing, semantic_fk, terms
from ontological.compile.packing import PlannedTable, TableCalls
from ontological.jobs import store as job_store

# S2: starting and closing `claude` takes 0.53 s a call at p50, the same on every model.
SPAWN_OVERHEAD_S = 0.53
# The spread around the point estimate that the range covers.
LOW = 0.7
HIGH = 1.5
# The calls' range allows one table call in ten to be answered invalidly. Split asks that call
# again (§6.2's retry); merged asks it again and then the table's split calls, up to four.
RETRY_SHARE = 0.1
RETRY_CALLS: dict[TableCalls, int] = {"split": 1, "merged": 5}
# A column named like a key that no FK declares: the semantic-FK stage asks about it.
_KEY_NAME = re.compile(r"(?i)(id|key)$")


@dataclass(frozen=True)
class Rate:
    """One call of a route: its turn, in seconds, and its cost at API prices, in USD, each a
    base plus an amount for every table and every column it asks about."""

    model: Literal["haiku", "sonnet", "opus"]
    base_s: float
    table_s: float = 0.0
    column_s: float = 0.0
    base_usd: float = 0.0
    table_usd: float = 0.0
    column_usd: float = 0.0


# From M3.9's compile of the five eval databases (256 calls, one table to a call, every one `ok`):
# keys took 2.8 s and $0.0095 a call on average, terms 2.7 s and $0.0080, and descriptions on
# Haiku 1.7 s plus 0.12 s a column, $0.0003 plus $0.00004 a column. A call's base is what a
# one-line call costs: S2's 1.4-1.6 s turn and, on Sonnet, about $0.004 of Claude Code's own
# prompt (a Haiku ping cost $0.0002, and Sonnet's prices are 20 times Haiku's). Synonyms, FK
# picks and bridges weren't in that run; theirs are the same arithmetic on their routes.
_SONNET_PER_HAIKU = 20.0
RATES: dict[str, Rate] = {
    "table_keys": Rate("sonnet", 1.6, table_s=1.2, base_usd=0.004, table_usd=0.0055),
    "column_descriptions": Rate("haiku", 1.7, column_s=0.12, base_usd=0.0003, column_usd=0.00004),
    "table_terms": Rate("sonnet", 1.6, table_s=1.1, base_usd=0.004, table_usd=0.004),
    "table_synonyms": Rate("haiku", 1.4, table_s=0.3, base_usd=0.0002, table_usd=0.0001),
    "semantic_fk_pick": Rate("sonnet", 1.6, column_s=0.4, base_usd=0.004, column_usd=0.0008),
    "bridge_sql": Rate("opus", 4.0, base_usd=0.015),
}
# Merged: the four stages' parts in one Sonnet call, the Haiku parts at Sonnet's prices.
_k, _d, _t, _s = (
    RATES[n] for n in ("table_keys", "column_descriptions", "table_terms", "table_synonyms")
)
RATES["table_semantics"] = Rate(
    "sonnet",
    _k.base_s,
    table_s=_k.table_s + _t.table_s + _s.table_s,
    column_s=_d.column_s,
    base_usd=_k.base_usd,
    table_usd=_k.table_usd + _t.table_usd + _s.table_usd * _SONNET_PER_HAIKU,
    column_usd=_d.column_usd * _SONNET_PER_HAIKU,
)


@dataclass(frozen=True)
class EstimatedTable:
    """What the estimate needs of a table, read from the catalog before profiling."""

    id: str
    schema_name: str
    name: str
    columns: int
    non_fk: int  # columns no declared FK starts from: what descriptions and Terms ask about
    undescribed: int  # of those, the ones with no description
    candidates: int  # of those, the ones outside the primary key: the keys stage's candidates
    described: bool = False  # the table has a description, so synonyms are asked
    key_like: int = 0  # candidates named like a key, which the semantic-FK stage may ask about


@dataclass(frozen=True)
class Call:
    task: str
    tables: int = 1
    columns: int = 0


class OptionEstimate(BaseModel):
    table_calls: TableCalls
    calls: int
    calls_high: int
    calls_by_model: dict[str, int]
    usd: float
    usd_low: float
    usd_high: float
    seconds: float
    seconds_low: float
    seconds_high: float


class Estimate(BaseModel):
    database_name: str
    tables: int  # the tables the compile would ask about
    split: OptionEstimate
    merged: OptionEstimate
    spawn_overhead_s: float = SPAWN_OVERHEAD_S


def estimate(database_name: str, tables: Sequence[EstimatedTable], bridges: int = 0) -> Estimate:
    """Both options' estimate for compiling `tables`, packed as compile would pack them, in a
    database with `bridges` bridge tables still to write."""
    planned = packing.plan(
        database_name,
        [PlannedTable(t.id, t.schema_name, t.name, t.columns) for t in tables],
    )
    by_id = {t.id: t for t in tables}
    packs: dict[int, list[EstimatedTable]] = {}
    for step in planned:
        packs.setdefault(step.pack, []).append(by_id[step.table.id])
    groups = [packs[n] for n in sorted(packs)]
    post = post_stage_calls(tables, bridges)
    return Estimate(
        database_name=database_name,
        tables=len(tables),
        split=_option("split", [c for g in groups for c in split_calls(g)], post),
        merged=_option("merged", [c for g in groups for c in merged_calls(g)], post),
    )


def split_calls(pack: Sequence[EstimatedTable]) -> list[Call]:
    """The calls the four split stages make for a pack's tables, as `split._split_stages` asks
    them, before any answer is retried."""
    live = [t for t in pack if t.columns > 0]
    calls: list[Call] = []
    keys = [t for t in live if t.candidates > 0 or t.columns > 2]  # `keys.build_prompt`
    if keys:
        calls.append(Call("table_keys", len(keys), sum(t.columns for t in keys)))
    asked = [t for t in live if t.undescribed > 0]
    for batch in descriptions._batches([t.undescribed for t in asked]):  # pyright: ignore[reportPrivateUsage]
        members = [asked[i] for i in batch]
        if len(members) > 1:
            calls.append(
                Call("column_descriptions", len(members), sum(t.undescribed for t in members))
            )
            continue
        width = members[0].undescribed
        for start in range(0, width, descriptions.BATCH_SIZE):
            n = min(descriptions.BATCH_SIZE, width - start)
            calls.append(Call("column_descriptions", 1, n))
    named = [t for t in live if t.non_fk > 0]
    packed = [t for t in named if t.non_fk <= terms.CHUNK_COLUMNS]
    if packed:
        calls.append(Call("table_terms", len(packed), sum(t.non_fk for t in packed)))
    for t in named:
        if t.non_fk > terms.CHUNK_COLUMNS:
            chunks = math.ceil(t.non_fk / terms.CHUNK_COLUMNS)
            calls.extend(Call("table_terms", 1, t.non_fk // chunks) for _ in range(chunks))
    described = [t for t in named if t.described]
    if described:
        calls.append(Call("table_synonyms", len(described)))
    return calls


def merged_calls(pack: Sequence[EstimatedTable]) -> list[Call]:
    """One `table_semantics` call for each table merged suits, then the split stages for the
    pack's others (`compile.merged`)."""
    suits = [t for t in pack if t.columns > 0 and 0 < t.non_fk <= terms.CHUNK_COLUMNS]
    calls = [Call("table_semantics", 1, t.undescribed) for t in suits]
    left = [t for t in pack if t not in suits]
    return calls + split_calls(left)


def post_stage_calls(tables: Sequence[EstimatedTable], bridges: int) -> list[Call]:
    """The post-stages' calls, the same for both options: semantic-FK picks for the columns
    named like a key that no FK declares, 25 to a call, and one `bridge_sql` a bridge table."""
    calls: list[Call] = []
    keyed = sum(t.key_like for t in tables)
    size = semantic_fk.BATCH_SIZE
    for start in range(0, keyed, size):
        calls.append(Call("semantic_fk_pick", 0, min(size, keyed - start)))
    calls.extend(Call("bridge_sql", 1) for _ in range(bridges))
    return calls


def call_seconds(call: Call) -> float:
    rate = RATES[call.task]
    turn = rate.base_s + rate.table_s * call.tables + rate.column_s * call.columns
    return SPAWN_OVERHEAD_S + turn


def call_usd(call: Call) -> float:
    rate = RATES[call.task]
    return rate.base_usd + rate.table_usd * call.tables + rate.column_usd * call.columns


def _option(name: TableCalls, table_calls: list[Call], post: list[Call]) -> OptionEstimate:
    calls = table_calls + post
    usd = sum(call_usd(c) for c in calls)
    seconds = sum(call_seconds(c) for c in calls)
    models = Counter(RATES[c.task].model for c in calls)
    return OptionEstimate(
        table_calls=name,
        calls=len(calls),
        calls_high=len(calls) + math.ceil(RETRY_SHARE * len(table_calls)) * RETRY_CALLS[name],
        calls_by_model=dict(sorted(models.items())),
        usd=round(usd, 4),
        usd_low=round(usd * LOW, 4),
        usd_high=round(usd * HIGH, 4),
        seconds=round(seconds, 1),
        seconds_low=round(seconds * LOW, 1),
        seconds_high=round(seconds * HIGH, 1),
    )


def is_key_like(name: str) -> bool:
    return len(name) > 2 and bool(_KEY_NAME.search(name))


def tables_to_compile(database_name: str) -> list[EstimatedTable]:
    """The tables the database's next compile would ask about: its unfinished job's pending
    steps, which a resumed job keeps, or else every table with no Term. Blocks."""
    work = {str(t["id"]): t for t in fetch_all_tables_without_term(database_name)}
    job = job_store.open_job("compile", database_name)
    if job is not None:
        pending = [s.table_id for s in job_store.job_steps(job.id) if s.state == "pending"]
        work = {i: work[i] for i in pending if i in work}
    return read_tables(list(work.values()))


def read_tables(work: Sequence[dict[str, Any]]) -> list[EstimatedTable]:
    """`EstimatedTable`s for rows of the work list (`fetch_all_tables_without_term`)."""
    ids = [str(t["id"]) for t in work]
    if not ids:
        return []
    pks = {
        str(r["id"]): set(r["pk"] or [])
        for r in store().query_read(
            "SELECT id, pk FROM catalog_table WHERE id = ANY(:ids)", {"ids": ids}
        )
    }
    rows = store().query_read(
        "SELECT c.table_id, c.name, coalesce(c.description, '') <> '' AS described, "
        "EXISTS (SELECT 1 FROM column__foreign_key f WHERE f.source_column_id = c.id) AS fk "
        "FROM catalog_column c WHERE c.table_id = ANY(:ids)",
        {"ids": ids},
    )
    columns: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        columns.setdefault(str(r["table_id"]), []).append(dict(r))
    out: list[EstimatedTable] = []
    for t in work:
        table_id = str(t["id"])
        cols = columns.get(table_id, [])
        non_fk = [c for c in cols if not c["fk"]]
        candidates = [c for c in non_fk if c["name"] not in pks.get(table_id, set())]
        out.append(
            EstimatedTable(
                id=table_id,
                schema_name=str(t.get("schema_name") or ""),
                name=str(t["name"]),
                columns=len(cols),
                non_fk=len(non_fk),
                undescribed=sum(1 for c in non_fk if not c["described"]),
                candidates=len(candidates),
                described=bool(t.get("description")),
                key_like=sum(1 for c in candidates if is_key_like(str(c["name"]))),
            )
        )
    return out


def estimate_database(database_name: str) -> Estimate:
    """The estimate for the database's next compile (`tables_to_compile`), with the bridge
    tables its bridge stage would write (`compile.bridges`). Blocks."""
    bridges = len(fetch_bridge_table_candidates(database_name))
    return estimate(database_name, tables_to_compile(database_name), bridges)
