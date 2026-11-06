# Test-only upstream: the golden DAL suite

This tree is vendored from [NVIDIA/auto-ontology](https://github.com/NVIDIA/auto-ontology) at `2a24f5d34ef6b6e168a7f22d308504d3ab36a198`, under the Apache License 2.0 (`engine/auto_ontology/LICENSE`). It is test-only: the engine never imports it, and it doesn't ship. It holds upstream's golden fixture seed, the connectors and catalog ingest the seed writes through, and upstream's DAL and catalog-store tests. `engine/tests/golden_dal/test_golden_dal.py` runs them as M2's conformance gate (ARCHITECTURE.md §5.4, §11).

Paths map one to one with upstream's checkout: `auto_ontology/<path>` and `dev_tools/<path>` here are the same paths there.

## How it runs

`test_golden_dal.py` starts a store on the bundled binaries with `pg.ensure_running` (initdb, both Alembic chains, the socket only, no password). Then it follows upstream's CI job:

1. `python -m dev_tools.fixtures.seed_fixtures` loads Pagila into a `pagila` database in the same cluster and builds `chinook.sqlite` beside its SQL (ignored by git).
2. `python -m seed_graph --reset` runs `dev_tools.fixtures.seed_graph_fixture` with `CONNECTION_STRINGS` naming both, through upstream's own connectors.
3. `alembic check` on upstream's chain.
4. `pytest auto_ontology/dal/tests auto_ontology/catalog/store/tests` in this directory, under `pytest.ini` (upstream's settings), with `POSTGRES_*` naming the store's socket.

The gate reads the JUnit report and fails on any test that doesn't pass, skips included, and on any file that runs fewer tests than it has.

`auto_ontology/` here is an overlay on the engine's package. `auto_ontology/`, `catalog/`, `server/` and `utils/` have no `__init__.py` here: `overlay.py` adds them to the engine package's `__path__`, so `auto_ontology.catalog.ingest` loads from here while `auto_ontology.catalog.store` still loads from `engine/auto_ontology`. `conftest.py` and `seed_graph.py` install it.

## Files

| File | State |
|---|---|
| `dev_tools/__init__.py`, `dev_tools/fixtures/__init__.py`, `seed_fixtures.py`, `seed_local_postgres.py`, `build_sqlite_fixtures.py`, `seed_graph_fixture.py`, `capture_dal_golden.py` | Unchanged. `capture_dal_golden.py` finds the goldens at `parents[2]/auto_ontology/dal/tests/golden`, which this layout keeps |
| `dev_tools/fixtures/sql/` (`pagila.sql`, `pagila_analytics.sql`, `chinook.sql`, `README.md`) | Unchanged. Pagila and Chinook are MIT (THIRD_PARTY_NOTICES.md) |
| `auto_ontology/catalog/ingest.py`, `extract.py`, `write.py`, `table_filter.py`, `sql_parse.py`, `parsers/*`, `services/*` | Unchanged |
| `auto_ontology/catalog/normalize.py` | Unchanged. The engine imports the port (`ontological/catalog/normalize.py`, M2.3); this copy keeps the seed's `extract.py` and `sql_parse.py` on upstream's own, since the goldens are byte-for-byte against its output. The seed's `write.py` reaches the diff through the store (`catalog/store/db.py`), so it gets the port, which a first load never calls |
| `auto_ontology/connectors/base.py`, `postgres.py`, `sqlite.py`, `connection_string_factory.py` | Unchanged |
| `auto_ontology/connectors/__init__.py` | Modified: only the Postgres and SQLite connectors |
| `auto_ontology/connectors/registry.py` | Modified: only the Postgres and SQLite connectors; drops `get_connectors_for_subject_token` (Databricks SSO) |
| `auto_ontology/retrieval/__init__.py`, `retrieval/data_access/__init__.py`, `retrieval/data_access/graph_schemas.py` | Unchanged |
| `auto_ontology/server/sql_utils.py` | Modified: `SqlParseError` comes from the engine's `server/sql_errors.py`, where the DAL takes it from, so the services' `except SqlParseError` still catches the DAL's errors |
| `auto_ontology/server/sql_attributes/service.py` | Modified: drops the imports of `description_suggester` (an LLM call) and `utils.embedding` (the NIM client), and stubs `get_embed_params` |
| `auto_ontology/server/custom_analyses/service.py` | Modified: stubs `get_embed_params`, which upstream's `utils/__init__.py` held and the engine's empty one doesn't |
| `auto_ontology/server/sql_attributes/__init__.py`, `server/custom_analyses/__init__.py` | Unchanged |
| `auto_ontology/utils/sql_identifiers.py` | Unchanged (`connectors/base.py` imports it) |
| `auto_ontology/dal/tests/*.py`, `golden/*.json` | Unchanged, except the two below |
| `auto_ontology/dal/tests/test_migration_revision.py` | Modified: finds the chain at `auto_ontology/alembic`; drops `test_the_chart_pins_the_head_revision`, which reads upstream's Helm `values.yaml` |
| `auto_ontology/dal/tests/dal_surface.json` | Regenerated (`python test_dal_surface.py`): `embed_custom_analyses` and `embed_pql_analyses` take a LangChain `Embeddings` (M1.1), and `model_interchange` is gone until M6.4 ports it |
| `auto_ontology/dal/tests/test_model_interchange.py` | Unchanged, and not run: `conftest.py` ignores it until M6.4 ports `dal/model_interchange.py` |
| `auto_ontology/catalog/store/tests/*.py` | Unchanged |

The seed stubs the services' embed calls (`_stub_embedding`), so the stubbed `get_embed_params` is never used, and nothing here embeds.

Added by Ontological: `overlay.py`, `conftest.py`, `seed_graph.py`, `pytest.ini`, `.gitignore` and this file.

To check a file against upstream: `diff <upstream checkout>/auto_ontology/connectors/registry.py engine/tests/upstream_fixtures/auto_ontology/connectors/registry.py`.
