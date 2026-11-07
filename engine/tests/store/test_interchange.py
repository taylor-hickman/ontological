"""Model import and export on Chinook (BUILD_PLAN.md M6.4, ARCHITECTURE.md §9.3): the native
document carries synonyms and certification, an Ossie model brings them back through the native
snapshot it embeds, an import commits in one transaction, and its re-embed runs as a tracked
`reembed` job. Upstream's own tests of the DAL port run in the golden DAL suite. Compiled with
test_compile.py's scripted gateway and embedded with hash vectors, as test_curation.py is."""

# The vendored code is untyped where these tests read its results.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false

from __future__ import annotations

import asyncio
import io
import time
import zipfile
from typing import Any

import pytest
import yaml
from fakes import ScriptedGateway
from test_compile import claude
from test_curation import Curated, Hashed, attr_id, curated, jobs, one, term_id

from ontological.curate.edits import edit_attribute, edit_term
from ontological.embed import reembed
from ontological.embed.semantic import Synced
from ontological.interchange import service
from ontological.interchange.embed import ImportEmbedBuffer
from ontological.interchange.schemas import AutoOntologyModelDocument, ExportRequest, ModelFormat
from ontological.interchange.store import StoreInterchange
from ontological.jobs.active import ActiveJobs
from ontological.jobs.service import Jobs
from ontological.store import pg

__all__ = ["curated"]  # the fixture, from test_curation

TIMEOUT = 30.0


def curate(c: Curated) -> None:
    """Album with synonyms and both certifications; one certified attribute."""
    lay = c.store
    edit_term(
        term_id(lay, "Album"),
        synonyms=["Record", "LP"],
        name_certified=True,
        description_certified=True,
        vdb=c.vdb,
    )
    edit_attribute(attr_id(lay, "Invoice", "BillingCountry"), certified=True, vdb=c.vdb)


def export(fmt: ModelFormat = ModelFormat.AUTO_ONTOLOGY) -> str:
    return service.export_model(ExportRequest(format=fmt))


def native(text: str) -> AutoOntologyModelDocument:
    return AutoOntologyModelDocument.model_validate(yaml.safe_load(text))


def empty_the_store(lay: pg.Layout) -> None:
    """Every catalog and semantic row and vector gone, as in a store that never held Chinook."""
    one(
        lay,
        "DELETE FROM term; DELETE FROM column_attribute; DELETE FROM sql_attribute; "
        "DELETE FROM custom_analysis; DELETE FROM sql_query; DELETE FROM catalog_database; "
        "DELETE FROM vdb.semantic_layer; DELETE FROM vdb.data_objects_layer",
    )


def album(doc: AutoOntologyModelDocument) -> Any:
    [term] = [t for t in doc.semantic_layer.terms if t.name == "Album"]
    return term


def certified_row(lay: pg.Layout) -> tuple[str, str, str, str]:
    """Album's synonyms and flags, and the certified attribute's flag, as the store holds them."""
    return (
        one(lay, "SELECT array_to_string(synonyms, ',') FROM term WHERE name = 'Album'"),
        one(lay, "SELECT name_certified FROM term WHERE name = 'Album'"),
        one(lay, "SELECT description_certified FROM term WHERE name = 'Album'"),
        one(
            lay,
            "SELECT bool_or(a.certified) FROM column_attribute a WHERE a.term_name = 'Invoice' "
            "AND a.source_column = 'BillingCountry'",
        ),
    )


# --- export -------------------------------------------------------------------------------------


def test_the_native_export_carries_synonyms_and_certification(curated: Curated) -> None:
    curate(curated)
    doc = native(export())
    term = album(doc)
    assert term.synonyms == ["Record", "LP"]
    assert term.name_certified and term.description_certified
    certified = [
        a.name for t in doc.semantic_layer.terms for a in t.columns_attributes if a.certified
    ]
    assert len(certified) == 1
    assert {db.dialect for db in doc.data_layer.databases} == {"sqlite"}


def test_an_ossie_export_is_ossie_and_embeds_the_native_document(curated: Curated) -> None:
    curate(curated)
    model = yaml.safe_load(export(ModelFormat.OSSIE))
    assert {"version", "name", "datasets"} <= set(model)
    assert "album" in {d["name"] for d in model["datasets"]}
    snapshot = service._native_snapshot(export(ModelFormat.OSSIE))  # pyright: ignore[reportPrivateUsage]
    assert snapshot is not None and album(snapshot).synonyms == ["Record", "LP"]


def test_an_ossie_export_with_its_ontology_is_a_zip_of_three_documents(curated: Curated) -> None:
    content = service.export_ossie_documents(
        ExportRequest(format=ModelFormat.OSSIE, include_ontology=True)
    )
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert archive.namelist() == list(service.OSSIE_EXPORT_FILES)


# --- import -------------------------------------------------------------------------------------


def test_importing_the_export_onto_its_own_store_changes_nothing(curated: Curated) -> None:
    # Compile's descriptions aren't in the data layer's texts until a re-embed rebuilds them
    reembed.reembed_database("chinook", curated.embedder)
    buffer = ImportEmbedBuffer()
    summary = service.import_model(export(), replace=True, embed_buffer=buffer)
    assert sum(summary["created"].values()) == 0, summary["created"]
    assert buffer.database_names == ["chinook"]
    assert summary["pending_embed"] == {"databases": ["chinook"]}
    done = reembed.reembed_database("chinook", curated.embedder)
    assert done.semantic == Synced(embedded=0, deleted=0, current=curated.total)
    assert done.data_layer == 0


