# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/tests/test_literal_check.py at
# 2a24f5d. The scripted connector answers the executor's `probe` with `Rows` in place of
# DataFrames, and the failing one raises the executor's `SourceQueryError`. Three tests are
# added for the numeric-scale check and for check 5 on DuckDB, which upstream doesn't test.

"""Tests for the case_duplicate detection in find_literal_mismatches
(literal_check.py) — a filter literal that exactly matches a real value, but
that same real-world value also appears under other casing/whitespace
elsewhere in the column, so an exact-match filter silently misses those rows.
"""

from __future__ import annotations

from fakes import DuckProber, ScriptedProber, frame

from ontological.answer.literal_check import (
    build_value_repair_error,
    find_literal_mismatches,
    find_numeric_scale_mismatches,
)
from ontological.answer.probe import ProbeExecutor
from ontological.sources.executor import Rows, SourceQueryError


def _dead_executor() -> ProbeExecutor:
    """A ProbeExecutor with no connector — every .run() fails gracefully
    (ok=False), simulating "the live probe couldn't confirm anything" without
    needing a real database.
    """
    return ProbeExecutor(None)


def test_case_duplicate_flagged_when_literal_exact_matches_but_has_siblings() -> None:
    """statustag = 'Certified' exactly matches a real value, but the same
    real-world value also exists as 'CERTIFIED'/'certified' in the column —
    an exact-match filter silently misses those rows. (Mirrors the real
    labor_certification_applications.cases.statustag data quality issue.)
    """
    connector = ScriptedProber([frame(value=["Certified", "CERTIFIED", "certified", "Denied"])])
    executor = ProbeExecutor(connector)
    sql = "SELECT * FROM cases WHERE statustag = 'Certified'"

    mismatches = find_literal_mismatches(executor, "postgres", sql)

    assert len(mismatches) == 1
    m = mismatches[0]
    assert m["kind"] == "case_duplicate"
    assert m["table"] == "cases"
    assert m["column"] == "statustag"
    assert m["used"] == "Certified"
    assert set(m["siblings"]) == {"Certified", "CERTIFIED", "certified"}


def test_no_mismatch_when_exact_match_has_no_case_siblings() -> None:
    """The clean-data case (e.g. an enum column like visacls): an exact
    match with no case-variant siblings must not be flagged.
    """
    connector = ScriptedProber([frame(value=["H-1B", "H-1B1 Chile"])])
    executor = ProbeExecutor(connector)
    sql = "SELECT * FROM cases WHERE visacls = 'H-1B'"

    mismatches = find_literal_mismatches(executor, "postgres", sql)

    assert mismatches == []


def test_near_miss_reports_every_case_sibling_as_a_suggestion() -> None:
    """A literal with no exact match, but several case variants of the same
    normalized value present, should surface every sibling as a suggestion —
    not just the first one encountered (this was a latent bug in the old
    single-valued actual_by_norm map, fixed as part of adding case_duplicate
    detection since both share the same normalized-value grouping).
    """
    connector = ScriptedProber([frame(value=["CERTIFIED", "certified", "Denied"])])
    executor = ProbeExecutor(connector)
    sql = "SELECT * FROM cases WHERE statustag = 'Certified'"

    mismatches = find_literal_mismatches(executor, "postgres", sql)

    assert len(mismatches) == 1
    m = mismatches[0]
    assert m["kind"] == "string"
    assert set(m["suggested"]) == {"CERTIFIED", "certified"}


def test_dead_executor_reports_nothing() -> None:
    """No connector available — probe fails gracefully, no mismatch."""
    sql = "SELECT * FROM cases WHERE statustag = 'Certified'"
    mismatches = find_literal_mismatches(_dead_executor(), "postgres", sql)
    assert mismatches == []


def test_build_value_repair_error_renders_case_duplicate_section() -> None:
    mismatch = {
        "kind": "case_duplicate",
        "table": "cases",
        "column": "statustag",
        "used": "Certified",
        "siblings": ["CERTIFIED", "Certified", "certified"],
    }

    error = build_value_repair_error([mismatch])

    assert "LOWER(TRIM(" in error
    assert "'CERTIFIED'" in error
    assert "'certified'" in error
    # The literal's own exact-match variant shouldn't be listed back as a
    # "different" sibling of itself.
    assert "'Certified', 'CERTIFIED'" not in error


