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

Ask: `POST /ask` answers one question from one database with the gated agent (§8.1), which uses
Claude quota, and returns once the session ends: the answer, whether the post-run check verified
it, the validated SQL, its rows and the tool log (`ontological.answer.service.Asked`). It answers
404 for a database with no source, and 503 in an engine with no store or no embedder.
`POST /answerable` is `check_answerable` (§8.3), the pre-check: one Haiku call, then how much of
the question the semantic layer covers, within the database given or the one that covers it best
(`ontological.answer.answerable.Answerability`). It answers 503 as `/ask` does.

The Terms and Ontology screens read and edit the ontology through the routes in `curate.py`, and
models are imported and exported through the routes in `interchange.py`.

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
import logging

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from ontological.answer.service import Answerability, Answers, Asked, NoEmbedder
from ontological.api.curate import add_curate_routes
from ontological.api.interchange import add_interchange_routes
from ontological.api.setup import add_setup_routes
from ontological.catalog.sources import Sources
from ontological.compile.estimate import Estimate
from ontological.compile.packing import TableCalls
from ontological.curate.service import OntologyService
from ontological.embed.embedder import LocalEmbedder
from ontological.engine.lifetime import Closing, Lease, Lifetime
from ontological.interchange.store import InterchangeService
from ontological.jobs.active import ActiveJobs, JobBusy
from ontological.jobs.service import JobsService, NoSource, NoSuchJob, NotNow
from ontological.jobs.view import CallRow, JobDetail, JobSummary
from ontological.llm.install import Claude

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
    database: str
    critic: bool = True  # the `answer.critic` setting (§8.1)


class AnswerableRequest(BaseModel):
    question: str
    database: str | None = None  # None: the database that covers the question best


class TableCallsRequest(BaseModel):
    table_calls: TableCalls


class JobList(BaseModel):
    jobs: list[JobSummary]


class CallList(BaseModel):
    calls: list[CallRow]


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

    @app.post("/ask")
    async def ask(body: AskRequest) -> Asked:  # pyright: ignore[reportUnusedFunction]
        if answers is None:
            raise HTTPException(503, "this engine has no store to answer from")
        try:
            return await answers.ask(body.question, body.database, critic=body.critic)
        except NoSource:
            raise HTTPException(404, f"no source for {body.database}") from None
        except NoEmbedder:
            raise HTTPException(503, "this engine has no embedding model to search with") from None

    @app.post("/answerable")
    async def answerable(body: AnswerableRequest) -> Answerability:  # pyright: ignore[reportUnusedFunction]
        if answers is None:
            raise HTTPException(503, "this engine has no store to check against")
        try:
            return await answers.check(body.question, body.database)
        except NoEmbedder:
            raise HTTPException(503, "this engine has no embedding model to search with") from None

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
