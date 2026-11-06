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

## Running the engine

The engine serves its API on `127.0.0.1` at a random port and writes `{pid, port, token, version}` to `<appData>/engine.json` (mode 0600); every request needs `Authorization: Bearer <token>`. It starts or adopts the bundled Postgres from `--pg-bin` (or `ONTOLOGICAL_PG_BIN`), and `<appData>` defaults to `~/Library/Application Support/Ontological` (or `ONTOLOGICAL_APP_DATA`). See [ARCHITECTURE.md §4.4](../docs/ARCHITECTURE.md#44-engine-lifetime) for leases and idle shutdown.

```sh
uv run ontological-engine --pg-bin <tree>/bin              # in the foreground; Ctrl-C stops it
uv run ontological-engine --pg-bin <tree>/bin --headless   # detached; returns once it serves
```

Both print one JSON line, `{"pid", "port", "version", "started"}`, where `started` is false if an engine was already running. The engine exits after 10 idle minutes with no leases; `--idle-timeout` and `--lease-renew` (seconds) shorten that.

## The embedding model

The engine embeds with `snowflake-arctic-embed-m-v1.5`'s int8 ONNX file on the CPU, loaded through `onnxruntime` and `tokenizers` ([ARCHITECTURE.md §7.1](../docs/ARCHITECTURE.md#71-a-local-model-with-nothing-to-set-up)). The app will bundle it; until then, fetch it once (110 MB, pinned by revision and SHA-256) and point the engine at it with `--model-dir` (or `ONTOLOGICAL_MODEL_DIR`):

```sh
uv run python -m ontological.embed.model ~/.cache/ontological/model
uv run ontological-engine --pg-bin <tree>/bin --model-dir ~/.cache/ontological/model
```

At start the engine times one batch and caps bulk batches at what takes about 100 ms, up to 1,024 padded tokens. Without a model directory it runs with no embedder.

## The MCP server

`ontological-mcp` is the stdio MCP server for Claude Code, Claude Desktop and Cursor ([ARCHITECTURE.md §9.2](../docs/ARCHITECTURE.md#92-mcp-for-external-agents)). It answers `initialize` at once, then attaches to the running engine, or starts one with `--headless`, and holds a lease on it until the client disconnects. It passes its options to the engine it starts, and logs to stderr.

```sh
claude mcp add ontological -- "$PWD/.venv/bin/ontological-mcp" --pg-bin <tree>/bin
```

## Tests

Contract tests that spawn your logged-in `claude` (and spend a little quota) are skipped unless `ONTOLOGICAL_CONTRACT=1` is set: `ONTOLOGICAL_CONTRACT=1 uv run pytest tests/contract`. `ONTOLOGICAL_CONTRACT_MODEL` picks the model (default `haiku`).

The gateway's tests in `tests/llm` run against a scripted `query()` and spawn nothing; its contract tests are in `tests/contract` with S1's.

The store lifecycle tests in `tests/store` drive the bundled Postgres binaries, and skip without them. Point them at the bin/ of a signed S4 tree (`tools/spikes/s4_postgres`) and, for the major-version tests, a PostgreSQL 16 build (`VERSIONS_ENV=tools/spikes/s5_pg_lifecycle/pg16.env tools/spikes/s4_postgres/build.sh <work>`):

```sh
ONTOLOGICAL_PG_BIN=<tree>/bin ONTOLOGICAL_PG16_BIN=<work>/postgres/bin uv run pytest tests/store
```

The engine process tests in `tests/engine`, and the shim's, run the real engine with a fake store and need no binaries, except `test_engine_pg.py`, which runs it on the bundled Postgres with `ONTOLOGICAL_PG_BIN` set.

The source executor tests in `tests/sources` run real executor processes on the SQLite, DuckDB, Parquet, CSV and JSON files they write. The Postgres and MySQL tests in `test_scanners.py` need servers, and skip without them. Postgres runs in a cluster of its own on the binaries `ONTOLOGICAL_PG_BIN` names (the bundled tree, or PGDG's packages in `ci.yml`); MySQL is any server you name with a user that can create databases and users (`ci.yml` starts the runner's own). With Docker:

```sh
docker run -d --rm --name src-mysql -e MYSQL_ROOT_PASSWORD=pw -p 53306:3306 mysql:8.4
ONTOLOGICAL_PG_BIN=<tree>/bin ONTOLOGICAL_MYSQL_URL=mysql://root:pw@127.0.0.1:53306 uv run pytest tests/sources
```

`ONTOLOGICAL_MYSQL_REQUIRED=1` turns a missing server into a failure. DuckDB downloads its `sqlite_scanner`, `postgres_scanner` and `mysql_scanner` extensions the first time a test needs them; bundling the extensions with autoinstall off is M7.2.

The catalog ingest tests in `tests/store/test_ingest.py` build Chinook from upstream's fixture SQL (`tests/upstream_fixtures/dev_tools/fixtures/sql/chinook.sql`), extract it through a real source executor and write it to the bundled Postgres, so they need `ONTOLOGICAL_PG_BIN` like the rest of `tests/store`.

The staleness check's tests in `tests/store/test_staleness.py` ingest copies of Chinook, back-date their `pulled` stamps and re-ingest the stale ones the same way. `test_engine_pg.py` also starts the real engine on a store with a source ingested 25 hours ago and waits for the refresh.

The compile tests answer for Claude with a scripted gateway, so they spend no quota. `tests/compile` checks the split stages' prompts and fallbacks, and profiles a SQLite file through a real executor. `tests/store/test_compile.py` ingests Chinook, compiles it and checks the profile, the Terms and attributes, and the semantic FKs in the store, so it needs `ONTOLOGICAL_PG_BIN`. `tests/store/test_compile_jobs.py` runs the compile as a job on a clock whose sleeps return at once, and checks its checkpoints, the pause until `resets_at`, and the resume after a stop, a crash or an engine restart mid-pause. `tests/compile/test_packing.py` checks the plan's order and packs. `tests/llm/test_compile_routes.py` runs each stage through the real gateway on a scripted `query()`, to check its route and schema.

`ONTOLOGICAL_PG_REQUIRED=1` turns a missing binary into a failure instead of a skip. CI sets it in two places: `ci.yml` runs these tests on every change on Linux, against PGDG's PostgreSQL 17 and 16 with pgvector (`.github/actions/postgres-linux`), and `postgres.yml` runs them on the bundled macOS tree when its build changes.

Tests that run the embedding model (`tests/embed`, the embedder's engine tests, `tests/store/test_dense_search.py` and two of `tests/store/test_hybrid_search.py`'s) skip without `ONTOLOGICAL_MODEL_DIR`, and `ONTOLOGICAL_MODEL_REQUIRED=1` turns that into a failure. CI caches the model and sets both (`.github/actions/embedding-model`). The disk-full tests mount small disk images with `hdiutil`, so they run on macOS only.
