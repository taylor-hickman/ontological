"""The edits Ontological makes to the vendored upstream package `auto_ontology`
(auto_ontology/UPSTREAM.md, ARCHITECTURE.md §11)."""

from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import keyring
import pytest
from fakes import HashEmbeddings, MemoryKeyring
from keyring.backend import KeyringBackend
from sqlalchemy.engine import make_url

import auto_ontology
import auto_ontology.dal
from auto_ontology.dal import custom_analyses, schema_version, sql_attributes, users
from auto_ontology.dal import schema as s
from auto_ontology.dal.owners import CATALOG_EDIT, EditSubject
from auto_ontology.dal.session import sqlalchemy_url
from auto_ontology.infra import keychain as keychain_mod
from auto_ontology.infra.postgres import get_postgres_connection_string
from auto_ontology.server.sql_errors import SqlParseError
from auto_ontology.vdb import get_semantic_vdb, get_vdb
from auto_ontology.vdb.records import embed_rows_into_vdb
from ontological.store import dal, local_owner, pg

UPSTREAM_HEAD = "99d4d343dd0b"  # the head of upstream's chain at 2a24f5d


@pytest.fixture
def socket_env(monkeypatch: pytest.MonkeyPatch) -> str:
    sock = "/Users/someone/Library/Application Support/Ontological/pgsocket"
    monkeypatch.setenv("POSTGRES_HOST", sock)
    monkeypatch.setenv("POSTGRES_USER", "ontological")
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.delenv("POSTGRES_PORT", raising=False)
    monkeypatch.setenv("POSTGRES_DATABASE", "ontological")
    return sock


def test_a_socket_directory_makes_a_socket_url_with_no_password(socket_env: str) -> None:
    url = get_postgres_connection_string()
    assert url == (
        "postgresql://ontological@/ontological"
        "?host=/Users/someone/Library/Application%20Support/Ontological/pgsocket"
    )
    parsed = make_url(sqlalchemy_url())
    assert parsed.drivername == "postgresql+psycopg"
    assert parsed.host is None and parsed.password is None
    assert parsed.query["host"] == socket_env


