# SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/server/model_interchange/service.py
# at 2a24f5d. Dialects come from each database's source (`interchange.dialects`), not from live
# connectors. An import embeds nothing itself: it names the databases it wrote, which the caller
# re-embeds as tracked jobs (ARCHITECTURE.md §9.3). An Ossie import takes the synonyms and
# certification back from the native snapshot the export embedded, which the converter's output
# leaves out, where the text they vouch for is unchanged.

"""Native and Apache Ossie model YAML: export a scope of the catalog, or import a document.

The native document is upstream's, with synonyms and certification (`interchange.schemas`). Ossie
goes through the Apache-2.0 `apache-ossie-nvidia-auto-ontology` converter, pinned to a commit in
pyproject.toml. Its Ossie model carries the whole native document in a custom extension, so a
model exported here comes back with what Ossie has no field for.
"""

# The vendored DAL and the converter are untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

import io
import json
import logging
import zipfile
from collections import Counter
from collections.abc import Mapping
from typing import Any, cast

import yaml
from ossie_nvidia_auto_ontology import (
    AutoOntologyConversionError,
    convert_auto_ontology_to_ossie,
    convert_auto_ontology_to_ossie_ontology,
    convert_ossie_to_auto_ontology,
)
from ossie_nvidia_auto_ontology.native_converter import AUTO_ONTOLOGY_VENDOR_ALIASES
from pydantic import ValidationError

from auto_ontology.dal import model_interchange as dal
from ontological.interchange.dialects import dialect_by_database_name
from ontological.interchange.embed import ImportEmbedBuffer
from ontological.interchange.schemas import (
    AutoOntologyModelDocument,
    ExportRequest,
    ModelFormat,
)

logger = logging.getLogger(__name__)

__all__ = [
    "OSSIE_EXPORT_FILES",
    "AutoOntologyConversionError",
    "detect_model_format",
    "export_model",
    "export_ossie_documents",
    "import_model",
]

_AUTO_ONTOLOGY_ROOT_KEYS = frozenset(AutoOntologyModelDocument.model_fields)
# The files of an Ossie export with its ontology. The mapping references the
# other two by these names, relative to itself.
OSSIE_EXPORT_FILES = ("semantic_model.yaml", "ontology.yaml", "mapping.yaml")


def detect_model_format(payload: Mapping[str, Any]) -> ModelFormat:
    """Tell an Apache Ossie document apart from a native one.

    Only a root holding the native document's own keys is read as native. Anything
    else goes to the Ossie converter, which rejects what it cannot read; read
    as native, it would validate as an empty document, and an import with
    ``replace`` would then wipe the catalog.
    """
    if _AUTO_ONTOLOGY_ROOT_KEYS.intersection(payload):
        return ModelFormat.AUTO_ONTOLOGY
    return ModelFormat.OSSIE


def _table_id_by_column_id(document: AutoOntologyModelDocument) -> dict[str, str]:
    """Map every catalog column id to the id of the table that owns it."""
    return {
        column.id: table.id
        for database in document.data_layer.databases
        for schema in database.schemas
        for table in schema.tables
        for column in table.columns
    }