def test_case_duplicate_mismatch_casts_to_text_on_postgres() -> None:
    """A Postgres ENUM column has no LOWER()/TRIM() overload without an
    explicit ::text cast (observed directly: `function pg_catalog.btrim
    (enum_visa_class) does not exist`) — the suggested rewrite must include
    it when the dialect is Postgres.
    """
    connector = ScriptedProber([frame(value=["Certified", "CERTIFIED", "certified", "Denied"])])
    executor = ProbeExecutor(connector)
    sql = "SELECT * FROM cases WHERE statustag = 'Certified'"

    mismatches = find_literal_mismatches(executor, "postgres", sql)

    assert mismatches[0]["dialect"] == "postgres"
    error = build_value_repair_error(mismatches)
    assert "statustag::text" in error


def test_case_duplicate_mismatch_has_no_cast_on_unrecognized_dialect() -> None:
    """No column-type information is available here, so an unrecognized (or
    absent) dialect must never guess at cast syntax — leave the column bare.
    """
    connector = ScriptedProber([frame(value=["Certified", "CERTIFIED", "certified", "Denied"])])
    executor = ProbeExecutor(connector)
    sql = "SELECT * FROM cases WHERE statustag = 'Certified'"

    mismatches = find_literal_mismatches(executor, None, sql)

    assert mismatches[0]["dialect"] is None
    error = build_value_repair_error(mismatches)
    assert "::text" not in error
    assert "LOWER(TRIM(statustag))" in error


def test_case_duplicate_like_flagged_when_pattern_misses_case_variants() -> None:
    """statustag LIKE 'Certified%' is case-sensitive, so it matches 'Certified'
    but misses the real 'CERTIFIED'/'certified' rows — the same underlying
    labor_certification_applications.cases.statustag issue, just expressed as
    a LIKE pattern instead of `=`.
    """
    connector = ScriptedProber([frame(value=["Certified", "CERTIFIED", "certified", "Denied"])])
    executor = ProbeExecutor(connector)
    sql = "SELECT * FROM cases WHERE statustag LIKE 'Certified%'"

    mismatches = find_literal_mismatches(executor, "postgres", sql)

    assert len(mismatches) == 1
    m = mismatches[0]
    assert m["kind"] == "case_duplicate_like"
    assert m["table"] == "cases"
    assert m["column"] == "statustag"
    assert m["pattern"] == "Certified%"
    assert set(m["missed"]) == {"CERTIFIED", "certified"}


def test_no_mismatch_when_like_pattern_already_matches_every_variant() -> None:
    connector = ScriptedProber([frame(value=["Certified", "CERTIFIED", "certified", "Denied"])])
    executor = ProbeExecutor(connector)
    sql = "SELECT * FROM cases WHERE statustag ILIKE 'Certified%'"

    mismatches = find_literal_mismatches(executor, "postgres", sql)

    assert mismatches == []


def test_no_mismatch_when_like_pattern_has_no_case_variants_to_miss() -> None:
    connector = ScriptedProber([frame(value=["H-1B", "H-1B1 Chile"])])
    executor = ProbeExecutor(connector)
    sql = "SELECT * FROM cases WHERE visacls LIKE 'H-1B%'"

    mismatches = find_literal_mismatches(executor, "postgres", sql)

    assert mismatches == []


def test_not_like_is_ignored() -> None:
    """NOT LIKE is a negation, out of scope — same rule as `=`/IN."""
    connector = ScriptedProber([frame(value=["Certified", "CERTIFIED", "certified", "Denied"])])
    executor = ProbeExecutor(connector)
    sql = "SELECT * FROM cases WHERE statustag NOT LIKE 'Certified%'"

    mismatches = find_literal_mismatches(executor, "postgres", sql)

    assert mismatches == []


def test_build_value_repair_error_renders_case_duplicate_like_section() -> None:
    mismatch = {
        "kind": "case_duplicate_like",
        "table": "cases",
        "column": "statustag",
        "pattern": "Certified%",
        "missed": ["CERTIFIED", "certified"],
    }

    error = build_value_repair_error([mismatch])

    assert "ILIKE" in error
    assert "'CERTIFIED'" in error
    assert "'certified'" in error


def _high_card_values(n: int = 21) -> Rows:
    """A value set larger than DB_PROBE_LOW_CARD_THRESHOLD, so the wholesale
    DISTINCT probe refuses to judge the column and the point-lookup path runs.
    """
    return frame(value=[f"badge-{i}" for i in range(n)])


