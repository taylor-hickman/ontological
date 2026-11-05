"""The edits Ontological makes to the vendored upstream package `auto_ontology`
(auto_ontology/UPSTREAM.md, ARCHITECTURE.md §11)."""

from __future__ import annotations

import pytest
from sqlalchemy.engine import make_url

from auto_ontology.dal import schema_version
from auto_ontology.dal.session import sqlalchemy_url
from auto_ontology.infra.postgres import get_postgres_connection_string

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
