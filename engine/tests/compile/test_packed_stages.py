"""The split stages' packed calls, the terms stage's chunks and the synonyms stage (BUILD_PLAN.md
M3.2; ARCHITECTURE.md §6.4): what each call asks, how a pack's answer is matched back to its
tables, and what a table the answer leaves out gets. The gateway is scripted."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from fakes import NoAnswer, ScriptedGateway

from ontological.compile import descriptions, keys, packed, synonyms, terms
from ontological.compile.descriptions import DescriptionsRequest
from ontological.compile.gateway import ClaudeUnavailable
from ontological.compile.keys import KeysRequest
from ontological.compile.models import (
    ColumnAttributeSpec,
    PackedColumnDescriptions,
    PackedSynonyms,
    PackedTableKeys,
    PackedTableTerms,
    SynonymExtraction,
)


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def sections(user: str) -> list[tuple[str, str]]:
    """A packed prompt's tables: each heading, and its prompt."""
    parts = re.split(r"^(?=Table: )", user, flags=re.M)
    return [
        (p.split("\n", 1)[0].removeprefix("Table: "), p) for p in parts if p.startswith("Table: ")
    ]


def table(name: str, description: str | None = None) -> dict[str, Any]:
    return {"id": name, "name": name, "schema_name": "main", "description": description}


def ctx(*names: str) -> dict[str, Any]:
    return {"columns": [{"name": n, "data_type": "VARCHAR"} for n in names], "fks": []}


# --- table_keys --------------------------------------------------------------------------------


def keys_answer(user: str) -> dict[str, Any]:
    columns = re.findall(r"^Candidate columns:\n((?:  - .*\n?)+)", user, flags=re.M)
    first = re.match(r"  - (\S+)", columns[0]) if columns else None
    return {
        "fk_suggestions": [{"column_name": first.group(1)}] if first else [],
        "is_junction_table": False,
    }


def test_a_pack_asks_its_tables_keys_in_one_call() -> None:
    def answer(task: str, user: str) -> dict[str, Any]:
        return {"tables": [{"table": h, **keys_answer(p)} for h, p in sections(user)]}

    gateway = ScriptedGateway(answer)
    requests = [
        KeysRequest(table("Artist"), ctx("ArtistName")),
        KeysRequest(table("Genre"), ctx("GenreName", "GenreCode")),
    ]

    got = run(keys.suggest_keys_pack(gateway, requests))

    [call] = gateway.calls
    assert (call.task, call.schema, call.prompt_version) == (
        "table_keys",
        PackedTableKeys,
        packed.PROMPT_VERSION,
    )
    assert call.system == keys.SYSTEM + packed.NOTE
    # Each table's own prompt, one after the other
    single = [keys.build_prompt(r.table, r.ctx, r.profiling) for r in requests]
    assert call.user == packed.join(prompt for prompt, _ in filter(None, single))
    assert [h for h, _ in sections(call.user)] == ["main.Artist", "main.Genre"]
    assert [[s.column_name for s in k.suggestions] for k in got] == [["ArtistName"], ["GenreName"]]


def test_a_table_the_pack_s_answer_leaves_out_is_asked_alone() -> None:
    def answer(task: str, user: str) -> dict[str, Any]:
        found = sections(user)
        if len(found) == 1:
            return keys_answer(user)
        # Only Genre answered, in another case
        return {"tables": [{"table": "MAIN.GENRE ", **keys_answer(found[1][1])}]}

    gateway = ScriptedGateway(answer)
    requests = [
        KeysRequest(table("Artist"), ctx("ArtistName")),
        KeysRequest(table("Genre"), ctx("GenreName")),
    ]

    got = run(keys.suggest_keys_pack(gateway, requests))

    assert [c.schema for c in gateway.calls] == [PackedTableKeys, keys.FkAndPkResult]
    assert sections(gateway.calls[1].user)[0][0] == "main.Artist"
    assert gateway.calls[1].system == keys.SYSTEM  # upstream's, unchanged
    assert [[s.column_name for s in k.suggestions] for k in got] == [["ArtistName"], ["GenreName"]]


