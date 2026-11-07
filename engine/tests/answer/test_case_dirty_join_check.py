# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/tests/test_case_dirty_join_check.py
# at 2a24f5d. The semantic-FK graph is a `JoinGraph` stand-in in place of `patch`es on the DAL's
# `find_join_path` and `find_column_id_by_table_and_name`, and the scripted connector answers the
# executor's `probe` with `Rows`. Tests of `find_join_path_mismatches`' verdicts, which upstream
# doesn't test, are added at the end.

"""Tests for find_case_dirty_join_mismatches (join_path_check.py) — a
compound JOIN where one predicate is the graph-verified relationship and
another, unverified predicate silently rejects real matches purely due to
casing/whitespace, confirmed by inspecting the rejected rows themselves
(not just a row-count difference, which can't tell dirty data apart from a
predicate correctly excluding a genuinely different row).

Scenario mirrors the real labor_certification_applications_13 bug: joining
worksite to case_worksite on the verified addr1 key plus an unverified
city/state pair rejects rows where wcity/wscity disagree only in
casing/whitespace for the same physical worksite.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fakes import DuckProber, FakeGraph, ScriptedProber, frame, hop

from ontological.answer.join_check import (
    build_case_dirty_join_repair_error,
    build_join_path_repair_error,
    find_case_dirty_join_mismatches,
    find_join_path_mismatches,
)
from ontological.answer.probe import ProbeExecutor
from ontological.sources.executor import Rows, SourceQueryError

_VERIFIED_HOP = [hop("worksite.w_addr1", "case_worksite.ws_addr1")]


def _only_addr1_verified() -> FakeGraph:
    return FakeGraph({("worksite.w_addr1", "case_worksite.ws_addr1"): _VERIFIED_HOP})


def _nothing_verified() -> FakeGraph:
    return FakeGraph()


_JOIN_SQL = (
    "SELECT w.wstate FROM public.worksite w "
    "JOIN public.case_worksite cw "
    "ON w.w_addr1 = cw.ws_addr1 AND w.wstate = cw.wsstate"
)


def test_flagged_when_every_rejected_row_is_casing_only() -> None:
    connector = ScriptedProber([frame(total_rejects=[13], casing_only_rejects=[13])])
    executor = ProbeExecutor(connector)

    mismatches = find_case_dirty_join_mismatches(
        executor, "postgres", _JOIN_SQL, _only_addr1_verified()
    )

    assert len(mismatches) == 1
    m = mismatches[0]
    assert m["verdict"] == "case_dirty_join"
    assert m["rejected_rows"] == 13
    assert m["verified_columns"] == ["worksite.w_addr1 = case_worksite.ws_addr1"]
    assert m["extra_columns"] == ["worksite.wstate = case_worksite.wsstate"]


def test_not_flagged_when_some_rejected_rows_differ_for_real() -> None:
    """Only 8 of 13 rejected rows agree once normalized — the other 5 are a
    genuine difference, so the extra predicate may be doing real
    disambiguation work. Must stay silent rather than guess.
    """
    connector = ScriptedProber([frame(total_rejects=[13], casing_only_rejects=[8])])
    executor = ProbeExecutor(connector)

    mismatches = find_case_dirty_join_mismatches(
        executor, "postgres", _JOIN_SQL, _only_addr1_verified()
    )

    assert mismatches == []


def test_not_flagged_when_extra_predicate_rejects_nothing() -> None:
    connector = ScriptedProber([frame(total_rejects=[0], casing_only_rejects=[0])])
    executor = ProbeExecutor(connector)

    mismatches = find_case_dirty_join_mismatches(
        executor, "postgres", _JOIN_SQL, _only_addr1_verified()
    )

    assert mismatches == []


def test_not_flagged_when_no_predicate_is_graph_verified() -> None:
    """No known-good "core" predicate to test the other one against — the
    check must stay silent rather than guess which predicate (if either) is
    the real relationship. That's find_join_path_mismatches' job.
    """
    connector = ScriptedProber([])  # no probes should even be attempted
    executor = ProbeExecutor(connector)

    mismatches = find_case_dirty_join_mismatches(
        executor, "postgres", _JOIN_SQL, _nothing_verified()
    )

    assert mismatches == []


def test_not_flagged_with_a_single_join_predicate() -> None:
    """Only one equality predicate between the two tables — nothing "extra"
    to be suspicious of.
    """
    sql = (
        "SELECT w.wstate FROM public.worksite w "
        "JOIN public.case_worksite cw ON w.w_addr1 = cw.ws_addr1"
    )
    executor = ProbeExecutor(None)

    mismatches = find_case_dirty_join_mismatches(executor, "postgres", sql, _only_addr1_verified())

    assert mismatches == []


def test_dead_executor_reports_nothing() -> None:
    """No connector available — the probe fails gracefully, no mismatch."""
    executor = ProbeExecutor(None)

    mismatches = find_case_dirty_join_mismatches(
        executor, "postgres", _JOIN_SQL, _only_addr1_verified()
    )

    assert mismatches == []


def test_build_case_dirty_join_repair_error_renders_expected_content() -> None:
    mismatch = {
        "table_a": "worksite",
        "table_b": "case_worksite",
        "verified_columns": ["worksite.w_addr1 = case_worksite.ws_addr1"],
        "extra_columns": ["worksite.wstate = case_worksite.wsstate"],
        "rejected_rows": 13,
        "verdict": "case_dirty_join",
    }

    error = build_case_dirty_join_repair_error([mismatch])

    assert "13" in error
    assert "worksite.wstate = case_worksite.wsstate" in error
    assert "LOWER(TRIM(" in error
    assert "Do NOT drop the condition" in error


# --- Ontological's: the case-dirty probe on DuckDB, and the join-path verdicts -------------------

_WORKSITES = (
    "CREATE TABLE worksite (w_addr1 VARCHAR, wstate VARCHAR)",
    "CREATE TABLE case_worksite (ws_addr1 VARCHAR, wsstate VARCHAR)",
    "INSERT INTO worksite VALUES ('1 Main St', 'CA'), ('2 Oak Ave', 'ny')",
    "INSERT INTO case_worksite VALUES ('1 Main St', 'CA'), ('2 Oak Ave', 'NY ')",
)


def test_the_case_dirty_probe_runs_on_duckdb() -> None:
    sql = (
        "SELECT w.wstate FROM worksite w JOIN case_worksite cw "
        "ON w.w_addr1 = cw.ws_addr1 AND w.wstate = cw.wsstate"
    )

    mismatches = find_case_dirty_join_mismatches(
        ProbeExecutor(DuckProber(*_WORKSITES)), "duckdb", sql, _only_addr1_verified()
    )

    assert [m["rejected_rows"] for m in mismatches] == [1]


_TRACKS = (
    "CREATE TABLE album (AlbumId INTEGER, ArtistId INTEGER)",
    "CREATE TABLE track (TrackId INTEGER, AlbumId INTEGER, GenreId INTEGER)",
    "INSERT INTO album VALUES (1, 1), (2, 1), (3, 2)",
    "INSERT INTO track VALUES (10, 1, 7), (11, 2, 8), (12, 3, 9)",
)


def _album_graph() -> FakeGraph:
    return FakeGraph(
        {("track.AlbumId", "album.AlbumId"): [hop("track.AlbumId", "album.AlbumId")]},
        fk_columns={"track.AlbumId", "album.AlbumId"},
    )


def test_the_graphs_own_join_is_verified_in_either_order() -> None:
    graph = _album_graph()

    mismatches = find_join_path_mismatches(
        ProbeExecutor(None),
        "duckdb",
        "SELECT t.TrackId FROM album a JOIN track t ON a.AlbumId = t.AlbumId",
        graph,
    )

    assert mismatches == []
    assert graph.asked == [("album.AlbumId", "track.AlbumId"), ("track.AlbumId", "album.AlbumId")]


def test_a_join_on_the_wrong_columns_names_the_graphs_pair() -> None:
    """track.TrackId = album.AlbumId: the graph knows the tables join on AlbumId, and the
    written columns barely overlap (no TrackId is an AlbumId)."""
    mismatches = find_join_path_mismatches(
        ProbeExecutor(DuckProber(*_TRACKS)),
        "duckdb",
        "SELECT t.TrackId FROM album a JOIN track t ON t.TrackId = a.AlbumId",
        FakeGraph(
            {("track.TrackId", "album.AlbumId"): [hop("track.AlbumId", "album.AlbumId")]},
            fk_columns={"album.AlbumId"},
        ),
    )

    assert [(m["verdict"], m["col_a"], m["col_b"]) for m in mismatches] == [
        ("wrong_column", "TrackId", "AlbumId")
    ]
    assert "uses track.AlbumId = album.AlbumId" in build_join_path_repair_error(mismatches)


def test_an_unmodelled_join_whose_values_overlap_is_left_alone() -> None:
    """No graph edge, but every track.AlbumId is an album.AlbumId: probably a real
    relationship ingestion never resolved."""
    mismatches = find_join_path_mismatches(
        ProbeExecutor(DuckProber(*_TRACKS)),
        "duckdb",
        "SELECT t.TrackId FROM album a JOIN track t ON t.AlbumId = a.AlbumId",
        FakeGraph(fk_columns={"album.AlbumId"}),
    )

    assert mismatches == []


def test_an_unmodelled_join_whose_values_dont_overlap_is_unverified() -> None:
    mismatches = find_join_path_mismatches(
        ProbeExecutor(DuckProber(*_TRACKS)),
        "duckdb",
        "SELECT t.TrackId FROM album a JOIN track t ON t.GenreId = a.AlbumId",
        FakeGraph(fk_columns={"album.AlbumId"}),
    )

    assert [(m["verdict"], m["overlap"]) for m in mismatches] == [("unverified", 0.0)]
    assert "very likely wrong" in build_join_path_repair_error(mismatches)


def test_a_join_between_columns_the_graph_doesnt_know_is_out_of_scope() -> None:
    connector = ScriptedProber([])  # no probe runs

    mismatches = find_join_path_mismatches(
        ProbeExecutor(connector),
        "duckdb",
        "SELECT t.TrackId FROM album a JOIN track t ON t.GenreId = a.ArtistId",
        FakeGraph(),
    )

    assert mismatches == []
    assert connector.seen == []


class _FailingProber:
    def probe(self, sql: str, *, max_rows: int) -> Rows:
        raise SourceQueryError("Binder Error: cannot compare INTEGER and VARCHAR")


def _spent() -> ProbeExecutor:
    executor = ProbeExecutor(DuckProber(*_TRACKS), max_calls=0)
    assert executor.budget_left == 0
    return executor


@pytest.mark.parametrize(
    "executor",
    [lambda: ProbeExecutor(None), _spent, lambda: ProbeExecutor(_FailingProber())],
    ids=["no source", "budget spent", "probe fails"],
)
def test_an_unmodelled_join_whose_overlap_cant_be_measured_is_left_alone(
    executor: Callable[[], ProbeExecutor],
) -> None:
    """With no graph path, only the overlap probe can call the join wrong. A probe that
    didn't run measured nothing, so it can't confirm the join is wrong."""
    mismatches = find_join_path_mismatches(
        executor(),
        "duckdb",
        "SELECT t.TrackId FROM album a JOIN track t ON t.GenreId = a.AlbumId",
        FakeGraph(fk_columns={"album.AlbumId"}),
    )

    assert mismatches == []
