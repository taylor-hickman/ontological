"""`python -m seed_graph --reset`: upstream's `dev_tools.fixtures.seed_graph_fixture`, with the
test-only modules it ingests and writes through (overlay.py)."""

import runpy

import overlay

overlay.install()
runpy.run_module("dev_tools.fixtures.seed_graph_fixture", run_name="__main__", alter_sys=True)
