"""Where the eval's downloaded data and its Postgres cluster live.

Everything sits under one directory outside the repo, so nothing downloaded can be committed and
every checkout and worktree shares one copy. ONTOLOGICAL_EVAL_HOME overrides the default. The
cluster's socket path has to fit in 103 bytes (ARCHITECTURE.md §5.3), which a worktree path
inside the repo wouldn't.

    <home>/wwi/ddl/*.sql, wwi/data/*.csv, wwi/evaluation.json, wwi/LICENSE
    <home>/bird/mini_dev_sqlite.jsonl
    <home>/bird/<db_id>.sqlite
    <home>/cluster/          the eval cluster's <appData>: pgdata, pgsocket, logs, pg.lock
    <home>/app/              the eval store: an engine <appData> that holds the eval's catalogs
    <home>/review/<config>/  a store per compile-quality review config (BUILD_PLAN.md M3.10)
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT = Path.home() / "Library" / "Application Support" / "Ontological Eval"


def eval_home() -> Path:
    value = os.environ.get("ONTOLOGICAL_EVAL_HOME")
    return Path(value).expanduser().resolve() if value else DEFAULT


def wwi_dir(home: Path) -> Path:
    return home / "wwi"


def bird_dir(home: Path) -> Path:
    return home / "bird"


def cluster_dir(home: Path) -> Path:
    return home / "cluster"


def app_data_dir(home: Path, store: str | None = None) -> Path:
    """The eval store, or with `store` the review config of that name's own store."""
    return home / "review" / store if store else home / "app"


def models_dir(home: Path) -> Path:
    """The candidate embedding models, one directory each (the retrieval benchmark's)."""
    return home / "models"


def results_dir(home: Path) -> Path:
    return home / "results"
