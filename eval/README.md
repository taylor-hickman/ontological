# Eval

The frozen question-set IDs (`sets/{dev,heldout,smoke}.json`), the runner, the verifier and the control arm. The sets are frozen in M0.8 and the runner arrives in M1.9. See [BUILD_PLAN.md, Eval set](../docs/BUILD_PLAN.md#eval-set).

So far it holds the eval's data: a download script, a Postgres cluster of its own that holds Wide World Importers (M0.7), the frozen question sets (M0.8), and the ingest of all five eval databases into a store of the eval's own (M2.9). It's a uv project on Python 3.12 that uses the engine's `ontological.store.pg` for the cluster. From this directory:

```sh
uv sync
uv run python -m ontological_eval fetch           # download, or check, every pinned file
uv run python -m ontological_eval cluster up      # start or adopt the cluster; loads WWI the first time
uv run python -m ontological_eval cluster check   # each WWI table's row count against its CSV
uv run python -m ontological_eval cluster status  # running or not, and the psql line to connect
uv run python -m ontological_eval cluster down
uv run python -m ontological_eval ingest          # WWI and the four BIRD databases into the eval store
uv run python -m ontological_eval sets check      # every set's gold SQL runs and returns rows
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

`cluster` needs the bundled Postgres's `bin/` in `ONTOLOGICAL_PG_BIN` (or `--pg-bin`). The quickest way to get one is the tree that `postgres.yml` builds and uploads when its build changes on main (or run it by hand with `gh workflow run postgres.yml`):

```sh
gh run download <run-id> -n postgres-17.11-pgvector-0.8.7-darwin-arm64 -D <dir>
mkdir <dir>/pg && tar -C <dir>/pg -xJf <dir>/*.tar.xz   # then ONTOLOGICAL_PG_BIN=<dir>/pg/bin
```

Then connect with the line `cluster status` prints:

```sh
psql -h "<home>/cluster/pgsocket" -p 5432 -U ontological -d wideworldimporters
```

## The eval store

`ingest` writes the catalogs of WWI and the four BIRD databases to `<home>/app`, an engine `<appData>` that only the eval uses, so they never show up in your own catalog. The eval runner (M1.9) starts its engine there with `--app-data`. Name databases to ingest some of them: `wwi`, `financial`, `california_schools`, `formula_1`, `debit_card_specializing`.

It goes through the engine's own source executor and catalog ingest, as a user's source would. WWI is read from the eval cluster as a Postgres source, through DuckDB's `postgres_scanner`; each BIRD database is attached as its `.sqlite` file. BIRD's `database_description/*.csv` files aren't read, so the score measures Ontological's own compile.

`ingest` starts the eval cluster and the store's Postgres if they aren't running and stops whichever it started. It refuses while an engine serves `<home>/app`, which wouldn't see the change. A database the store already holds is reported and left as it is: updating one needs the catalog diff (M2.3). To start over, delete `<home>/app`.

## The sets

`sets/{dev,heldout,smoke}.json` hold question IDs only, by source: `{"wwi": [...], "bird": [...]}`. WWI's IDs are `question_id` in `evaluation.json` and BIRD's are `question_id` in `mini_dev_sqlite.jsonl`. [`ontological_eval/sets.py`](ontological_eval/sets.py) chose them with a fixed seed (`sets freeze`):

| Set | WWI | BIRD |
|---|---|---|
| dev | 36 | 40: 10 from each of the four databases |
| heldout | 0 | 40 more: 12 each from `financial`, `california_schools` and `formula_1`, and the 4 `debit_card_specializing` questions dev didn't take (it has 14) |
| smoke | 10 from dev | 10 from dev: 3, 3, 2 and 2 |

Each BIRD draw follows Mini-Dev's own difficulty mix over all 500 questions (8 simple, 32 moderate, 460 challenging), so dev and held-out each get 1 simple, 2 moderate and 37 challenging.

[`ontological_eval/questions.py`](ontological_eval/questions.py) loads the questions and their gold SQL. It fixes two WWI gold queries that don't run as published (27 lost its newlines; 4 selects a column it doesn't group by) and lists five that every set leaves out, because their gold SQL returns no rows on the data: 1 asks about 2024, and WWI's orders end in 2016; 7, 24, 36 and 41 join special deals on a stock item that both deals leave NULL.

`sets check` runs each set's gold SQL natively, BIRD's with `sqlite3` on the read-only `.sqlite` file and WWI's on the eval cluster, and fails if any query errors, returns no rows, or returns only NULLs. It needs the data and `cluster up`.

The sets are frozen: `sets freeze` won't overwrite them without `--force`, and after M0 nothing in `sets/` changes unless a question turns out to be broken, and then as a commit of its own that says why.

## Tests

```sh
uv run ruff check . && uv run ruff format --check . && uv run pyright
ONTOLOGICAL_PG_BIN=<tree>/bin uv run pytest
```

`tests/test_sets.py` chooses sets from synthetic questions shaped like Mini-Dev and checks gold SQL on a small SQLite file. `tests/test_fetch.py` runs `fetch` against a local HTTP server that answers range requests. `tests/test_cluster.py` drives the bundled binaries on a small WWI-shaped dataset, and `tests/test_ingest.py` ingests that dataset and a small BIRD-shaped SQLite file into an eval store. Both skip without `ONTOLOGICAL_PG_BIN` unless `ONTOLOGICAL_PG_REQUIRED=1`, which CI sets: `ci.yml` runs both on Linux, and `postgres.yml` runs the cluster's on the bundled tree.
