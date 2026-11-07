"""`validate_sql` and `run_validated_sql` with no agent session (BUILD_PLAN.md M6.2): the gate on
SQL a caller wrote for a database, and a run of only what passed it, with the store and the
source stood in for as in test_tools.py."""

from __future__ import annotations

from typing import Any

import pytest
from test_tools import (
    GOOD_SQL,
    NEAR_MISS_HIGH_CARD,
    FakeOntology,
    FakeRunner,
    chinook_runner,
)

from ontological.answer import sql_checks
from ontological.answer.service import NoSource
from ontological.answer.sql_checks import NoSuchValidation, RunFailed, SqlChecks
from ontological.sources.executor import Rows


class Harness:
    def __init__(self, runner: Any = None) -> None:
        self.runner: Any = runner or FakeRunner()
        self.opened: list[str] = []
        self.checks = SqlChecks(FakeOntology(), self.runners)

    def runners(self, database: str) -> Any:
        if database != "chinook":
            raise NoSource(database)
        self.opened.append(database)
        return self.runner


def test_sql_that_passes_gets_an_id_and_names_what_it_reads() -> None:
    h = Harness()
    v = h.checks.validate("chinook", GOOD_SQL)
    assert v.ok and v.issues == [] and v.warnings == []
    assert v.validation_id is not None and v.validation_id.startswith("val-")
    assert (v.database, v.sql) == ("chinook", GOOD_SQL)
    assert sorted(v.tables) == ["main.Album", "main.Artist"]
    assert "Artist.Name" in v.columns


def test_sql_with_issues_gets_no_id_and_runs_nothing() -> None:
    h = Harness()
    v = h.checks.validate("chinook", "SELECT Titel FROM Album")
    assert not v.ok and v.validation_id is None
    assert any("titel" in issue for issue in v.issues)
    assert h.runner.ran == []


def test_writes_are_refused_before_the_catalog_is_read() -> None:
    v = Harness().checks.validate("chinook", "DELETE FROM Artist")
    assert not v.ok and v.validation_id is None


def test_ids_are_unique_across_calls() -> None:
    h = Harness()
    ids = {h.checks.validate("chinook", GOOD_SQL).validation_id for _ in range(5)}
    assert len(ids) == 5


def test_a_database_with_no_source_is_refused() -> None:
    with pytest.raises(NoSource):
        Harness().checks.validate("nope", GOOD_SQL)


def test_soft_checks_warn_only_when_asked_to() -> None:
    sql = "SELECT Name FROM Artist WHERE Name = 'ac/dc'"  # a near miss of AC/DC
    h = Harness()
    blocked = h.checks.validate("chinook", sql)
    assert not blocked.ok and any("AC/DC" in issue for issue in blocked.issues)
    warned = h.checks.validate("chinook", sql, soft_as_warnings=True)
    assert warned.ok and warned.validation_id is not None
    [warning] = warned.warnings
    assert "AC/DC" in warning


def test_run_runs_only_sql_that_passed() -> None:
    h = Harness()
    validation_id = h.checks.validate("chinook", GOOD_SQL).validation_id
    assert validation_id is not None
    ran = h.checks.run(validation_id)
    assert h.runner.ran == [GOOD_SQL]
    assert ran.outcome == "rows" and ran.validation_id == validation_id
    assert ran.columns == ["Name", "albums"] and ran.types == ["VARCHAR", "BIGINT"]
    assert ran.rows == [["AC/DC", 2], ["Accept", 2]]
    assert ran.row_count == 2 and not ran.truncated
    assert ran.note is None and ran.repairs == "" and ran.without_like is None


def test_run_refuses_an_id_it_did_not_issue() -> None:
    h = Harness()
    with pytest.raises(NoSuchValidation):
        h.checks.run("val-1")
    assert h.runner.ran == []


def test_the_oldest_ids_are_forgotten(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sql_checks, "MAX_KEPT", 2)
    h = Harness()
    first, second, third = (h.checks.validate("chinook", GOOD_SQL).validation_id for _ in range(3))
    assert first and second and third
    with pytest.raises(NoSuchValidation):
        h.checks.run(first)
    assert h.checks.run(third).outcome == "rows"


def test_a_source_error_is_a_failed_run() -> None:
    h = Harness()
    validation_id = h.checks.validate("chinook", GOOD_SQL).validation_id
    assert validation_id is not None
    h.runner.error = "Binder Error: no such function: foo"
    with pytest.raises(RunFailed, match="Binder Error"):
        h.checks.run(validation_id)


def test_the_source_is_opened_once_for_every_call() -> None:
    h = Harness()
    for _ in range(3):
        validation_id = h.checks.validate("chinook", GOOD_SQL).validation_id
        assert validation_id is not None
        h.checks.run(validation_id)
    assert h.opened == ["chinook"] * 6  # SourceRunners keeps it open; the harness counts asks


def test_an_empty_result_says_what_to_recheck_with_the_fixes_found() -> None:
    h = Harness(chinook_runner())
    validated = h.checks.validate("chinook", NEAR_MISS_HIGH_CARD)
    assert validated.validation_id is not None
    ran = h.checks.run(validated.validation_id)
    assert ran.outcome == "empty" and ran.rows == [] and ran.row_count == 0
    assert "'ac/dc'" in ran.repairs and "AC/DC" in ran.repairs
    assert ran.note is not None and "no rows" in ran.note and "AC/DC" in ran.note


def test_an_empty_result_says_its_like_filter_emptied_it() -> None:
    h = Harness(chinook_runner())
    sql = "SELECT Name FROM Artist WHERE Name LIKE 'ac/dc%' AND ArtistId < 3"
    validated = h.checks.validate("chinook", sql)
    assert validated.validation_id is not None
    ran = h.checks.run(validated.validation_id)
    assert ran.without_like is not None
    assert ran.without_like.dropped == ["Name LIKE 'ac/dc%'"]
    assert ran.without_like.rows == [["AC/DC"], ["Artist 00"]]
    assert ran.note is not None and "LIKE filter" in ran.note


def test_an_empty_result_with_nothing_to_fix_can_be_the_answer() -> None:
    h = Harness()
    h.runner.rows = Rows(["Name", "albums"], ["VARCHAR", "BIGINT"], [], False)
    validation_id = h.checks.validate("chinook", GOOD_SQL).validation_id
    assert validation_id is not None
    ran = h.checks.run(validation_id)
    assert ran.outcome == "empty" and ran.repairs == "" and ran.without_like is None
    assert ran.note is not None and "can be the answer" in ran.note
