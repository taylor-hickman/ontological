"""The engine runs on what the app bundles (ARCHITECTURE.md §10): it checks the model's files
against their pinned SHA-256s rather than fetching them, and points every source executor at the
DuckDB extensions it is given, refusing to start if they don't load from there."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from engine_proc import Proc, app_data, store_log

from ontological.embed import model
from ontological.engine import main
from ontological.sources import extensions

__all__ = ["app_data"]  # the fixture


def test_the_extension_directory_comes_from_the_flag_or_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(extensions.ENV, raising=False)
    assert main.parse_args([])[0].duckdb_extensions is None
    monkeypatch.setenv(extensions.ENV, str(tmp_path / "env"))
    assert main.parse_args([])[0].duckdb_extensions == tmp_path / "env"
    flag = ["--duckdb-extensions", str(tmp_path / "flag")]
    assert main.parse_args(flag)[0].duckdb_extensions == tmp_path / "flag"


def test_a_model_file_that_isn_t_the_pinned_one_stops_the_embedder(tmp_path: Path) -> None:
    for name in model.FILES:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(b"not the model")
    config = main.Config(app_data=tmp_path, pg_bin=None, model_dir=tmp_path)
    with pytest.raises(model.ModelError, match="pinned SHA-256"):
        main.start_embedder(config)


def test_the_engine_hands_its_extension_directory_to_the_executors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = extensions.directory()
    if directory is None:
        pytest.skip(f"{extensions.ENV} is unset")
    monkeypatch.delenv(extensions.ENV)
    main.use_extensions(main.Config(app_data=tmp_path, pg_bin=None, duckdb_extensions=directory))
    assert extensions.directory() == directory


def test_extensions_that_don_t_load_stop_the_engine_before_the_store_starts(
    app_data: Path, tmp_path: Path
) -> None:
    empty = tmp_path / "no-extensions"
    empty.mkdir()
    e = Proc.stub(app_data, "--duckdb-extensions", str(empty), ready=False)
    line = json.loads(e.lines.get(timeout=30))
    assert "sqlite_scanner" in line["error"]
    assert e.wait_exit(10) == 1
    assert not (app_data / "engine.json").exists()
    assert store_log(app_data) == []
