# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: the split stages' models from upstream's
# auto_ontology/semantic/models.py at 2a24f5d. The structured-output models (`FkAndPkResult`,
# `ColumnDescriptionResult`, `RawTableTermsResult` and what they hold) are unchanged, so the JSON
# schemas the gateway sends are upstream's; the sanitized results are dataclasses here.

"""What the split stages ask Claude for, and what they hand on (ARCHITECTURE.md §6.3)."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

# --- table_keys --------------------------------------------------------------------------------


class PotentialFkSuggestion(BaseModel):
    """One column the LLM suspects is a foreign key."""

    model_config = ConfigDict(extra="forbid")

    column_name: str = Field(
        ...,
        description="Physical column name that likely references another table.",
    )
    rationale: str = Field(
        default="",
        description="Brief reason this column looks like a foreign key.",
    )


class FkAndPkResult(BaseModel):
    """LLM output: FK/PK suggestions plus table-grain classification."""

    model_config = ConfigDict(extra="forbid")

    fk_suggestions: list[PotentialFkSuggestion] = Field(
        default_factory=list[PotentialFkSuggestion],
        description="Suspected FK columns; empty when none apply.",
    )
    pk_column_names: list[str] = Field(
        default_factory=list[str],
        description=(
            "Column names that appear to be the table's own primary key "
            "even if not declared as such. Usually empty or one entry."
        ),
    )
    is_junction_table: bool = Field(
        default=False,
        description=(
            "True only when each row represents an association between at least two entity roles."
        ),
    )
    junction_table_rationale: str = Field(
        default="",
        description="Brief reason for the junction-table decision.",
    )


@dataclass(frozen=True)
class TableKeys:
    """Sanitized FK suggestions and the table-grain decision (upstream's `PotentialFkResult`)."""

    suggestions: list[PotentialFkSuggestion] = field(default_factory=list[PotentialFkSuggestion])
    is_junction_table: bool = False
    junction_table_rationale: str = ""


# --- column_descriptions -----------------------------------------------------------------------


class ColumnDescription(BaseModel):
    """LLM-generated business description for a single column."""

    model_config = ConfigDict(extra="forbid")

    column_name: str = Field(
        ...,
        description="Physical column name — must match a provided column.",
    )
    description: str = Field(
        default="",
        description="One concise sentence describing what the column represents.",
    )


class ColumnDescriptionResult(BaseModel):
    """LLM output: business descriptions for a table's columns."""

    model_config = ConfigDict(extra="forbid")

    descriptions: list[ColumnDescription] = Field(
        default_factory=list[ColumnDescription],
        description="One entry per column provided in the prompt.",
    )


@dataclass
class ColumnAttributeSpec:
    """Column candidate for Term assignment; `display_name` is set by the terms stage."""

    source_column: str
    name: str
    display_name: str = ""
    datatype: str = ""
    description: str | None = None


# --- table_terms -------------------------------------------------------------------------------


class TermColumnRef(BaseModel):
    """LLM output: column assignment with a user-friendly display label."""

    model_config = ConfigDict(extra="forbid")

    source_column: str = Field(
        ...,
        description="Physical column name — must match a candidate column.",
    )
    display_name: str = Field(
        ...,
        description=(
            "User-friendly ColumnAttribute label with spaces between words "
            "(e.g. Order Date, Total Amount)."
        ),
    )


class RawTermProposal(BaseModel):
    """LLM output: one business Term with column assignments only."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ...,
        description=(
            "User-friendly business Term name with spaces between words (e.g. Purchase Order)."
        ),
    )
    description: str = Field(default="", description="Short business definition.")
    attributes: list[TermColumnRef] = Field(
        default_factory=list[TermColumnRef],
        description=("Candidate columns assigned to this Term with user-friendly display names."),
    )


class RawTableTermsResult(BaseModel):
    """LLM output: Terms and column assignments for a single physical table."""

    model_config = ConfigDict(extra="forbid")

    terms: list[RawTermProposal] = Field(
        ...,
        description=(
            "Usually one Term. Propose multiple only when columns clearly belong "
            "to distinct business concepts."
        ),
    )


@dataclass(frozen=True)
class TermAttributeAssignment:
    """Resolved column assignment on a Term."""

    source_column: str
    display_name: str


@dataclass
class TermProposal:
    """Sanitized Term with resolved ColumnAttribute assignments."""

    name: str
    description: str = ""
    synonyms: list[str] = field(default_factory=list[str])
    attributes: list[TermAttributeAssignment] = field(default_factory=list[TermAttributeAssignment])


# --- table_synonyms ----------------------------------------------------------------------------


class SynonymExtraction(BaseModel):
    """LLM output: alternate names written in a table description (upstream's
    `_SynonymExtractionModel`)."""

    model_config = ConfigDict(extra="ignore")

    synonyms: list[str] = Field(
        default_factory=list[str],
        description=(
            "Abbreviations, acronyms, or alternate names explicitly written in the "
            "description. Empty list if none are stated."
        ),
    )


# --- packed calls ------------------------------------------------------------------------------
# A pack's call returns one entry per table: upstream's answer for that table, named by its
# heading in the prompt (ARCHITECTURE.md §6.4).


def _table_field() -> str:
    return Field(
        ..., description="The table this entry answers for, exactly as written after 'Table:'."
    )


class TableKeysEntry(FkAndPkResult):
    table: str = _table_field()


class PackedTableKeys(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tables: list[TableKeysEntry] = Field(..., description="One entry per table in the request.")


class TableDescriptionsEntry(ColumnDescriptionResult):
    table: str = _table_field()


class PackedColumnDescriptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tables: list[TableDescriptionsEntry] = Field(
        ..., description="One entry per table in the request."
    )


class TableTermsEntry(RawTableTermsResult):
    table: str = _table_field()


class PackedTableTerms(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tables: list[TableTermsEntry] = Field(..., description="One entry per table in the request.")


class TableSynonymsEntry(SynonymExtraction):
    table: str = _table_field()


class PackedSynonyms(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tables: list[TableSynonymsEntry] = Field(..., description="One entry per table in the request.")
