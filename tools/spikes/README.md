# Spikes

Scripts for the M0 spikes (S1 to S9), kept so they can be re-run. Each spike's result note goes in [BUILD_PLAN.md, Spike results](../../docs/BUILD_PLAN.md#spike-results).

- `s7_embedder.py`: S7, the embedder's loader and speed. `uv run tools/spikes/s7_embedder.py` (about 30 minutes; `--quick` takes 3). Its last full run is `s7_results.json`.
