# Third-party notices

Ontological includes the following third-party material.

## Auto Ontology

- Files: `engine/auto_ontology/`, and the test-only `engine/tests/upstream_fixtures/`, vendored from [NVIDIA/auto-ontology](https://github.com/NVIDIA/auto-ontology) at commit `2a24f5d`. The `UPSTREAM.md` in each lists the files and which ones Ontological modified; each modified file says so under its SPDX header. Files under `engine/ontological/` and `engine/tests/` ported from it (the catalog ingest, `profile/`, parts of `answer/`, and their tests) keep upstream's SPDX header and say what Ontological changed.
- Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
- License: Apache License, Version 2.0. The full text is in `engine/auto_ontology/LICENSE`.

## Pagila and Chinook

- Files: `engine/tests/upstream_fixtures/dev_tools/fixtures/sql/pagila.sql` and `chinook.sql`, as vendored by Auto Ontology. Test data only; never shipped.
- Pagila ([devrimgunduz/pagila](https://github.com/devrimgunduz/pagila), `pagila-v3.1.0`, trimmed by Auto Ontology): Copyright (c) Devrim Gündüz. MIT License.
- Chinook ([lerocha/chinook-database](https://github.com/lerocha/chinook-database), unmodified): Copyright (c) 2008-2024 Luis Rocha. MIT License.
- The full license texts accompany each project at the URLs above. `dev_tools/fixtures/sql/README.md` there says what Auto Ontology changed.

## Geist and Geist Mono

- Files: `apps/desktop/src/renderer/src/assets/fonts/Geist-Variable.woff2` and `GeistMono-Variable.woff2`, from the `geist` npm package, version 1.9.0.
- Copyright (c) 2023 Vercel, in collaboration with basement.studio.
- License: SIL Open Font License, Version 1.1. The full text is in `apps/desktop/src/renderer/src/assets/fonts/OFL.txt`.
