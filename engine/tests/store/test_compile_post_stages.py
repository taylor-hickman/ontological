"""The compile's post-stages (BUILD_PLAN.md M3.5) on Chinook with a query history: SQL attributes
judged from it, ranked by recent use, several Terms to a call; the bridge table's join SQL; and,
with PII on, the rules' and Claude's tags, carried to the attributes. The gateway is scripted."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fakes import NoAnswer, ScriptedGateway
from test_compile import PLAYLIST_TRACKS, claude, rows

from ontological.catalog.ingest import ingest
from ontological.catalog.parse_pool import ParsePool
from ontological.catalog.query_history import HistoryRow, ingest_query_history
from ontological.compile.split import Compiled, compile_database
from ontological.pii.detector import BATCH
from ontological.sources.executor import SourceExecutor, SqliteSource
from ontological.store import pg

NOV = datetime(2026, 11, 3, tzinfo=UTC)
OCT = datetime(2026, 10, 20, tzinfo=UTC)
LAST_YEAR = datetime(2025, 6, 1, tzinfo=UTC)

HISTORY = [
    # busy this month
    HistoryRow("SELECT * FROM Invoice WHERE BillingCountry = 'USA'", NOV, 5),
    # used last month
    HistoryRow("SELECT SUM(Total) FROM Invoice WHERE Total > 10", OCT, 2),
    # busier all told, but idle for over a year: outside the window
    HistoryRow("SELECT * FROM Invoice WHERE BillingCity = 'Paris'", LAST_YEAR, 50),
    HistoryRow("SELECT * FROM Track WHERE Milliseconds > 300000", NOV, 1),
]


def judged(user: str) -> list[tuple[str, list[str]]]:
    """Each Term a judge prompt asks about, with its expressions in the order given."""
    out: list[tuple[str, list[str]]] = []
    for part in re.split(r"^(?=Business term: )", user, flags=re.M):
        head = re.match(r"Business term: (\S+)", part)
        if head:
            out.append((head.group(1), re.findall(r"^\d+\. \[\d+ uses\] (.+)$", part, flags=re.M)))
    return out


KEEP = {
    "BillingCountry = 'USA'": "US Invoices",
    "SUM(Total)": "Invoice Revenue",
    "Milliseconds > 300000": "Long Tracks",
}


def answer(task: str, user: str) -> Any:
    if task == "sql_attribute_judge":
        entries = [
            {
                "term": name,
                "suggestions": [
                    {"name": KEEP[e], "description": f"About {e}.", "expression": e}
                    for e in exprs
                    if e in KEEP
                ],
            }
            for name, exprs in judged(user)
        ]
        return (
            {"terms": entries} if len(entries) > 1 else {"suggestions": entries[0]["suggestions"]}
        )
    if task == "pii_classify":
        request = json.loads(user)
        return {
            "columns": [{"id": c["id"], "status": "not_pii", "confidence": 0.9} for c in request]
        }
    return claude(task, user)


@pytest.fixture
def executor(store: pg.Layout, chinook: Path) -> Iterator[SourceExecutor]:
    with SourceExecutor(SqliteSource(chinook)) as ex:
        ingest(ex)
        with ParsePool(workers=1) as pool:
            ingest_query_history("chinook", HISTORY, dialects=["sqlite"], pool=pool)
        yield ex


@pytest.fixture
def compiled(executor: SourceExecutor) -> tuple[Compiled, ScriptedGateway]:
    gateway = ScriptedGateway(answer)
    return asyncio.run(compile_database("chinook", executor, gateway, pii=True)), gateway


def attributes(store: pg.Layout) -> list[str]:
    return rows(
        store,
        "SELECT a.name || '|' || a.source || '|' || t.name || '|' || a.expression "
        "FROM sql_attribute a JOIN sql_attribute__term at ON at.attribute_id = a.id "
        "JOIN term t ON t.id = at.term_id ORDER BY a.name",
    )


def test_fragments_rank_by_recent_use_and_two_terms_share_one_judge_call(
    compiled: tuple[Compiled, ScriptedGateway],
) -> None:
    got, gateway = compiled

    [call] = [c for c in gateway.calls if c.task == "sql_attribute_judge"]
    asked = dict(judged(call.user))
    assert asked["Invoice"] == [
        "BillingCountry = 'USA'",
        "Total > 10",  # 2 uses, as SUM(Total) has; first in its statement
        "SUM(Total)",
        "BillingCity = 'Paris'",  # 50 uses, none in the window
    ]
    assert asked["Track"] == ["Milliseconds > 300000"]
    assert "1. [5 uses] BillingCountry = 'USA'" in call.user
    post = got.post
    assert post is not None and post.sql_attributes is not None
    assert (post.sql_attributes.terms, post.sql_attributes.calls) == (2, 1)


def test_kept_fragments_are_written_as_selects_under_their_terms(
    compiled: tuple[Compiled, ScriptedGateway], store: pg.Layout
) -> None:
    assert attributes(store) == [
        "Invoice Revenue|sql|Invoice|SELECT SUM(Total) FROM main.Invoice",
        "Long Tracks|sql|Track|SELECT * FROM main.Track WHERE Milliseconds > 300000",
        f"Playlist Tracks|bridgeTable|Playlist|{PLAYLIST_TRACKS}",
        "US Invoices|sql|Invoice|SELECT * FROM main.Invoice WHERE BillingCountry = 'USA'",
    ]
    # each with its statement, which reads the tables and has no sightings of its own
    assert rows(
        store,
        "SELECT a.name || '|' || q.total_counter || '|' || string_agg(t.name, ',' ORDER BY t.name) "
        "FROM sql_attribute a JOIN sql_attribute__sql s ON s.attribute_id = a.id "
        "JOIN sql_query q ON q.id = s.sql_query_id "
        "JOIN sql_query__table qt ON qt.sql_query_id = q.id "
        "JOIN catalog_table t ON t.id = qt.table_id GROUP BY a.name, q.total_counter ORDER BY 1",
    ) == [
        "Invoice Revenue|0|Invoice",
        "Long Tracks|0|Track",
        "Playlist Tracks|0|Playlist,PlaylistTrack,Track",
        "US Invoices|0|Invoice",
    ]


def test_the_bridge_is_a_junction_whose_owner_claude_picked(
    compiled: tuple[Compiled, ScriptedGateway], store: pg.Layout
) -> None:
    got, gateway = compiled
    [call] = [c for c in gateway.calls if c.task == "bridge_sql"]
    assert call.prompt_version == "2a24f5d+owner.1"
    assert "Candidate owning terms" in call.user
    assert got.post is not None and got.post.bridges is not None
    assert got.post.bridges.created == ["Playlist Tracks"]
    assert rows(
        store, "SELECT is_junction_table FROM catalog_table WHERE name = 'PlaylistTrack'"
    ) == ["t"]


def test_pii_tags_what_the_rules_and_claude_decide_and_carries_it_to_attributes(
    compiled: tuple[Compiled, ScriptedGateway], store: pg.Layout
) -> None:
    got, gateway = compiled
    pii = got.post.pii if got.post else None
    assert pii is not None
    calls = [json.loads(c.user) for c in gateway.calls if c.task == "pii_classify"]
    asked = [len(c) for c in calls]
    assert asked and all(n <= BATCH for n in asked) and pii.calls == len(asked)
    assert sum(asked) == pii.llm_decided
    columns = int(rows(store, "SELECT count(*) FROM catalog_column")[0])
    assert pii.scanned == pii.processed == columns
    # metadata only: never a sample value
    assert set().union(*(c.keys() for batch in calls for c in batch)) == {
        "id", "column_name", "table_name", "description",
    }  # fmt: skip

    tagged = rows(
        store,
        "SELECT t.name || '.' || c.name FROM tag_target tc JOIN tag g ON g.id = tc.tag_id "
        "JOIN catalog_column c ON c.id = tc.column_id JOIN catalog_table t ON t.id = c.table_id "
        "WHERE g.name = 'PII' ORDER BY 1",
    )
    assert {"Customer.Email", "Customer.FirstName", "Employee.Email", "Employee.BirthDate"} <= set(
        tagged
    )
    assert "Invoice.Total" not in tagged
    assert got.post is not None and got.post.pii_propagation is not None
    assert got.post.pii_propagation.column_attributes >= 4
    assert rows(
        store,
        "SELECT count(*) FROM tag_target ta JOIN tag g ON g.id = ta.tag_id "
        "JOIN column_attribute a ON a.id = ta.column_attribute_id "
        "WHERE g.name = 'PII' AND a.term_name = 'Customer' AND a.source_column = 'Email'",
    ) == ["1"]


def test_a_second_compile_writes_nothing_new_and_classifies_nothing_again(
    compiled: tuple[Compiled, ScriptedGateway], executor: SourceExecutor, store: pg.Layout
) -> None:
    before = attributes(store)
    gateway = ScriptedGateway(answer)

    again = asyncio.run(compile_database("chinook", executor, gateway, pii=True))

    assert attributes(store) == before
    # the judge is asked again (the real gateway's cache answers it), the bridge and PII aren't
    assert gateway.tasks() == ["sql_attribute_judge"]
    assert again.post is not None and again.post.sql_attributes is not None
    assert again.post.sql_attributes.created == []
    assert again.post.pii is not None and again.post.pii.scanned == 0


def test_a_limit_in_a_post_stage_pauses_the_job_and_reruns_the_stage(
    executor: SourceExecutor,
) -> None:
    from fakes import FakeClock

    limited: list[bool] = []

    def once_limited(task: str, user: str) -> Any:
        if task == "bridge_sql" and not limited:
            limited.append(True)
            return NoAnswer("limited", "limited")
        return answer(task, user)

    gateway = ScriptedGateway(once_limited)
    got = asyncio.run(compile_database("chinook", executor, gateway, clock=FakeClock()))

    assert got.complete
    assert gateway.tasks().count("bridge_sql") == 2
    assert got.post is not None and got.post.bridges is not None
    assert got.post.bridges.created == ["Playlist Tracks"]
    assert got.post.pii is None  # off by default


def test_a_logged_out_claude_stops_the_job_in_its_post_stages(
    executor: SourceExecutor, store: pg.Layout
) -> None:
    def logged_out_at_bridge(task: str, user: str) -> Any:
        if task == "bridge_sql":
            return NoAnswer("error", "needs_login")
        return answer(task, user)

    got = asyncio.run(compile_database("chinook", executor, ScriptedGateway(logged_out_at_bridge)))

    assert not got.complete and got.stopped is not None and got.stopped.task == "bridge_sql"
    assert got.post is not None and got.post.sql_attributes is not None
    assert len(got.post.sql_attributes.created) == 3  # kept
    assert rows(store, "SELECT state FROM ops.job") == ["stopped"]

    resumed = ScriptedGateway(answer)
    done = asyncio.run(compile_database("chinook", executor, resumed))
    assert done.complete and done.job_id == got.job_id
    assert resumed.tasks() == ["sql_attribute_judge", "bridge_sql"]


def test_a_hold_during_a_post_stage_pause_holds_the_job_and_resuming_finishes_it(
    executor: SourceExecutor, store: pg.Layout
) -> None:
    from fakes import FakeClock

    from ontological.llm.calls import CallScope, current_scope

    hold = asyncio.Event()
    scopes: dict[str, CallScope] = {}

    def limited_at_bridge(task: str, user: str) -> Any:
        scopes[task] = current_scope()
        if task == "bridge_sql":
            hold.set()  # the person pauses the job while it waits out the limit
            return NoAnswer("limited", "limited")
        return answer(task, user)

    got = asyncio.run(
        compile_database(
            "chinook", executor, ScriptedGateway(limited_at_bridge), clock=FakeClock(), hold=hold
        )
    )

    assert got.held and not got.complete
    assert got.post is not None and got.post.sql_attributes is not None
    assert rows(store, "SELECT state FROM ops.job") == ["held"]
    # its calls are logged under the job, with no pack, by their stage
    job = int(rows(store, "SELECT id FROM ops.job")[0])
    assert scopes["sql_attribute_judge"] == CallScope(job, None, "SQL attributes")
    assert scopes["bridge_sql"] == CallScope(job, None, "bridge tables")

    done = asyncio.run(compile_database("chinook", executor, ScriptedGateway(answer)))
    assert done.complete and done.post is not None and done.post.bridges is not None
    assert done.post.bridges.created == ["Playlist Tracks"]
