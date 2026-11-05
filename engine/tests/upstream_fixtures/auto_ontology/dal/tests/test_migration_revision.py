# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: finds the vendored chain at auto_ontology/alembic, and drops
# test_the_chart_pins_the_head_revision, which reads upstream's Helm values.yaml.

"""The Helm chart's pinned Alembic revision must match the head migration.

The backend's ``wait-for-catalog-schema`` init container blocks until
``public.alembic_version`` equals ``backend.alembicRevision``. That makes the value
load-bearing in a way a comment cannot enforce: pin it to a revision that never
arrives and every backend Pod waits forever; leave it behind after adding a
migration and the gate passes before the new schema is applied, which is the
bug it exists to prevent.
"""

from __future__ import annotations

import pathlib
import re

import auto_ontology

VERSIONS = pathlib.Path(auto_ontology.__file__).resolve().parent / "alembic" / "versions"


def _head_revisions() -> list[str]:
    """Revisions nothing else lists as its ``down_revision``."""
    revisions: dict[str, str | None] = {}
    for path in VERSIONS.glob("*.py"):
        text = path.read_text()
        rev = re.search(r'^revision: str = ["\']([^"\']+)["\']', text, re.M)
        down = re.search(
            r'^down_revision: [^=]+= (?:["\']([^"\']+)["\']|None)', text, re.M
        )
        if rev:
            revisions[rev.group(1)] = down.group(1) if down else None
    parents = {d for d in revisions.values() if d}
    return sorted(set(revisions) - parents)


def test_there_is_exactly_one_head_migration() -> None:
    """Two heads mean `upgrade head` is ambiguous and the gate cannot be pinned."""
    heads = _head_revisions()
    assert len(heads) == 1, f"expected a single head, found {heads}"

