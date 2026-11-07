"""Questions answered in the background, by conversation, for the UI's stream (ARCHITECTURE.md
§8.4, BUILD_PLAN.md M4.5), as upstream's chat runs are kept for `/chat/watch`.

`Runs.start()` reads `Answers.stream()` into the run's buffer, in a task of its own, so the run
goes on whether or not anyone is reading it, as upstream's `_pump`. `Run.watch()` replays the
buffer from its first event, then follows it live until the run ends, so a reader that leaves
(a reload, another screen) and comes back misses nothing. Each event is the JSON object one SSE
frame carries: `run` first (the conversation and the question), then the stream's `step`, `sql`
and `usage` events, and last either `result` or `error`.

A conversation answers one question at a time: starting another while one runs is `Busy`. A run
that has ended is kept, for a reader to replay, until a newer question in its conversation
replaces it or it is among the oldest past `keep` ended runs.

`Runs.cancel()` ends the run at once with a `cancelled` error, so its readers stop and the
conversation is free, and cancels its task. That stops the stream: the SDK closes the session's
`claude` child (stdin EOF, then SIGTERM 5 s later) and the question's source executors close,
which interrupts a source query in flight and kills its executor if it won't stop (§5.6). That
cleanup finishes in the background.

The runs live in the engine's memory: they don't outlast it. A run counts as activity for idle
shutdown while it runs, read or not.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from ontological.answer.service import Answers, NoEmbedder, NoSource, NothingFound

__all__ = ["Busy", "Event", "Failed", "Opened", "Run", "Runs"]

log = logging.getLogger(__name__)

KEEP = 20  # ended runs kept for a reader to replay

Event = dict[str, Any]  # one SSE frame's JSON


class Busy(Exception):
    """A question is already being answered in this conversation."""


class Activity(Protocol):
    """What idle shutdown counts (`ontological.engine.lifetime.Lifetime`)."""

    def request_started(self) -> None: ...

    def request_finished(self) -> None: ...


class Opened(BaseModel):
    """A run's first event."""

    type: Literal["run"] = "run"
    conversation: str
    question: str
    database: str | None  # None: the database that covers the question best
    critic: bool


class Failed(BaseModel):
    """A run that ended without an answer. `status` is the HTTP status `POST /ask` answers with
    for the same failure; a cancelled run has none."""

    type: Literal["error"] = "error"
    code: Literal["cancelled", "no_source", "nothing_found", "no_embedder", "failed"]
    status: int | None
    message: str


CANCELLED = Failed(code="cancelled", status=None, message="the question was cancelled")


class Run:
    def __init__(self, conversation: str, question: str) -> None:
        self.conversation = conversation
        self.question = question
        self.events: list[Event] = []
        self.finished = False
        self._grew = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    async def watch(self) -> AsyncGenerator[Event]:
        """Every event from the first, then each new one as it comes, until the run ends."""
        i = 0
        while True:
            if i < len(self.events):
                i += 1
                yield self.events[i - 1]
            elif self.finished:
                return
            else:
                await self._grew.wait()

    # Runs' own: the run's events come from its stream

    def add(self, event: BaseModel) -> None:
        if not self.finished:
            self.events.append(event.model_dump(mode="json"))
            self._wake()

    def end(self, event: BaseModel | None = None) -> None:
        if self.finished:
            return
        if event is not None:
            self.add(event)
        self.finished = True
        self._wake()

    def _wake(self) -> None:
        self._grew.set()
        self._grew = asyncio.Event()


class Runs:
    def __init__(
        self, answers: Answers, *, activity: Activity | None = None, keep: int = KEEP
    ) -> None:
        self._answers = answers
        self._activity = activity
        self._keep = keep
        self._runs: dict[str, Run] = {}  # in the order they started
        self.tasks: set[asyncio.Task[None]] = set()  # cancelled ones too, until they stop

    def start(
        self, question: str, database: str | None, *, critic: bool, conversation: str | None
    ) -> Run:
        """Start answering `question` in `conversation`, or in a new one when it is None. Raises
        `Busy` while the conversation's last question is still being answered."""
        conversation = conversation or uuid.uuid4().hex
        current = self._runs.get(conversation)
        if current is not None and not current.finished:
            raise Busy(conversation)
        self._runs.pop(conversation, None)
        self._prune()
        run = Run(conversation, question)
        run.add(
            Opened(conversation=conversation, question=question, database=database, critic=critic)
        )
        self._runs[conversation] = run
        task = asyncio.create_task(self._pump(run, database, critic))
        run.task = task
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return run

    def get(self, conversation: str) -> Run | None:
        """The conversation's latest run, running or kept."""
        return self._runs.get(conversation)

    def cancel(self, conversation: str) -> bool:
        """End the conversation's run now, and stop its session. False when none is running."""
        run = self._runs.get(conversation)
        if run is None or run.finished:
            return False
        run.end(CANCELLED)
        if run.task is not None:
            run.task.cancel()
        return True

    async def close(self) -> None:
        """Cancel every run, and wait until each has stopped: at the engine's shutdown."""
        for conversation in list(self._runs):
            self.cancel(conversation)
        if self.tasks:
            await asyncio.wait(list(self.tasks))

    def _prune(self) -> None:
        ended = [c for c, run in self._runs.items() if run.finished]
        for conversation in ended[: max(0, len(ended) - self._keep)]:
            del self._runs[conversation]

    async def _pump(self, run: Run, database: str | None, critic: bool) -> None:
        if self._activity is not None:
            self._activity.request_started()
        try:
            async for event in self._answers.stream(run.question, database, critic=critic):
                run.add(event)
        except asyncio.CancelledError:
            run.end(CANCELLED)
            raise
        except NoSource as e:
            run.end(Failed(code="no_source", status=404, message=f"no source for {e}"))
        except NothingFound:
            message = "nothing in any database's ontology matches the question; name a database"
            run.end(Failed(code="nothing_found", status=404, message=message))
        except NoEmbedder:
            message = "this engine has no embedding model to search with"
            run.end(Failed(code="no_embedder", status=503, message=message))
        except Exception as e:
            log.exception("answering %r failed", run.question)
            message = f"{type(e).__name__}: {e}"
            run.end(Failed(code="failed", status=500, message=message))
        else:
            run.end()
        finally:
            if self._activity is not None:
                self._activity.request_finished()