def test_an_invalid_pack_answer_asks_each_table_alone() -> None:
    gateway = ScriptedGateway(lambda task, user: None if "Genre" in user else keys_answer(user))
    requests = [
        KeysRequest(table("Artist"), ctx("ArtistName")),
        KeysRequest(table("Genre"), ctx("GenreName")),
    ]

    got = run(keys.suggest_keys_pack(gateway, requests))

    # The pack, then each alone; Genre's own answer is invalid too, so it takes the fallback
    assert [c.schema for c in gateway.calls] == [PackedTableKeys] + [keys.FkAndPkResult] * 2
    assert [[s.column_name for s in k.suggestions] for k in got] == [["ArtistName"], []]


def test_a_pack_with_one_table_to_ask_sends_upstream_s_prompt() -> None:
    gateway = ScriptedGateway(lambda task, user: keys_answer(user))
    pair = {**table("PlaylistTrack"), "pk": ["PlaylistId", "TrackId"]}
    pair_ctx = {
        "columns": [{"name": "PlaylistId"}, {"name": "TrackId"}],
        "fks": [{"source_column": "PlaylistId"}, {"source_column": "TrackId"}],
    }
    requests = [KeysRequest(pair, pair_ctx), KeysRequest(table("Genre"), ctx("GenreName"))]

    got = run(keys.suggest_keys_pack(gateway, requests))

    [call] = gateway.calls
    assert (call.system, call.schema, call.prompt_version) == (
        keys.SYSTEM,
        keys.FkAndPkResult,
        "2a24f5d",
    )
    assert [[s.column_name for s in k.suggestions] for k in got] == [[], ["GenreName"]]


# --- column_descriptions -----------------------------------------------------------------------


def describe(user: str) -> list[dict[str, str]]:
    names = re.findall(r"^  - (\S+) \(", user, flags=re.M)
    return [{"column_name": n, "description": f"About {n}."} for n in names]


def describe_any(task: str, user: str) -> dict[str, Any]:
    found = sections(user)
    if not found:
        return {"descriptions": describe(user)}
    return {"tables": [{"table": h, "descriptions": describe(p)} for h, p in found]}


Pack = tuple[str, list[dict[str, Any]], dict[str, Any]]


def columns(prefix: str, n: int) -> list[dict[str, Any]]:
    return [{"name": f"{prefix}{i}", "data_type": "INTEGER"} for i in range(n)]


def test_a_pack_s_columns_go_15_to_a_call_without_splitting_a_table() -> None:
    gateway = ScriptedGateway(describe_any)
    tables: list[Pack] = [
        (name, columns(name, n), {}) for name, n in [("a", 6), ("b", 6), ("c", 6), ("d", 2)]
    ]

    got = run(descriptions.describe_pack(gateway, tables))

    assert got == [{f"{t}{i}": f"About {t}{i}." for i in range(n)} for t, n in [
        ("a", 6), ("b", 6), ("c", 6), ("d", 2),
    ]]  # fmt: skip
    # a and b fill one call (12 columns); c would make 18, so c and d share the next
    assert [[h for h, _ in sections(c.user)] for c in gateway.calls] == [["a", "b"], ["c", "d"]]
    assert [c.schema for c in gateway.calls] == [PackedColumnDescriptions] * 2
    assert gateway.calls[0].user == (
        "Table: a\nColumns:\n" + "".join(f"  - a{i} (INTEGER)\n" for i in range(6))
        + "\nTable: b\nColumns:\n" + "".join(f"  - b{i} (INTEGER)\n" for i in range(6))
    )  # fmt: skip


def test_a_batch_of_one_table_sends_upstream_s_prompt() -> None:
    gateway = ScriptedGateway(describe_any)
    tables: list[Pack] = [("a", columns("a", 6), {}), ("b", [], {}), ("c", columns("c", 12), {})]

    got = run(descriptions.describe_pack(gateway, tables))

    assert [len(d) for d in got] == [6, 0, 12]
    assert [(c.schema, c.system) for c in gateway.calls] == [
        (descriptions.ColumnDescriptionResult, descriptions.SYSTEM)
    ] * 2
    assert gateway.calls[0].user.startswith("Columns:\n  - a0 (INTEGER)")


