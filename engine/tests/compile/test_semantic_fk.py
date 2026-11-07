"""Semantic FK inference (BUILD_PLAN.md M3.4; ARCHITECTURE.md §6.4): the candidates each column
gets, the `semantic_fk_pick` calls, 25 columns to one, what a column the batch's answer leaves
out gets, and the two probes, run through a real source executor on SQLite. The gateway is
scripted; the store isn't needed."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fakes import NoAnswer, ScriptedGateway

from ontological.compile import semantic_fk as fk
from ontological.compile.gateway import ClaudeUnavailable
from ontological.compile.semantic_fk import Asking, BatchedFkPicks, FkHitSelection
from ontological.sources.executor import SourceExecutor, SqliteSource


def column(i: int, *, name: str | None = None, table: str = "orders") -> dict[str, Any]:
    return {
        "id": f"col-{i}",
        "name": name or f"ref_{i}",
        "table_id": f"t-{table}",
        "table_name": table,
        "description": f"Refers to thing {i}.",
        "sample_values": json.dumps([i, i + 1]),
    }


def hit(attr: str, *, table: str = "things", column: str = "id", distance: float = 0.1) -> Any:
    return {
        "text": f"ColumnAttribute: {attr} of Term Thing.",
        "metadata": {
            "id": attr,
            "table_id": f"t-{table}",
            "table_name": table,
            "schema_name": "main",
            "source_column": column,
        },
        "_distance": distance,
    }


def asking(i: int, **kw: Any) -> Asking:
    return Asking(column(i, **kw), [hit(f"attr-{i}"), hit(f"other-{i}", table="others")])


def headings(user: str) -> list[str]:
    return re.findall(r"^Foreign-key column: (.+)$", user, flags=re.M)


def first_candidate(user: str) -> str:
    found = re.search(r"^column_id=(\S+) \|", user, flags=re.M)
    assert found
    return found.group(1)


def sections(user: str) -> list[str]:
    return [p for p in re.split(r"^(?=Foreign-key column: )", user, flags=re.M) if p.strip()]


def picks_first(task: str, user: str) -> dict[str, Any]:
    """Claude picks each column's first candidate."""
    assert task == "semantic_fk_pick"
    parts = sections(user)
    if len(parts) == 1:
        return {"selected_id": first_candidate(user)}
    return {
        "columns": [
            {"column": h, "selected_id": first_candidate(p)} for p in parts for h in headings(p)
        ]
    }


def run(gateway: ScriptedGateway, items: list[Asking]) -> dict[str, str | None]:
    return asyncio.run(fk.pick(gateway, items))


# --- the calls ---------------------------------------------------------------------------------


def test_columns_are_asked_25_to_a_call_in_order() -> None:
    items = [asking(i) for i in range(60)]
    gateway = ScriptedGateway(picks_first)

    got = run(gateway, items)

    assert got == {f"col-{i}": f"attr-{i}" for i in range(60)}
    assert [len(headings(c.user)) for c in gateway.calls] == [25, 25, 10]
    assert headings(gateway.calls[0].user)[:2] == ["ref_0 (table: orders)", "ref_1 (table: orders)"]
    assert {c.schema for c in gateway.calls} == {BatchedFkPicks}
    assert {c.prompt_version for c in gateway.calls} == {fk.BATCH_PROMPT_VERSION}
    assert all(c.system == fk.SYSTEM + fk.BATCH_NOTE for c in gateway.calls)


def test_a_batched_prompt_is_each_column_s_own_prompt_in_turn() -> None:
    items = [asking(1), asking(2)]
    gateway = ScriptedGateway(picks_first)

    run(gateway, items)

    [call] = gateway.calls
    assert call.user == fk.prompt(items[0]) + "\n\n" + fk.prompt(items[1]) + "\n"


def test_a_column_alone_gets_upstream_s_prompt() -> None:
    items = [asking(i) for i in range(26)]
    gateway = ScriptedGateway(picks_first)

    run(gateway, items)

    last = gateway.calls[-1]
    assert (last.schema, last.prompt_version, last.system) == (
        FkHitSelection,
        fk.PROMPT_VERSION,
        fk.SYSTEM,
    )
    assert last.user == fk.prompt(items[25])
    assert last.user == (
        "Foreign-key column: ref_25 (table: orders)\n"
        "Description: Refers to thing 25.\n"
        "Sample values: sample_values: 25, 26\n\n"
        "Candidate PK columns (from vector search):\n"
        "column_id=attr-25 | ColumnAttribute: attr-25 of Term Thing.\n"
        "column_id=other-25 | ColumnAttribute: other-25 of Term Thing.\n\n"
        "Which candidate does this FK column reference? "
        "Return its exact column_id value, or null if none are a confident match."
    )


def test_a_pass_is_kept_and_a_column_left_out_or_misanswered_is_asked_alone() -> None:
    items = [asking(1), asking(2), asking(3), asking(4)]

    def claude(task: str, user: str) -> dict[str, Any]:
        if len(sections(user)) == 1:
            return {"selected_id": first_candidate(user)}
        return {
            "columns": [
                {"column": "ref_1 (table: orders)", "selected_id": None},
                {"column": "REF_2 (table: orders) ", "selected_id": "attr-2"},  # matched folded
                {"column": "ref_3 (table: orders)", "selected_id": "attr-99"},  # not offered
            ]
        }

    gateway = ScriptedGateway(claude)
    got = run(gateway, items)

    assert got == {"col-1": None, "col-2": "attr-2", "col-3": "attr-3", "col-4": "attr-4"}
    assert [headings(c.user) for c in gateway.calls[1:]] == [
        ["ref_3 (table: orders)"],
        ["ref_4 (table: orders)"],
    ]