def test_a_native_import_into_an_empty_store_restores_synonyms_and_certification(
    curated: Curated,
) -> None:
    lay = curated.store
    curate(curated)
    before = certified_row(lay)
    text = export()
    empty_the_store(lay)

    summary = service.import_model(text, replace=True)
    assert summary["format"] == "auto_ontology"
    assert summary["created"]["terms"] > 0 and summary["created"]["sql_attributes"] > 0
    assert certified_row(lay) == before == ("Record,LP", "t", "t", "t")
    # The SQL attributes' statements and their table links came back with them
    assert one(lay, "SELECT count(*) FROM sql_attribute__sql") == one(
        lay, "SELECT count(*) FROM sql_attribute"
    )


def test_the_re_embed_after_an_import_embeds_both_layers(curated: Curated) -> None:
    lay = curated.store
    curate(curated)
    data_rows = int(one(lay, "SELECT count(*) FROM vdb.data_objects_layer"))
    text = export()
    empty_the_store(lay)
    service.import_model(text, replace=True)

    done = reembed.reembed_database("chinook", curated.embedder)
    assert done.semantic.embedded == curated.total
    assert done.data_layer == data_rows
    assert jobs(lay)[-1] == "reembed chinook done"
    album_id = term_id(lay, "Album")
    assert (
        one(lay, f"SELECT langchain_metadata ->> 'certified' FROM vdb.semantic_layer "
            f"WHERE langchain_metadata ->> 'id' = '{album_id}'")
        == "true"
    )  # fmt: skip
    again = reembed.reembed_database("chinook", curated.embedder)
    assert (again.semantic.embedded, again.data_layer) == (0, 0)


def test_a_re_embed_rebuilds_a_data_layer_whose_texts_changed(curated: Curated) -> None:
    lay = curated.store
    one(lay, "UPDATE catalog_table SET description = 'Every track sold.' WHERE name = 'Track'")
    done = reembed.reembed_database("chinook", curated.embedder)
    assert done.data_layer > 0
    assert reembed.reembed_database("chinook", curated.embedder).data_layer == 0


def test_an_ossie_import_brings_back_what_ossie_has_no_field_for(curated: Curated) -> None:
    lay = curated.store
    curate(curated)
    text = export(ModelFormat.OSSIE)
    empty_the_store(lay)

    summary = service.import_model(text, replace=True)
    assert summary["format"] == "ossie"
    assert certified_row(lay) == ("Record,LP", "t", "t", "t")


def test_a_description_edited_in_ossie_loses_its_certification(curated: Curated) -> None:
    lay = curated.store
    curate(curated)
    model = yaml.safe_load(export(ModelFormat.OSSIE))
    [dataset] = [d for d in model["datasets"] if d["name"].lower() == "album"]
    dataset["description"] = "Rewritten in another tool."
    empty_the_store(lay)

    service.import_model(yaml.safe_dump(model, sort_keys=False), replace=True)
    assert one(lay, "SELECT description FROM term WHERE name = 'Album'") == (
        "Rewritten in another tool."
    )
    assert one(lay, "SELECT name_certified FROM term WHERE name = 'Album'") == "t"
    assert one(lay, "SELECT description_certified FROM term WHERE name = 'Album'") == "f"
    assert one(lay, "SELECT array_to_string(synonyms, ',') FROM term WHERE name = 'Album'") == (
        "Record,LP"
    )


def test_a_failed_import_writes_nothing_and_starts_no_re_embed(curated: Curated) -> None:
    lay = curated.store
    doc = native(export())
    doc.data_layer.foreign_keys[0].target_column_id = "no-such-column"
    empty_the_store(lay)
    buffer = ImportEmbedBuffer()
    with pytest.raises(Exception, match="foreign-key"):
        service.import_model(yaml.safe_dump(doc.model_dump()), embed_buffer=buffer)
    assert one(lay, "SELECT count(*) FROM catalog_table") == "0"
    assert buffer.database_names == []


# --- the engine's import: commit, then a tracked re-embed job ---------------------------------


def wait_for_job(lay: pg.Layout, line: str) -> None:
    deadline = time.monotonic() + TIMEOUT
    while line not in jobs(lay):
        assert time.monotonic() < deadline, jobs(lay)
        time.sleep(0.1)


def test_an_engine_import_re_embeds_as_a_job_and_resume_retries_a_failed_one(
    curated: Curated,
) -> None:
    lay = curated.store
    text = export()
    empty_the_store(lay)
    broken = Hashed()
    broken.fail = True

    async def run() -> None:
        active = ActiveJobs()
        engine_jobs = Jobs(gateway=ScriptedGateway(claude), active=active, embedder=broken)
        imported = await StoreInterchange(engine_jobs).import_model(text, replace=True)
        assert imported.reembedding == ["chinook"]
        await asyncio.to_thread(wait_for_job, lay, "reembed chinook stopped")
        [stopped] = [j for j in await engine_jobs.summaries() if j.kind == "reembed"]

        broken.fail = False
        await engine_jobs.resume(stopped.id)
        await asyncio.to_thread(wait_for_job, lay, "reembed chinook done")
        assert active.count() == 0
        await engine_jobs.stop()

    asyncio.run(run())
    assert jobs(lay).count("reembed chinook done") == 1
    assert int(one(lay, "SELECT count(*) FROM vdb.semantic_layer")) == curated.total
