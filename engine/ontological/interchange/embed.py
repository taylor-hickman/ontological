# SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: stands in for upstream's auto_ontology/server/model_interchange/embed.py
# at 2a24f5d. Upstream's buffer collected the texts of the rows an import created, which the
# importer embedded through its NIM client after the commit, and nothing told anyone when that
# failed. Here the import names the databases it wrote, and each is re-embedded afterwards as a
# tracked `reembed` job (ontological/embed/reembed.py), whose sync embeds the texts that changed.

"""What an import leaves for the re-embed that follows it (ARCHITECTURE.md §9.3)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ImportEmbedBuffer:
    """The databases an import wrote, by name, in the document's order: each one's vectors are
    synced once the import commits."""

    database_names: list[str] = field(default_factory=list[str])
