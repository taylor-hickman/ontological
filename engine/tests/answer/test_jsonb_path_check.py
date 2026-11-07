# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/tests/test_jsonb_path_check.py at
# 2a24f5d. `find_jsonb_path_mismatches` is `find_json_path_mismatches` here, since it also checks
# DuckDB's JSON, and the wrong-type message says JSON rather than JSONB. Upstream's two
# `format_semantic_context` tests aren't ported: they test its prompt formatter, not the check.
# The DuckDB variant's tests (ARCHITECTURE.md §8.2, check 6) are added at the end.

"""Tests for the known_types fallback in find_jsonb_path_mismatches."""

from __future__ import annotations

from typing import Any

import pytest
from fakes import DuckProber, ScriptedProber, frame

from ontological.answer.json_check import (
    build_json_path_repair_error,
    find_json_path_mismatches,
)
from ontological.answer.probe import MAX_PROBE_ROWS, ProbeExecutor
from ontological.sources.executor import Rows


def _dead_executor() -> ProbeExecutor:
    """A ProbeExecutor with no connector — every .run() fails gracefully
    (ok=False), simulating "the live probe couldn't confirm anything" without
    needing a real database.
    """
    return ProbeExecutor(None)


def test_wrong_type_flagged_when_known_types_says_not_json() -> None:
    """A ->> used on a column known (via known_types) to not be JSON gets
    flagged as wrong_type, instead of silently skipped as before.
    """
    sql = "SELECT notes->>'summary' FROM aliens"
    mismatches = find_json_path_mismatches(
        _dead_executor(),
        "postgres",
        sql,
        known_types={("aliens", "notes"): "text"},
    )
    assert len(mismatches) == 1
    assert mismatches[0]["wrong_type"] == "text"
    assert mismatches[0]["table"] == "aliens"
    assert mismatches[0]["column"] == "notes"


def test_no_mismatch_when_known_types_absent() -> None:
    """Without known_types (or without an entry for this column), stay
    silent rather than guess — preserves prior behavior.
    """
    sql = "SELECT notes->>'summary' FROM aliens"
    mismatches = find_json_path_mismatches(_dead_executor(), "postgres", sql)
    assert mismatches == []


def test_no_mismatch_when_known_type_is_jsonb() -> None:
    """A column known to actually be jsonb must never be flagged as
    wrong_type just because the probe itself failed (e.g. no connector) —
    that's a probe-availability issue, not evidence of a type mismatch.
    """
    sql = "SELECT notes->>'summary' FROM aliens"
    mismatches = find_json_path_mismatches(
        _dead_executor(),
        "postgres",
        sql,
        known_types={("aliens", "notes"): "jsonb"},
    )
    assert mismatches == []


def test_repair_error_renders_wrong_type_distinctly_from_key_mismatch() -> None:
    key_mismatch = {
        "table": "aliens",
        "column": "notes",
        "container": None,
        "used_key": "summary",
        "available_keys": ["overview", "detail"],
    }
    type_mismatch = {
        "table": "aliens",
        "column": "age",
        "container": None,
        "used_key": "value",
        "wrong_type": "integer",
    }

    key_only = build_json_path_repair_error([key_mismatch])
    assert "does not exist and returns NULL" in key_only
    assert "isn't JSON-typed" not in key_only

    type_only = build_json_path_repair_error([type_mismatch])
    assert "isn't JSON-typed" in type_only
    assert "does not exist and returns NULL" not in type_only

    both = build_json_path_repair_error([key_mismatch, type_mismatch])
    assert "does not exist and returns NULL" in both
    assert "isn't JSON-typed" in both


# --- Ontological's: the DuckDB variant (check 6) ------------------------------------------------

_CLAIMS = (
    "CREATE TABLE claims (id INTEGER, cost JSON, notes VARCHAR, score INTEGER)",
    """INSERT INTO claims VALUES
        (1, '{"total": 10, "repair_costs": {"parts": 4, "labour": 6}}', '{"summary": "ok"}', 3),
        (2, '{"total": 12, "fees": {"towing": 2}}', 'not json at all', 5),
        (3, NULL, NULL, NULL)""",
)


def duckdb_check(
    sql: str, known_types: dict[tuple[str, str], str] | None = None
) -> list[dict[str, Any]]:
    source = DuckProber(*_CLAIMS)
    found = find_json_path_mismatches(ProbeExecutor(source), "duckdb", sql, known_types)
    assert not any("jsonb_" in probe for probe in source.seen)
    return found


def test_duckdb_a_flat_key_that_exists_passes() -> None:
    assert duckdb_check("SELECT cost->>'total' FROM claims") == []


def test_duckdb_a_flat_key_that_doesnt_exist_names_the_real_keys() -> None:
    [m] = duckdb_check("SELECT c.cost->>'amount' FROM claims c")

    assert (m["table"], m["column"], m["container"], m["used_key"]) == (
        "claims",
        "cost",
        None,
        "amount",
    )
    assert sorted(m["available_keys"]) == ["fees", "repair_costs", "total"]