def test_specs_for_a_pack_leave_out_fk_columns() -> None:
    gateway = ScriptedGateway(describe_any)
    requests = [
        DescriptionsRequest("Album", ctx("AlbumId", "Title", "ArtistId")["columns"],
                            [{"source_column": "ArtistId"}]),
        DescriptionsRequest("Genre", ctx("GenreId", "Name")["columns"], [],
                            suggested_fk_columns={"GenreId"}),
    ]  # fmt: skip

    got = run(descriptions.column_attribute_specs_pack(gateway, requests))

    assert [[(s.source_column, s.description) for s in specs] for specs in got] == [
        [("AlbumId", "About AlbumId."), ("Title", "About Title.")],
        [("Name", "About Name.")],
    ]
    [call] = gateway.calls
    assert [h for h, _ in sections(call.user)] == ["Album", "Genre"]


# --- table_terms -------------------------------------------------------------------------------


def specs(n: int, prefix: str = "c") -> list[ColumnAttributeSpec]:
    return [
        ColumnAttributeSpec(f"{prefix}{i}", f"{prefix}{i}", datatype="INTEGER") for i in range(n)
    ]


def candidate_columns(user: str) -> list[str]:
    return re.findall(r"^  - source_column=(\S+) ", user, flags=re.M)


def test_a_wide_table_is_asked_in_chunks_of_40_that_see_the_terms_before_them() -> None:
    names = iter(["Order", "Order", "Shipment"])

    def answer(task: str, user: str) -> dict[str, Any]:
        name = next(names)
        return {
            "terms": [
                {
                    "name": name,
                    "description": "" if name == "Order" and "Terms already" in user else name,
                    "attributes": [
                        {"source_column": c, "display_name": c.upper()}
                        for c in candidate_columns(user)
                    ],
                }
            ]
        }

    gateway = ScriptedGateway(answer)
    wide = specs(85)

    got = run(terms.extract_terms(gateway, table("Order"), wide))

    assert [len(candidate_columns(c.user)) for c in gateway.calls] == [40, 40, 5]
    assert [c.system for c in gateway.calls] == [terms.SYSTEM] * 3
    assert "Terms already" not in gateway.calls[0].user
    assert gateway.calls[1].user.endswith(
        "Terms already proposed for this table's other columns (reuse a name exactly when "
        "a column belongs to that Term):\n  - Order — Order\n"
    )
    assert gateway.calls[2].user.endswith("  - Order — Order\n")
    # The chunks that named Order share one Term; the last chunk's Shipment is a second
    assert [(t.name, t.description, len(t.attributes)) for t in got] == [
        ("Order", "Order", 80),
        ("Shipment", "Shipment", 5),
    ]
    assert [a.source_column for a in got[0].attributes] == [f"c{i}" for i in range(80)]


def test_a_chunk_with_an_invalid_answer_leaves_its_columns_to_the_first_term() -> None:
    answers = iter([True, False, True])

    def answer(task: str, user: str) -> dict[str, Any] | None:
        if not next(answers):
            return None
        cols = candidate_columns(user)
        return {"terms": [{"name": "Order", "attributes": [
            {"source_column": c, "display_name": "Value"} for c in cols
        ]}]}  # fmt: skip

    got = run(terms.extract_terms(ScriptedGateway(answer), table("Order"), specs(100)))

    [term] = got
    assert sorted(a.source_column for a in term.attributes) == sorted(f"c{i}" for i in range(100))


