"""The merged option's `table_semantics` call (BUILD_PLAN.md M3.3; ARCHITECTURE.md §6.4): the
four split stages' prompts as sections of one call, and an answer cleaned up into what the split
stages give for the same table when Claude says the same things. The gateway is scripted."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Coroutine
from typing import Any

import pytest
from fakes import NoAnswer, ScriptedGateway, answering_merged, merged_sections

from ontological.compile import descriptions, keys, merged, synonyms, terms
from ontological.compile.descriptions import to_term_name
from ontological.compile.gateway import ClaudeUnavailable
from ontological.compile.models import TableSemantics

INVOICE: dict[str, Any] = {
    "id": "t-1",
    "name": "Invoice",
    "schema_name": "main",
    "description": "Customer invoices (INV).",
    "pk": ["InvoiceId"],
}
COLUMNS: list[dict[str, Any]] = [
    {"name": "InvoiceId", "data_type": "BIGINT", "is_foreign_key_target": True},
    {"name": "CustomerId", "data_type": "BIGINT"},
    {"name": "InvoiceDate", "data_type": "TIMESTAMP"},
    {"name": "BillingCountry", "data_type": "VARCHAR", "description": "Country billed."},
    {"name": "Total", "data_type": "DOUBLE"},
    {"name": "StoreRef", "data_type": "BIGINT"},
]
CTX = {"columns": COLUMNS, "fks": [{"source_column": "CustomerId", "target_table": "Customer"}]}
PROFILING: dict[str, dict[str, Any]] = {
    "InvoiceId": {"sample_values": [1, 2], "is_unique": True, "format": None},
    "CustomerId": {"sample_values": [2, 4], "is_unique": False, "format": None},
    "InvoiceDate": {"sample_values": [], "is_unique": False, "format": "YYYY-MM-DD HH:MM:SS"},
    "BillingCountry": {"sample_values": ["USA", "Canada"], "is_unique": False, "format": None},
    "Total": {"sample_values": [1.98, 3.96], "is_unique": False, "format": None},
    "StoreRef": {"sample_values": [7, 7], "is_unique": False, "format": None},
}


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def stages(task: str, user: str) -> dict[str, Any] | None:
    """What the scripted Claude says to each split stage's prompt."""
    if task == "table_keys":
        return {"fk_suggestions": [{"column_name": "StoreRef"}], "is_junction_table": False}
    if task == "column_descriptions":
        names = re.findall(r"^  - (\S+) \(", user, flags=re.M)
        return {"descriptions": [{"column_name": n, "description": f"About {n}."} for n in names]}
    if task == "table_synonyms":
        return {"synonyms": re.findall(r"\(([A-Z]+)\)", user)}
    assert task == "table_terms"
    columns = re.findall(r"^  - source_column=(\S+) ", user, flags=re.M)
    attributes = [{"source_column": c, "display_name": to_term_name(c)} for c in columns]
    return {"terms": [{"name": "Invoice", "description": "A sale.", "attributes": attributes}]}


def test_the_call_is_the_four_stages_prompts_as_sections() -> None:
    gateway = ScriptedGateway(answering_merged(stages))

    run(merged.table_semantics(gateway, INVOICE, CTX, PROFILING))

    [call] = gateway.calls
    assert (call.task, call.schema, call.prompt_version) == (
        "table_semantics",
        TableSemantics,
        "2a24f5d+merged.1",
    )
    for stage in (keys, descriptions, terms, synonyms):
        assert stage.SYSTEM in call.system
    got = merged_sections(call.user)
    assert list(got) == [merged.KEYS, merged.DESCRIPTIONS, merged.TERMS, merged.SYNONYMS]
    built = keys.build_prompt(INVOICE, CTX, PROFILING)
    assert built is not None
    assert got[merged.KEYS] == built[0]
    # Every column but the declared FK, before the keys answer can drop the one it suggests;
    # BillingCountry has a description, so isn't asked for one
    undescribed = [c for c in COLUMNS if c["name"] in ("InvoiceId", "InvoiceDate", "Total")]
    undescribed.append(COLUMNS[5])
    assert got[merged.DESCRIPTIONS] == descriptions.build_prompt(undescribed, PROFILING) + "\n"
    assert got[merged.TERMS].splitlines() == [
        "Table: Invoice",
        "Description: Customer invoices (INV).",
        "Candidate columns (assign each to exactly one Term with a display_name):",
        "  - source_column=BillingCountry (VARCHAR) — Country billed. — samples: USA, Canada",
        "  - source_column=InvoiceId (BIGINT)",
        "  - source_column=InvoiceDate (TIMESTAMP) — format: YYYY-MM-DD HH:MM:SS",
        "  - source_column=Total (DOUBLE)",
        "  - source_column=StoreRef (BIGINT)",
    ]
    assert got[merged.SYNONYMS] == "Description: Customer invoices (INV).\n"