def test_duckdb_a_nested_key_under_the_wrong_container_names_its_siblings() -> None:
    [m] = duckdb_check("SELECT cost->'fees'->>'parts' FROM claims")

    assert (m["container"], m["used_key"], m["available_keys"]) == ("fees", "parts", ["towing"])
    assert sorted(m["sibling_containers"]) == ["fees", "repair_costs", "total"]
    assert "Other top-level sections in cost" in build_json_path_repair_error([m])


def test_duckdb_a_nested_key_that_exists_passes() -> None:
    assert duckdb_check("SELECT cost->'repair_costs'->>'labour' FROM claims") == []


def test_duckdb_json_held_as_text_is_read_as_json() -> None:
    """A Postgres or MySQL source's JSON reaches DuckDB as VARCHAR through the scanner, and
    rows that aren't JSON objects don't stop the probe."""
    [m] = duckdb_check(
        "SELECT notes->>'title' FROM claims", known_types={("claims", "notes"): "VARCHAR"}
    )

    assert m["available_keys"] == ["summary"]
    assert "wrong_type" not in m


def test_duckdb_a_number_column_is_the_wrong_type() -> None:
    [m] = duckdb_check(
        "SELECT score->>'value' FROM claims", known_types={("claims", "score"): "INTEGER"}
    )

    assert m["wrong_type"] == "INTEGER"


def test_duckdb_text_that_holds_no_json_object_isnt_the_wrong_type() -> None:
    """A VARCHAR column may be JSON, so its type is no evidence the operator is misused."""
    source = DuckProber(
        "CREATE TABLE t (label VARCHAR)",
        "INSERT INTO t VALUES ('plain')",
    )

    assert (
        find_json_path_mismatches(
            ProbeExecutor(source),
            "duckdb",
            "SELECT label->>'x' FROM t",
            known_types={("t", "label"): "VARCHAR"},
        )
        == []
    )


def test_duckdb_function_form_is_checked_too() -> None:
    """`json_extract_string(col, '$.key')` is the same extraction as `col ->> 'key'`."""
    [m] = duckdb_check("SELECT json_extract_string(cost, '$.amount') FROM claims")

    assert m["used_key"] == "amount"


def test_other_dialects_are_not_checked() -> None:
    assert (
        find_json_path_mismatches(
            _dead_executor(),
            "sqlite",
            "SELECT notes->>'summary' FROM aliens",
            known_types={("aliens", "notes"): "text"},
        )
        == []
    )


def _wide_bag(keys: int) -> DuckProber:
    """One row per key, so the column's objects hold `keys` distinct keys between them."""
    return DuckProber(
        "CREATE TABLE events (payload JSON)",
        f"INSERT INTO events SELECT json_object('k' || i, i) FROM range({keys}) t(i)",
    )


@pytest.mark.parametrize("key", ["k59", "k3", "no_such_key"])
def test_duckdb_a_key_set_larger_than_the_probe_cap_is_never_judged(key: str) -> None:
    """60 distinct keys is more than one probe returns (`MAX_PROBE_ROWS`), so the listing is
    cut short and can't show a key is missing: no path into the column is flagged."""
    source = _wide_bag(60)

    found = find_json_path_mismatches(
        ProbeExecutor(source),
        "duckdb",
        f"SELECT payload->>'{key}' FROM events",
        known_types={("events", "payload"): "JSON"},
    )

    assert found == []
    assert source.seen  # it was probed, and came back truncated


def test_duckdb_a_key_set_within_the_probe_cap_is_judged() -> None:
    [m] = find_json_path_mismatches(
        ProbeExecutor(_wide_bag(MAX_PROBE_ROWS)), "duckdb", "SELECT payload->>'nope' FROM events"
    )

    assert len(m["available_keys"]) == MAX_PROBE_ROWS


def test_a_truncated_listing_on_a_text_column_isnt_the_wrong_type() -> None:
    """The probe returned keys, so the column holds JSON objects whatever its catalog type."""
    cut_short = Rows(["value"], ["VARCHAR"], [(f"k{i}",) for i in range(MAX_PROBE_ROWS)], True)

    found = find_json_path_mismatches(
        ProbeExecutor(ScriptedProber([cut_short])),
        "postgres",
        "SELECT notes->>'summary' FROM aliens",
        known_types={("aliens", "notes"): "text"},
    )

    assert found == []


def test_a_truncated_top_level_listing_lists_no_sibling_sections() -> None:
    nested = frame(value=["towing"])
    top = Rows(["value"], ["VARCHAR"], [(f"k{i}",) for i in range(MAX_PROBE_ROWS)], True)

    [m] = find_json_path_mismatches(
        ProbeExecutor(ScriptedProber([nested, top])),
        "postgres",
        "SELECT cost->'fees'->>'parts' FROM claims",
    )

    assert (m["available_keys"], m["sibling_containers"]) == (["towing"], None)
