# Spikes

Scripts for the M0 spikes (S1 to S9), kept so they can be re-run. Each spike's result note goes in [BUILD_PLAN.md, Spike results](../../docs/BUILD_PLAN.md#spike-results).

| Spike | Script | Run |
|---|---|---|
| S1 | [s1_agent_sdk.py](s1_agent_sdk.py) | `python3 tools/spikes/s1_agent_sdk.py` from inside a Claude Code session, so the `current` column is the nested case. It runs the Agent SDK contract tests in a fresh login-shell environment and in the current one, and prints pass or fail for each behaviour and every difference between the two |
| S4 | [s4_postgres/](s4_postgres/) | `.github/workflows/postgres.yml` runs it on every change to the folder. Locally, run the steps in [its README](s4_postgres/README.md) on a Mac with SIP on: CI runners have SIP off, so only a local run checks library validation |
| S5 | [s5_pg_lifecycle/](s5_pg_lifecycle/) | `python3 tools/spikes/s5_pg_lifecycle/run.py <pg17 bin> <pg16 bin>`. `postgres.yml` runs it after `smoke.sh`. It needs the bin/ of a signed S4 tree and a PostgreSQL 16 build with pgvector: `VERSIONS_ENV=tools/spikes/s5_pg_lifecycle/pg16.env tools/spikes/s4_postgres/build.sh <work>` makes one in `<work>/postgres/bin`. `pg.py` is the prototype `store/pg.py` |
