"""Sample values as the stage prompts show them."""

# The vendored helper is untyped.
# pyright: reportUnknownVariableType=false, reportMissingTypeStubs=false

from __future__ import annotations

from typing import Any

from auto_ontology.utils.sample_values import stringify_sample_values


def format_sample_values(raw: Any) -> str:
    """`samples: a, b, c` from values of at most 30 characters, or ""."""
    values: list[str] | None = stringify_sample_values(raw, max_len=30)
    if not values:
        return ""
    return "samples: " + ", ".join(values)