def _project_terms_onto_one_table(
    document: AutoOntologyModelDocument,
) -> AutoOntologyModelDocument:
    """Narrow every term down to a single represented table.

    An Ossie dataset is backed by exactly one table, while a term may
    represent several. Until the converter can split such a term into one
    dataset per table, the export keeps the table carrying most of the term's
    column attributes and drops the attributes belonging to the others, so
    that no field ends up referencing a column the dataset does not have.
    """
    table_of_column = _table_id_by_column_id(document)
    projected = document.model_copy(deep=True)
    for term in projected.semantic_layer.terms:
        if len(term.represents) <= 1:
            continue
        attributes_per_table = Counter(
            table_of_column.get(attribute.column_id, "") for attribute in term.columns_attributes
        )
        kept_table_id = max(term.represents, key=lambda t: attributes_per_table[t])
        dropped = [t for t in term.represents if t != kept_table_id]
        term.represents = [kept_table_id]
        term.columns_attributes = [
            attribute
            for attribute in term.columns_attributes
            if table_of_column.get(attribute.column_id) == kept_table_id
        ]
        logger.warning(
            "Term %r represents %d tables; exporting only table %s to Ossie and dropping %s",
            term.name,
            len(dropped) + 1,
            kept_table_id,
            ", ".join(dropped),
        )

    kept_attribute_ids = {
        attribute.id
        for term in projected.semantic_layer.terms
        for attribute in term.columns_attributes
    }
    projected.semantic_layer.semantic_fks = [
        semantic_fk
        for semantic_fk in projected.semantic_layer.semantic_fks
        if semantic_fk.column_attribute_id in kept_attribute_ids
    ]
    return projected


def _dump_yaml(document: AutoOntologyModelDocument) -> str:
    return yaml.safe_dump(
        document.model_dump(mode="python"),
        sort_keys=False,
        default_flow_style=False,
    )


def _validate_ossie_metric_names(document: AutoOntologyModelDocument) -> None:
    """Reject custom analyses that Ossie's global metric namespace cannot hold."""
    counts = Counter(analysis.name for analysis in document.semantic_layer.custom_analyses)
    duplicates = [f"{name!r} ({count})" for name, count in counts.items() if name and count > 1]
    if duplicates:
        raise AutoOntologyConversionError(
            "Cannot export custom analyses with duplicate names as Apache Ossie "
            f"metrics: {', '.join(duplicates)}"
        )


def _export_document(request: ExportRequest) -> AutoOntologyModelDocument:
    """Assemble the native model document for the databases *request* scopes."""
    database_ids = request.databases
    dal.validate_database_ids(database_ids)
    rows = dal.fetch_export_rows(database_ids)
    return dal.assemble_export_document(
        rows,
        dialect_by_db_name=dialect_by_database_name(),
        sql_column_resolver=dal.make_cached_sql_column_resolver(),
    )


def _ossie_source(document: AutoOntologyModelDocument) -> str:
    """Return the native YAML the Ossie converter reads for *document*."""
    _validate_ossie_metric_names(document)
    return _dump_yaml(_project_terms_onto_one_table(document))


def export_model(request: ExportRequest) -> str:
    """Export the scoped model document as a YAML string in *request.format*."""
    document = _export_document(request)
    if request.format is ModelFormat.OSSIE:
        return convert_auto_ontology_to_ossie(_ossie_source(document))
    return _dump_yaml(document)


