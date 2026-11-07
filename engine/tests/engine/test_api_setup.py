"""The routes onboarding and the catalog screen read (api/setup.py), over the stub engine's fakes,
and how picked files become a source (catalog/sources.py)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_sources import FakeSources
from fastapi.testclient import TestClient

from ontological.api.app import create_app
from ontological.catalog.sources import AddServer, SourceError, files_source, server_source
from ontological.engine.lifetime import Lifetime
from ontological.llm.install import ClaudeInstall, PingResult
from ontological.sources.keychain import KeychainRef
from ontological.sources.spec import DuckDBSource, FileSource, MySQLSource, SqliteSource

TOKEN = "t" * 43


class StubClaude:
    async def detect(self) -> ClaudeInstall:
        return ClaudeInstall(
            state="needs_login",
            cli_path=Path("/usr/local/bin/claude"),
            version="2.1.319",
            min_version="2.1.318",
            logged_in=False,
            ignored_variables=["ANTHROPIC_API_KEY"],
        )

    async def test_call(self) -> PingResult:
        return PingResult(ok=False, seconds=None, state="needs_login", error="Not logged in")


def client_for(**kwargs: object) -> TestClient:
    lifetime = Lifetime(idle_timeout_s=600, renew_every_s=30)
    app = create_app(token=TOKEN, lifetime=lifetime, pid=1, version="9.9.9", **kwargs)  # type: ignore[arg-type]
    c = TestClient(app, base_url="http://127.0.0.1")
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    return c


@pytest.fixture
def client() -> Iterator[TestClient]:
    with client_for(claude=StubClaude(), sources=FakeSources()) as c:
        yield c


@pytest.fixture
def shop(tmp_path: Path) -> Path:
    path = tmp_path / "shop.sqlite"
    with sqlite3.connect(path) as con:
        con.executescript(
            "CREATE TABLE customer (id INTEGER PRIMARY KEY, email TEXT NOT NULL);"
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER "
            "REFERENCES customer(id), total REAL);"
            "INSERT INTO customer VALUES (1, 'a@x.com'), (2, 'b@x.com');"
            "INSERT INTO orders VALUES (10, 1, 9.5);"
        )
    return path


def test_claude_reports_the_install_login_and_ignored_variables(client: TestClient) -> None:
    got = client.get("/claude")
    assert got.status_code == 200
    assert got.json() == {
        "state": "needs_login",
        "cli_path": "/usr/local/bin/claude",
        "version": "2.1.319",
        "min_version": "2.1.318",
        "logged_in": False,
        "ignored_variables": ["ANTHROPIC_API_KEY"],
    }
    assert client.post("/claude/test").json() == {
        "ok": False,
        "seconds": None,
        "state": "needs_login",
        "error": "Not logged in",
    }


def test_an_engine_without_a_store_or_gateway_answers_503() -> None:
    with client_for() as c:
        assert c.get("/claude").status_code == 503
        assert c.get("/sources").status_code == 503
        assert c.post("/sources", json={"kind": "file", "paths": ["/x.db"]}).status_code == 503
        assert c.get("/sources/x/terms").status_code == 503
        assert c.get("/terms/x").status_code == 503
        assert c.patch("/terms/x", json={"name": "Y"}).status_code == 503
        assert c.post("/edits/1/undo").status_code == 503


def test_add_a_source_then_browse_and_remove_it(client: TestClient, shop: Path) -> None:
    assert client.get("/sources").json() == []
    added = client.post("/sources", json={"kind": "file", "paths": [str(shop)]})
    assert added.status_code == 201, added.text
    source = added.json()["source"]
    assert (source["name"], source["kind"], source["tables"], source["columns"]) == (
        "shop",
        "sqlite",
        2,
        5,
    )
    assert [s["id"] for s in client.get("/sources").json()] == [source["id"]]

    catalog = client.get(f"/sources/{source['id']}/catalog").json()
    tables = {t["name"]: t for t in catalog["schemas"][0]["tables"]}
    assert sorted(tables) == ["customer", "orders"]

    detail = client.get(f"/tables/{tables['orders']['id']}").json()
    columns = {c["name"]: c for c in detail["columns"]}
    assert columns["id"]["pk"] is True
    assert columns["customer_id"]["references"] == ["main.customer.id"]

    assert client.post("/sources", json={"kind": "file", "paths": [str(shop)]}).status_code == 409
    assert client.delete(f"/sources/{source['id']}").status_code == 204
    assert client.get("/sources").json() == []
    assert client.delete(f"/sources/{source['id']}").status_code == 404
    assert client.get(f"/tables/{tables['orders']['id']}").status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "file", "paths": []},
        {"kind": "file"},
        {"kind": "postgres", "host": "", "database": "d", "user": "u"},
        {"kind": "oracle", "paths": ["/x"]},
    ],
)
def test_a_malformed_request_is_422(client: TestClient, body: dict[str, object]) -> None:
    assert client.post("/sources", json=body).status_code == 422


def test_a_file_that_isn_t_a_source_is_400(client: TestClient, tmp_path: Path) -> None:
    notes = tmp_path / "notes.txt"
    notes.write_text("hello")
    got = client.post("/sources", json={"kind": "file", "paths": [str(notes)]})
    assert got.status_code == 400
    assert "format" in got.json()["detail"]


# --- picked files -------------------------------------------------------------------------------


def test_database_files_are_told_by_suffix_or_header(tmp_path: Path, shop: Path) -> None:
    assert files_source([str(shop)]) == SqliteSource(shop)
    renamed = tmp_path / "shop.data"
    renamed.write_bytes(shop.read_bytes())
    assert files_source([str(renamed)]) == SqliteSource(renamed)
    duck = tmp_path / "warehouse.duckdb"
    duck.write_bytes(b"")
    assert files_source([str(duck)]) == DuckDBSource(duck)
    sniffed = tmp_path / "warehouse.bin"
    sniffed.write_bytes(b"\0" * 8 + b"DUCK" + b"\0" * 4)
    assert files_source([str(sniffed)]) == DuckDBSource(sniffed)


def test_data_files_make_one_file_source(tmp_path: Path) -> None:
    (tmp_path / "sales.parquet").write_bytes(b"")
    (tmp_path / "regions.csv").write_text("a\n")
    got = files_source([str(tmp_path / "sales.parquet"), str(tmp_path / "regions.csv")])
    assert isinstance(got, FileSource)
    assert [t.name for t in got.tables] == ["sales", "regions"]


def test_a_database_file_goes_on_its_own(tmp_path: Path, shop: Path) -> None:
    (tmp_path / "regions.csv").write_text("a\n")
    with pytest.raises(SourceError, match="on its own"):
        files_source([str(shop), str(tmp_path / "regions.csv")])


@pytest.mark.parametrize("path", ["relative.sqlite", "/no/such/file.sqlite"])
def test_a_path_must_be_an_existing_absolute_file(path: str) -> None:
    with pytest.raises(SourceError):
        files_source([path])


def test_a_server_password_goes_to_the_keychain_by_the_server_s_url() -> None:
    filed: list[tuple[str, str]] = []

    def save(account: str, password: str) -> KeychainRef:
        filed.append((account, password))
        return KeychainRef(account)

    request = AddServer(kind="mysql", host="db.local", database="shop", user="ro", password="pw")
    got = server_source(request, save=save)
    assert got == MySQLSource(
        host="db.local",
        database="shop",
        user="ro",
        port=3306,
        password=KeychainRef("mysql://ro@db.local:3306/shop"),
    )
    assert filed == [("mysql://ro@db.local:3306/shop", "pw")]
    no_password = AddServer(kind="postgres", host="h", database="d", user="u")
    assert server_source(no_password, save=save).password is None
    assert len(filed) == 1
