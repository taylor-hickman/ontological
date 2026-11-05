# Spikes

Scripts for the M0 spikes (S1 to S9), kept so they can be re-run. Each spike's result note goes in [BUILD_PLAN.md, Spike results](../../docs/BUILD_PLAN.md#spike-results).

| Spike | Script | Run |
|---|---|---|
| S1 | [s1_agent_sdk.py](s1_agent_sdk.py) | `python3 tools/spikes/s1_agent_sdk.py` from inside a Claude Code session, so the `current` column is the nested case. It runs the Agent SDK contract tests in a fresh login-shell environment and in the current one, and prints pass or fail for each behaviour and every difference between the two |
| S4 | [s4_postgres/](s4_postgres/) | `.github/workflows/postgres.yml` runs it on every change to the folder. Locally, run the steps in [its README](s4_postgres/README.md) on a Mac with SIP on: CI runners have SIP off, so only a local run checks library validation |
| S6 | [s6_golden_dal/](s6_golden_dal/) | `run.sh <work> <s4-dist>` with a signed tree from S4. It fetches upstream at the pinned commit, seeds the golden fixture into a test cluster and runs `dal/tests/`. See [its README](s6_golden_dal/README.md) |
| S7 | [s7_embedder.py](s7_embedder.py) | `uv run tools/spikes/s7_embedder.py`, about 30 minutes (`--quick`: 3). It downloads the model once, loads it through fastembed and directly, and prints load time, parity, batch cost by shape, bulk texts per second and query latency. The last full run is [s7_results.json](s7_results.json) |
