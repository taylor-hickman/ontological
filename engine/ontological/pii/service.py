# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/pii_detection/service.py at
# 2a24f5d (`_apply_batch`, `detect_and_tag_pii`, `propagate_pii_to_attributes`). Upstream
# classifies the columns its ingest returned; here the database's unprocessed columns are read
# from the store, after compile has described them. Each call's columns are tagged and marked
# as soon as it answers, as upstream's batches are, so a stop keeps them.

"""PII tags for a database's columns, and for the attributes built from them (ARCHITECTURE.md
§1.2 step 4).

The rules decide what they can (`pii.rules`), and Claude classifies the rest, 40 columns to a
call (`pii.detector`). A decision of PII with confidence of at least 0.9 attaches the shared
`PII` tag. Every column that got a decision, PII or not, is marked `pii_processed` and is never
classified again, so a tag a person removes stays removed; a column whose answer was invalid
stays unprocessed for the next run. Then the tag is copied to the column and SQL attributes
built from tagged columns (the vendored DAL's `tag_attributes_of_tagged_columns`), whoever
tagged the column.
"""

# The vendored DAL is untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select

from auto_ontology.dal import schema as s
from auto_ontology.dal.pii import mark_columns_pii_processed, tag_attributes_of_tagged_columns
from auto_ontology.dal.session import store, write_transaction
from auto_ontology.dal.tags import (
    PII_TAG_NAME,
    TARGET_COLUMN,
    attach_tag,
    fetch_tags_map,
    get_or_create_tag,
    get_tag_by_name,
)
from ontological.compile.gateway import Structured
from ontological.pii.detector import BATCH, UNCONFIGURED, classify_batch, rules_first
from ontological.pii.models import ColumnInput, PiiDecision, PiiStatus

logger = logging.getLogger(__name__)

AUTO_TAG_THRESHOLD = 0.9


@dataclass(frozen=True)
class PiiTagging:
    """Upstream's `PiiTaggingResult`, and the calls made."""

    scanned: int = 0
    processed: int = 0
    rules_decided: int = 0
    llm_decided: int = 0
    review: int = 0
    tagged: int = 0
    already_tagged: int = 0
    calls: int = 0

    def __add__(self, other: PiiTagging) -> PiiTagging:
        return PiiTagging(
            *(getattr(self, f) + getattr(other, f) for f in self.__dataclass_fields__)
        )


@dataclass(frozen=True)
class PiiPropagation:
    column_attributes: int = 0
    sql_attributes: int = 0


Record = tuple[str, ColumnInput]  # a catalog column's id, and what the classifier sees


def unprocessed_columns(database_name: str) -> list[Record]:
    """The database's columns no run has classified, in table and column order."""
    c, t, sc, d = s.catalog_column, s.catalog_table, s.catalog_schema, s.catalog_database
    rows = store().query_read(
        select(c.c.id, c.c.name, c.c.description, t.c.name.label("table_name"))
        .select_from(
            c.join(t, t.c.id == c.c.table_id)
            .join(sc, sc.c.id == t.c.schema_id)
            .join(d, d.c.id == sc.c.database_id)
        )
        .where(d.c.name == database_name, c.c.pii_processed.is_(False))
        .order_by(sc.c.name, t.c.name, c.c.ordinal_position, c.c.name)
    )
    out: list[Record] = []
    for r in rows:
        name = (r["name"] or "").strip()
        if not name:
            continue
        out.append(
            (
                str(r["id"]),
                ColumnInput(
                    column_name=name,
                    table_name=(r["table_name"] or "").strip() or None,
                    description=(r["description"] or "").strip() or None,
                ),
            )
        )
    return out


def apply(
    records: Sequence[Record], decisions: dict[ColumnInput, PiiDecision], threshold: float
) -> PiiTagging:
    """Upstream's `_apply_batch` for decisions already made: tag the confident PII columns and
    mark every decided column processed, in one transaction."""
    decided = [(cid, decisions[col]) for cid, col in records]
    processed_ids = [cid for cid, d in decided if d.source != "fallback"]
    candidates = [
        cid for cid, d in decided if d.source != "fallback" and d.should_auto_tag(threshold)
    ]
    tagged = already = 0
    with write_transaction():
        if candidates:
            tag_id = str(get_or_create_tag(name=PII_TAG_NAME)["id"])
            existing = fetch_tags_map(TARGET_COLUMN, candidates)
            for cid in candidates:
                if any(tag["id"] == tag_id for tag in existing.get(cid, [])):
                    already += 1
                    continue
                if (
                    attach_tag(tag_id=tag_id, kind=TARGET_COLUMN, item_id=cid, tagged_by=None)
                    is None
                ):
                    raise RuntimeError(f"Could not attach PII tag to catalog column {cid!r}")
                tagged += 1
        processed = mark_columns_pii_processed(processed_ids)
    return PiiTagging(
        scanned=len(records),
        processed=processed,
        rules_decided=sum(d.source == "rules" for _, d in decided),
        llm_decided=sum(d.source == "llm" for _, d in decided),
        review=sum(d.status is PiiStatus.REVIEW for _, d in decided),
        tagged=tagged,
        already_tagged=already,
    )


async def detect_and_tag_pii(
    database_name: str,
    gateway: Structured | None,
    *,
    threshold: float = AUTO_TAG_THRESHOLD,
) -> PiiTagging:
    """Classify the database's unprocessed columns and tag the PII ones: the rules' decisions
    first, then Claude's, a call's worth at a time. With no `gateway`, only the rules decide.
    Raises `ClaudeUnavailable` when Claude doesn't answer, keeping what earlier calls decided."""
    records = await asyncio.to_thread(unprocessed_columns, database_name)
    if not records:
        return PiiTagging()
    decided, uncertain = rules_first([col for _, col in records])
    total = await asyncio.to_thread(
        apply, [r for r in records if r[1] in decided], decided, threshold
    )
    left = [r for r in records if r[1] not in decided]
    if gateway is None:
        total += await asyncio.to_thread(
            apply, left, dict.fromkeys(uncertain, UNCONFIGURED), threshold
        )
    else:
        for i in range(0, len(uncertain), BATCH):
            batch = uncertain[i : i + BATCH]
            decisions = dict(zip(batch, await classify_batch(gateway, batch), strict=True))
            part = await asyncio.to_thread(
                apply, [r for r in left if r[1] in decisions], decisions, threshold
            )
            total += PiiTagging(calls=1) + part
    logger.info(
        "PII of %s: %d scanned, %d processed, %d by rules, %d by Claude in %d call(s), "
        "%d review, %d tagged, %d already tagged",
        database_name,
        total.scanned,
        total.processed,
        total.rules_decided,
        total.llm_decided,
        total.calls,
        total.review,
        total.tagged,
        total.already_tagged,
    )
    return total


def propagate_pii_to_attributes() -> PiiPropagation:
    """Upstream's: tag the column and SQL attributes built from PII-tagged columns, once each.
    Nothing to do when no column was ever tagged."""
    tag = get_tag_by_name(PII_TAG_NAME)
    if tag is None:
        return PiiPropagation()
    column_attributes, sql_attributes = tag_attributes_of_tagged_columns(str(tag["id"]))
    if column_attributes or sql_attributes:
        logger.info(
            "PII propagation: %d column attribute(s) and %d SQL attribute(s) tagged",
            column_attributes,
            sql_attributes,
        )
    return PiiPropagation(column_attributes, sql_attributes)
