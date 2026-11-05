# Ontological

A local, single-user desktop app that builds a semantic ontology over your databases and answers natural-language questions against them with validated SQL. It runs on your own Claude subscription through your installed Claude Code, and keeps source data on your machine.

Status: M0, engine skeleton. The engine is in [engine/](engine/); its README has the dev commands.

## Docs

- [Architecture](docs/ARCHITECTURE.md): how upstream auto-ontology works, and the design for Ontological's engine, storage, Claude gateway, answer agent and surfaces.
- [Build plan](docs/BUILD_PLAN.md): spikes, the eval set, and milestones M0 to M7.
- [Design](docs/DESIGN.md): the Electron UI, with tokens, window and shell, components, screens and states. Mockups are in [docs/design/mockups/](docs/design/mockups/).
