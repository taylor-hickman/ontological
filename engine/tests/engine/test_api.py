"""The engine's HTTP API: the bearer token on every request (§4.3) and the lease routes (§4.4)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fake_answers import SQL, FakeAnswers
from fake_jobs import FakeJobs
from fastapi.testclient import TestClient

from ontological.answer.service import Answerability, Answers, Asked, NoEmbedder
from ontological.api.app import create_app
from ontological.compile.estimate import EstimatedTable, estimate
from ontological.engine.lifetime import Lifetime

TOKEN = "t" * 43


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def lifetime(clock: Clock) -> Lifetime:
    return Lifetime(idle_timeout_s=600, renew_every_s=30, clock=clock)


@pytest.fixture
def client(lifetime: Lifetime) -> Iterator[TestClient]:
    app = create_app(token=TOKEN, lifetime=lifetime, pid=4242, version="9.9.9")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        yield c


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer", f"Bearer {TOKEN}x", f"bearer{TOKEN}", f"Basic {TOKEN}", TOKEN],
)
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/health"),
        ("POST", "/leases"),
        ("PUT", "/leases/x"),
        ("DELETE", "/leases/x"),
        ("GET", "/claude"),
        ("POST", "/claude/test"),
        ("GET", "/sources"),
        ("POST", "/sources"),
        ("DELETE", "/sources/x"),
        ("GET", "/sources/x/catalog"),
        ("GET", "/tables/x"),
        ("POST", "/ask"),
        ("POST", "/answerable"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
        ("GET", "/no-such-route"),
    ],
)
def test_every_request_needs_the_token(
    client: TestClient, method: str, path: str, header: str | None
) -> None:
    headers = {} if header is None else {"Authorization": header}
    del client.headers["Authorization"]
    r = client.request(method, path, headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_health_names_the_engine(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"pid": 4242, "version": "9.9.9"}


def test_there_are_no_open_docs_routes(client: TestClient) -> None:
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_take_renew_and_release_a_lease(client: TestClient, lifetime: Lifetime) -> None:
    r = client.post("/leases", json={"client": "mcp"})
    assert r.status_code == 201
    body = r.json()
    assert body["renew_every_s"] == 30
    assert body["expires_after_s"] == 90
    lease = body["lease"]
    assert lifetime.live_leases() == 1

    r = client.put(f"/leases/{lease}")
    assert r.status_code == 200
    assert r.json()["lease"] == lease

    assert client.delete(f"/leases/{lease}").status_code == 204
    assert lifetime.live_leases() == 0
    assert client.delete(f"/leases/{lease}").status_code == 404


def test_a_lease_needs_no_body(client: TestClient) -> None:
    assert client.post("/leases").status_code == 201


def test_renewing_an_expired_lease_is_404(
    client: TestClient, lifetime: Lifetime, clock: Clock
) -> None:
    lease = client.post("/leases").json()["lease"]
    clock.now += 91
    r = client.put(f"/leases/{lease}")
    assert r.status_code == 404, "the client takes a new lease"
    assert client.put("/leases/never-issued").status_code == 404


def test_a_closing_engine_refuses_new_leases(
    client: TestClient, lifetime: Lifetime, clock: Clock
) -> None:
    clock.now += 600
    assert lifetime.should_exit()
    r = client.post("/leases")
    assert r.status_code == 503, "the client starts a new engine"


def test_authorised_requests_count_as_activity(
    client: TestClient, lifetime: Lifetime, clock: Clock
) -> None:
    clock.now += 500
    client.get("/health")
    clock.now += 599
    assert not lifetime.should_exit()


def test_unauthorised_requests_do_not_keep_the_engine_up(
    client: TestClient, lifetime: Lifetime, clock: Clock
) -> None:
    clock.now += 500
    del client.headers["Authorization"]
    client.get("/health")
    clock.now += 100
    assert lifetime.should_exit()


# The Electron renderer loads from app://ontological and calls the engine with fetch, so the API
# answers CORS for that origin, and for the Vite dev server on localhost (ARCHITECTURE.md §4.1).


def preflight(client: TestClient, origin: str, method: str = "POST") -> httpx2.Response:
    return client.options(
        "/leases",
        headers={
            "Authorization": "",
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "authorization, content-type",
        },
    )


@pytest.mark.parametrize("origin", ["app://ontological", "http://localhost:5173"])
def test_the_app_origin_passes_a_preflight_without_the_token(
    client: TestClient, origin: str
) -> None:
    r = preflight(client, origin)
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == origin
    allowed = r.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed


@pytest.mark.parametrize(
    "origin",
    [
        "https://example.com",
        "app://other",
        "http://localhost.evil.com:80",
        "null",
        "http://127.0.0.1:5173",
    ],
)
def test_other_origins_fail_the_preflight(client: TestClient, origin: str) -> None:
    r = preflight(client, origin)
    assert r.status_code == 400
    assert "access-control-allow-origin" not in r.headers


def test_a_preflight_is_not_activity(client: TestClient, lifetime: Lifetime, clock: Clock) -> None:
    clock.now += 500
    preflight(client, "app://ontological")
    clock.now += 100
    assert lifetime.should_exit()


def test_responses_to_the_app_carry_its_origin(client: TestClient) -> None:
    r = client.get("/health", headers={"Origin": "app://ontological"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "app://ontological"
    r = client.get("/health", headers={"Origin": "https://example.com"})
    assert "access-control-allow-origin" not in r.headers


# --- jobs ---------------------------------------------------------------------------------------

WIDE_TABLE = EstimatedTable(
    "t1", "dbo", "orders", columns=20, non_fk=18, undescribed=10, candidates=17, described=True
)

JOB: dict[str, Any] = {
    "id": 3,
    "kind": "compile",
    "database_name": "wwi",
    "state": "paused",
    "paused_until": "2026-11-06T16:10:00Z",
    "error": None,
    "created_at": "2026-11-06T14:31:00Z",
    "finished_at": None,
    "tables": 48,
    "tables_done": 31,
    "calls": 142,
    "cache_hits": 37,
    "new_tables": [],
    "dismissed": False,
    "table_calls": "split",
}


@pytest.fixture
def jobs_client(lifetime: Lifetime, tmp_path: Path) -> Iterator[TestClient]:
    scenario: dict[str, Any] = {
        "jobs": [JOB],
        "details": {"3": {"tables": [
            {"name": "cities", "state": "done", "stages_done": 4, "skipped": None},
            {"name": "orders", "state": "pending", "table_calls": "merged", "stages_done": 0,
             "skipped": None},
        ]}},
        "estimates": {"wwi": estimate("wwi", [WIDE_TABLE]).model_dump(mode="json")},
        "calls": {"3": [{
            "id": 9, "at": "2026-11-06T15:31:58Z", "task": "table_terms",
            "table_name": "cities_archive", "model": "claude-sonnet-5-5", "outcome": "limited",
            "cached": False, "attempt": 1, "duration_ms": 900,
        }]},
    }  # fmt: skip
    (tmp_path / "jobs.json").write_text(json.dumps(scenario))
    app = create_app(
        token=TOKEN, lifetime=lifetime, pid=1, version="9.9.9", jobs_service=FakeJobs(tmp_path)
    )
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        yield c


def test_jobs_routes_need_a_store(client: TestClient) -> None:
    assert client.get("/jobs").status_code == 503
    assert client.post("/jobs/compile", json={"database_name": "x"}).status_code == 503


def test_jobs_lists_reads_and_acts_on_jobs(jobs_client: TestClient) -> None:
    [job] = jobs_client.get("/jobs").json()["jobs"]
    assert (job["id"], job["state"], job["cache_hits"]) == (3, "paused", 37)
    detail = jobs_client.get("/jobs/3").json()
    assert detail["stages"]["merged"] == ["table_semantics"]
    assert [(t["name"], t["table_calls"]) for t in detail["tables"]] == [
        ("cities", "split"), ("orders", "merged"),
    ]  # fmt: skip

    estimated = jobs_client.get("/jobs/estimate", params={"database_name": "wwi"}).json()
    assert (estimated["tables"], estimated["split"]["calls"], estimated["merged"]["calls"]) == (
        1, 4, 1,
    )  # fmt: skip
    assert (
        jobs_client.post("/jobs/3/table_calls", json={"table_calls": "merged"}).status_code == 202
    )
    assert jobs_client.get("/jobs").json()["jobs"][0]["table_calls"] == "merged"
    assert jobs_client.post("/jobs/3/table_calls", json={"table_calls": "x"}).status_code == 422
    [call] = jobs_client.get("/jobs/3/calls").json()["calls"]
    assert (call["task"], call["outcome"], call["cached"]) == ("table_terms", "limited", False)

    assert jobs_client.post("/jobs/3/hold").status_code == 202
    assert jobs_client.get("/jobs").json()["jobs"][0]["state"] == "held"
    assert jobs_client.post("/jobs/3/hold").status_code == 409, "a held job can't be held"
    assert jobs_client.post("/jobs/3/resume").status_code == 202
    assert jobs_client.post("/jobs/compile", json={"database_name": "wwi"}).status_code == 409
    assert jobs_client.post("/jobs/3/dismiss").status_code == 202
    assert jobs_client.get("/jobs").json()["jobs"][0]["dismissed"] is True


def test_jobs_routes_answer_404_for_what_isnt_there(jobs_client: TestClient) -> None:
    assert jobs_client.get("/jobs/99").status_code == 404
    assert jobs_client.get("/jobs/99/calls").status_code == 404
    assert jobs_client.post("/jobs/99/hold").status_code == 404
    assert jobs_client.post("/jobs/3/explode").status_code == 404
    assert jobs_client.post("/jobs/compile", json={"database_name": "nope"}).status_code == 404


# --- ask -------------------------------------------------------------------------------------


def ask_client(lifetime: Lifetime, answers: Answers) -> TestClient:
    app = create_app(token=TOKEN, lifetime=lifetime, pid=1, version="9.9.9", answers=answers)
    c = TestClient(app, base_url="http://127.0.0.1")
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    return c


def test_ask_returns_the_answer_its_sql_rows_and_steps(lifetime: Lifetime) -> None:
    answers = FakeAnswers()
    with ask_client(lifetime, answers) as c:
        r = c.post("/ask", json={"question": "Who has the most albums?", "database": "chinook"})
    assert r.status_code == 200
    got = Asked.model_validate(r.json())
    assert got.verified and got.reason is None
    assert got.sql == SQL
    assert got.result is not None and got.result.rows[0] == ["Iron Maiden", 21]
    assert [s.name for s in got.steps] == [
        "plan",
        "validate_sql",
        "validate_sql",
        "run_sql",
        "review_answer",
    ]
    assert got.steps[1].is_error
    assert answers.asked == [("Who has the most albums?", "chinook", True)]


def test_ask_passes_the_critic_setting(lifetime: Lifetime) -> None:
    answers = FakeAnswers()
    with ask_client(lifetime, answers) as c:
        r = c.post("/ask", json={"question": "q", "database": "chinook", "critic": False})
    assert r.status_code == 200
    assert answers.asked == [("q", "chinook", False)]


def test_ask_answers_404_for_a_database_with_no_source(lifetime: Lifetime) -> None:
    with ask_client(lifetime, FakeAnswers()) as c:
        r = c.post("/ask", json={"question": "q", "database": "nope"})
    assert r.status_code == 404
    assert "nope" in r.json()["detail"]


def test_ask_needs_a_store_and_an_embedder(client: TestClient, lifetime: Lifetime) -> None:
    assert client.post("/ask", json={"question": "q", "database": "chinook"}).status_code == 503

    class NoModel(FakeAnswers):
        async def ask(self, question: str, database: str, *, critic: bool = True) -> Asked:
            raise NoEmbedder

    with ask_client(lifetime, NoModel()) as c:
        r = c.post("/ask", json={"question": "q", "database": "chinook"})
    assert r.status_code == 503
    assert "embedding" in r.json()["detail"]


def test_ask_counts_as_activity_while_it_runs(lifetime: Lifetime, clock: Clock) -> None:
    class Slow(FakeAnswers):
        async def ask(self, question: str, database: str, *, critic: bool = True) -> Asked:
            assert lifetime.should_exit() is False
            clock.now += 10_000  # a long session: no lease, but a request in flight
            assert lifetime.should_exit() is False
            return await FakeAnswers().ask(question, database, critic=critic)

    with ask_client(lifetime, Slow()) as c:
        assert c.post("/ask", json={"question": "q", "database": "chinook"}).status_code == 200


# --- answerable ------------------------------------------------------------------------------


def test_answerable_returns_the_coverage_and_what_is_missing(lifetime: Lifetime) -> None:
    answers = FakeAnswers()
    with ask_client(lifetime, answers) as c:
        r = c.post("/answerable", json={"question": "Artists by release year?"})
        scoped = c.post("/answerable", json={"question": "q", "database": "chinook"})
    assert r.status_code == 200 and scoped.status_code == 200
    got = Answerability.model_validate(r.json())
    assert (got.database, got.coverage, got.missing) == ("chinook", 0.5, ["release year"])
    assert got.entities[0].attributes[0].column == "Name"
    assert answers.checked == [("Artists by release year?", None), ("q", "chinook")]


def test_answerable_needs_a_store_and_an_embedder(client: TestClient, lifetime: Lifetime) -> None:
    assert client.post("/answerable", json={"question": "q"}).status_code == 503

    class NoModel(FakeAnswers):
        async def check(self, question: str, database: str | None = None) -> Answerability:
            raise NoEmbedder

    with ask_client(lifetime, NoModel()) as c:
        r = c.post("/answerable", json={"question": "q"})
    assert r.status_code == 503
    assert "embedding" in r.json()["detail"]
