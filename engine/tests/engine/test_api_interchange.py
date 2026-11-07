"""The model import and export routes (BUILD_PLAN.md M6.4): what they send the service, and how
they answer its refusals. The service itself runs against the store in
tests/store/test_interchange.py."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from auto_ontology.dal.model_interchange import ModelImportValidationError, UnknownDatabaseIdsError
from auto_ontology.server.sql_errors import SqlParseError
from ontological.api.app import create_app
from ontological.engine.lifetime import Lifetime
from ontological.interchange.schemas import ExportRequest
from ontological.interchange.service import AutoOntologyConversionError
from ontological.interchange.store import Exported, Imported

TOKEN = "t" * 43


class FakeInterchange:
    def __init__(self) -> None:
        self.exports: list[ExportRequest] = []
        self.imports: list[tuple[str, bool]] = []
        self.refuse: Exception | None = None

    async def export(self, request: ExportRequest) -> Exported:
        if self.refuse:
            raise self.refuse
        self.exports.append(request)
        return Exported(b"data_layer: {}\n", "application/x-yaml", "ontological-model.yaml")

    async def import_model(self, yaml_text: str, *, replace: bool) -> Imported:
        if self.refuse:
            raise self.refuse
        self.imports.append((yaml_text, replace))
        return Imported(summary={"created": {"terms": 1}}, reembedding=["chinook"])


@pytest.fixture
def fake() -> FakeInterchange:
    return FakeInterchange()


@pytest.fixture
def client(fake: FakeInterchange) -> Iterator[TestClient]:
    lifetime = Lifetime(idle_timeout_s=600, renew_every_s=30)
    app = create_app(token=TOKEN, lifetime=lifetime, pid=1, version="9.9.9", interchange=fake)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        yield c


def test_export_answers_an_attachment(client: TestClient, fake: FakeInterchange) -> None:
    r = client.post("/model/export", json={"databases": ["db-1"], "format": "ossie"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-yaml")
    assert r.headers["content-disposition"] == 'attachment; filename="ontological-model.yaml"'
    assert [(e.databases, e.format.value) for e in fake.exports] == [(["db-1"], "ossie")]


def test_an_ontology_is_only_an_ossie_export(client: TestClient) -> None:
    r = client.post("/model/export", json={"include_ontology": True})
    assert r.status_code == 422


def test_import_takes_the_body_and_replace(client: TestClient, fake: FakeInterchange) -> None:
    r = client.post("/model/import?replace=false", content="data_layer: {}\n")
    assert r.status_code == 200
    assert r.json()["reembedding"] == ["chinook"]
    assert fake.imports == [("data_layer: {}\n", False)]
    client.post("/model/import", content="x: 1\n")
    assert fake.imports[-1][1] is True


class _Doc(BaseModel):
    n: int


def _validation_error() -> Exception:
    try:
        _Doc.model_validate({"n": "x"})
    except Exception as e:
        return e
    raise AssertionError


@pytest.mark.parametrize(
    ("refusal", "status"),
    [
        (ModelImportValidationError("Cannot resolve foreign-key source column"), 422),
        (UnknownDatabaseIdsError(["nope"]), 422),
        (AutoOntologyConversionError("no datasets"), 422),
        (SqlParseError("SQL parse error"), 422),
        (ValueError("YAML document must deserialize to a mapping"), 422),
        (_validation_error(), 422),
        (IntegrityError("INSERT", {}, Exception("uq_term_name_source")), 409),
    ],
)
def test_import_refusals(
    client: TestClient, fake: FakeInterchange, refusal: Exception, status: int
) -> None:
    fake.refuse = refusal
    assert client.post("/model/import", content="x: 1\n").status_code == status


def test_an_empty_or_non_utf8_body_is_refused(client: TestClient) -> None:
    assert client.post("/model/import", content=b"").status_code == 422
    assert client.post("/model/import", content=b"\xff\xfe").status_code == 422


def test_an_unknown_database_is_refused_on_export(
    client: TestClient, fake: FakeInterchange
) -> None:
    fake.refuse = UnknownDatabaseIdsError(["nope"])
    r = client.post("/model/export", json={"databases": ["nope"]})
    assert (r.status_code, r.json()["detail"]) == (422, "Unknown database id(s): nope")


def test_an_engine_with_no_store_answers_503() -> None:
    lifetime = Lifetime(idle_timeout_s=600, renew_every_s=30)
    app = create_app(token=TOKEN, lifetime=lifetime, pid=1, version="9.9.9")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        assert c.post("/model/export", json={}).status_code == 503
        assert c.post("/model/import", content="x: 1\n").status_code == 503
