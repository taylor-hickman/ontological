"""The engine's HTTP API (ARCHITECTURE.md §4.3, §4.4).

Every request needs `Authorization: Bearer <token>`, the token engine.json holds, so other local
processes can't reach the API; there are no open routes, docs included. Authorised requests count
as activity for idle shutdown, for as long as they are in flight (an SSE stream keeps the engine
up). Unauthorised ones don't.

Onboarding and the catalog screen read the routes in `setup.py`: Claude Code's install and login,
and the sources and their catalogs.

Jobs (DESIGN.md §5.4): `GET /jobs` lists the latest, `GET /jobs/{id}` adds a compile's tables
and their stages, `GET /jobs/{id}/calls` is its call log. `GET /jobs/estimate?database_name=`
estimates a database's next compile, or the rest of its unfinished one, with split and with
merged (§6.5), and asks Claude nothing. `POST /jobs/compile` starts or resumes a database's
compile, which uses Claude quota, optionally with `table_calls` for its tables;
`POST /jobs/{id}/table_calls` switches an unfinished compile's option from its next pack;
`POST /jobs/{id}/hold` pauses one until `POST /jobs/{id}/resume`; `POST /jobs/{id}/dismiss` puts
off a refresh's new-tables prompt. The routes answer 503 in an engine with no store to keep jobs
in.

Settings → Embeddings: `GET /embeddings` names the engine's embedding model and the databases with
vectors from another; `POST /jobs/reembed` starts a `reembed` job for each of them and names them.
It answers 503 in an engine with no embedder too.

Ask: `POST /ask` answers one question from one database with the gated agent (§8.1), which uses
Claude quota, and returns once the session ends: the answer, whether the post-run check verified
it, the validated SQL, its rows and the tool log (`ontological.answer.service.Asked`). With no
`database`, the database whose ontology covers the question best answers it. It answers 404 for
a database with no source, or a question no database's ontology matches, and 503 in an engine
with no store or no embedder.
`POST /ask/stream` asks the same question as server-sent events, as upstream's
`/chat/completions` streams (§8.4): each frame is `data: <JSON>`, the JSON's `type` saying what it
is (`run`, `step`, `sql`, `usage`, then `result` or `error`; `ontological.answer.runs`), and
the stream ends with `data: [DONE]`. The question runs in the background, keyed by its
`conversation` (the client's id, or a new one the `run` event names), so leaving the stream
doesn't stop it. `GET /ask/watch?conversation=` re-attaches: it replays the conversation's latest
run from its first event and follows it to the end, or ends at once when there is none.
`POST /ask/cancel?conversation=` stops a run in flight: its streams end with a `cancelled` error,
and the session's `claude` child and source query are stopped. A second question in a
conversation whose last is still running is 409. Failures that `/ask` answers 404 or 503 for
end the stream with an `error` event carrying that status.
`POST /answerable` is `check_answerable` (§8.3), the pre-check: one Haiku call, then how much of
the question the semantic layer covers, within the database given or the one that covers it best
(`ontological.answer.answerable.Answerability`). It answers 503 as `/ask` does.

For the MCP shim (M6.2): `GET /terms?q=&database=&limit=` searches the Terms by hybrid search,
503 in an engine with no embedder. `POST /sql/validate` runs the gate (§8.2) on SQL for a
database with no agent session and returns its issues, or a `validation_id`; 404 for a database
with no source. `POST /sql/run` runs SQL by that id: its rows, or an `empty` outcome with what to
recheck; 404 for an id the engine didn't issue (or has forgotten, as a restart does), 422 when the
source refuses the SQL. None of them use Claude quota, and all answer 503 in an engine with no
store.

The Terms and Ontology screens read and edit the ontology through the routes in `curate.py`,
models are imported and exported through the routes in `interchange.py`, and Settings reads and
writes the person's settings, such as Compile's auto-compile, through the routes in `settings.py`.

Leases: `POST /leases` takes one, `PUT /leases/{id}` renews it, `DELETE /leases/{id}` releases
it. A renewal of an expired lease is 404, and the client takes a new one. A closing engine
refuses new leases with 503, and the client starts a new engine.

CORS: the Electron renderer loads from app://ontological and calls the API with fetch, so the API
allows that origin, and the Vite dev server on localhost in development. A preflight carries no
token and is answered before the token check. Other origins still can't read engine.json, so the
token stays the API's protection; CORS only lets the app's own page through.
"""

