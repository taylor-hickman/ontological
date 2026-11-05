"""The engine's HTTP API: the bearer token on every request (§4.3) and the lease routes (§4.4)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
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
