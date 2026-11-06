"""The compile plan (BUILD_PLAN.md M3.1; ARCHITECTURE.md §6.4): the order tables compile in, and
the packs small tables share. The same tables always give the same plan, so a pack's prompts, and
so its cache keys, are the same from one run to the next."""

from __future__ import annotations

import random
from itertools import pairwise

from ontological.compile.packing import (
    PACK_TOKEN_BUDGET,
    PlannedTable,
    estimated_tokens,
    plan,
)


def table(name: str, columns: int, schema: str = "main") -> PlannedTable:
    return PlannedTable(id=f"{schema}.{name}", schema_name=schema, name=name, columns=columns)


# Chinook's tables and their column counts
CHINOOK = [
    table("Album", 3), table("Artist", 2), table("Customer", 13), table("Employee", 15),
    table("Genre", 2), table("Invoice", 9), table("InvoiceLine", 5), table("MediaType", 2),
    table("Playlist", 2), table("PlaylistTrack", 2), table("Track", 9),
]  # fmt: skip


def packs(steps: list[tuple[str, int]]) -> list[list[str]]:
    by_pack: dict[int, list[str]] = {}
    for name, pack in steps:
        by_pack.setdefault(pack, []).append(name)
    return [by_pack[p] for p in sorted(by_pack)]


def named(tables: list[PlannedTable]) -> list[tuple[str, int]]:
    return [(s.table.name, s.pack) for s in plan("chinook", tables)]


def test_tables_run_in_canonical_order_and_small_ones_share_a_pack() -> None:
    steps = plan("chinook", CHINOOK)

    assert [s.position for s in steps] == list(range(len(CHINOOK)))
    assert [s.table.name for s in steps] == [
        "Album", "Artist", "Customer", "Employee", "Genre", "Invoice", "InvoiceLine",
        "MediaType", "Playlist", "PlaylistTrack", "Track",
    ]  # fmt: skip
    # Six columns or fewer is small; packs are numbered by their first table
    assert packs(named(CHINOOK)) == [
        ["Album", "Artist", "Genre", "InvoiceLine", "MediaType", "Playlist", "PlaylistTrack"],
        ["Customer"],
        ["Employee"],
        ["Invoice"],
        ["Track"],
    ]


def test_the_plan_does_not_depend_on_the_order_tables_arrive_in() -> None:
    expected = named(CHINOOK)
    shuffled = list(CHINOOK)
    for seed in range(5):
        random.Random(seed).shuffle(shuffled)
        assert named(shuffled) == expected


def test_order_is_casefolded_with_ties_broken_by_exact_name_then_id() -> None:
    tables = [
        PlannedTable(id="b", schema_name="main", name="order", columns=2),
        PlannedTable(id="a", schema_name="main", name="Order", columns=2),
        PlannedTable(id="c", schema_name="Main", name="item", columns=2),
    ]

    assert [s.table.id for s in plan("db", tables)] == ["c", "a", "b"]


def test_a_pack_stays_within_the_token_budget() -> None:
    tables = [table(f"t{i:03}", 6) for i in range(40)]

    got = packs(named(tables))

    assert sum(len(p) for p in got) == 40
    assert len(got) > 1
    by_name = {t.name: t for t in tables}
    for pack in got:
        assert sum(estimated_tokens(by_name[n]) for n in pack) <= PACK_TOKEN_BUDGET
    # Greedy in canonical order: every pack but the last is full
    for pack, after in pairwise(got):
        assert (
            sum(estimated_tokens(by_name[n]) for n in pack) + estimated_tokens(by_name[after[0]])
            > PACK_TOKEN_BUDGET
        )


def test_packs_never_cross_schemas() -> None:
    tables = [table("a", 2, "sales"), table("b", 2, "sales"), table("c", 2, "hr")]
    steps = [(f"{s.table.schema_name}.{s.table.name}", s.pack) for s in plan("db", tables)]

    assert packs(steps) == [["hr.c"], ["sales.a", "sales.b"]]


def test_a_new_wide_table_leaves_the_small_table_packs_as_they_were() -> None:
    before = [p for p in packs(named(CHINOOK)) if len(p) > 1]

    after = packs(named([*CHINOOK, table("Audit", 30), table("Box", 7)]))

    assert [p for p in after if len(p) > 1] == before


def test_a_change_in_one_schema_leaves_the_other_schemas_packs_as_they_were() -> None:
    hr = [table(f"h{i}", 3, "hr") for i in range(4)]
    sales = [table(f"s{i}", 3, "sales") for i in range(4)]

    def contents(tables: list[PlannedTable]) -> list[list[str]]:
        steps = [(f"{s.table.schema_name}.{s.table.name}", s.pack) for s in plan("db", tables)]
        return [p for p in packs(steps) if p[0].startswith("sales.")]

    assert contents([*hr, *sales]) == contents([*hr[:2], table("h9", 2, "hr"), *sales])
