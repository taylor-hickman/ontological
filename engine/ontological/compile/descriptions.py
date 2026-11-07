# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/semantic/deterministic.py at
# 2a24f5d. The prompt is upstream's, sent through ClaudeGateway.structured() on the
# `column_descriptions` route, one batch of 15 columns after another.

"""The `column_descriptions` stage, and the column specs the terms stage assigns
(ARCHITECTURE.md §1.3 stage 4, §6.3).

Every column that isn't a declared or suggested foreign key becomes a ColumnAttribute candidate.
Columns without a description get one from Claude, from their names, types and sample values.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ontological.compile import packed
from ontological.compile.gateway import Structured, ask
from ontological.compile.keys import fk_source_columns
from ontological.compile.models import (
    ColumnAttributeSpec,
    ColumnDescription,
    ColumnDescriptionResult,
    PackedColumnDescriptions,
    TableDescriptionsEntry,
)
from ontological.profile.date_format import is_date_type

logger = logging.getLogger(__name__)

TASK = "column_descriptions"
PROMPT_VERSION = "2a24f5d"
# Wide tables make the model emit one JSON object per column in a single response; batches of 15
# keep each response small (⌈n/15⌉ calls per table, §6.3).
BATCH_SIZE = 15

SYSTEM = """\
You are a data analyst documenting the columns of a relational table for a \
semantic layer. For every column you are given, write ONE concise sentence \
describing what the column represents in business terms.

Rules:
- Use the column name, data type, and sample values as evidence.
- Keep each description to a single, factual sentence — no speculation.
- Return exactly one entry per column provided, using the physical column name."""

_FORMAT_MARKER = "format:"


def to_term_name(table_name: str) -> str:
    """A human-readable fallback Term name from a physical table name."""
    separated = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", table_name.strip())
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", separated)
    parts = re.split(r"[^A-Za-z0-9]+", separated)
    return " ".join(part if part.isupper() else part.capitalize() for part in parts if part)


def _column_to_attr_name(column_name: str) -> str:
    parts = re.split(r"[_\s]+", column_name.strip())
    if not parts:
        return column_name
    return parts[0].lower() + "".join(p.capitalize() for p in parts[1:] if p)


def _get_column_samples(col: dict[str, Any], profiling: dict[str, dict[str, Any]]) -> list[str]:
    """Profiled sample values for a column. Date and time columns have none: a concrete date
    adds no business meaning."""
    if is_date_type(col.get("data_type")):
        return []
    samples: list[Any] = (profiling.get(col.get("name", "")) or {}).get("sample_values") or []
    return [str(s) for s in samples]


def _add_samples_suffix(text: str, samples: list[str]) -> str:
    if not samples:
        return text
    return f"{text} — samples: {', '.join(samples)}"


def _date_format_clause(col: dict[str, Any], profiling: dict[str, dict[str, Any]]) -> str | None:
    """How stored values are written, when a single notation fits them all."""
    profile = profiling.get(col.get("name", "")) or {}
    notation = profile.get("format") or col.get("format")
    if not notation:
        return None
    return f"{_FORMAT_MARKER} {notation}"


def _enrich_description(
    col: dict[str, Any], profiling: dict[str, dict[str, Any]], text: str | None
) -> str | None:
    """Attach the date notation, once."""
    clause = _date_format_clause(col, profiling)
    text = text or ""
    if clause and _FORMAT_MARKER not in text:
        text = f"{text} — {clause}" if text else clause
    return text or None


def build_prompt(columns: list[dict[str, Any]], profiling: dict[str, dict[str, Any]]) -> str:
    lines = [
        _add_samples_suffix(
            f"  - {col.get('name', '')} ({col.get('data_type') or 'unknown'})",
            _get_column_samples(col, profiling),
        )
        for col in columns
    ]
    return "Columns:\n" + "\n".join(lines)


async def describe_batch(
    gateway: Structured, columns: list[dict[str, Any]], profiling: dict[str, dict[str, Any]]
) -> dict[str, str]:
    """`{column_name: description}` for one batch; empty when the answer is invalid. Raises
    `ClaudeUnavailable` when Claude doesn't answer."""
    result = await ask(
        gateway,
        TASK,
        SYSTEM,
        build_prompt(columns, profiling),
        ColumnDescriptionResult,
        PROMPT_VERSION,
    )
    if result is None:
        logger.warning("column description batch failed; proceeding without them")
        return {}
    return matched(columns, result.descriptions)


def matched(columns: list[dict[str, Any]], answered: list[ColumnDescription]) -> dict[str, str]:
    # The prompt annotates each column as "<name> (<dtype>)", and the model sometimes echoes
    # that back as the column name. Match exactly first, then the longest requested name the
    # returned one starts with.
    requested = [c.get("name", "") for c in columns if c.get("name")]
    requested_set = set(requested)
    out: dict[str, str] = {}
    for d in answered:
        raw = d.column_name or ""
        desc = (d.description or "").strip()
        if not desc:
            continue
        if raw in requested_set:
            out.setdefault(raw, desc)
            continue
        prefixes = [n for n in requested if n and raw.startswith(n)]
        if prefixes:
            out.setdefault(max(prefixes, key=len), desc)
    return out


