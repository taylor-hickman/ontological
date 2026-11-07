# SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/server/model_interchange/schemas.py
# at 2a24f5d. The document is upstream's, with fields for what upstream's export drops: a Term's
# synonyms, and the certification flags of Terms, column attributes, SQL attributes, tables and
# columns (ARCHITECTURE.md §9.3). Each defaults to what the store holds when nothing set it, so a
# document upstream wrote reads as it did there.

"""Pydantic models for the model interchange YAML document."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class ModelFormat(StrEnum):
    """Serialisation dialect of an exchanged model document."""

    AUTO_ONTOLOGY = "auto_ontology"
    OSSIE = "ossie"


class ExportRequest(BaseModel):
    """Export scope: empty ``databases`` exports every catalog database."""

    databases: list[str] = Field(default_factory=list[str])
    format: ModelFormat = ModelFormat.AUTO_ONTOLOGY
    include_ontology: bool = Field(
        default=False,
        description=(
            "Apache Ossie only: also export the terms as an Ossie ontology, with a "
            "mapping onto the semantic model, as a zip of the three documents."
        ),
    )

    @model_validator(mode="after")
    def _ontology_is_an_ossie_document(self) -> ExportRequest:
        if self.include_ontology and self.format is not ModelFormat.OSSIE:
            raise ValueError("include_ontology applies only to the ossie format")
        return self


class ModelColumn(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    description_certified: bool = False
    type: str = ""
    # Typed rather than `list[str]`: an integer column's samples round-trip as
    # numbers, so a re-import can still tell 1 from '1' (see
    # ``auto_ontology.utils.sample_values.dump_sample_values``).
    sample_values: list[Any] = Field(default_factory=list[Any])
    is_nullable: bool = True
    is_unique: bool = False


class ModelTable(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    description_certified: bool = False
    pk: list[str] = Field(default_factory=list[str])
    type: str = ""
    columns: list[ModelColumn] = Field(default_factory=list[ModelColumn])


class ModelSchema(BaseModel):
    id: str
    name: str = ""
    database_name: str = ""
    tables: list[ModelTable] = Field(default_factory=list[ModelTable])


class ModelDatabase(BaseModel):
    id: str
    dialect: str = ""
    schemas: list[ModelSchema] = Field(default_factory=list[ModelSchema])


class ModelForeignKey(BaseModel):
    source_column_id: str
    target_column_id: str


class ModelJoin(BaseModel):
    source_table_id: str
    target_table_id: str
    join_columns: list[dict[str, str]] = Field(default_factory=list[dict[str, str]])


class ModelDataLayer(BaseModel):
    databases: list[ModelDatabase] = Field(default_factory=list[ModelDatabase])
    foreign_keys: list[ModelForeignKey] = Field(default_factory=list[ModelForeignKey])
    joins: list[ModelJoin] = Field(default_factory=list[ModelJoin])


class ModelColumnAttribute(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    certified: bool = False
    column_id: str = ""


class ModelTerm(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    synonyms: list[str] = Field(default_factory=list[str])
    name_certified: bool = False
    description_certified: bool = False
    represents: list[str] = Field(default_factory=list[str])
    columns_attributes: list[ModelColumnAttribute] = Field(
        default_factory=list[ModelColumnAttribute]
    )


class ModelSemanticFk(BaseModel):
    column_attribute_id: str
    column_id: str
    use_in_join_paths: bool = True


class ModelSqlAttribute(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    certified: bool = False
    sql: str = ""
    sql_column_is: list[str] = Field(default_factory=list[str])
    term_id: str = ""


class ModelSqlAttributesBySource(BaseModel):
    manual: list[ModelSqlAttribute] = Field(default_factory=list[ModelSqlAttribute])
    table: list[ModelSqlAttribute] = Field(default_factory=list[ModelSqlAttribute])
    sql: list[ModelSqlAttribute] = Field(default_factory=list[ModelSqlAttribute])
    bridge_table: list[ModelSqlAttribute] = Field(default_factory=list[ModelSqlAttribute])


class ModelCustomAnalysis(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    sql: str = ""
    sql_column_is: list[str] = Field(default_factory=list[str])


class ModelSemanticLayer(BaseModel):
    terms: list[ModelTerm] = Field(default_factory=list[ModelTerm])
    semantic_fks: list[ModelSemanticFk] = Field(default_factory=list[ModelSemanticFk])
    sql_attributes: ModelSqlAttributesBySource = Field(
        default_factory=ModelSqlAttributesBySource,
    )
    custom_analyses: list[ModelCustomAnalysis] = Field(default_factory=list[ModelCustomAnalysis])


class AutoOntologyModelDocument(BaseModel):
    """Top-level model document exchanged as YAML."""

    data_layer: ModelDataLayer = Field(default_factory=ModelDataLayer)
    semantic_layer: ModelSemanticLayer = Field(default_factory=ModelSemanticLayer)
    zones: list[dict[str, str]] = Field(default_factory=list[dict[str, str]])
