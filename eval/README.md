# Eval

The frozen question-set IDs (`sets/{dev,heldout,smoke}.json`), the runner, the verifier and the control arm. The sets are frozen in M0.8 and the runner arrives in M1.9. See [BUILD_PLAN.md, Eval set](../docs/BUILD_PLAN.md#eval-set).

So far it holds the eval's data: a download script, and a Postgres cluster of its own that holds Wide World Importers (M0.7). It's a uv project on Python 3.12 that uses the engine's `ontological.store.pg` for the cluster. From this directory:

```sh
uv sync
uv run python -m ontological_eval fetch           # download, or check, every pinned file
uv run python -m ontological_eval cluster up      # start or adopt the cluster; loads WWI the first time
uv run python -m ontological_eval cluster check   # each WWI table's row count against its CSV
uv run python -m ontological_eval cluster status  # running or not, and the psql line to connect
uv run python -m ontological_eval cluster down
```

## The data

Downloaded, never committed. Everything lives under `ONTOLOGICAL_EVAL_HOME`, by default `~/Library/Application Support/Ontological Eval`, outside the repo. Every checkout and worktree shares one copy, and the cluster's socket path stays within macOS's 103 bytes (ARCHITECTURE.md §5.3), which a worktree path wouldn't.

| Group | What | From | License |
|---|---|---|---|
| `wwi` | `ddl/*.sql`, `data/*.csv` and the 41 questions in `evaluation.json` | `datasets/wideworldimporters/` in [NVIDIA/ontology-sql-eval](https://github.com/NVIDIA/ontology-sql-eval) at a pinned commit | ddl and data: MIT, from Microsoft's samples; the repo: Apache-2.0 |
| `bird-questions` | `mini_dev_sqlite.jsonl`, 500 questions with `evidence` and gold SQL | [birdsql/bird_mini_dev](https://huggingface.co/datasets/birdsql/bird_mini_dev) at a pinned revision | CC BY-SA 4.0 |
| `bird-databases` | `financial`, `california_schools`, `formula_1` and `debit_card_specializing` as `.sqlite` | BIRD's `minidev.zip` | CC BY-SA 4.0 |

[`ontological_eval/sources.json`](ontological_eval/sources.json) pins every URL and every file's sha256. A file on disk with the right hash isn't fetched again, and a download that doesn't match never replaces anything. To move to a new upstream version, change the URL and the hashes there in a commit of their own.

What isn't fetched: BIRD's `database_description/*.csv` and ontology-sql-eval's `custom_analyses.json`, because neither is ingested (BUILD_PLAN.md, Eval set). BIRD's databases ship only inside the 800 MB `minidev.zip`, so `fetch` reads its central directory and the four databases' compressed bytes with HTTP range requests, about 46 MB, and nothing else.

## The eval cluster

A separate Postgres 17 cluster on the bundled binaries, run by the store's lifecycle code (`engine/ontological/store/pg.py`) with `<home>/cluster` as its `<appData>`. It never touches the app's store. WWI's gold SQL runs here, and M2 ingests WWI from here as a Postgres source.

On its first launch the cluster creates the `wideworldimporters` database and loads it in one transaction: the DDL, a server-side `COPY` per CSV, then the foreign keys and views. It then checks each table's row count against its CSV. A load that fails or comes up short leaves no cluster, and the next `up` starts again. To reload, `cluster down` and delete `<home>/cluster`.

`cluster` needs the bundled Postgres's `bin/` in `ONTOLOGICAL_PG_BIN` (or `--pg-bin`). The quickest way to get one is the tree that `postgres.yml` builds and uploads on every push to main:

```sh
gh run download <run-id> -n postgres-17.11-pgvector-0.8.7-darwin-arm64 -D <dir>
mkdir <dir>/pg && tar -C <dir>/pg -xJf <dir>/*.tar.xz   # then ONTOLOGICAL_PG_BIN=<dir>/pg/bin
```

Then connect with the line `cluster status` prints:

```sh
psql -h "<home>/cluster/pgsocket" -p 5432 -U ontological -d wideworldimporters
```

## Tests

```sh
uv run ruff check . && uv run ruff format --check . && uv run pyright
ONTOLOGICAL_PG_BIN=<tree>/bin uv run pytest
```

`tests/test_fetch.py` runs `fetch` against a local HTTP server that answers range requests. `tests/test_cluster.py` drives the bundled binaries on a small WWI-shaped dataset, and skips without `ONTOLOGICAL_PG_BIN` unless `ONTOLOGICAL_PG_REQUIRED=1`, which `postgres.yml` sets.