from __future__ import annotations

import hmac
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from ontological.answer.runs import Busy, Event, Runs
from ontological.answer.service import (
    SEARCH_TERMS_K,
    Answerability,
    Answers,
    Asked,
    NoEmbedder,
    NoSuchValidation,
    NothingFound,
    RanSql,
    RunFailed,
    TermHit,
    Validated,
)
from ontological.api.curate import add_curate_routes
from ontological.api.interchange import add_interchange_routes
from ontological.api.settings import add_settings_routes
from ontological.api.setup import add_setup_routes
from ontological.catalog.sources import Sources
from ontological.compile.estimate import Estimate
from ontological.compile.packing import TableCalls
from ontological.curate.service import OntologyService
from ontological.embed.embedder import LocalEmbedder
from ontological.engine.lifetime import Closing, Lease, Lifetime
from ontological.interchange.store import InterchangeService
from ontological.jobs.active import ActiveJobs, JobBusy
from ontological.jobs.service import Embeddings, JobsService, NoSource, NoSuchJob, NotNow
from ontological.jobs.service import NoEmbedder as NoReembedder
from ontological.jobs.view import CallRow, JobDetail, JobSummary
from ontological.llm.install import Claude
from ontological.settings import SettingsService

log = logging.getLogger(__name__)

APP_ORIGIN = "app://ontological"
DEV_ORIGIN_REGEX = r"http://localhost:\d+"


class LeaseRequest(BaseModel):
    client: str = ""


class LeaseOut(BaseModel):
    lease: str
    renew_every_s: float
    expires_after_s: float

    @classmethod
    def of(cls, lease: Lease) -> LeaseOut:
        return cls(
            lease=lease.id,
            renew_every_s=lease.renew_every_s,
            expires_after_s=lease.expires_after_s,
        )


class CompileRequest(BaseModel):
    database_name: str
    table_calls: TableCalls | None = None  # None: a new job splits, a resumed one keeps its own


class AskRequest(BaseModel):
    question: str
    database: str | None = None  # none: the database that covers the question best
    critic: bool = True  # the `answer.critic` setting (§8.1)


class StreamRequest(AskRequest):
    conversation: str | None = Field(default=None, min_length=1, max_length=200)  # None: new


class Cancelled(BaseModel):
    cancelled: bool  # False when nothing was running


# `no-store`: the frames carry the user's SQL and its rows, as upstream's chat streams do
SSE_HEADERS = {"Cache-Control": "no-store"}


def event_stream(events: AsyncIterator[Event]) -> StreamingResponse:
    async def frames() -> AsyncIterator[str]:
        async for event in events:
            yield f"data: {json.dumps(event)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(frames(), media_type="text/event-stream", headers=SSE_HEADERS)


async def nothing() -> AsyncIterator[Event]:
    return
    yield


class AnswerableRequest(BaseModel):
    question: str
    database: str | None = None  # None: the database that covers the question best


class ValidateRequest(BaseModel):
    sql: str
    database: str
    soft_as_warnings: bool = False  # the soft checks warn instead of failing the SQL


class RunRequest(BaseModel):
    validation_id: str


class TermHits(BaseModel):
    terms: list[TermHit]


class TableCallsRequest(BaseModel):
    table_calls: TableCalls


class JobList(BaseModel):
    jobs: list[JobSummary]


class CallList(BaseModel):
    calls: list[CallRow]


class Reembedding(BaseModel):
    databases: list[str]


class Health(BaseModel):
    pid: int
    version: str


class BearerAuth:
    """Refuses a request without the token before any route sees it, and tracks the ones that
    have it as activity."""

    def __init__(self, app: ASGIApp, *, token: str, lifetime: Lifetime) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()
        self.lifetime = lifetime

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        given = dict(scope["headers"]).get(b"authorization", b"")
        if not hmac.compare_digest(given, self.expected):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"www-authenticate", b"Bearer"),
                        (b"content-type", b"application/json"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b'{"detail":"Unauthorized"}'})
            return
        self.lifetime.request_started()
        try:
            await self.app(scope, receive, send)
        finally:
            self.lifetime.request_finished()


