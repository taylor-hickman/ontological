"""The engine's HTTP API (ARCHITECTURE.md §4.3, §4.4).

Every request needs `Authorization: Bearer <token>`, the token engine.json holds, so other local
processes can't reach the API; there are no open routes, docs included. Authorised requests count
as activity for idle shutdown, for as long as they are in flight (an SSE stream keeps the engine
up). Unauthorised ones don't.

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

from ontological.embed.embedder import LocalEmbedder
from ontological.engine.lifetime import Closing, Lease, Lifetime

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

    app.add_middleware(BearerAuth, token=token, lifetime=lifetime)
    # Added last, so it runs first: a preflight is answered before BearerAuth sees it.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[APP_ORIGIN],
        allow_origin_regex=DEV_ORIGIN_REGEX,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["authorization", "content-type"],
    )
    return app