async def describe_columns(
    gateway: Structured, columns: list[dict[str, Any]], profiling: dict[str, dict[str, Any]]
) -> dict[str, str]:
    """Descriptions for `columns`, in batches of `BATCH_SIZE`, one call at a time. A batch with
    an invalid answer is skipped."""
    descriptions: dict[str, str] = {}
    for i in range(0, len(columns), BATCH_SIZE):
        descriptions.update(await describe_batch(gateway, columns[i : i + BATCH_SIZE], profiling))
    return descriptions


@dataclass(frozen=True)
class DescriptionsRequest:
    """One table of a pack: its name, its columns and fks from the store, the FK columns the
    keys stage suggested, and its profile."""

    name: str
    columns: list[dict[str, Any]]
    fks: list[dict[str, Any]]
    suggested_fk_columns: set[str] = field(default_factory=set[str])
    profiling: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])


def _batches(sizes: Sequence[int]) -> list[list[int]]:
    """The tables of a pack in batches of at most `BATCH_SIZE` columns, in order, a table never
    split; a table wider than that is a batch of its own."""
    batches: list[list[int]] = []
    filled = BATCH_SIZE
    for i, size in enumerate(sizes):
        if not batches or filled + size > BATCH_SIZE:
            batches.append([])
            filled = 0
        batches[-1].append(i)
        filled += size
    return batches


async def describe_pack(
    gateway: Structured, tables: Sequence[tuple[str, list[dict[str, Any]], dict[str, Any]]]
) -> list[dict[str, str]]:
    """`describe_columns` for each `(name, columns, profiling)` of a pack. The tables go in
    batches of at most `BATCH_SIZE` columns, a table never split; a batch of several tables is
    one packed call (`compile.packed`), and a table the answer leaves out is asked alone."""
    out: list[dict[str, str]] = [{} for _ in tables]
    asked = [i for i, (_, columns, _) in enumerate(tables) if columns]
    for batch in _batches([len(tables[i][1]) for i in asked]):
        members = [asked[b] for b in batch]
        found: dict[str, TableDescriptionsEntry] = {}
        if len(members) > 1:
            sections = [
                (name, f"Table: {name}\n{build_prompt(columns, profiling)}")
                for name, columns, profiling in (tables[i] for i in members)
            ]
            found = await packed.ask_pack(
                gateway, TASK, SYSTEM, sections, PackedColumnDescriptions, lambda r: r.tables
            )
        for i in members:
            name, columns, profiling = tables[i]
            entry = found.get(name)
            if entry is None:
                out[i] = await describe_columns(gateway, columns, profiling)
            else:
                out[i] = matched(columns, entry.descriptions)
    return out


def split_columns(
    columns: list[dict[str, Any]], fks: list[dict[str, Any]], suggested_fk_columns: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The non-FK columns: those with a description, and those without."""
    fk_cols = fk_source_columns(fks) | suggested_fk_columns
    candidates = [c for c in columns if (name := c.get("name", "")) and name not in fk_cols]
    described = [c for c in candidates if c.get("description")]
    undescribed = [c for c in candidates if not c.get("description")]
    return described, undescribed


def column_specs(
    described: list[dict[str, Any]],
    undescribed: list[dict[str, Any]],
    generated: dict[str, str],
    profiling: dict[str, dict[str, Any]],
) -> list[ColumnAttributeSpec]:
    def spec(col: dict[str, Any], description: str | None) -> ColumnAttributeSpec:
        name = col["name"]
        return ColumnAttributeSpec(
            source_column=name,
            name=_column_to_attr_name(name),
            datatype=str(col.get("data_type") or ""),
            description=description,
        )

    # An existing description never saw the samples, so they are appended to it; a generated
    # one was written from them.
    specs = [
        spec(
            col,
            _enrich_description(
                col,
                profiling,
                _add_samples_suffix(col["description"], _get_column_samples(col, profiling)),
            ),
        )
        for col in described
    ]
    specs += [
        spec(col, _enrich_description(col, profiling, generated.get(col["name"])))
        for col in undescribed
    ]
    return specs


async def column_attribute_specs(
    gateway: Structured,
    columns: list[dict[str, Any]],
    fks: list[dict[str, Any]],
    suggested_fk_columns: set[str] | None = None,
    profiling: dict[str, dict[str, Any]] | None = None,
) -> list[ColumnAttributeSpec]:
    """The non-FK columns, one ColumnAttribute candidate each, described."""
    profiling = profiling or {}
    described, undescribed = split_columns(columns, fks, suggested_fk_columns or set())
    generated = await describe_columns(gateway, undescribed, profiling)
    return column_specs(described, undescribed, generated, profiling)


async def column_attribute_specs_pack(
    gateway: Structured, requests: Sequence[DescriptionsRequest]
) -> list[list[ColumnAttributeSpec]]:
    """`column_attribute_specs` for each table of a pack, its undescribed columns described in
    packed calls (`describe_pack`)."""
    split = [split_columns(r.columns, r.fks, r.suggested_fk_columns) for r in requests]
    generated = await describe_pack(
        gateway,
        [
            (r.name, undescribed, r.profiling)
            for r, (_, undescribed) in zip(requests, split, strict=True)
        ],
    )
    return [
        column_specs(described, undescribed, made, r.profiling)
        for r, (described, undescribed), made in zip(requests, split, generated, strict=True)
    ]
