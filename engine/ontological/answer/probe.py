# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's
# auto_ontology/retrieval/text_to_sql/db_probe/executor.py and config.py at 2a24f5d. A probe runs
# through the source executor's `probe` (ARCHITECTURE.md §5.6), which takes only a read-only
# SELECT, caps the rows it returns and has a statement timeout, so upstream's keyword filter and
# appended LIMIT aren't ported. Its limits are constants here, not environment variables.

"""The gate's probes (ARCHITECTURE.md §8.2): read-only queries on the question's source, at most
`MAX_PROBES` of them. A probe never raises: a failure is an entry with `ok` false, which the
checks read as "can't confirm", never as a verdict."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from ontological.sources.executor import Rows, SourceError

logger = logging.getLogger(__name__)

MAX_PROBES = 40  # upstream's DB_PROBE_MAX_CALLS: read-only queries per question
MAX_PROBE_ROWS = 50  # upstream's DB_PROBE_MAX_ROWS
LOW_CARD_THRESHOLD = 20  # upstream's DB_PROBE_LOW_CARD_THRESHOLD: a "categorical" column's values


class Prober(Protocol):
    """The source executor's `probe` (`SourceExecutor.probe`)."""

    def probe(self, sql: str, *, max_rows: int) -> Rows: ...


@dataclass
class ProbeExecutor:
    """Runs the probes of one question against `prober`, which may be None: every `run` then
    fails gracefully."""

    prober: Prober | None
    max_calls: int = MAX_PROBES
    max_rows: int = MAX_PROBE_ROWS
    calls: int = field(default=0, init=False)
    log: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]], init=False)

    @property
    def budget_left(self) -> int:
        return max(0, self.max_calls - self.calls)

    def run(self, sql: str, purpose: str = "") -> dict[str, Any]:
        """One probe, as a log entry: `{sql, purpose, ok, rows, row_count, truncated, error,
        elapsed_ms}`, with `rows` as one dict per row."""
        entry: dict[str, Any] = {
            "sql": sql,
            "purpose": purpose,
            "ok": False,
            "rows": None,
            "row_count": 0,
            "truncated": False,
            "error": None,
            "elapsed_ms": 0,
        }
        self.log.append(entry)
        if self.prober is None:
            entry["error"] = "no connector available"
            return entry
        if self.calls >= self.max_calls:
            entry["error"] = "probe budget exhausted"
            return entry

        self.calls += 1
        start = time.perf_counter()
        try:
            rows = self.prober.probe(sql, max_rows=self.max_rows)
        except SourceError as exc:
            entry["error"] = str(exc)
            logger.info("Probe failed: %s :: %s", sql, exc)
            return entry
        finally:
            entry["elapsed_ms"] = int((time.perf_counter() - start) * 1000)

        entry["ok"] = True
        entry["rows"] = [dict(zip(rows.columns, row, strict=True)) for row in rows.rows]
        entry["row_count"] = len(rows.rows)
        entry["truncated"] = rows.truncated
        return entry
