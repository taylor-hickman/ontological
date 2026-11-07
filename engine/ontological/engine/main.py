"""The engine process (ARCHITECTURE.md §4.3, §4.4).

    python -m ontological.engine [--app-data DIR] [--pg-bin DIR] [--model-dir DIR]
                                 [--claude-cli PATH] [--headless]
                                 [--idle-timeout S] [--lease-renew S]

Under engine.lock, it looks for a live engine through engine.json. If there is one, it prints
that engine and exits 0: the caller attaches to it. Otherwise it starts or adopts the store's
Postgres, applies pending migrations (§5.3), loads the embedder from `--model-dir` and calibrates
its bulk batch cap (§7.1; with no model directory it runs without one), serves the API on
127.0.0.1 at a random port with a new bearer token, writes engine.json and releases the lock. It
prints one JSON line when it serves or attaches, `{"pid", "port", "version", "started"}`, or
`{"error"}` and exits 1. Once it serves, it re-ingests the metadata of every source last ingested
over a day ago, in the background (§9.1, `jobs/staleness.py`), and resumes the compiles an earlier
engine left running or paused (`jobs/service.py`), through its one Claude gateway.

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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any, Protocol

import uvicorn

import ontological
from ontological.answer.service import Answers, StoreAnswers
from ontological.api.app import create_app
from ontological.catalog.sources import Sources, StoreSources
from ontological.curate.service import OntologyService, StoreOntology
from ontological.embed.embedder import LocalEmbedder
from ontological.engine import discovery
from ontological.engine.lifetime import IDLE_TIMEOUT_S, RENEW_EVERY_S, Lifetime
from ontological.jobs import store as job_store
from ontological.jobs.active import ActiveJobs
from ontological.jobs.service import Jobs, JobsService, auto_compile_from
from ontological.jobs.staleness import StalenessCheck
from ontological.llm.calls import PgCallLog
from ontological.llm.env import sanitize_environ
from ontological.llm.gateway import ClaudeGateway
from ontological.llm.install import Claude, ClaudeCheck
from ontological.store import dal, pg

log = logging.getLogger("ontological.engine")

HEADLESS_START_TIMEOUT_S = 300  # a first launch or a major-version move can take a while
GRACEFUL_SHUTDOWN_S = 5  # how long an open request (an SSE stream) may hold up a stop


def close_inherited_fds() -> None:
    """Close every descriptor above stdio that the engine inherited. Electron on Linux leaks
    dozens into its children: Chromium's lock files and IPC sockets, and the pipe to its own
    parent. The engine outlives Electron (§4.4), so holding them would keep Chromium's locks
    taken and make whoever reads that pipe wait for the engine to exit. Python's subprocess does
    the same for its own children. Call it first, before the engine opens anything."""
    try:
        limit = os.sysconf("SC_OPEN_MAX")
    except (ValueError, OSError):
        limit = -1
    os.closerange(3, limit if limit > 3 else 65536)


@dataclass(frozen=True)
class Config:
    app_data: Path  # <appData>/Ontological
    pg_bin: Path | None
    model_dir: Path | None = None  # the embedding model's files (ontological.embed.model)
    claude_cli: Path | None = None  # the user's `claude`, when it isn't the one on PATH
    idle_timeout_s: float = IDLE_TIMEOUT_S
    renew_every_s: float = RENEW_EVERY_S


class Store(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def staleness_check(self, new_tables: Callable[[job_store.Job], object]) -> StalenessCheck: ...
    def sources(self, jobs: ActiveJobs, embedder: LocalEmbedder | None) -> Sources: ...
    def claude(self, config: Config) -> Claude: ...
    def jobs(self, active: ActiveJobs, embedder: LocalEmbedder | None) -> JobsService: ...
    def ontology(self, embedder: LocalEmbedder | None) -> OntologyService: ...
    def answers(self, embedder: LocalEmbedder | None) -> Answers: ...


class PgStore:
    """The bundled Postgres (§5.3)."""

    def __init__(self, layout: pg.Layout, *, claude_cli: Path | None = None) -> None:
        self.layout = layout
        self._calls: PgCallLog | None = None
        self.claude_cli = claude_cli
        self._shared_gateway: ClaudeGateway | None = None
        self._answers: StoreAnswers | None = None

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
        if self._answers is not None:
            self._answers.close()
        if self._calls is not None:
            self._calls.close()
        dal.close()
        dump = pg.stop(self.layout)
        log.info("store stopped; quit dump %s", dump.name if dump else "not needed")

    def staleness_check(self, new_tables: Callable[[job_store.Job], object]) -> StalenessCheck:
        return StalenessCheck(new_tables=new_tables)

    def _gateway(self) -> ClaudeGateway:
        """The one gateway every Claude call goes through (§6): compile jobs and the onboarding
        test call, so both see the same status."""
        if self._shared_gateway is None:
            self._calls = PgCallLog(pg.url(self.layout))
            self._shared_gateway = ClaudeGateway(
                calls=self._calls,
                sandbox=self.layout.app_data / "claude-sandbox",
                cli_path=self.claude_cli,
            )
        return self._shared_gateway

    def sources(self, jobs: ActiveJobs, embedder: LocalEmbedder | None) -> Sources:
        return StoreSources(jobs=jobs, embedder=embedder)

    def claude(self, config: Config) -> Claude:
        return ClaudeCheck(self._gateway, cli_path=config.claude_cli)

    def jobs(self, active: ActiveJobs, embedder: LocalEmbedder | None) -> JobsService:
        """Compile jobs, with the gateway every Claude call goes through (§6)."""
        return Jobs(gateway=self._gateway(), active=active, embedder=embedder)

    def ontology(self, embedder: LocalEmbedder | None) -> OntologyService:
        """The Terms and Ontology screens' reads and edits; an edit re-embeds with `embedder`."""
        return StoreOntology(embedder)

    def answers(self, embedder: LocalEmbedder | None) -> Answers:
        """Questions, answered through the same gateway, with one hybrid search for the engine's
        lifetime."""
        self._answers = StoreAnswers(gateway=self._gateway(), embedder=embedder)
        return self._answers


