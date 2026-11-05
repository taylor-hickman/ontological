# S6: upstream's golden DAL suite on a bare Postgres

Runs upstream's golden fixture seed and `auto_ontology/dal/tests/` against a cluster built from S4's binaries, with no Docker. The steps are upstream's own CI job (`.github/workflows/ci-python.yml`, "Pytest (with Postgres fixtures)"), with the `pgvector/pgvector:pg17` service container swapped for an `initdb`'d cluster. Result note: [BUILD_PLAN.md, S6](../../../docs/BUILD_PLAN.md#spike-results).

```sh
# A signed tree from S4 (under a minute on an M-series Mac):
../s4_postgres/build.sh <work> && ../s4_postgres/trim.sh <work>/postgres <work>/dist
../s4_postgres/sign.sh <work>/dist ../s4_postgres/entitlements.plist

./run.sh <work>/run <work>/dist    # about 30 s once uv's cache is warm
```

`run.sh` needs `git`, `uv` and network access to fetch upstream at the commit pinned in `versions.env`. Set `UPSTREAM_REPO` to a local clone to fetch from there instead.

## What it does

1. Fetches upstream at `2a24f5d` into `<work>/upstream` and runs `uv sync --locked`.
2. Runs `initdb` (UTF-8, locale `C`, as the engine will) with `scram-sha-256` and a password, because upstream connects over TCP with `POSTGRES_USER` and `POSTGRES_PASSWORD`. It then starts the server on `127.0.0.1:55432` (`S6_PORT` overrides the port).
3. Runs upstream's `dev_tools.fixtures.seed_fixtures`, which loads Pagila into a `pagila` database and builds Chinook as `chinook.sqlite`. Then `alembic upgrade head`, then `seed_graph_fixture --reset` through upstream's own connectors, then `alembic check`.
4. Runs `pytest auto_ontology/dal/tests` with `POSTGRES_*` set and writes the pass/fail list (`report.py`). A skip counts as a failure, so the script exits non-zero on any failure, error or skip.
5. Restarts the server on the Unix socket only (`listen_addresses=''`, how §5.3 has the engine connect) and runs the tests again with `POSTGRES_HOST` set to the socket directory. This pass is expected to skip until upstream's `infra/postgres.py` is edited, so it doesn't change the exit status.
6. Runs `imports.py`, which lists every upstream module that ARCHITECTURE.md §11's vendored files import from outside that set. It also lists the outside modules the seed and the tests actually loaded, which `s6_loaded.py` records during steps 3 and 4.

Reports go to `<work>/run/reports/`: `dal.md` (the pass/fail list), `dal-socket.md`, `imports.md`, and pytest's raw output and JUnit XML.

## Files

| File | What it is |
|---|---|
| `run.sh` | The steps above |
| `versions.env` | Upstream's repo and pinned commit |
| `report.py` | JUnit XML to a per-file pass/fail table, every non-pass listed, exit 1 on any skip |
| `imports.py` | Static import analysis of §11's vendored set (`ast`, nothing upstream runs). Needs Python 3.10 or later, so `run.sh` runs it with upstream's venv |
| `s6_loaded.py` | pytest plugin and `python -m` wrapper that write the loaded `auto_ontology` and `dev_tools` modules to `$S6_LOADED` at exit |