def test_an_invalid_batch_asks_each_column_alone() -> None:
    items = [asking(1), asking(2)]

    def claude(task: str, user: str) -> dict[str, Any] | None:
        return None if len(sections(user)) > 1 else {"selected_id": None}

    gateway = ScriptedGateway(claude)
    got = run(gateway, items)

    assert got == {"col-1": None, "col-2": None}
    assert [c.schema for c in gateway.calls] == [BatchedFkPicks, FkHitSelection, FkHitSelection]


def test_columns_sharing_a_heading_are_asked_alone() -> None:
    items = [asking(1, name="ref"), asking(2, name="ref"), asking(3)]
    gateway = ScriptedGateway(picks_first)

    got = run(gateway, items)

    assert got == {"col-1": "attr-1", "col-2": "attr-2", "col-3": "attr-3"}
    assert [len(sections(c.user)) for c in gateway.calls] == [3, 1, 1]


def test_an_id_it_wasn_t_offered_alone_is_a_pass() -> None:
    gateway = ScriptedGateway(lambda task, user: {"selected_id": "attr-99"})

    assert run(gateway, [asking(1)]) == {"col-1": None}


def test_no_answer_raises_and_picks_nothing() -> None:
    gateway = ScriptedGateway(lambda task, user: NoAnswer("error", "needs_login"))

    with pytest.raises(ClaudeUnavailable):
        run(gateway, [asking(1), asking(2)])


# --- candidates --------------------------------------------------------------------------------


def test_candidates_are_two_searches_merged_unique_and_off_the_column_s_table() -> None:
    searched: list[tuple[str, int, dict[str, Any]]] = []
    results = [
        [hit("a"), hit("own", table="orders"), hit("b")],
        [hit("b"), hit("c"), {"text": "no id", "metadata": {"table_id": "t-x"}}],
    ]

    def search(query: str, k: int, where: dict[str, Any]) -> list[dict[str, Any]]:
        searched.append((query, k, where))
        return results[len(searched) - 1]

    got = fk.candidates(column(7), search, "shop")

    assert [h["metadata"]["id"] for h in got] == ["a", "b", "c"]
    where = {"label": "ColumnAttribute", "database_name": "shop", "is_unique": True}
    assert searched == [
        ("column_name: ref_7, description: Refers to thing 7., sample_values: 7, 8", 3, where),
        ("column_name: ref_7", 3, where),
    ]


# --- probes ------------------------------------------------------------------------------------


@pytest.fixture
def executor(tmp_path: Path) -> Iterator[SourceExecutor]:
    path = tmp_path / "shop.sqlite"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE customer (customer_id INTEGER PRIMARY KEY, code TEXT);
        CREATE TABLE store (store_id INTEGER PRIMARY KEY, customer_id INTEGER);
        CREATE TABLE orders (order_id INTEGER PRIMARY KEY, customer_id INTEGER, store_ref INTEGER,
                             blank INTEGER);
        INSERT INTO customer VALUES (1, 'a'), (2, 'b'), (3, 'c');
        INSERT INTO store VALUES (10, 1), (20, 2);
        INSERT INTO orders VALUES (100, 1, 900, NULL), (101, 3, 901, NULL);
        """
    )
    con.commit()
    con.close()
    with SourceExecutor(SqliteSource(path)) as ex:
        yield ex


def edge(source_table: str, source_column: str, target_table: str, target_column: str) -> Any:
    return {
        "source_schema_name": "main",
        "source_table_name": source_table,
        "source_column_name": source_column,
        "target_schema_name": "main",
        "target_table_name": target_table,
        "target_column_name": target_column,
    }


def overlap(executor: SourceExecutor, row: dict[str, Any]) -> tuple[Any, ...]:
    [found] = executor.probe(fk.overlap_sql(row), max_rows=1).rows
    return found


def test_the_overlap_probe_reads_presence_and_one_shared_value(
    executor: SourceExecutor,
) -> None:
    assert overlap(executor, edge("orders", "customer_id", "customer", "customer_id")) == (1, 1)
    assert overlap(executor, edge("orders", "store_ref", "store", "store_id")) == (1, 0)
    assert overlap(executor, edge("orders", "blank", "customer", "customer_id")) == (0, 0)


def test_the_sample_match_takes_the_nearest_candidate_holding_every_sample(
    executor: SourceExecutor,
) -> None:
    col = {
        "name": "customer_id",
        "table_name": "orders",
        "sample_values": json.dumps([1, 3]),
    }
    hits = [
        hit("store.customer_id", table="store", column="customer_id", distance=0.1),  # 3 missing
        hit("customer.customer_id", table="customer", column="customer_id", distance=0.3),
        hit("customer.code", table="customer", column="code", distance=0.0),  # another name
    ]

    assert fk.match_by_sample_values(col, hits, executor) == "customer.customer_id"
    assert fk.match_by_sample_values({**col, "sample_values": None}, hits, executor) is None


def test_a_sample_match_the_source_refuses_is_skipped(executor: SourceExecutor) -> None:
    col = {"name": "customer_id", "table_name": "orders", "sample_values": json.dumps([1])}
    hits = [hit("gone", table="missing", column="customer_id")]

    assert fk.match_by_sample_values(col, hits, executor) is None