def pg_store(config: Config) -> Store:
    if config.pg_bin is None:
        raise RuntimeError("no Postgres binaries: pass --pg-bin or set ONTOLOGICAL_PG_BIN")
    return PgStore(pg.Layout(config.app_data, config.pg_bin), claude_cli=config.claude_cli)


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
    ap.add_argument("--claude-cli", type=Path, default=os.environ.get("ONTOLOGICAL_CLAUDE_CLI"))
    ap.add_argument("--headless", action="store_true", help="start detached, print, exit")
    ap.add_argument("--idle-timeout", type=float, default=IDLE_TIMEOUT_S, metavar="S")
    ap.add_argument("--lease-renew", type=float, default=RENEW_EVERY_S, metavar="S")
    a = ap.parse_args(argv)
    config = Config(
        app_data=(a.app_data or default_app_data()).absolute(),
        pg_bin=Path(a.pg_bin) if a.pg_bin else None,
        model_dir=Path(a.model_dir) if a.model_dir else None,
        claude_cli=Path(a.claude_cli) if a.claude_cli else None,
        idle_timeout_s=a.idle_timeout,
        renew_every_s=a.lease_renew,
    )
    return config, a.headless


def emit(**fields: Any) -> None:
    try:
        print(json.dumps(fields), flush=True)
    except BrokenPipeError:
        # The caller read the first line and has gone: Electron quits and leaves its engine
        # running. Later lines go nowhere, so an error path still stops the store.
        sys.stdout = open(os.devnull, "w")  # noqa: SIM115 - the engine's stdout from now on


def announce(info: discovery.EngineInfo, *, started: bool) -> None:
    emit(pid=info.pid, port=info.port, version=info.version, started=started)


def main(
    argv: Sequence[str] | None = None,
    *,
    store: Callable[[Config], Store] = pg_store,
    relaunch: Sequence[str] = (sys.executable, "-m", "ontological.engine"),
) -> int:
    """`store` makes the store; `relaunch` is the command `--headless` runs, without arguments."""
    close_inherited_fds()
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
    `_Server.handle_exit`, which lands here too.

    The handler only records the signal. Python runs it between two bytecodes of the main
    thread, so logging from it can land in the middle of a write to stderr and raise "reentrant
    call inside BufferedWriter", and the engine never stops. The engine logs it from its own
    loop instead, with `log_once`."""

    def __init__(self) -> None:
        self.signum: int | None = None
        self._logged = False

    @property
    def requested(self) -> bool:
        return self.signum is not None

    def __call__(self, signum: int, frame: FrameType | None) -> None:
        if self.signum is None:
            self.signum = signum

    def log_once(self) -> None:
        if self.signum is not None and not self._logged:
            self._logged = True
            log.info("%s: stopping", signal.Signals(self.signum).name)


class _Server(uvicorn.Server):
    """uvicorn's server, except that a signal doesn't stop it at once: the engine's own loop
    takes engine.lock first (see `_serve`)."""

    def __init__(self, config: uvicorn.Config, stop: _Stop) -> None:
        super().__init__(config)
        self.stop = stop

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        self.stop(sig, frame)


def serve(config: Config, make_store: Callable[[Config], Store]) -> int:
    # Before anything spawns `claude`: the Claude Code session variables an engine started by
    # the MCP shim inherits (§6.2).
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
        if stop.requested:
            stop.log_once()
        else:
            asyncio.run(_serve(config, sock, lock, stop, embedder, new_store))
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
    store: Store,
) -> None:
    jobs = ActiveJobs()
    service = store.jobs(jobs, embedder)
    staleness = store.staleness_check(auto_compile_from(service, asyncio.get_running_loop()))
    lifetime = Lifetime(
        idle_timeout_s=config.idle_timeout_s,
        renew_every_s=config.renew_every_s,
        jobs_running=lambda: jobs.count() + int(staleness.running),
    )
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
        jobs=jobs,
        claude=store.claude(config),
        sources=store.sources(jobs, embedder),
        jobs_service=service,
        ontology=store.ontology(embedder),
        answers=store.answers(embedder),
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
    staleness.start()
    try:
        await service.resume_unfinished()
        tick = min(1.0, config.idle_timeout_s / 4, config.renew_every_s)
        while not task.done():
            if lifetime.should_exit():
                log.info("idle for %gs: stopping", config.idle_timeout_s)
            if lifetime.closing or stop.requested:
                stop.log_once()
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
    finally:
        # Before the store stops: a compile and a refresh write to it.
        await service.stop()
        staleness.cancel()
        await asyncio.to_thread(staleness.join)


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