def export_ossie_documents(request: ExportRequest) -> bytes:
    """Export the scoped model as a zip of three Apache Ossie documents.

    The semantic model is the one ``export_model`` returns in the Ossie format.
    The ontology holds the terms as concepts, and the mapping populates it from
    the semantic model, referencing both by their names in the zip.
    """
    source = _ossie_source(_export_document(request))
    semantic_model_file, ontology_file, mapping_file = OSSIE_EXPORT_FILES
    ontology, mapping = convert_auto_ontology_to_ossie_ontology(
        source,
        semantic_model_iri=f"./{semantic_model_file}",
        ontology_iri=f"./{ontology_file}",
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(semantic_model_file, convert_auto_ontology_to_ossie(source))
        archive.writestr(ontology_file, ontology)
        archive.writestr(mapping_file, mapping)
    return buffer.getvalue()


def import_model(
    yaml_text: str,
    *,
    replace: bool = True,
    embed_buffer: ImportEmbedBuffer | None = None,
) -> dict[str, Any]:
    """Validate and apply a YAML model document in either supported format, in one transaction.

    The summary's ``pending_embed`` names the databases the import wrote, which *embed_buffer*
    collects too, for the caller to re-embed."""
    payload = yaml.safe_load(yaml_text)
    if not isinstance(payload, dict):
        raise ValueError("YAML document must deserialize to a mapping")
    source_format = detect_model_format(cast(dict[str, Any], payload))
    if source_format is ModelFormat.OSSIE:
        payload = yaml.safe_load(convert_ossie_to_auto_ontology(yaml_text))
    document = AutoOntologyModelDocument.model_validate(payload)
    if source_format is ModelFormat.OSSIE:
        _restore_from_snapshot(document, _native_snapshot(yaml_text))
    summary = dal.apply_import_model(
        document,
        replace=replace,
        embed_buffer=embed_buffer if embed_buffer is not None else ImportEmbedBuffer(),
    )
    summary["format"] = source_format.value
    return summary


# --- what Ossie has no field for -----------------------------------------------------------------


def _native_snapshot(ossie_yaml: str) -> AutoOntologyModelDocument | None:
    """The native document an Ossie export embedded, which the converter matches its ids back
    against; None when the model has none, as one written elsewhere wouldn't."""
    model = yaml.safe_load(ossie_yaml)
    if not isinstance(model, dict):
        return None
    for extension in cast(dict[str, Any], model).get("custom_extensions") or []:
        if not isinstance(extension, dict):
            continue
        extension = cast(dict[str, Any], extension)
        if extension.get("vendor_name") not in AUTO_ONTOLOGY_VENDOR_ALIASES:
            continue
        try:
            snapshot = json.loads(str(extension.get("data") or "{}")).get("native_document")
            return AutoOntologyModelDocument.model_validate(snapshot) if snapshot else None
        except (json.JSONDecodeError, AttributeError, ValidationError):
            logger.warning("ignoring an unreadable native snapshot in an Ossie model")
            return None
    return None


def _restore_from_snapshot(
    document: AutoOntologyModelDocument, snapshot: AutoOntologyModelDocument | None
) -> None:
    """Put back each entity's synonyms and certification from the snapshot, by id. A flag comes
    back only while what it certifies reads as it did: a description edited in Ossie since the
    export isn't the one a person certified."""
    if snapshot is None:
        return
    tables = {t.id: t for t in _tables(snapshot)}
    columns = {c.id: c for t in _tables(snapshot) for c in t.columns}
    for table in _tables(document):
        if (was := tables.get(table.id)) and was.description == table.description:
            table.description_certified = was.description_certified
        for column in table.columns:
            if (was := columns.get(column.id)) and was.description == column.description:
                column.description_certified = was.description_certified

    terms = {t.id: t for t in snapshot.semantic_layer.terms}
    attributes = {a.id: a for t in snapshot.semantic_layer.terms for a in t.columns_attributes}
    for term in document.semantic_layer.terms:
        if (was := terms.get(term.id)) is None:
            continue
        term.synonyms = list(was.synonyms)
        term.name_certified = was.name_certified and was.name == term.name
        term.description_certified = (
            was.description_certified and was.description == term.description
        )
        for attribute in term.columns_attributes:
            if (a := attributes.get(attribute.id)) is not None:
                attribute.certified = a.certified and (a.name, a.description) == (
                    attribute.name,
                    attribute.description,
                )

    def sql_attributes(d: AutoOntologyModelDocument) -> list[Any]:
        groups = d.semantic_layer.sql_attributes
        return [*groups.manual, *groups.table, *groups.sql, *groups.bridge_table]

    previous = {a.id: a for a in sql_attributes(snapshot)}
    for attribute in sql_attributes(document):
        if (was := previous.get(attribute.id)) is not None:
            attribute.certified = was.certified and (was.name, was.sql) == (
                attribute.name,
                attribute.sql,
            )


def _tables(document: AutoOntologyModelDocument) -> list[Any]:
    return [
        table
        for database in document.data_layer.databases
        for schema in database.schemas
        for table in schema.tables
    ]
