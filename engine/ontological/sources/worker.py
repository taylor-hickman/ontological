"""The source executor's child process: one read-only source connection (ARCHITECTURE.md §5.6).

The engine side is `executor.py`. Messages cross a `multiprocessing` pipe. The child sends
`("ready", None)` or `("failed", message)` once, then the engine sends `(op_id, op, args)`. The
child sends `(op_id, "started", None)` as it takes the operation, then answers
`(op_id, "ok", payload)`, `(op_id, "error", message)` or `(op_id, "interrupted", None)`.
`("cancel", op_id)` asks it to `interrupt()` that operation. Everything sent is plain Python data;
the engine builds its own types from it.

The main thread runs operations one at a time, so the reader thread is free to take a cancel
while a query runs. When the pipe closes, the engine has gone, and the child exits at once,
whatever it is running.
"""

from __future__ import annotations

import os
import queue
import signal
import threading
from multiprocessing.connection import Connection
from typing import Any

import duckdb

from ontological.sources.local import DuckDBSession, FileSession
from ontological.sources.scanners import MySQLSession, PostgresSession
from ontological.sources.session import Session
from ontological.sources.spec import (
    DuckDBSource,
    FileSource,
    MySQLSource,
    PostgresSource,
    Source,
    SqliteSource,
)
from ontological.sources.sqlite import SqliteSession


def open_session(source: Source, password: str | None) -> Session:
    match source:
        case SqliteSource():
            return SqliteSession(source)
        case DuckDBSource():
            return DuckDBSession(source)
        case FileSource():
            return FileSession(source)
        case PostgresSource():
            return PostgresSession(source, password)
        case MySQLSource():
            return MySQLSession(source, password)


def serve(conn: Connection, source: Source, password: str | None) -> None:
    """The child's entry point. `password` came over the spawn pipe from the keychain."""
    # Ctrl-C in the engine's terminal reaches its whole process group. The engine decides when
    # an executor stops, so the child leaves SIGINT to it.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        session = open_session(source, password)
    except Exception as e:
        conn.send(("failed", f"{type(e).__name__}: {e}"))
        return
    conn.send(("ready", None))

    requests: queue.Queue[tuple[int, str, tuple[Any, ...]]] = queue.Queue()

    def read() -> None:
        while True:
            try:
                msg = conn.recv()
            except (EOFError, OSError):
                os._exit(0)  # the engine has gone; there is nothing to save
            if msg[0] == "cancel":
                session.interrupt(msg[1])
            else:
                requests.put(msg)

    threading.Thread(target=read, daemon=True).start()
    while True:
        op_id, op, args = requests.get()
        session.begin(op_id)
        conn.send((op_id, "started", None))
        try:
            reply = (op_id, "ok", session.run(op, args))
        except Exception as e:  # a query error, a refusal or a bug: the child carries on
            # A scanner's query stopped by the server fails with the server's error.
            if isinstance(e, duckdb.InterruptException) or session.cancelled(op_id):
                reply = (op_id, "interrupted", None)
            else:
                reply = (op_id, "error", str(e))
        finally:
            session.end()
        conn.send(reply)