def test_a_socket_url_keeps_a_port_other_than_the_default(
    socket_env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POSTGRES_PORT", "5433")
    assert make_url(get_postgres_connection_string()).query["port"] == "5433"


def test_a_host_name_still_makes_upstreams_tcp_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_HOST", "127.0.0.1")
    monkeypatch.setenv("POSTGRES_PORT", "55432")
    monkeypatch.setenv("POSTGRES_USER", "auto_ontology")
    monkeypatch.setenv("POSTGRES_PASSWORD", "pw")
    monkeypatch.setenv("POSTGRES_DATABASE", "auto_ontology")
    assert get_postgres_connection_string() == (
        "postgresql://auto_ontology:pw@127.0.0.1:55432/auto_ontology"
    )


def test_schema_version_finds_the_vendored_chain() -> None:
    schema_version.head_revision.cache_clear()
    assert schema_version.head_revision() == UPSTREAM_HEAD


# --- M1.1: the DAL, catalog/store/ and vdb/ (UPSTREAM.md) ---------------------------------------

#: Top-level packages S6 found the full set pulling in, which the edits cut (BUILD_PLAN.md, S6).
CUT = (
    "hvac", "dotenv", "openai", "langchain_openai", "langchain_nvidia_ai_endpoints", "sqlglot",
    "duckdb", "snowflake", "databricks", "trino", "pyhive", "heavydb", "pymysql", "fastapi",
)  # fmt: skip


def vendored_modules() -> list[str]:
    root = Path(auto_ontology.__file__).parent
    return sorted(
        m.name
        for m in pkgutil.walk_packages([str(root)], "auto_ontology.")
        if not m.name.startswith("auto_ontology.alembic")
    )


def test_every_vendored_module_imports_without_the_cut_packages() -> None:
    script = (
        "import importlib, json, sys\n"
        f"for m in {vendored_modules()!r}: importlib.import_module(m)\n"
        "print(json.dumps(sorted({m.split('.')[0] for m in sys.modules})))\n"
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    loaded = set(json.loads(out.stdout))
    assert loaded.isdisjoint(CUT), sorted(loaded & set(CUT))


def test_model_interchange_is_ported_not_vendored() -> None:
    """M6.4 ports dal/model_interchange.py with server/model_interchange/* (§11): it keeps
    upstream's functions and their signatures (dal_surface.json), reads and writes the engine's
    document, and says it was modified. The import above, of every vendored module, shows it
    loads without sqlglot."""
    from auto_ontology.dal import model_interchange
    from ontological.interchange import schemas

    assert "auto_ontology.dal.model_interchange" in vendored_modules()
    assert model_interchange.AutoOntologyModelDocument is schemas.AutoOntologyModelDocument
    header = Path(model_interchange.__file__).read_text().splitlines()[:4]
    assert header[3].startswith("# Modified by Ontological"), header


def test_every_public_dal_read_scoped_by_zone_defaults_to_the_whole_catalog() -> None:
    scoped: list[str] = []
    for info in pkgutil.iter_modules(auto_ontology.dal.__path__):
        if info.name == "users":  # the scoping helpers themselves; see the next test
            continue
        module = importlib.import_module(f"auto_ontology.dal.{info.name}")
        for name, fn in inspect.getmembers(module, inspect.isfunction):
            if name.startswith("_") or fn.__module__ != module.__name__:
                continue
            param = inspect.signature(fn).parameters.get("zone_ids")
            if param is None:
                continue
            scoped.append(f"{info.name}.{name}")
            assert param.default is None, f"{info.name}.{name}"
    assert len(scoped) > 30  # the DAL's reads, not a vacuous pass


def test_no_zones_means_no_filter() -> None:
    assert users.resolve_accessible_catalog_ids(None) is None
    assert users.resolve_table_filter(None, s.catalog_table.c.id) is None


def test_the_dal_takes_sql_parse_error_from_the_one_class_module() -> None:
    assert sql_attributes.SqlAttributeSqlError is SqlParseError
    assert issubclass(custom_analyses.CustomAnalysisSqlError, SqlParseError)


@pytest.fixture
def keychain() -> Iterator[MemoryKeyring]:
    before = keyring.get_keyring()
    ring = MemoryKeyring()
    keyring.set_keyring(ring)
    try:
        yield ring
    finally:
        keyring.set_keyring(before)


def test_a_connection_in_the_keychain_is_read_as_json(keychain: MemoryKeyring) -> None:
    conn = {"type": "postgres", "database_name": "pagila", "password": "pw"}
    keychain.items[(keychain_mod.KEYRING_SERVICE, "pagila")] = json.dumps(conn)
    assert keychain_mod.read_secret("pagila") == conn
    assert keychain_mod.read_secret("chinook") == ""


class BrokenKeyring(KeyringBackend):
    priority = 1  # pyright: ignore[reportAssignmentType]

    def get_password(self, service: str, username: str) -> str | None:
        raise RuntimeError("locked")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise RuntimeError("locked")


def test_an_unreadable_keychain_entry_counts_as_no_secret(keychain: MemoryKeyring) -> None:
    keychain.items[(keychain_mod.KEYRING_SERVICE, "pagila")] = "{not json"
    assert keychain_mod.read_secret("pagila") == ""
    keyring.set_keyring(BrokenKeyring())
    assert keychain_mod.read_secret("pagila") == ""


class RecordingVDB:
    def __init__(self) -> None:
        self.records: list[Any] = []

    def run(self, records: list[Any]) -> int:
        self.records = records
        return len(records[0])


def test_embedding_rows_writes_each_with_its_vector() -> None:
    vdb = RecordingVDB()
    emb = HashEmbeddings()
    rows: list[dict[str, Any]] = [
        {"text": "Revenue", "path": "auto_ontology:a1", "metadata": {"id": "a1"}},
        {"text": "Churn", "path": "auto_ontology:a2", "metadata": {"id": "a2"}},
    ]
    assert embed_rows_into_vdb(rows, emb, vdb, what="PqlAnalysis") == 2  # type: ignore[arg-type]
    [records] = vdb.records
    assert [r["metadata"]["embedding"] for r in records] == emb.embed_documents(
        ["Revenue", "Churn"]
    )


class FailingEmbeddings(HashEmbeddings):
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise OSError("model missing")


class EmptyEmbeddings(HashEmbeddings):
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[] for _ in texts]


@pytest.mark.parametrize("emb", [FailingEmbeddings(), EmptyEmbeddings()])
def test_embedding_rows_fails_loudly_and_writes_nothing(emb: HashEmbeddings) -> None:
    vdb = RecordingVDB()
    rows: list[dict[str, Any]] = [{"text": "Revenue", "metadata": {"id": "a1"}}]
    with pytest.raises(RuntimeError, match="PqlAnalysis"):
        embed_rows_into_vdb(rows, emb, vdb, what="PqlAnalysis")  # type: ignore[arg-type]
    assert vdb.records == []


def test_the_vdb_factories_take_the_embedder_and_its_dimension(socket_env: str) -> None:
    emb = HashEmbeddings(dim=768)
    for vdb in (get_vdb(embeddings=emb, vector_size=768), get_semantic_vdb(embeddings=emb,
                vector_size=768)):  # fmt: skip
        assert vdb.embeddings is emb
        assert vdb.vector_size == 768
        assert vdb.schema_name == "vdb"
        assert make_url(vdb.connection_string).query["host"] == socket_env


def test_the_owner_stub_always_allows() -> None:
    for owner in (None, local_owner.LOCAL_OWNER_ID, "someone-else"):
        subject = EditSubject(permission=CATALOG_EDIT, owner_id=owner)
        assert local_owner.can_edit_entity(subject=subject)
        assert local_owner.enforce_edit(subject) is None
        assert local_owner.require_assignable(owner, CATALOG_EDIT) == owner


def test_connect_names_the_store_socket_with_no_password() -> None:
    layout = pg.Layout(Path("/Users/someone/Library/Application Support/Ontological"), Path("/b"))
    env = {"POSTGRES_PASSWORD": "pw", "POSTGRES_HOST": "db.example"}
    dal.connect(layout, env)
    assert env == {
        "POSTGRES_HOST": str(layout.pgsocket),
        "POSTGRES_PORT": "5432",
        "POSTGRES_USER": pg.USER,
        "POSTGRES_DATABASE": pg.DATABASE,
    }
