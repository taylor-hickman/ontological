# Spikes

Scripts for the M0 spikes (S1 to S9), kept so they can be re-run. Each spike's result note goes in [BUILD_PLAN.md, Spike results](../../docs/BUILD_PLAN.md#spike-results).

| Spike | Script | Run |
|---|---|---|
| S1 | [s1_agent_sdk.py](s1_agent_sdk.py) | `python3 tools/spikes/s1_agent_sdk.py` from inside a Claude Code session, so the `current` column is the nested case. It runs the Agent SDK contract tests in a fresh login-shell environment and in the current one, and prints pass or fail for each behaviour and every difference between the two |
