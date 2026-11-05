"""The engine loads the embedder and calibrates its batch cap at start (ARCHITECTURE.md §7.1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from engine_proc import Proc, app_data, store_log

from ontological.embed.embedder import CAP_TOKENS, MIN_CAP_TOKENS
from ontological.engine import main

__all__ = ["app_data"]  # the fixture


def test_the_model_directory_comes_from_the_flag_or_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ONTOLOGICAL_MODEL_DIR", raising=False)
    assert main.parse_args([])[0].model_dir is None
    monkeypatch.setenv("ONTOLOGICAL_MODEL_DIR", str(tmp_path / "env"))
    assert main.parse_args([])[0].model_dir == tmp_path / "env"
    assert main.parse_args(["--model-dir", str(tmp_path / "flag")])[0].model_dir == (
        tmp_path / "flag"
    )


def test_without_a_model_the_engine_runs_without_an_embedder(tmp_path: Path) -> None:
    assert main.start_embedder(main.Config(app_data=tmp_path, pg_bin=None)) is None


def test_the_embedder_starts_calibrated(model_dir: Path, tmp_path: Path) -> None:
    embedder = main.start_embedder(main.Config(app_data=tmp_path, pg_bin=None, model_dir=model_dir))
    assert embedder is not None
    assert MIN_CAP_TOKENS <= embedder.cap_tokens <= CAP_TOKENS
    assert len(embedder.embed_query("revenue by country")) == embedder.dim


def test_the_engine_serves_with_the_embedder(model_dir: Path, app_data: Path) -> None:
    e = Proc.stub(app_data, "--model-dir", str(model_dir))
    assert e.info["started"] is True
    assert e.term() == 0
    assert store_log(app_data) == ["start", "quit-dump", "stop"]


def test_a_model_that_fails_to_load_is_reported_and_the_store_stopped(
    app_data: Path, tmp_path: Path
) -> None:
    e = Proc.stub(app_data, "--model-dir", str(tmp_path / "no-model"), ready=False)
    line = json.loads(e.lines.get(timeout=30))
    assert "error" in line
    assert e.wait_exit(10) == 1
    assert not (app_data / "engine.json").exists()
    assert store_log(app_data) == ["start", "quit-dump", "stop"]
