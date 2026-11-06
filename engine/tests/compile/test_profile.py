"""Column profiling as compile reads it, through a real source executor (BUILD_PLAN.md M1.4,
M2.5; ARCHITECTURE.md §1.3 stages 1 and 2), on a SQLite file written here."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from ontological.profile.run import profile_table
from ontological.sources.executor import SourceExecutor, SqliteSource

COLUMNS = [
    {"name": "id", "data_type": "BIGINT"},
    {"name": "status", "data_type": "VARCHAR"},
    {"name": "joined", "data_type": "VARCHAR"},
    {"name": "seen", "data_type": "TIMESTAMP"},
    {"name": "note", "data_type": "VARCHAR"},
    {"name": "token", "data_type": "UUID"},
    {"name": "empty", "data_type": "VARCHAR"},
]


@pytest.fixture
def executor(tmp_path: Path) -> Iterator[SourceExecutor]:
    path = tmp_path / "members.sqlite"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE member (id INTEGER PRIMARY KEY, status TEXT, joined TEXT, seen DATETIME, "
        "note TEXT, token TEXT, empty TEXT)"
    )
    rows = [
        (
            i,
            "Banned" if i == 1100 else "Active",  # only past the 1,000-row sample
            f"{(i % 28) + 1:02d}/07/2011",  # day-first: 13/07 can't be month-first
            "2021-02-01 10:00:00",
            "x" * 40 if i % 2 else "short",
            f"uuid-{i % 3}",
            None,
        )
        for i in range(1, 1201)
    ]
    con.executemany("INSERT INTO member VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()
    with SourceExecutor(SqliteSource(path)) as ex:
        yield ex


def test_profile_reads_uniqueness_top_values_and_date_notations(executor: SourceExecutor) -> None:
    profile = profile_table(executor, "main", "member", COLUMNS)

    cols = profile["columns"]
    assert (cols["id"]["is_unique"], cols["id"]["sample_values"]) == (True, [1, 2, 3, 4, 5])
    # The sample holds one status; the DISTINCT probe finds the rare one past it
    assert (cols["status"]["is_unique"], cols["status"]["sample_values"]) == (
        False,
        ["Active", "Banned"],
    )
    assert cols["joined"]["format"] == "DD/MM/YYYY"
    assert cols["seen"]["format"] == "YYYY-MM-DD HH:MM:SS"
    assert cols["seen"]["sample_values"] == ["2021-02-01T10:00:00"]
    assert cols["empty"] == {"sample_values": [], "is_unique": False, "format": None}
    assert profile["uniqueness"]["status"] is False
    assert profile["date_formats"] == {"joined": "DD/MM/YYYY", "seen": "YYYY-MM-DD HH:MM:SS"}


def test_profile_keeps_short_samples_of_non_date_columns(executor: SourceExecutor) -> None:
    stored = profile_table(executor, "main", "member", COLUMNS)["sample_values"]

    assert stored["status"] == ["Active", "Banned"]
    assert stored["note"] == ["short"]  # the 40-character value is dropped
    assert "seen" not in stored  # a timestamp
    assert "token" not in stored  # a uuid
    assert "empty" not in stored


def test_profile_without_the_distinct_probe_keeps_the_sample_s_values(
    executor: SourceExecutor,
) -> None:
    profile = profile_table(executor, "main", "member", COLUMNS, probe_distinct_values=False)
    assert profile["columns"]["status"]["sample_values"] == ["Active"]


def test_a_failed_sample_gives_an_empty_profile(executor: SourceExecutor) -> None:
    profile = profile_table(executor, "main", "missing", COLUMNS)
    assert (profile["columns"], profile["sample_values"]) == ({}, {})
