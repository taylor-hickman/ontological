# Ontological engine

The Python package `ontological`: jobs, the answer agent, the Claude gateway, the embedder and the bundled Postgres lifecycle. Beside it, `auto_ontology` is upstream's code, vendored under its own name (see [auto_ontology/UPSTREAM.md](auto_ontology/UPSTREAM.md)); ruff skips it so it stays close to upstream. See [ARCHITECTURE.md §10](../docs/ARCHITECTURE.md#10-packaging-and-repository-layout) for the planned layout.

Managed with [uv](https://docs.astral.sh/uv/) on Python 3.12. From this directory:

```sh
uv sync                    # create .venv with the dev tools
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Contract tests that spawn your logged-in `claude` (and spend a little quota) are skipped unless `ONTOLOGICAL_CONTRACT=1` is set: `ONTOLOGICAL_CONTRACT=1 uv run pytest tests/contract`. `ONTOLOGICAL_CONTRACT_MODEL` picks the model (default `haiku`).

The store lifecycle tests in `tests/store` drive the bundled Postgres binaries, and skip without them. Point them at the bin/ of a signed S4 tree (`tools/spikes/s4_postgres`) and, for the major-version tests, a PostgreSQL 16 build (`VERSIONS_ENV=tools/spikes/s5_pg_lifecycle/pg16.env tools/spikes/s4_postgres/build.sh <work>`):

```sh
ONTOLOGICAL_PG_BIN=<tree>/bin ONTOLOGICAL_PG16_BIN=<work>/postgres/bin uv run pytest tests/store
```

`ONTOLOGICAL_PG_REQUIRED=1` turns a missing binary into a failure instead of a skip; `postgres.yml` sets it. The disk-full tests mount small disk images with `hdiutil`, so they run on macOS only.