def test_case_mismatch_found_on_high_cardinality_column() -> None:
    """badges.Name has far more than 20 distinct values, so the column can't be
    judged by its whole value set — but 'Outliers' vs. the stored 'outliers' is
    still a plain case mismatch, and a point lookup finds it. (Mirrors BIRD
    q650, where the exact-match filter silently returned nothing.)
    """
    connector = ScriptedProber(
        [
            _high_card_values(),  # DISTINCT probe: too many to judge wholesale
            frame(hit=[]),  # exact lookup: 'Outliers' is not there
            frame(value=["outliers"]),  # but this variant is
        ]
    )
    executor = ProbeExecutor(connector)
    sql = "SELECT * FROM badges WHERE Name = 'Outliers'"

    mismatches = find_literal_mismatches(executor, "postgres", sql, result_was_empty=True)

    assert len(mismatches) == 1
    m = mismatches[0]
    assert m["kind"] == "string"
    assert m["table"] == "badges"
    assert m["column"] == "Name"
    assert m["used"] == "Outliers"
    assert m["suggested"] == ["outliers"]
    # Nothing to quote back — the column is too large to enumerate.
    assert m["actual"] == []


def test_high_cardinality_literal_that_exists_costs_no_variant_scan() -> None:
    """The common case: the literal is simply correct. The exact lookup settles
    it, and the case-folded search (which can't use an index) must not run.
    """
    connector = ScriptedProber(
        [
            _high_card_values(),
            frame(hit=[1]),  # exact lookup: the literal is real
        ]
    )
    executor = ProbeExecutor(connector)
    sql = "SELECT * FROM badges WHERE Name = 'outliers'"

    mismatches = find_literal_mismatches(executor, "postgres", sql, result_was_empty=True)

    assert mismatches == []
    assert executor.calls == 2  # DISTINCT + exact lookup, no variant scan


def test_high_cardinality_literal_absent_with_no_variant_is_left_alone() -> None:
    """A value that exists in no casing at all is not a repair target — the
    empty result may well be the correct answer, same rule the low-cardinality
    path follows for a literal with no near-miss.
    """
    connector = ScriptedProber(
        [
            _high_card_values(),
            frame(hit=[]),  # not there exactly
            frame(value=[]),  # nor in any other casing
        ]
    )
    executor = ProbeExecutor(connector)
    sql = "SELECT * FROM badges WHERE Name = 'no-such-badge'"

    mismatches = find_literal_mismatches(executor, "postgres", sql, result_was_empty=True)

    assert mismatches == []


def test_high_cardinality_column_is_not_point_probed_before_execution() -> None:
    """Without an empty result to stand on, a missing literal is not evidence of
    a bug — a query that returned rows may well be answering the question. The
    point lookup is reserved for the post-execution caller, which both keeps its
    scan off every query in the run and avoids sending queries that already
    produced an answer back through reconstruction.
    """
    connector = ScriptedProber([_high_card_values()])
    executor = ProbeExecutor(connector)
    sql = "SELECT COUNT(*) FROM badges WHERE Name = 'Outliers'"

    mismatches = find_literal_mismatches(executor, "postgres", sql)

    assert mismatches == []
    assert executor.calls == 1  # the DISTINCT probe only — no point lookup


def test_high_cardinality_string_mismatch_renders_without_actual_values() -> None:
    """The string section quotes the column's real values back to
    reconstruction, but a high-cardinality column has none to quote — the
    message must drop that clause rather than render an empty list.
    """
    mismatch = {
        "kind": "string",
        "table": "badges",
        "column": "Name",
        "used": "Outliers",
        "suggested": ["outliers"],
        "actual": [],
    }

    error = build_value_repair_error([mismatch])

    assert "Actual values are" not in error
    assert "Closest real value(s): outliers." in error


class _FailingThenScriptedConnector:
    """Raises for SQL touching *failing_table*, otherwise returns scripted rows.

    Stands in for a candidate table that doesn't own the column being probed.
    """

    def __init__(self, failing_table: str, results: list[Rows]) -> None:
        self.failing_table = failing_table
        self._results = list(results)
        self.seen: list[str] = []

    def probe(self, sql: str, *, max_rows: int) -> Rows:
        self.seen.append(sql)
        if f"{self.failing_table}." in sql:
            raise SourceQueryError(f"no such column: {self.failing_table}.language")
        return self._results.pop(0)


