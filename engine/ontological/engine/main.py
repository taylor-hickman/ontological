"""The engine process (ARCHITECTURE.md §4.3, §4.4).

    python -m ontological.engine [--app-data DIR] [--pg-bin DIR] [--model-dir DIR] [--headless]
                                 [--idle-timeout S] [--lease-renew S]

Under engine.lock, it looks for a live engine through engine.json. If there is one, it prints
that engine and exits 0: the caller attaches to it. Otherwise it starts or adopts the store's
Postgres, applies pending migrations (§5.3), loads the embedder from `--model-dir` and calibrates
its bulk batch cap (§7.1; with no model directory it runs without one), serves the API on
127.0.0.1 at a random port with a new bearer token, writes engine.json and releases the lock. It
prints one JSON line when it serves or attaches, `{"pid", "port", "version", "started"}`, or
`{"error"}` and exits 1.

It exits 0 after `--idle-timeout` seconds (600) with no live lease, no request in flight and no
running job, or on SIGTERM or SIGINT. Either way it removes engine.json, writes the quit dump
and stops Postgres, all under engine.lock, so an engine starting meanwhile can't adopt the
server this one is stopping.

In the foreground the engine is its caller's child, which suits the Electron app: it restarts an
engine it started if it crashes. `--headless` is for every other client, such as the MCP shim
and a shell: it starts the engine detached, in its own session with its output in
logs/engine.log, prints the same line once it serves, and exits, leaving the engine to run until
it idles out.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any, Protocol

import uvicorn

import ontological
from ontological.api.app import create_app
from ontological.embed.embedder import LocalEmbedder
from ontological.engine import discovery
from ontological.engine.lifetime import IDLE_TIMEOUT_S, RENEW_EVERY_S, Lifetime
from ontological.store import dal, pg

log = logging.getLogger("ontological.engine")

HEADLESS_START_TIMEOUT_S = 300  # a first launch or a major-version move can take a while
GRACEFUL_SHUTDOWN_S = 5  # how long an open request (an SSE stream) may hold up a stop

# The Claude Code session variables §6.2 lists, which an engine started by the MCP shim inherits
# from a Claude Code parent. `options.env` can't remove a key (spike S1), so the engine deletes
# them from its own environment at startup, before anything spawns `claude`. M0.6 fills in the
# list, with its test.
SESSION_VARIABLES: tuple[str, ...] = ()


def sanitize_environ(environ: MutableMapping[str, str] = os.environ) -> None:
    for name in SESSION_VARIABLES:
        environ.pop(name, None)


@dataclass(frozen=True)
class Config:
    app_data: Path  # <appData>/Ontological
    pg_bin: Path | None
    model_dir: Path | None = None  # the embedding model's files (ontological.embed.model)
    idle_timeout_s: float = IDLE_TIMEOUT_S
    renew_every_s: float = RENEW_EVERY_S


class Store(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...


class PgStore:
    """The bundled Postgres (§5.3)."""

    def __init__(self, layout: pg.Layout) -> None:
        self.layout = layout

    def start(self) -> None:
        """Start or adopt the server, then bring both Alembic chains to their heads (a first
        launch has already run them)."""
        server = pg.ensure_running(self.layout)
        log.info("store: %s, postmaster %d", server.action, server.postmaster_pid)
        for note in server.notes:
            log.info("store: %s", note)
        if (dump := pg.migrate(self.layout)) is not None:
            log.info("store: migrated, after dumping %s", dump.name)
        dal.connect(self.layout)

    def stop(self) -> None:
        dal.close()
        dump = pg.stop(self.layout)
        log.info("store stopped; quit dump %s", dump.name if dump else "not needed")


def pg_store(config: Config) -> Store:
    if config.pg_bin is None:
        raise RuntimeError("no Postgres binaries: pass --pg-bin or set ONTOLOGICAL_PG_BIN")
    return PgStore(pg.Layout(config.app_data, config.pg_bin))


def start_embedder(config: Config) -> LocalEmbedder | None:
    """Load the embedder and set its bulk batch cap from one timed batch (§7.1)."""
    if config.model_dir is None:
        log.warning("embedder: no model directory, so no embeddings")
        return None
    embedder = LocalEmbedder(config.model_dir)
    cap = embedder.calibrate()
    log.info("embedder: %s, bulk batches up to %d padded tokens", embedder.model, cap)
    return embedder


def default_app_data() -> Path:
    if env := os.environ.get("ONTOLOGICAL_APP_DATA"):
        return Path(env)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Ontological"
    data_home = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(data_home) / "Ontological"


def parse_args(argv: Sequence[str]) -> tuple[Config, bool]:
    ap = argparse.ArgumentParser(prog="ontological-engine", description="Ontological's engine")
    ap.add_argument("--app-data", type=Path, default=None, help="<appData>/Ontological")
    ap.add_argument("--pg-bin", type=Path, default=os.environ.get("ONTOLOGICAL_PG_BIN"))
    ap.add_argument("--model-dir", type=Path, default=os.environ.get("ONTOLOGICAL_MODEL_DIR"))
    ap.add_argument("--headless", action="store_true", help="start detached, print, exit")
    ap.add_argument("--idle-timeout", type=float, default=IDLE_TIMEOUT_S, metavar="S")
    ap.add_argument("--lease-renew", type=float, default=RENEW_EVERY_S, metavar="S")
    a = ap.parse_args(argv)
    config = Config(
        app_data=(a.app_data or default_app_data()).absolute(),
        pg_bin=Path(a.pg_bin) if a.pg_bin else None,
        model_dir=Path(a.model_dir) if a.model_dir else None,
        idle_timeout_s=a.idle_timeout,
        renew_every_s=a.lease_renew,
    )
    return config, a.headless


def emit(**fields: Any) -> None:
    print(json.dumps(fields), flush=True)


def announce(info: discovery.EngineInfo, *, started: bool) -> None:
    emit(pid=info.pid, port=info.port, version=info.version, started=started)


def main(
    argv: Sequence[str] | None = None,
    *,
    store: Callable[[Config], Store] = pg_store,
    relaunch: Sequence[str] = (sys.executable, "-m", "ontological.engine"),
) -> int:
    """`store` makes the store; `relaunch` is the command `--headless` runs, without arguments."""
    args = list(sys.argv[1:] if argv is None else argv)
    config, headless = parse_args(args)
    if headless:
        rest = [a for a in args if a != "--headless"]
        return start_headless(config, [*relaunch, *rest])
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s %(message)s"
    )
    return serve(config, store)


# --- the engine --------------------------------------------------------------------------------


class _Stop:
    """SIGTERM and SIGINT ask the engine to stop. While uvicorn serves, it routes them to
    `_Server.handle_exit`, which lands here too."""

    def __init__(self) -> None:
        self.requested = False

    def __call__(self, signum: int, frame: FrameType | None) -> None:
        if not self.requested:
            log.info("%s: stopping", signal.Signals(signum).name)
        self.requested = True


class _Server(uvicorn.Server):
    """uvicorn's server, except that a signal doesn't stop it at once: the engine's own loop
    takes engine.lock first (see `_serve`)."""

    def __init__(self, config: uvicorn.Config, stop: _Stop) -> None:
        super().__init__(config)
        self.stop = stop

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        self.stop(sig, frame)


def serve(config: Config, make_store: Callable[[Config], Store]) -> int:
    sanitize_environ()
    stop = _Stop()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # Held from here until engine.json is written, and again from when the engine decides to
    # stop until its store has stopped.
    lock = discovery.EngineLock(config.app_data)
    lock.acquire()
    store: Store | None = None
    code = 0
    try:
        if (live := discovery.find_engine(config.app_data)) is not None:
            announce(live, started=False)
            return 0
        sock = discovery.bind_localhost()
        new_store = make_store(config)
        new_store.start()
        store = new_store
        embedder = start_embedder(config)
        if not stop.requested:
            asyncio.run(_serve(config, sock, lock, stop, embedder))
    except Exception as e:
        log.exception("engine failed")
        emit(error=f"{type(e).__name__}: {e}")
        code = 1
    lock.acquire()
    try:
        discovery.remove_engine_json(config.app_data, pid=os.getpid())
        if store is not None:
            store.stop()
    except Exception:
        log.exception("stopping the store failed")
        code = 1
    finally:
        lock.release()
    return code


async def _serve(
    config: Config,
    sock: socket.socket,
    lock: discovery.EngineLock,
    stop: _Stop,
    embedder: LocalEmbedder | None,
) -> None:
    lifetime = Lifetime(idle_timeout_s=config.idle_timeout_s, renew_every_s=config.renew_every_s)
    info = discovery.EngineInfo(
        pid=os.getpid(),
        port=sock.getsockname()[1],
        token=discovery.new_token(),
        version=ontological.__version__,
    )
    app = create_app(
        token=info.token,
        lifetime=lifetime,
        pid=info.pid,
        version=info.version,
        embedder=embedder,
    )
    server = _Server(
        uvicorn.Config(
            app,
            log_config=None,
            access_log=False,
            lifespan="off",
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
        ),
        stop,
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        if task.done():
            await task
            raise RuntimeError("the API server stopped before it started")
        await asyncio.sleep(0.01)
    discovery.write_engine_json(config.app_data, info)
    lock.release()
    announce(info, started=True)
    log.info("serving on 127.0.0.1:%d", info.port)

    tick = min(1.0, config.idle_timeout_s / 4, config.renew_every_s)
    while not task.done():
        if lifetime.should_exit():
            log.info("idle for %gs: stopping", config.idle_timeout_s)
        if lifetime.closing or stop.requested:
            lifetime.close()
            # Take engine.lock before we stop answering: an engine starting meanwhile holds it
            # while it checks for us, so it either finds us still serving and exits, or waits
            # until our store has stopped. Otherwise it could adopt the Postgres we are about to
            # stop. The lock is taken off the loop, so /health keeps answering.
            await asyncio.to_thread(lock.acquire)
            discovery.remove_engine_json(config.app_data, pid=info.pid)
            server.should_exit = True
            break
        await asyncio.wait({task}, timeout=tick)
    await task


# --- headless ----------------------------------------------------------------------------------


def start_headless(config: Config, cmd: Sequence[str]) -> int:
    """Start the engine detached and wait until it serves, or until it has found another engine
    serving and exited."""
    logs = config.app_data / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / "engine.log", "ab") as out:
        child = subprocess.Popen(
            list(cmd),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            start_new_session=True,
        )
    deadline = time.monotonic() + HEADLESS_START_TIMEOUT_S
    while True:
        exited = child.poll()
        live = discovery.find_engine(config.app_data)
        if live is not None and live.pid == child.pid:
            announce(live, started=True)
            return 0
        if exited is not None:
            # Another engine was running, so ours exited (0), or ours failed. Only once ours has
            # exited is there one engine for the caller to attach to.
            if exited == 0 and live is not None:
                announce(live, started=False)
                return 0
            emit(error=f"the engine exited with {exited}; see {logs / 'engine.log'}")
            return 1
        if time.monotonic() > deadline:
            emit(error=f"the engine didn't start in {HEADLESS_START_TIMEOUT_S}s")
            return 1
        time.sleep(0.05)


def cli() -> None:
    """The `ontological-engine` console script."""
    sys.exit(main())
