"""fetch.py against a local HTTP server that answers range requests, as GitHub, Hugging Face
and BIRD's bucket do."""

from __future__ import annotations

import hashlib
import io
import os
import re
import threading
import zipfile
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ontological_eval import fetch

DB = b"SQLite format 3\x00" + b"rows " * 50_000
DESCRIPTION = os.urandom(1 << 20)  # incompressible, so fetching it would show in the byte count


class Server:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.served = 0
        self.requests: list[str] = []
        files, server = self.files, self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                server.requests.append(self.path)
                body = files.get(self.path)
                if body is None:
                    self.send_error(404)
                    return
                status, first, last = 200, 0, len(body) - 1
                if m := re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers.get("Range", "")):
                    status, first, last = 206, int(m[1]), min(int(m[2]), len(body) - 1)
                chunk = body[first : last + 1]
                self.send_response(status)
                if status == 206:
                    self.send_header("Content-Range", f"bytes {first}-{last}/{len(body)}")
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                self.wfile.write(chunk)
                server.served += len(chunk)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture
def server() -> Iterator[Server]:
    s = Server()
    yield s
    s.httpd.shutdown()


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def minidev_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("MINIDEV/dev_databases/fin/fin.sqlite", DB, zipfile.ZIP_DEFLATED)
        z.writestr("MINIDEV/dev_databases/fin/database_description/loan.csv", DESCRIPTION)
        z.writestr("MINIDEV/raw.sqlite", b"stored, not deflated", zipfile.ZIP_STORED)
    return buf.getvalue()


def sources(server: Server, **override: str) -> fetch.Sources:
    csv, ddl, zipped = b"id,name\n1,a\n", b"CREATE SCHEMA s;\n", minidev_zip()
    server.files.update({"/wwi/data/s.t.csv": csv, "/wwi/ddl/01.sql": ddl, "/minidev.zip": zipped})
    return {
        "wwi": {
            "base": f"{server.url}/wwi/",
            "dest": "wwi",
            "files": {
                "data/s.t.csv": override.get("csv", sha(csv)),
                "ddl/01.sql": sha(ddl),
            },
        },
        "bird-databases": {
            "zip": f"{server.url}/minidev.zip",
            "dest": "bird",
            "files": {
                "MINIDEV/dev_databases/fin/fin.sqlite": override.get("db", sha(DB)),
                "MINIDEV/raw.sqlite": sha(b"stored, not deflated"),
            },
        },
    }


def run(home: Path, src: fetch.Sources, *groups: str) -> list[Path]:
    return fetch.fetch(home, groups or list(src), sources=src, log=lambda _: None)


def test_fetches_files_and_only_the_listed_zip_members(server: Server, tmp_path: Path) -> None:
    written = run(tmp_path, sources(server))

    assert sorted(p.relative_to(tmp_path).as_posix() for p in written) == [
        "bird/fin.sqlite",
        "bird/raw.sqlite",
        "wwi/data/s.t.csv",
        "wwi/ddl/01.sql",
    ]
    assert (tmp_path / "bird/fin.sqlite").read_bytes() == DB
    assert (tmp_path / "wwi/data/s.t.csv").read_bytes() == b"id,name\n1,a\n"
    # The central directory and the two members, never the description CSV.
    assert server.served < len(DESCRIPTION)
    assert not list(tmp_path.rglob("*.part"))


def test_files_already_current_are_not_fetched_again(server: Server, tmp_path: Path) -> None:
    src = sources(server)
    run(tmp_path, src)
    server.requests.clear()

    assert run(tmp_path, src) == []
    assert server.requests == []


def test_only_the_named_groups_are_fetched(server: Server, tmp_path: Path) -> None:
    run(tmp_path, sources(server), "wwi")

    assert (tmp_path / "wwi/ddl/01.sql").exists()
    assert not (tmp_path / "bird").exists()
    assert all(not r.startswith("/minidev") for r in server.requests)


def test_a_wrong_file_never_replaces_the_one_on_disk(server: Server, tmp_path: Path) -> None:
    old = tmp_path / "wwi/data/s.t.csv"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"what was there\n")

    with pytest.raises(fetch.FetchError, match=r"sources\.json pins"):
        run(tmp_path, sources(server, csv="0" * 64), "wwi")

    assert old.read_bytes() == b"what was there\n"
    assert not list(tmp_path.rglob("*.part"))


def test_a_wrong_zip_member_is_refused(server: Server, tmp_path: Path) -> None:
    with pytest.raises(fetch.FetchError, match=r"fin\.sqlite"):
        run(tmp_path, sources(server, db="0" * 64), "bird-databases")

    assert not (tmp_path / "bird/fin.sqlite").exists()


def test_a_missing_zip_member_is_named(server: Server, tmp_path: Path) -> None:
    src = sources(server)
    src["bird-databases"]["files"]["MINIDEV/dev_databases/gone/gone.sqlite"] = "0" * 64

    with pytest.raises(fetch.FetchError, match="no member MINIDEV/dev_databases/gone"):
        run(tmp_path, src, "bird-databases")


def test_the_pinned_sources_skip_what_is_not_ingested() -> None:
    src = fetch.load_sources()
    files = {name: h for g in src.values() for name, h in g["files"].items()}

    assert not [n for n in files if "database_description" in n or "custom_analyses" in n]
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in files.values())
    assert {Path(n).stem for n in src["bird-databases"]["files"]} == {
        "financial",
        "california_schools",
        "formula_1",
        "debit_card_specializing",
    }
