"""The split stages (BUILD_PLAN.md M1.4): the prompts they build from upstream's templates, and
how they clean up and fall back from what Claude returns. The gateway is scripted."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Coroutine
from typing import Any

from fakes import ScriptedGateway

from ontological.compile import descriptions, keys, terms
from ontological.compile.models import ColumnAttributeSpec

INVOICE = {"id": "t-1", "name": "Invoice", "schema_name": "main", "description": None}
INVOICE_COLUMNS: list[dict[str, Any]] = [
    {"name": "InvoiceId", "data_type": "BIGINT", "is_foreign_key_target": True},
    {"name": "CustomerId", "data_type": "BIGINT"},
    {"name": "InvoiceDate", "data_type": "TIMESTAMP"},
    {"name": "BillingCountry", "data_type": "VARCHAR", "sample_values": '["USA", "Canada"]'},
    {"name": "Total", "data_type": "DOUBLE"},
    {"name": "StoreUuid", "data_type": "UUID"},
]
INVOICE_CTX = {
    "columns": INVOICE_COLUMNS,
    "fks": [{"source_column": "CustomerId", "target_table": "Customer"}],
}
PROFILING: dict[str, dict[str, Any]] = {
    "InvoiceId": {"sample_values": [1, 2], "is_unique": True, "format": None},
    "CustomerId": {"sample_values": [2, 4], "is_unique": False, "format": None},
    "InvoiceDate": {
        "sample_values": ["2021-02-01T00:00:00"],
        "is_unique": False,
        "format": "YYYY-MM-DD HH:MM:SS",
    },
    "BillingCountry": {"sample_values": ["USA", "Canada"], "is_unique": False, "format": None},
    "Total": {"sample_values": [1.98, 3.96], "is_unique": False, "format": None},
    "StoreUuid": {"sample_values": ["a"], "is_unique": False, "format": None},
}


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


# --- table_keys --------------------------------------------------------------------------------


def test_keys_prompt_is_upstream_s_template_with_the_declared_pk() -> None:
    built = keys.build_prompt({**INVOICE, "pk": ["InvoiceId"]}, INVOICE_CTX, PROFILING)

    assert built is not None
    prompt, candidates = built
    assert prompt.splitlines()[:6] == [
        "Table: main.Invoice",
        "Description: ",
        "Primary key columns (exclude from suggestions): InvoiceId",
        "Known foreign key columns (exclude from suggestions): CustomerId",
        "Known foreign key roles:",
        "  - CustomerId -> Customer",
    ]
    assert "  - BillingCountry (VARCHAR) [samples: USA, Canada] [is_unique: false]" in prompt
    # the PK, the declared FK and a column other tables point at aren't candidates
    assert [c["name"] for c in candidates] == [
        "InvoiceDate",
        "BillingCountry",
        "Total",
        "StoreUuid",
    ]
    assert prompt.endswith(
        "Candidate columns:\n"
        "  - InvoiceDate (TIMESTAMP) [is_unique: false]\n"
        "  - BillingCountry (VARCHAR) [samples: USA, Canada] [is_unique: false]\n"
        "  - Total (DOUBLE) [is_unique: false]\n"
        "  - StoreUuid (UUID) [is_unique: false]\n"
    )


def test_keys_keep_only_candidates_and_add_a_repeated_uuid() -> None:
    gateway = ScriptedGateway(
        lambda task, user: {
            "fk_suggestions": [
                {"column_name": " Total ", "rationale": " looks like one "},
                {"column_name": "Total", "rationale": "again"},
                {"column_name": "CustomerId", "rationale": "declared already"},
                {"column_name": "Nope", "rationale": "not a column"},
            ],
            "pk_column_names": [],
            "is_junction_table": False,
            "junction_table_rationale": " an event ",
        }
    )

    got = run(keys.suggest_keys(gateway, INVOICE, INVOICE_CTX, PROFILING))

    assert [(s.column_name, s.rationale) for s in got.suggestions] == [
        ("Total", "looks like one"),
        ("StoreUuid", "uuid type, not a declared or inferred primary key — "
         "almost certainly references another entity"),
    ]  # fmt: skip
    assert (got.is_junction_table, got.junction_table_rationale) == (False, "an event")
    [call] = gateway.calls
    assert (call.task, call.system, call.prompt_version) == ("table_keys", keys.SYSTEM, "2a24f5d")


def test_keys_skip_the_call_for_a_two_column_table_with_no_candidates() -> None:
    gateway = ScriptedGateway(lambda task, user: None)
    pair = {"name": "PlaylistTrack", "schema_name": "main", "pk": ["PlaylistId", "TrackId"]}
    ctx = {
        "columns": [{"name": "PlaylistId"}, {"name": "TrackId"}],
        "fks": [{"source_column": "PlaylistId"}, {"source_column": "TrackId"}],
    }

    got = run(keys.suggest_keys(gateway, pair, ctx))

    assert (got.suggestions, got.is_junction_table, gateway.calls) == ([], False, [])


def test_a_failed_keys_call_suggests_nothing() -> None:
    gateway = ScriptedGateway(lambda task, user: None)
    got = run(keys.suggest_keys(gateway, INVOICE, INVOICE_CTX, PROFILING))
    assert (got.suggestions, got.is_junction_table) == ([], False)


# --- column_descriptions -----------------------------------------------------------------------


def describe_all(task: str, user: str) -> dict[str, Any]:
    names = re.findall(r"^  - (\S+) \(", user, flags=re.M)
    return {"descriptions": [{"column_name": n, "description": f"About {n}."} for n in names]}


def test_descriptions_go_15_columns_to_a_call_one_call_at_a_time() -> None:
    columns = [{"name": f"c{i}", "data_type": "INTEGER"} for i in range(31)]
    gateway = ScriptedGateway(describe_all)

    got = run(descriptions.describe_columns(gateway, columns, {}))

    assert got == {f"c{i}": f"About c{i}." for i in range(31)}
    assert [c.user.count("\n  - ") for c in gateway.calls] == [15, 15, 1]
    assert gateway.tasks() == ["column_descriptions"] * 3
    assert gateway.peak == 1


def test_descriptions_match_names_echoed_with_their_type() -> None:
    columns = [
        {"name": "Academic Year", "data_type": "TEXT"},
        {"name": "Academic", "data_type": "TEXT"},
    ]
    gateway = ScriptedGateway(
        lambda task, user: {
            "descriptions": [
                {"column_name": "Academic Year (TEXT)", "description": " The year. "},
                {"column_name": "Academic", "description": ""},
                {"column_name": "Other", "description": "Not asked for."},
            ]
        }
    )

    got = run(descriptions.describe_batch(gateway, columns, {}))

    assert got == {"Academic Year": "The year."}


def test_specs_leave_out_fk_columns_and_describe_only_undescribed_ones() -> None:
    columns = [
        {"name": "InvoiceId", "data_type": "BIGINT"},
        {"name": "CustomerId", "data_type": "BIGINT"},
        {"name": "InvoiceDate", "data_type": "TIMESTAMP"},
        {"name": "BillingCountry", "data_type": "VARCHAR", "description": "Country billed."},
        {"name": "Total", "data_type": "DOUBLE"},
    ]
    gateway = ScriptedGateway(describe_all)

    specs = run(
        descriptions.column_attribute_specs(
            gateway,
            columns,
            [{"source_column": "CustomerId"}],
            suggested_fk_columns={"Total"},
            profiling=PROFILING,
        )
    )

    [call] = gateway.calls
    # dates show no samples; the described column isn't asked about
    assert (
        call.user == "Columns:\n  - InvoiceId (BIGINT) — samples: 1, 2\n  - InvoiceDate (TIMESTAMP)"
    )
    assert [(s.source_column, s.name, s.description) for s in specs] == [
        ("BillingCountry", "billingcountry", "Country billed. — samples: USA, Canada"),
        ("InvoiceId", "invoiceid", "About InvoiceId."),
        ("InvoiceDate", "invoicedate", "About InvoiceDate. — format: YYYY-MM-DD HH:MM:SS"),
    ]


def test_a_failed_description_batch_leaves_its_columns_undescribed() -> None:
    gateway = ScriptedGateway(lambda task, user: None)
    columns = [{"name": "Total", "data_type": "DOUBLE"}]

    specs = run(descriptions.column_attribute_specs(gateway, columns, [], profiling=PROFILING))

    assert [(s.source_column, s.description) for s in specs] == [("Total", None)]


# --- table_terms -------------------------------------------------------------------------------


def specs() -> list[ColumnAttributeSpec]:
    return [
        ColumnAttributeSpec("InvoiceId", "invoiceid", datatype="BIGINT", description="The id."),
        ColumnAttributeSpec("Total", "total", datatype="DOUBLE"),
        ColumnAttributeSpec("BillingCity", "billingcity", datatype="VARCHAR"),
    ]


def test_terms_prompt_lists_every_candidate_column() -> None:
    wide = [ColumnAttributeSpec(f"c{i}", f"c{i}", datatype="INTEGER") for i in range(45)]
    prompt = terms.build_prompt(INVOICE, wide)

    assert prompt.startswith(
        "Table: Invoice\nDescription: \n"
        "Candidate columns (assign each to exactly one Term with a display_name):\n"
        "  - source_column=c0 (INTEGER)\n"
    )
    assert prompt.endswith("  - source_column=c44 (INTEGER)\n")  # upstream stops at 40


def test_terms_keep_each_known_column_once_and_give_leftovers_to_the_first_term() -> None:
    gateway = ScriptedGateway(
        lambda task, user: {
            "terms": [
                {
                    "name": "Invoice",
                    "description": " A bill. ",
                    "attributes": [
                        {"source_column": "InvoiceId", "display_name": "Invoice Id"},
                        {"source_column": "Ghost", "display_name": "Ghost"},
                        {"source_column": "Total", "display_name": "total_amount"},
                    ],
                },
                {
                    "name": "Invoice",
                    "description": "duplicate name",
                    "attributes": [{"source_column": "BillingCity", "display_name": "City"}],
                },
            ]
        }
    )
    columns = specs()

    got = run(terms.extract_terms(gateway, INVOICE, columns))

    [term] = got
    assert (term.name, term.description) == ("Invoice", "A bill.")
    assert [(a.source_column, a.display_name) for a in term.attributes] == [
        ("InvoiceId", "Invoice Id"),
        ("Total", "total"),  # a label that isn't one falls back to the spec's name
        ("BillingCity", "billingcity"),  # left out by the model
    ]
    assert [s.display_name for s in columns] == ["Invoice Id", "total", "billingcity"]
    assert [c.task for c in gateway.calls] == ["table_terms"]


def test_a_failed_terms_call_names_one_term_after_the_table() -> None:
    gateway = ScriptedGateway(lambda task, user: None)

    [term] = run(terms.extract_terms(gateway, {"name": "invoice_line"}, specs()))

    assert (term.name, term.description) == (
        "Invoice Line",
        "Business entity represented by table invoice_line",
    )
    assert [a.source_column for a in term.attributes] == ["InvoiceId", "Total", "BillingCity"]