def test_an_answer_is_what_the_split_stages_give_for_the_same_claude() -> None:
    gateway = ScriptedGateway(answering_merged(stages))

    got = run(merged.table_semantics(gateway, INVOICE, CTX, PROFILING))

    async def split() -> tuple[Any, ...]:
        split_gateway = ScriptedGateway(stages)
        found = await keys.suggest_keys(split_gateway, INVOICE, CTX, PROFILING)
        specs = await descriptions.column_attribute_specs(
            split_gateway, COLUMNS, CTX["fks"], {"StoreRef"}, PROFILING
        )
        proposed = await terms.extract_terms(split_gateway, INVOICE, specs)
        words = await synonyms.extract_synonyms(split_gateway, INVOICE["description"])
        for term in proposed:
            term.synonyms = words
        return found, specs, proposed

    found, specs, proposed = run(split())
    assert got is not None
    assert (got.keys, got.specs, got.terms) == (found, specs, proposed)
    # The suggested FK is in neither, the declared one never was, and the new descriptions and
    # the synonyms are on what is written
    assert [s.source_column for s in got.specs] == [
        "BillingCountry", "InvoiceId", "InvoiceDate", "Total",
    ]  # fmt: skip
    assert got.specs[2].description == "About InvoiceDate. — format: YYYY-MM-DD HH:MM:SS"
    [term] = got.terms
    assert (term.name, term.synonyms) == ("Invoice", ["INV"])
    assert [a.display_name for a in term.attributes] == [
        "Billing Country", "Invoice Id", "Invoice Date", "Total",
    ]  # fmt: skip


def test_a_section_with_nothing_to_ask_says_so_and_its_answer_is_ignored() -> None:
    genre: dict[str, Any] = {"id": "t-2", "name": "Genre", "schema_name": "main", "pk": ["GenreId"]}
    ctx = {
        "columns": [
            {"name": "GenreId", "data_type": "BIGINT", "description": "Its id."},
            {"name": "ParentId", "data_type": "BIGINT"},
        ],
        "fks": [{"source_column": "ParentId", "target_table": "Genre"}],
    }

    def answer(task: str, user: str) -> dict[str, Any]:
        out = answering_merged(stages)(task, user)
        assert isinstance(out, dict)
        # Answered anyway, for a section that asked nothing
        return {**out, "is_junction_table": True, "synonyms": ["X"]}

    gateway = ScriptedGateway(answer)

    got = run(merged.table_semantics(gateway, genre, ctx))

    sections = merged_sections(gateway.calls[0].user)
    # Two columns and no candidate keys: upstream's keys stage asks nothing for it
    assert [sections[h] for h in (merged.KEYS, merged.DESCRIPTIONS, merged.SYNONYMS)] == [
        "(nothing to ask)\n"
    ] * 3
    assert got is not None
    assert not got.keys.is_junction_table
    assert [t.synonyms for t in got.terms] == [[]]


def test_an_invalid_answer_leaves_the_table_to_the_split_stages() -> None:
    gateway = ScriptedGateway(lambda task, user: None)

    assert run(merged.table_semantics(gateway, INVOICE, CTX, PROFILING)) is None
    assert gateway.tasks() == ["table_semantics"]


def test_a_table_too_wide_or_with_no_candidate_columns_is_left_to_split_unasked() -> None:
    wide = {"columns": [{"name": f"c{i}", "data_type": "INT"} for i in range(41)], "fks": []}
    only_fks = {
        "columns": [{"name": "a"}, {"name": "b"}],
        "fks": [{"source_column": "a"}, {"source_column": "b"}],
    }
    gateway = ScriptedGateway(answering_merged(stages))

    assert run(merged.table_semantics(gateway, INVOICE, wide)) is None
    assert run(merged.table_semantics(gateway, INVOICE, only_fks)) is None
    assert gateway.calls == []


def test_no_answer_raises() -> None:
    gateway = ScriptedGateway(lambda task, user: NoAnswer())

    with pytest.raises(ClaudeUnavailable) as raised:
        run(merged.table_semantics(gateway, INVOICE, CTX, PROFILING))

    assert raised.value.task == "table_semantics"
