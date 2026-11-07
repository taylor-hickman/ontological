"""`Runs` (ARCHITECTURE.md §8.4, BUILD_PLAN.md M4.5): a question's events, buffered by
conversation, so a reader that leaves and comes back sees every event from the first; a run that
nobody reads still finishes; a cancel ends the run at once and stops its session."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest

from ontological.answer.answerable import Answerability
from ontological.answer.models import AnswerModel
from ontological.answer.runs import Busy, Runs
from ontological.answer.service import (
    AnswerEvent,
    Asked,
    Finished,
    NoEmbedder,
    NoSource,
    NothingFound,
    SaidStep,
    SqlPassed,
)

SQL = "SELECT COUNT(*) FROM Album"
FINISHED = Finished(
    question="q",
    database="chinook",
    answer=AnswerModel(answer="347.", sql=SQL, plan_id="p", validation_id="v", answerable=True),
    verified=True,
    reason=None,
    sql=SQL,
    result=None,
    steps=[],
    outcome="ok",
    error=None,
    answered_model="claude-opus-5-5",
)


class Scripted:
    """Answers whose stream yields what the test puts in `events`, until it puts None. An
    exception put there is raised. `stopped` counts the streams that were closed early."""

    def __init__(self) -> None:
        self.events: asyncio.Queue[AnswerEvent | Exception | None] = asyncio.Queue()
        self.asked: list[tuple[str, str | None, bool]] = []
        self.stopped = 0

    async def stream(
        self, question: str, database: str | None, *, critic: bool = True
    ) -> AsyncIterator[AnswerEvent]:
        self.asked.append((question, database, critic))
        try:
            while (event := await self.events.get()) is not None:
                if isinstance(event, Exception):
                    raise event
                yield event
        except asyncio.CancelledError:
            self.stopped += 1
            raise

    async def ask(self, question: str, database: str | None, *, critic: bool = True) -> Asked:
        raise AssertionError("runs stream")

    async def check(self, question: str, database: str | None = None) -> Answerability:
        raise AssertionError("runs stream")


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


def run(test: Callable[[Scripted, Runs], Awaitable[None]]) -> Scripted:
    answers = Scripted()

    async def go() -> None:
        runs = Runs(answers)
        try:
            await asyncio.wait_for(test(answers, runs), timeout=5)
        finally:
            await runs.close()

    asyncio.run(go())
    return answers


async def read(events: AsyncIterator[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e async for e in events]


def said(text: str) -> SaidStep:
    return SaidStep(session=1, text=text)


def test_a_run_opens_with_its_question_and_ends_with_the_answer() -> None:
    async def test(answers: Scripted, runs: Runs) -> None:
        r = runs.start("q", "chinook", critic=False, conversation="c-1")
        for event in (said("Planning."), SqlPassed(session=1, sql=SQL, validation_id="v")):
            answers.events.put_nowait(event)
        answers.events.put_nowait(FINISHED)
        answers.events.put_nowait(None)
        got = await read(r.watch())
        assert [e["type"] for e in got] == ["run", "step", "sql", "result"]
        assert got[0] == {
            "type": "run",
            "conversation": "c-1",
            "question": "q",
            "database": "chinook",
            "critic": False,
        }
        assert got[1] == {"type": "step", "kind": "said", "session": 1, "text": "Planning."}
        assert got[3]["answer"]["answer"] == "347." and got[3]["verified"] is True
        assert r.finished

    answers = run(test)
    assert answers.asked == [("q", "chinook", False)]


def test_a_reader_that_comes_back_sees_every_event_from_the_first_then_the_rest_live() -> None:
    async def test(answers: Scripted, runs: Runs) -> None:
        runs.start("q", None, critic=True, conversation="c-1")
        answers.events.put_nowait(said("one"))
        first = runs.get("c-1")
        assert first is not None
        events = first.watch()
        assert (await anext(events))["type"] == "run"
        assert (await anext(events))["text"] == "one"
        await events.aclose()  # the reader leaves; the run goes on

        answers.events.put_nowait(said("two"))
        again = runs.get("c-1")
        assert again is not None and again is first
        late = again.watch()
        assert [(await anext(late)).get("text") for _ in range(3)] == [None, "one", "two"]
        answers.events.put_nowait(said("three"))
        assert (await anext(late))["text"] == "three"
        answers.events.put_nowait(FINISHED)
        answers.events.put_nowait(None)
        assert [e["type"] for e in await read(late)] == ["result"]

    answers = run(test)
    assert answers.stopped == 0


def test_a_run_nobody_reads_still_finishes_and_is_kept_to_replay() -> None:
    async def test(answers: Scripted, runs: Runs) -> None:
        r = runs.start("q", None, critic=True, conversation="c-1")
        answers.events.put_nowait(FINISHED)
        answers.events.put_nowait(None)
        await until(lambda: r.finished)
        kept = runs.get("c-1")
        assert kept is not None and kept is r
        assert [e["type"] for e in await read(kept.watch())] == ["run", "result"]

    run(test)


def test_a_conversation_answers_one_question_at_a_time() -> None:
    async def test(answers: Scripted, runs: Runs) -> None:
        r = runs.start("q", None, critic=True, conversation="c-1")
        with pytest.raises(Busy):
            runs.start("again", None, critic=True, conversation="c-1")
        runs.start("other", None, critic=True, conversation="c-2")  # another conversation can
        answers.events.put_nowait(None)
        await until(lambda: r.finished)
        follow_up = runs.start("next", None, critic=True, conversation="c-1")
        assert runs.get("c-1") is follow_up
        await runs.close()

    run(test)


def test_without_a_conversation_the_run_gets_one() -> None:
    async def test(answers: Scripted, runs: Runs) -> None:
        r = runs.start("q", None, critic=True, conversation=None)
        assert r.conversation and runs.get(r.conversation) is r
        assert (await anext(r.watch()))["conversation"] == r.conversation

    run(test)


def test_cancel_ends_the_run_at_once_and_stops_its_stream() -> None:
    async def test(answers: Scripted, runs: Runs) -> None:
        r = runs.start("q", None, critic=True, conversation="c-1")
        answers.events.put_nowait(said("one"))
        await until(lambda: len(r.events) == 2)
        reader = asyncio.create_task(read(r.watch()))
        assert runs.cancel("c-1") is True
        got = await reader
        assert [e["type"] for e in got] == ["run", "step", "error"]
        assert got[-1] == {
            "type": "error",
            "code": "cancelled",
            "status": None,
            "message": "the question was cancelled",
        }
        await until(lambda: answers.stopped == 1)
        assert runs.cancel("c-1") is False  # it has ended
        assert runs.cancel("nope") is False
        runs.start("next", None, critic=True, conversation="c-1")  # the conversation is free

    run(test)


@pytest.mark.parametrize(
    ("error", "code", "status"),
    [
        (NoSource("garden"), "no_source", 404),
        (NothingFound("q"), "nothing_found", 404),
        (NoEmbedder(), "no_embedder", 503),
        (RuntimeError("the store is down"), "failed", 500),
    ],
)
def test_a_failure_ends_the_run_with_an_error(error: Exception, code: str, status: int) -> None:
    async def test(answers: Scripted, runs: Runs) -> None:
        r = runs.start("q", "garden", critic=True, conversation="c-1")
        answers.events.put_nowait(error)
        got = await read(r.watch())
        assert [e["type"] for e in got] == ["run", "error"]
        assert (got[-1]["code"], got[-1]["status"]) == (code, status)
        assert got[-1]["message"]

    run(test)


def test_finished_runs_are_kept_up_to_a_limit_oldest_dropped_first() -> None:
    async def test(answers: Scripted, runs: Runs) -> None:
        for n in range(3):
            started = runs.start("q", None, critic=True, conversation=f"c-{n}")
            answers.events.put_nowait(None)
            await until(lambda r=started: r.finished)
        runs.start("q", None, critic=True, conversation="c-3")
        assert runs.get("c-0") is None
        assert all(runs.get(f"c-{n}") is not None for n in (1, 2, 3))

    answers = Scripted()

    async def go() -> None:
        runs = Runs(answers, keep=2)
        try:
            await asyncio.wait_for(test(answers, runs), timeout=5)
        finally:
            await runs.close()

    asyncio.run(go())


def test_a_run_counts_as_activity_while_it_runs() -> None:
    class Activity:
        def __init__(self) -> None:
            self.in_flight = 0

        def request_started(self) -> None:
            self.in_flight += 1

        def request_finished(self) -> None:
            self.in_flight -= 1

    activity = Activity()
    answers = Scripted()

    async def go() -> None:
        runs = Runs(answers, activity=activity)
        r = runs.start("q", None, critic=True, conversation="c-1")
        await until(lambda: activity.in_flight == 1)
        answers.events.put_nowait(None)
        await until(lambda: r.finished)
        await until(lambda: activity.in_flight == 0)
        runs.start("q", None, critic=True, conversation="c-2")
        await until(lambda: activity.in_flight == 1)
        runs.cancel("c-2")
        await until(lambda: activity.in_flight == 0)
        await runs.close()

    asyncio.run(asyncio.wait_for(go(), timeout=5))


def test_close_cancels_the_runs_in_flight() -> None:
    async def test(answers: Scripted, runs: Runs) -> None:
        r = runs.start("q", None, critic=True, conversation="c-1")
        await until(lambda: answers.asked != [])
        await runs.close()
        assert r.finished and r.events[-1]["code"] == "cancelled"
        assert answers.stopped == 1

    run(test)
