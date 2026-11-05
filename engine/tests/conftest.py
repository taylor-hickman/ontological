"""The embedding model, for every test that runs it (ARCHITECTURE.md §7.1).

    ONTOLOGICAL_MODEL_DIR=<dir>   a directory `python -m ontological.embed.model <dir>` filled

Without it those tests skip, unless ONTOLOGICAL_MODEL_REQUIRED=1 (set in CI, which caches the
model), which turns a missing model into a failure so a test can't skip quietly.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ontological.embed import model
from ontological.embed.embedder import LocalEmbedder


@pytest.fixture(scope="session")
def model_dir() -> Path:
    value = os.environ.get("ONTOLOGICAL_MODEL_DIR")
    if not value:
        if os.environ.get("ONTOLOGICAL_MODEL_REQUIRED") == "1":
            pytest.fail("ONTOLOGICAL_MODEL_DIR is not set and ONTOLOGICAL_MODEL_REQUIRED=1")
        pytest.skip("set ONTOLOGICAL_MODEL_DIR to a directory holding the embedding model")
    path = Path(value)
    model.verify(path)
    return path


@pytest.fixture(scope="session")
def embedder(model_dir: Path) -> LocalEmbedder:
    return LocalEmbedder(model_dir)
