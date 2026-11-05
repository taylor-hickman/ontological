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

The source executor tests in `tests/sources` run real executor processes on SQLite files they write. DuckDB downloads its `sqlite_scanner` extension the first time it attaches one; bundling the extensions with autoinstall off is M7.2.

`ONTOLOGICAL_PG_REQUIRED=1` turns a missing binary into a failure instead of a skip; `postgres.yml` sets it.

Tests that run the embedding model (`tests/embed`, the embedder's engine tests and `tests/store/test_dense_search.py`) skip without `ONTOLOGICAL_MODEL_DIR`, and `ONTOLOGICAL_MODEL_REQUIRED=1` turns that into a failure. CI caches the model and sets both (`.github/actions/embedding-model`). The disk-full tests mount small disk images with `hdiutil`, so they run on macOS only.
