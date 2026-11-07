"""A pending table's stages on the Jobs screen (DESIGN.md §4.8), read from the calls logged under
it: `ontological.jobs.view.stages_done`."""

from __future__ import annotations

from typing import cast

from ontological.jobs.view import StageCall, stages_done
from ontological.llm.calls import Outcome


def calls(*spec: str) -> list[StageCall]:
    """`task:outcome` or `task:outcome:attempt`, with the tasks abbreviated keys, desc, terms."""
    tasks = {"keys": "table_keys", "desc": "column_descriptions", "terms": "table_terms"}
    out: list[StageCall] = []
    for item in spec:
        task, outcome, *attempt = item.split(":")
        tried = int(attempt[0]) if attempt else 1
        out.append(StageCall(task=tasks[task], outcome=cast(Outcome, outcome), attempt=tried))
    return out


def test_a_table_with_no_calls_has_done_nothing() -> None:
    assert stages_done([]) == 0


def test_a_running_try_counts_the_stages_it_has_answered() -> None:
    assert stages_done(calls("keys:ok")) == 1
    assert stages_done(calls("keys:ok", "desc:ok")) == 2
    assert stages_done(calls("keys:ok", "desc:ok", "desc:ok", "terms:ok")) == 3


def test_an_invalid_answer_counts_once_its_retry_has_ended() -> None:
    assert stages_done(calls("keys:ok", "desc:invalid")) == 1, "the retry is still to come"
    assert stages_done(calls("keys:ok", "desc:invalid", "desc:invalid:2")) == 2, "it fell back"


def test_a_try_that_hit_the_limit_counts_what_it_got_through_before() -> None:
    assert stages_done(calls("keys:ok", "desc:ok", "terms:limited")) == 2
    assert stages_done(calls("keys:ok", "desc:ok", "desc:limited")) == 1
    assert stages_done(calls("keys:limited")) == 0
    assert stages_done(calls("keys:ok", "desc:ok", "terms:error")) == 2


def test_a_resumed_try_counts_from_its_own_start() -> None:
    # Paused at terms, then resumed: keys came back from the cache, desc is under way
    assert stages_done(calls("keys:ok", "desc:ok", "terms:limited", "keys:ok")) == 1
    # And a second limit, earlier this time
    assert stages_done(calls("keys:ok", "desc:ok", "terms:limited", "keys:ok", "desc:limited")) == 1


def test_calls_for_other_tasks_are_ignored() -> None:
    other = StageCall(task="semantic_fk_pick", outcome="limited", attempt=1)
    assert stages_done([*calls("keys:ok", "desc:ok"), other]) == 2