def create_app(
    *,
    token: str,
    lifetime: Lifetime,
    pid: int,
    version: str,
    embedder: LocalEmbedder | None = None,
    jobs: ActiveJobs | None = None,
    claude: Claude | None = None,
    sources: Sources | None = None,
    jobs_service: JobsService | None = None,
    ontology: OntologyService | None = None,
    answers: Answers | None = None,
    interchange: InterchangeService | None = None,
    settings: SettingsService | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Ontological engine",
        version=version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # The one embedder, calibrated at start. Routes call it concurrently, with no queue (§5.6).
    app.state.embedder = embedder
    # The jobs running now, paused ones included: the count idle shutdown reads (§4.4). A job
    # started from a route runs under it.
    app.state.jobs = jobs or ActiveJobs()
    # Questions streamed by conversation (§8.4); the engine closes them at shutdown.
    runs = Runs(answers, activity=lifetime) if answers is not None else None
    app.state.runs = runs

    def service() -> JobsService:
        if jobs_service is None:
            raise HTTPException(503, "this engine has no store to keep jobs in")
        return jobs_service

    @app.get("/health")
    async def health() -> Health:  # pyright: ignore[reportUnusedFunction]
        return Health(pid=pid, version=version)

    @app.post("/leases", status_code=201)
    async def take_lease(body: LeaseRequest | None = None) -> LeaseOut:  # pyright: ignore[reportUnusedFunction]
        client = body.client if body else ""
        try:
            lease = lifetime.take(client)
        except Closing:
            raise HTTPException(503, "the engine is shutting down; start a new one") from None
        log.info("lease %s taken by %s", lease.id[:6], client or "a client")
        return LeaseOut.of(lease)

    @app.put("/leases/{lease_id}")
    async def renew_lease(lease_id: str) -> LeaseOut:  # pyright: ignore[reportUnusedFunction]
        lease = lifetime.renew(lease_id)
        if lease is None:
            raise HTTPException(404, "no such lease; it expired or was released")
        return LeaseOut.of(lease)

    @app.delete("/leases/{lease_id}", status_code=204)
    async def release_lease(lease_id: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        if not lifetime.release(lease_id):
            raise HTTPException(404, "no such lease")
        log.info("lease %s released", lease_id[:6])
        return Response(status_code=204)

    add_setup_routes(app, claude=claude, sources=sources)
    add_curate_routes(app, ontology=ontology)
    add_interchange_routes(app, interchange=interchange)
    add_settings_routes(app, settings=settings)

    @app.post("/ask")
    async def ask(body: AskRequest) -> Asked:  # pyright: ignore[reportUnusedFunction]
        if answers is None:
            raise HTTPException(503, "this engine has no store to answer from")
        try:
            return await answers.ask(body.question, body.database, critic=body.critic)
        except NoSource as e:
            raise HTTPException(404, f"no source for {e}") from None
        except NothingFound:
            raise HTTPException(
                404, "nothing in any database's ontology matches the question; name a database"
            ) from None
        except NoEmbedder:
            raise HTTPException(503, "this engine has no embedding model to search with") from None

    @app.post("/ask/stream")
    async def ask_stream(body: StreamRequest) -> StreamingResponse:  # pyright: ignore[reportUnusedFunction]
        if runs is None:
            raise HTTPException(503, "this engine has no store to answer from")
        try:
            run = runs.start(
                body.question, body.database, critic=body.critic, conversation=body.conversation
            )
        except Busy:
            raise HTTPException(
                409, "a question is still being answered in this conversation; cancel it first"
            ) from None
        return event_stream(run.watch())

    @app.get("/ask/watch")
    async def ask_watch(conversation: str) -> StreamingResponse:  # pyright: ignore[reportUnusedFunction]
        run = runs.get(conversation) if runs is not None else None
        return event_stream(run.watch() if run is not None else nothing())

    @app.post("/ask/cancel")
    async def ask_cancel(conversation: str) -> Cancelled:  # pyright: ignore[reportUnusedFunction]
        return Cancelled(cancelled=runs is not None and runs.cancel(conversation))

    @app.post("/answerable")
    async def answerable(body: AnswerableRequest) -> Answerability:  # pyright: ignore[reportUnusedFunction]
        if answers is None:
            raise HTTPException(503, "this engine has no store to check against")
        try:
            return await answers.check(body.question, body.database)
        except NoEmbedder:
            raise HTTPException(503, "this engine has no embedding model to search with") from None

    def need_answers() -> Answers:
        if answers is None:
            raise HTTPException(503, "this engine has no store")
        return answers

    @app.get("/terms")
    async def search_terms(  # pyright: ignore[reportUnusedFunction]
        q: str,
        database: str | None = None,
        limit: Annotated[int, Query(ge=1, le=50)] = SEARCH_TERMS_K,
    ) -> TermHits:
        try:
            return TermHits(terms=await need_answers().search_terms(q, database, limit))
        except NoEmbedder:
            raise HTTPException(503, "this engine has no embedding model to search with") from None

    @app.post("/sql/validate")
    async def validate_sql(body: ValidateRequest) -> Validated:  # pyright: ignore[reportUnusedFunction]
        try:
            return await need_answers().validate_sql(
                body.sql, body.database, soft_as_warnings=body.soft_as_warnings
            )
        except NoSource as e:
            raise HTTPException(404, f"no source for {e}") from None

    @app.post("/sql/run")
    async def run_sql(body: RunRequest) -> RanSql:  # pyright: ignore[reportUnusedFunction]
        try:
            return await need_answers().run_sql(body.validation_id)
        except NoSuchValidation:
            raise HTTPException(
                404,
                f"{body.validation_id!r} isn't a validation_id this engine issued; validate the "
                "SQL again",
            ) from None
        except RunFailed as e:
            raise HTTPException(422, f"the source couldn't run the SQL: {e}") from None

    @app.get("/jobs")
    async def list_jobs() -> JobList:  # pyright: ignore[reportUnusedFunction]
        return JobList(jobs=await service().summaries())

    # Before /jobs/{job_id}, which would read "estimate" as an id
    @app.get("/jobs/estimate")
    async def estimate(database_name: str) -> Estimate:  # pyright: ignore[reportUnusedFunction]
        return await service().estimate(database_name)

    @app.get("/jobs/{job_id}")
    async def job_detail(job_id: int) -> JobDetail:  # pyright: ignore[reportUnusedFunction]
        try:
            return await service().detail(job_id)
        except NoSuchJob:
            raise HTTPException(404, "no such job") from None

    @app.get("/jobs/{job_id}/calls")
    async def job_calls(job_id: int) -> CallList:  # pyright: ignore[reportUnusedFunction]
        try:
            return CallList(calls=await service().calls(job_id))
        except NoSuchJob:
            raise HTTPException(404, "no such job") from None

    @app.post("/jobs/compile", status_code=202)
    async def start_compile(body: CompileRequest) -> Response:  # pyright: ignore[reportUnusedFunction]
        try:
            await service().compile(body.database_name, body.table_calls)
        except NoSource:
            raise HTTPException(404, f"no source for {body.database_name}") from None
        except JobBusy as e:
            raise HTTPException(409, str(e)) from None
        return Response(status_code=202)

    @app.get("/embeddings")
    async def embeddings() -> Embeddings:  # pyright: ignore[reportUnusedFunction]
        return await service().embeddings()

    @app.post("/jobs/reembed", status_code=202)
    async def reembed_stale() -> Reembedding:  # pyright: ignore[reportUnusedFunction]
        try:
            return Reembedding(databases=await service().reembed_stale())
        except NoReembedder:
            raise HTTPException(503, "this engine has no embedding model") from None

    @app.post("/jobs/{job_id}/table_calls", status_code=202)
    async def choose(job_id: int, body: TableCallsRequest) -> Response:  # pyright: ignore[reportUnusedFunction]
        try:
            await service().choose(job_id, body.table_calls)
        except NoSuchJob:
            raise HTTPException(404, "no such job") from None
        except NotNow as e:
            raise HTTPException(409, str(e)) from None
        return Response(status_code=202)

    @app.post("/jobs/{job_id}/{action}", status_code=202)
    async def act_on_job(job_id: int, action: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        jobs = service()
        actions = {"hold": jobs.hold, "resume": jobs.resume, "dismiss": jobs.dismiss}
        if action not in actions:
            raise HTTPException(404, "no such action")
        try:
            await actions[action](job_id)
        except NoSuchJob:
            raise HTTPException(404, "no such job") from None
        except (NotNow, JobBusy) as e:
            raise HTTPException(409, str(e)) from None
        except NoSource as e:
            raise HTTPException(404, f"no source for {e}") from None
        return Response(status_code=202)

    app.add_middleware(BearerAuth, token=token, lifetime=lifetime)
    # Added last, so it runs first: a preflight is answered before BearerAuth sees it.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[APP_ORIGIN],
        allow_origin_regex=DEV_ORIGIN_REGEX,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["authorization", "content-type"],
    )
    return app