def test_a_pack_asks_its_tables_terms_in_one_call() -> None:
    def answer(task: str, user: str) -> dict[str, Any]:
        entries = [
            {
                "table": h,
                "terms": [{"name": h, "description": f"A {h}.", "attributes": [
                    {"source_column": c, "display_name": "Name"} for c in candidate_columns(p)
                ]}],
            }
            for h, p in sections(user)
            if h != "Genre"
        ]  # fmt: skip
        if len(sections(user)) == 1:
            return {"terms": entries[0]["terms"]} if entries else {"terms": []}
        return {"tables": entries}

    gateway = ScriptedGateway(answer)
    requests: list[tuple[dict[str, Any], list[ColumnAttributeSpec]]] = [
        (table("Artist"), specs(2, "a")),
        (table("Empty"), []),
        (table("Genre"), specs(2, "g")),
        (table("Wide"), specs(41, "w")),
    ]

    got = run(terms.extract_terms_pack(gateway, requests))

    # Artist and Genre share a call; Genre, left out, is asked alone; Wide is chunked alone
    assert [[h for h, _ in sections(c.user)] for c in gateway.calls] == [
        ["Artist", "Genre"], ["Genre"], ["Wide"], ["Wide"],
    ]  # fmt: skip
    assert gateway.calls[0].schema is PackedTableTerms
    assert [[t.name for t in found] for found in got] == [
        ["Artist"],
        ["Empty"],
        ["Genre"],
        ["Wide"],
    ]
    assert got[2][0].description == "Business entity represented by table Genre"  # fallback


# --- table_synonyms ----------------------------------------------------------------------------


def test_synonyms_send_upstream_s_prompt_and_skip_a_table_with_no_description() -> None:
    gateway = ScriptedGateway(lambda task, user: {"synonyms": [" BU ", "", "BU", "Unit"]})

    assert run(synonyms.extract_synonyms(gateway, None)) == []
    assert gateway.calls == []
    got = run(synonyms.extract_synonyms(gateway, "A business unit (BU)."))

    assert got == ["BU", "Unit"]
    [call] = gateway.calls
    assert (call.task, call.system, call.user, call.schema, call.prompt_version) == (
        "table_synonyms",
        synonyms.SYSTEM,
        "Description: A business unit (BU).",
        SynonymExtraction,
        "2a24f5d",
    )


def test_a_pack_asks_its_described_tables_synonyms_in_one_call() -> None:
    def answer(task: str, user: str) -> dict[str, Any]:
        found = sections(user)
        if not found:
            return {"synonyms": ["Solo"]}
        return {"tables": [{"table": "Genre", "synonyms": ["GN"]}]}

    gateway = ScriptedGateway(answer)

    got = run(
        synonyms.extract_synonyms_pack(
            gateway,
            [("Artist", "Artists (ART)."), ("Track", None), ("Genre", "Genres, also known as GN.")],
        )
    )

    assert got == [["Solo"], [], ["GN"]]
    assert gateway.calls[0].user == (
        "Table: Artist\nDescription: Artists (ART).\n\n"
        "Table: Genre\nDescription: Genres, also known as GN.\n"
    )
    assert [c.schema for c in gateway.calls] == [PackedSynonyms, SynonymExtraction]


# --- no answer ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "no_answer",
    [
        pytest.param(NoAnswer("limited", "limited"), id="rate-limited"),
        pytest.param(NoAnswer("error", "needs_login"), id="logged-out"),
    ],
)
def test_a_pack_claude_does_not_answer_raises(no_answer: NoAnswer) -> None:
    two = [table("Artist", "Artists (ART)."), table("Genre", "Genres (GN).")]
    stages: list[Callable[[ScriptedGateway], Coroutine[Any, Any, object]]] = [
        lambda g: keys.suggest_keys_pack(g, [KeysRequest(t, ctx("Name")) for t in two]),
        lambda g: descriptions.describe_pack(g, [(t["name"], columns("x", 2), {}) for t in two]),
        lambda g: terms.extract_terms_pack(g, [(t, specs(2)) for t in two]),
        lambda g: synonyms.extract_synonyms_pack(g, [(t["name"], t["description"]) for t in two]),
    ]
    for stage in stages:
        gateway = ScriptedGateway(lambda task, user: no_answer)
        with pytest.raises(ClaudeUnavailable):
            run(stage(gateway))
        assert len(gateway.calls) == 1
