"""The engine's HTTP API: the bearer token on every request (§4.3) and the lease routes (§4.4)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fake_jobs import FakeJobs
from fastapi.testclient import TestClient

from ontological.api.app import create_app
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
}


@pytest.fixture
def jobs_client(lifetime: Lifetime, tmp_path: Path) -> Iterator[TestClient]:
    scenario: dict[str, Any] = {
        "jobs": [JOB],
        "details": {"3": {"stages": ["table_keys"], "tables": [
            {"name": "cities", "state": "done", "stages_done": 1, "skipped": None},
        ]}},
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
    assert (detail["job"]["id"], detail["stages"], detail["tables"][0]["name"]) == (
        3, ["table_keys"], "cities",
    )  # fmt: skip
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
