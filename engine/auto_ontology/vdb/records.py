# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: adds embed_rows_into_vdb, upstream's utils.embedding helper on a
# LangChain Embeddings in place of the NIM client.

"""Turn embedded rows into the records :meth:`PostgresVDB.run` writes.

An embedded row is the dict the row builders produce (``text``, ``path``,
``page_number``, ``metadata``) after the embed step has set
``metadata["embedding"]``. The record keeps every top-level metadata key --
retrieval reads hits' ``metadata["id"]``, ``label``, ``name`` and so on from
there -- and adds ``content``, ``content_metadata`` and ``source_metadata``.

The shape matches what ``nemo_retriever``'s ``to_client_vdb_records`` wrote
for these rows, so rows written before and after it was replaced are
indistinguishable in the store.
"""

from __future__ import annotations

import logging
import time
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    import pandas as pd
    from langchain_core.embeddings import Embeddings

    from auto_ontology.vdb.postgres import PostgresVDB

__all__ = ["embed_rows_into_vdb", "to_vdb_records", "write_embedded_rows"]

logger = logging.getLogger(__name__)


def _text(row: dict[str, Any], metadata: dict[str, Any]) -> str:
    """The first non-blank text field, unmodified."""
    for value in (row.get("text"), row.get("content"), metadata.get("content")):
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _page_number(row: dict[str, Any], content_metadata: dict[str, Any]) -> int | None:
    pages = []
    for value in (content_metadata.get("page_number"), row.get("page_number")):
        try:
            pages.append(int(value))
        except (TypeError, ValueError):
            continue
    return max(pages) if pages else None


def _record(row: dict[str, Any]) -> dict[str, Any] | None:
    metadata = dict(row.get("metadata") or {})
    # An empty list is what a failed request leaves behind, so it is as absent
    # as a missing key: writing it would store a row no search can reach.
    if not metadata.get("embedding"):
        return None
    text = _text(row, metadata)
    if not text:
        return None

    content_metadata = dict(metadata.get("content_metadata") or {})
    page = _page_number(row, content_metadata)
    if page is not None:
        content_metadata["page_number"] = page

    source_metadata = dict(metadata.get("source_metadata") or {})
    source_path = next(
        (
            value.strip()
            for value in (metadata.get("source_path"), row.get("path"))
            if isinstance(value, str) and value.strip()
        ),
        "",
    )
    if source_path:
        source_metadata.setdefault("source_id", source_path)
        source_metadata.setdefault("source_name", PurePosixPath(source_path).name)

    metadata["content"] = text
    metadata["content_metadata"] = content_metadata
    metadata["source_metadata"] = source_metadata
    return {
        "document_type": str(row.get("document_type") or "text"),
        "metadata": metadata,
    }


def to_vdb_records(
    rows: "Iterable[dict[str, Any]] | pd.DataFrame",
) -> list[list[dict[str, Any]]]:
    """Records for the *rows* that carry an embedding and some text.

    Returns ``[records]``, or ``[]`` when none qualify -- never ``[[]]``, which
    is truthy and would read as "something to write".
    """
    if hasattr(rows, "to_dict"):
        rows = rows.to_dict(orient="records")
    records = [
        record
        for row in rows or []
        if isinstance(row, dict) and (record := _record(row)) is not None
    ]
    return [records] if records else []


def write_embedded_rows(
    vdb: "PostgresVDB", rows: "Iterable[dict[str, Any]] | pd.DataFrame"
) -> int:
    """Write the embedded *rows* to *vdb*; returns the number of rows written."""
    records = to_vdb_records(rows)
    return vdb.run(records) if records else 0


def embed_rows_into_vdb(
    rows: list[dict[str, Any]],
    embeddings: "Embeddings",
    vdb: "PostgresVDB",
    *,
    what: str,
) -> int:
    """Embed already-built rows as passages, all or nothing, and append them to *vdb*.

    For the small, interactive writes -- one analysis, one attribute -- where a
    failure should reach the caller. Raises ``RuntimeError`` when the embedder
    fails or returns no embeddings; *what* names the rows in that message.
    Returns the number of rows written.
    """
    if not rows:
        return 0
    before = time.time()
    try:
        vectors = embeddings.embed_documents([row.get("text") or "" for row in rows])
    except Exception as exc:
        raise RuntimeError(
            f"Embedding {len(rows)} {what} row(s) via {type(embeddings).__name__} "
            f"failed: {exc}"
        ) from exc
    embedded = [
        {**row, "metadata": {**(row.get("metadata") or {}), "embedding": vector}}
        for row, vector in zip(rows, vectors)
        if vector
    ]
    if not embedded:
        raise RuntimeError(
            f"Embedding step produced 0/{len(rows)} {what} rows with embeddings."
        )
    written = write_embedded_rows(vdb, embedded)
    logger.info(
        "Embedded and appended %d/%d %s row(s) via %s in %.2fs.",
        len(embedded),
        len(rows),
        what,
        type(vdb).__name__,
        time.time() - before,
    )
    return written
