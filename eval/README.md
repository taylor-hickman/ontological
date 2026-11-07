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
uv run python -m ontological_eval compile         # compile them there: spends Claude quota (~$1.50 at API prices)
uv run python -m ontological_eval sets check      # every set's gold SQL runs and returns rows
uv run python -m ontological_eval retrieval       # the retrieval benchmark: recall@k, no Claude
uv run python -m ontological_eval review run      # the compile-quality review: spends Claude quota (~$3.75)
uv run python -m ontological_eval review dump     # what each review config wrote, for reading side by side
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

`ingest` starts the eval cluster and the store's Postgres if they aren't running and stops whichever it started. It refuses while an engine serves `<home>/app`, which wouldn't see the change. A database the store already holds is re-ingested through the catalog diff (M2.3), so an unchanged source leaves its catalog as it is. To start over, delete `<home>/app`.

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

## The eval compile

`compile` runs the engine's compile on the eval databases in the eval store, through the real gateway on your Claude subscription, and syncs their Terms and attributes into the semantic layer with the default embedding model. [`ontological_eval/compile_store.py`](ontological_eval/compile_store.py) holds it. Every call is logged in the store's `ops.llm_call` with its API-equivalent cost, and its answer is cached there. A database whose tables all have Terms makes no call, and a compile that Claude stopped picks up where it left off on the next run. With M3.1's compile, the five databases took 256 calls, $1.49 at API prices and 14 minutes (BUILD_PLAN.md M3.9). It refuses while an engine serves the store, as `ingest` does.

## The retrieval benchmark

`retrieval` measures what retrieval finds of each question's gold tables and columns, for each candidate embedding model (ARCHITECTURE.md §7.2) and each configuration: dense only, BM25 only, trigram only, and hybrid (§7.3). It makes no Claude call, so it costs no quota once the store holds what it searches. [`ontological_eval/retrieval.py`](ontological_eval/retrieval.py) has the details.

```sh
uv run python -m ontological_eval retrieval                          # the grounding pack, all four models, dev
uv run python -m ontological_eval retrieval --layer data             # the data layer: needs only the ingest
uv run python -m ontological_eval retrieval --models arctic-m bge-small --set smoke
```

- **Gold.** The engine's SQL parser, the one query history goes through, resolves each gold query to catalog table and column ids.
- **`--layer semantic`** (the default) builds the grounding pack as the agent's pre-work does: the raw question and its evidence, scoped to the question's database, with k as the pack's `question_k`. Column recall counts only gold columns that have an attribute; a declared-FK column gets a semantic FK instead. This layer needs a compile of the eval store.
- **`--layer data`** searches the data layer that ingest builds, the question's k nearest columns plus its nearest table, as a stand-in that needs no compile.
- **Collections.** Each model gets its own collection per layer (`vdb.bench_<layer>_<model>`), rebuilt from the store on every run, so nothing the engine reads is touched. The models are fetched into `<home>/models/<model>` and checked against their pinned hashes; all four come to about 750 MB.
- **Coverage threshold.** It is recalibrated per model from dense distances. A question's nearest row in its own database counts as a positive, and its nearest row in each other eval database as a negative. The threshold is the distance that best separates the two (Youden's J), shown beside upstream's 0.75.

The report prints as Markdown and is written to `<home>/results/retrieval-<layer>-<set>.json`, with every distance the threshold came from. The data layer on dev takes about a minute a model on an M-series Mac.

## The compile-quality review

`review` compiles Pagila and WWI four ways for BUILD_PLAN.md M3.10 and records what each compile cost. [`ontological_eval/review.py`](ontological_eval/review.py) holds it.

- **Configs.** `split` and `merged` are `compile.table_calls` on the default routes. `split-cheaper` and `merged-cheaper` move every compile route one step down: Opus to Sonnet and Sonnet to Haiku at the same effort, and a Haiku route one effort level down.
- **A store each.** Each config ingests into and compiles in `<home>/review/<config>`, so each starts with no Terms and no cache. A second `review run` makes no calls and keeps the figures the first recorded.
- **Pagila** loads into the eval cluster the first time it's asked for, from the trimmed copy in `engine/tests/upstream_fixtures/dev_tools/fixtures/sql/` and its `analytics` schema, so there's nothing to download.
- **What's recorded.** `<home>/results/compile-review.json` holds each compile's wall time, the gateway's utilization before and after, and from the call log its calls, outcomes, tokens and API-equivalent cost per task and per model. `review dump` writes `<home>/results/compile-review-<database>.json`, keyed by table, then config: the keys, Terms with their attributes and descriptions, FK edges marked declared or inferred, and the columns with neither.
- **Retrieval per config.** `retrieval --store <config>` runs the benchmark against a review store instead, on the set's questions whose database the store holds (WWI's), calibrating against its other database (Pagila). The report ends in `-<config>`.

## Tests

```sh
uv run ruff check . && uv run ruff format --check . && uv run pyright
ONTOLOGICAL_PG_BIN=<tree>/bin uv run pytest
```

`tests/test_retrieval.py` checks gold extraction and the threshold's calibration on their own, and runs the data-layer benchmark end to end on two small BIRD-shaped files with the default model (it also needs `ONTOLOGICAL_MODEL_DIR`, which CI sets). `tests/test_sets.py` chooses sets from synthetic questions shaped like Mini-Dev and checks gold SQL on a small SQLite file. `tests/test_fetch.py` runs `fetch` against a local HTTP server that answers range requests. `tests/test_cluster.py` drives the bundled binaries on a small WWI-shaped dataset, and `tests/test_ingest.py` ingests that dataset and a small BIRD-shaped SQLite file into an eval store. Both skip without `ONTOLOGICAL_PG_BIN` unless `ONTOLOGICAL_PG_REQUIRED=1`, which CI sets: `ci.yml` runs both on Linux, and `postgres.yml` runs the cluster's on the bundled tree.