def test_probe_qualifies_column_and_drops_table_alias() -> None:
    """SQLite reinterprets an unresolvable double-quoted identifier as a string
    literal, so a bare `"language"` probed against a table without that column
    comes back as one row of text instead of an error — indistinguishable from a
    real single-valued column. Qualifying the column makes it fail properly, and
    that only resolves if the FROM clause drops the query's alias.
    """
    connector = ScriptedProber([frame(value=["Korean"])])
    executor = ProbeExecutor(connector)

    find_literal_mismatches(
        executor,
        "sqlite",
        "SELECT * FROM set_translations AS st WHERE st.language = 'Korean'",
    )

    probe = executor.log[0]["sql"]
    assert "set_translations.language" in probe
    assert " AS st" not in probe


def test_unowned_candidate_table_does_not_shadow_the_real_one() -> None:
    """An unqualified column in a multi-table query is probed against each
    candidate. A table that doesn't own it must be skipped, not accepted — the
    literal has to be judged against the column's real values.
    """
    connector = _FailingThenScriptedConnector(
        "sets", [frame(value=["Korean", "Japanese", "French"])]
    )
    executor = ProbeExecutor(connector)
    sql = (
        "SELECT name FROM sets JOIN set_translations ON sets.code = set_translations.setCode "
        "WHERE language = 'Korean'"
    )

    mismatches = find_literal_mismatches(executor, "sqlite", sql)

    # 'Korean' is a real value in the owning table, so nothing to repair.
    assert mismatches == []
    assert any("set_translations.language" in s for s in connector.seen)


def test_build_value_repair_error_keeps_case_duplicate_separate_from_string_section() -> None:
    """The three mismatch kinds (string / case_duplicate / numeric_scale)
    must render into independent sections so reconstruction gets a targeted
    instruction per failure mode, not a merged, confusing one.
    """
    string_mismatch = {
        "kind": "string",
        "table": "cases",
        "column": "visacls",
        "used": "H1B",
        "suggested": ["H-1B"],
        "actual": ["H-1B", "H-1B1 Chile"],
    }
    case_duplicate_mismatch = {
        "kind": "case_duplicate",
        "table": "cases",
        "column": "statustag",
        "used": "Certified",
        "siblings": ["CERTIFIED", "Certified", "certified"],
    }

    error = build_value_repair_error([string_mismatch, case_duplicate_mismatch])

    assert "does not exist in the database" in error  # string section
    assert "silently misses those rows" in error  # case_duplicate section


# --- Ontological's: the numeric-scale check, and check 5 on DuckDB ------------------------------


def test_a_percent_threshold_on_a_fraction_column_is_flagged_with_its_rescale() -> None:
    connector = ScriptedProber([frame(lo=[0.01], hi=[0.95])])
    executor = ProbeExecutor(connector)

    mismatches = find_numeric_scale_mismatches(
        executor, "duckdb", "SELECT * FROM loans WHERE rate > 60"
    )

    assert mismatches == [
        {
            "kind": "numeric_scale",
            "table": "loans",
            "column": "rate",
            "used": 60.0,
            "suggested": 0.6,
            "real_min": 0.01,
            "real_max": 0.95,
        }
    ]
    assert "100x rescale" in build_value_repair_error(mismatches)


def test_a_threshold_inside_the_columns_range_is_left_alone() -> None:
    connector = ScriptedProber([frame(lo=[0.01], hi=[0.95])])

    assert (
        find_numeric_scale_mismatches(
            ProbeExecutor(connector), "duckdb", "SELECT * FROM loans WHERE rate > 0.6"
        )
        == []
    )


def test_literal_check_probes_a_duckdb_source() -> None:
    """The probes are DuckDB SQL that runs: a near-miss literal on a real table is found, with
    the column's values to choose from."""
    source = DuckProber(
        "CREATE TABLE genre (GenreId INTEGER, Name VARCHAR)",
        "INSERT INTO genre VALUES (1, 'Rock'), (2, 'Jazz'), (3, 'Hip Hop/Rap')",
    )

    mismatches = find_literal_mismatches(
        ProbeExecutor(source), "duckdb", "SELECT GenreId FROM genre g WHERE g.Name = 'rock'"
    )

    assert [(m["kind"], m["used"], m["suggested"]) for m in mismatches] == [
        ("string", "rock", ["Rock"])
    ]
    assert sorted(mismatches[0]["actual"]) == ["Hip Hop/Rap", "Jazz", "Rock"]
