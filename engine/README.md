# Ontological engine

The Python package `ontological`, managed with [uv](https://docs.astral.sh/uv/). Layout and design are in [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) §10.

```sh
cd engine
uv sync
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pytest
```
