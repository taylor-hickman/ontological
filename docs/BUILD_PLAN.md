# Ontological: Build plan

This file turns [ARCHITECTURE.md §12](ARCHITECTURE.md#12-build-plan-and-verification) into ordered work. §12 owns the milestone list, each milestone's scope and its "done when" criteria. This file owns the order: spikes, tasks, dependencies, and what each milestone hands to the next. If the two disagree, fix whichever is wrong in the same commit.

Task IDs (`S3`, `E2`, `M1.4`) are stable, so commits and issues can refer to them. `after:` lists the tasks that must finish first. **TDD** marks work that is written test-first (see [Test-first work](#test-first-work)).

## How the order was chosen

1. **Unverified claims go first, as spikes.** Each spike answers one question, has a decision rule written before it runs, and either confirms the architecture or names the section to change.
2. **One thin vertical slice before any depth.** The riskiest parts are the Agent SDK behaviour behind the gated agent (§8.1) and the eval loop. M1 runs both end to end on Chinook, headless, before the catalog, compile or UI get deep.
3. **The eval set exists before anything depends on it.** M3's retrieval benchmark needs gold SQL, and M4's tuning needs end-to-end scores, so the question set is frozen in M0.
4. **Usage is a budget.** Every eval run comes out of the user's subscription. Development runs use the 20-question smoke set with the answer agent on Sonnet. Full runs on the real routes happen only when a default is being set.
5. **The engine comes before the UI.** The eval runner, the MCP shim and the Electron app are all clients of the same engine API, so the API settles in M1 and M2 before the Electron work in M2 starts building on it.

## Decisions from the 2026-11-04 review

The review of ARCHITECTURE.md at `0269407` changed these. ARCHITECTURE.md now carries each one in its own section.

| Decision | Where |
|---|---|
| Milestones restructured around an early headless vertical slice. Electron moves from M0 to M2, and route tuning closes M4 | §12, this file |
| Eval: a small set of our own (about 80 dev, 40 held-out, 20 smoke) drawn from Wide World Importers and four BIRD Mini-Dev databases, BIRD `evidence` included, no description CSVs or extra analyses, plus a schema-only control arm | §12, [Eval set](#eval-set) |
| Tuning rounds are 3 runs; compile routes are judged by quota-free checks | §12, [Eval set](#eval-set) |
| `answer_agent` starts at `medium` effort; `high` is a tuning candidate | §6.3 |
| Routes accept full model IDs; eval configs pin them; the cache key includes effort | §6.1, §6.3, §6.4 |
| One shared engine with leases and an idle shutdown, for Electron, MCP shims and the eval runner | §4.4 |
| The golden DAL suite keeps upstream's seed, with upstream's connectors and two service modules vendored as test-only code | §5.4, §11 |
| A question's source database comes from a scope or from entity coverage, and `plan` records it | §8.1 |
| Source executors gain a generic capped `probe` op; the embedder gets a priority queue; BM25 rebuilds swap atomically | §5.6 |
| The spawned `claude` gets a sanitized environment that also drops inherited Claude Code session variables | §6.2 |

Settled by reading upstream at `2a24f5d`, so no spike is needed:
- The only DAL module that reads `frontend` tables is `dal/groups.py` (it reads `frontend."user"`). No other DAL module imports `groups` or `owners`, so the local-owner stub covers it. The other `frontend` readers (`server/chat/*`, `infra/feature_flags.py`) are in code Ontological replaces; any ported stage code that calls `feature_flags` gets a version that reads `app.setting`.
- DAL reads that take `zone_ids` return the whole catalog when it is `None`. Zones need no stub.
- Upstream requires Python `>=3.12,<3.14`, which matches the engine's 3.12.

## Phase 0: spikes

Spikes run in M0, mostly in parallel. Each one leaves behind code that stays (a contract test, a script in `tools/spikes/`, or a module the milestone keeps) and a short result note appended to this file under [Spike results](#spike-results).

| ID | Question | Method | Decision rule | Feeds |
|---|---|---|---|---|
| **S1** | Does the Agent SDK do what the gated agent needs? | With `claude-agent-sdk` (≥ 0.2.178) and the installed Claude Code (2.1.318): an SDK MCP `@tool` that returns an error the model must read and recover from; `output_format` with a JSON schema in the same session as tools; the `init` message's `model`; `ResultMessage.model_usage`; `RateLimitEvent` parsing; `permission_mode` deny-by-default with `allowed_tools`; `setting_sources=[]`; `tools=[]`; `fallback_model`. Run once from a normal shell and once from an environment copied from inside a Claude Code session (`CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, …), as the MCP shim's engine would inherit | Everything works: keep §8.1 as written. `output_format` can't be combined with tools: the final answer becomes a `submit_answer` tool whose arguments are `AnswerModel`, and the post-run check reads it from the tool log. A nested run misbehaves: extend the §6.2 strip list until it doesn't | M0.6, M1.7 |
| **S2** | How long does a spawn take? | Time `query()` for a one-line structured call, 20 times each on `haiku`, `sonnet` and `opus`, cold and back to back | p50 above 1.5 s: add a warm pool of single-use pre-spawned clients to the gateway (§6.4) in M1. Either way, the measured number replaces the guess in §6.5's compile-time estimate | M0.6, M3.10 |
| **S3** | What does one question cost? | After the M1 slice works: 10 smoke questions on the real routes and 10 with the agent on Sonnet. Record the change in `RateLimitEvent.utilization` and the per-model tokens from `model_usage` | Replaces the estimates in [Usage](#usage). If a full dev run on the real routes would use more than about a fifth of a usage window, shrink the dev set before M4 | M1.10, E5 |
| **S4** | How big are trimmed Postgres 17 + pgvector binaries, and can CI build and sign them? | A CI job that builds PostgreSQL 17 from source with a minimal `configure` (no ICU, which locale `C` doesn't need), builds pgvector against it, keeps the §10 file list and measures it. Compare with `@embedded-postgres/darwin-arm64` (337 MB unpacked). Sign with the hardened runtime and confirm that `postgres` can still fork its backends and load `vector.dylib` | A size number goes into §10. If the hardened runtime blocks forking or library loading, find the entitlements that fix it now, not in M7. Notarization needs the user's Apple Developer account; until it exists, test with ad-hoc signing and the same entitlements | M0.3, M7 |
| **S5** | Does the lifecycle in §5.3 work? | A prototype `store/pg.py` against the S4 binaries: first launch, normal stop, `kill -9` of the engine then adopt through `postmaster.pid`, a stale `postmaster.pid` whose pid now belongs to another process, a second engine, a failed migration then restore. For the major-version path, dump a PostgreSQL 16 cluster and restore it into 17 | Each case either passes or names the §5.3 change. The prototype becomes M0.3 | M0.3 |
| **S6** | Do upstream's golden fixtures and DAL tests run against a bare Postgres outside Docker? | A test cluster on the S4 binaries with Pagila loaded as a Postgres database and Chinook as a SQLite file. Run `dev_tools/fixtures/seed_graph_fixture.py --reset` through upstream's own connectors, then `test_golden.py` and the rest of `dal/tests/` with `POSTGRES_*` set. Watch for tests that skip quietly: the golden test skips when `POSTGRES_USER` is unset or the fixture is unseeded | A pass/fail list and the list of vendored files that need edits. Any skip counts as a failure | M2.1 |
| **S7** | Does fastembed load `snowflake-arctic-embed-m-v1.5` int8? How fast is it, and how slow do queries get during bulk embedding? | Load it through fastembed, falling back to the direct `onnxruntime` + `tokenizers` loader (§7.1). Measure texts per second for a 10⁵-text bulk run, and single-query latency with and without a bulk batch in flight | Pick the loader. Set the bulk batch cap so that a query embed waits at most about 100 ms behind a running batch | M1.5, M3.6 |
| **S8** | How long does a `bm25s` rebuild take at 10⁵ documents? | Synthetic catalog texts with identifier-aware tokens (§7.3), timing build and query | Under about 3 s: rebuild on every change, swapping atomically. Slower: rebuild incrementally per database and debounce edits | M3.6 |
| **S9** | Can a Pro plan use Opus in Claude Code? | Check Anthropic's current plan docs. If the docs don't say, record it as unknown; `fallback_model="sonnet"` covers it either way (§6.3) | Documentation only. It matters for other people running the app, not for the author's Ultra plan | §6.3 |

S3 has to wait for M1, so it is the one spike that runs late. The split vs merged quota ratio (§6.3's 1.5–2x estimate) needs real compiles, so it is measured in M3 (M3.10), not here.

## Eval set

The eval is a scoreboard for this project, not a reproduction of anyone's benchmark. It answers two questions: did a change make answers better or worse, and which setting should be the default.

### Questions

| Set | Contents | When it runs |
|---|---|---|
| **Dev** (~80) | All 41 Wide World Importers questions, plus about 40 BIRD Mini-Dev questions: 10 each from `financial`, `california_schools`, `formula_1` and `debit_card_specializing`, sampled to Mini-Dev's own simple / moderate / challenging mix | Tuning rounds; full runs when a default is being set |
| **Held-out** (~40) | 10 further Mini-Dev questions from each of the same four databases | Once per milestone, for the winning configuration only |
| **Smoke** (20) | 10 WWI and 10 BIRD questions taken from dev | Day-to-day development, with `answer_agent` pinned to Sonnet |

Why these four BIRD databases: `financial` and `debit_card_specializing` are transaction data close to what users will point the tool at, `formula_1` has many tables and long join paths, and `california_schools` has column names with spaces and abbreviations that exercise the identifier tokenizer. WWI is a sales and warehouse database with checked gold SQL.

Rules:
- `eval/sets/{dev,heldout,smoke}.json` holds question IDs only, chosen by a script with a fixed seed. Nothing in `eval/sets/` changes once M0 ends unless a question turns out to be broken, and then the change is a commit of its own with the reason.
- The data is downloaded, never committed. BIRD is CC BY-SA 4.0. The WWI DDL and CSVs come from `datasets/wideworldimporters/` in [ontology-sql-eval](https://github.com/NVIDIA/ontology-sql-eval/), MIT-licensed from Microsoft's samples.
- BIRD questions include their `evidence` text, as the benchmark defines them. BIRD's `database_description/*.csv` files and ontology-sql-eval's extra custom analyses are not ingested, so the score measures Ontological's own compile.
- The eval passes each question's database as the scope (§8.1), as the reference harness routes by `db_id`.
- Later (M5): answers the user marks correct on their own data can be saved as eval cases. They form a separate personal set and never replace the frozen ones.

### Scoring and runs

- **Execution accuracy** uses BIRD's rule (compare result sets), ported from the verifier in ontology-sql-eval (Apache-2.0). Gold SQL runs on the source natively: `sqlite3` for BIRD and the eval Postgres cluster for WWI. Ontological's SQL runs through its own source executor.
- **Dialect artifacts.** BIRD's gold SQL runs on SQLite, while Ontological's SQL runs on DuckDB, and the two differ on some semantics (integer division is the common one). The verifier logs every mismatch where the rows agree after numeric normalization; these get one hand review in E4 and a fixed tolerance rule after that.
- **Control arm.** A schema-only baseline: one structured call per question with the question and a schema dump, no tools, on the same model as the agent. It runs once per model per set, not once per configuration.
- **Recorded per run:** execution accuracy, share of answers the gate verified, spawns and turns per question, per-model tokens, the `utilization` change, and the resolved model IDs. Eval configs pin full model IDs (§6.3), so a model release can't land in the middle of a comparison.
- **Variance.** LLM output isn't repeatable, so "reproducible" means one command, pinned data, pinned model IDs and stored results. With about 80 questions only differences of about 10 points stand out from noise. When two configurations land within that range, the cheaper one wins.

### Tuning rounds

A round is 3 full dev runs: the current baseline, the critic off, and `answer_agent` on Sonnet. The first round in M4 adds a fourth run, `answer_agent` at `high` effort (§6.3 starts it at `medium`). After the round, the winner runs once on held-out.

Compile settings (`compile.table_calls`, the compile routes, the embedding model) are not tuned with full eval runs. They are judged by the compile-quality review and the retrieval benchmark (§7.4), which cost no quota once a compile exists. Only a finalist gets a full run.

### Usage

Estimates at API prices, as a way to compare runs. They are not a statement about subscription quota, which Anthropic doesn't publish. S3 replaces them with measured numbers.

| Run | Question-runs | API-equivalent |
|---|---|---|
| Smoke, agent on Sonnet | 20 | ~$5 |
| Dev, real routes | ~80 | ~$40–50 |
| Tuning round (3 dev runs + held-out) | ~280 | ~$170 |
| Control arm, per model | ~120 | ~$10 |

## Milestones

The milestone list and "done when" criteria are in §12. The tasks below are in order within each milestone; tasks without an `after:` can start as soon as the milestone does.

### M0: Spikes and engine skeleton

Produces: a headless engine that starts and adopts its own Postgres, migrates both schemas, makes a structured Claude call with a clean environment, and holds a frozen eval set.

| Task | Work | after |
|---|---|---|
| M0.1 | Repo skeleton per §10: `engine/` (uv, Python 3.12), `apps/desktop/` placeholder, `eval/`, `tools/spikes/`, CI running ruff, pyright and pytest on macOS arm64 | |
| M0.2 | Spikes S1, S2, S4, S5, S6, S7, S8, S9 | M0.1 |
| M0.3 | **TDD** `store/pg.py`: initdb, start, adopt, stop, quit dump, restore, the disk-full pause. The lifecycle test list in §12 written first, against the S4 binaries | S4, S5 |
| M0.4 | Vendor upstream's Alembic chain into `upstream/`; Ontological's chain for `app` and `ops` with its version table in `ops` (§5.2); pre-migration dump and restore on failure | M0.3 |
| M0.5 | **TDD** Engine process: FastAPI on `127.0.0.1` with a random port, bearer token, `engine.json` at mode 0600, leases with idle shutdown (§4.4), headless mode | M0.3 |
| M0.6 | **TDD** `ClaudeGateway.structured()` and `text()`: route table accepting aliases and full IDs, env sanitization (§6.2, with S1's strip list), cache keyed per §6.4 including effort, `ops.llm_call` logging, the retry rule, `GatewayStatus` from `claude --version` and a `ping`. Gateway contract tests behind a flag | S1, S2, M0.4 |
| M0.7 | Eval data: download scripts for BIRD Mini-Dev and WWI; a separate eval Postgres cluster on the bundled binaries that holds WWI | M0.3 |
| M0.8 | Freeze the eval set: the selection script, `eval/sets/*.json`, and a check that each gold query runs and returns rows | M0.7 |

### M1: Thin vertical slice (headless)

Produces: `ontological ask "<question>" --source chinook` and `ontological eval smoke`, end to end, on Chinook, with no UI. The slice is deliberately shallow: every later milestone deepens one stage of it.

| Task | Work | after |
|---|---|---|
| M1.1 | Vendor upstream's DAL, `catalog/store/` and `vdb/postgres.py` with the owner and group stub (§5.4); `zone_ids=None` everywhere | M0.4 |
| M1.2 | Source executor process for DuckDB-attached SQLite: extract, `run_sql`, `probe`, cancel by `interrupt()` then kill after 2 s (§5.6) | M0.5 |
| M1.3 | Minimal ingest: extract and write Chinook's catalog through the DAL (no diff, no query history) | M1.1, M1.2 |
| M1.4 | Minimal split compile: profiling, then `table_keys`, `column_descriptions` and `table_terms` with upstream's prompts, sequential, no packing; semantic FKs from declared FKs only | M0.6, M1.3 |
| M1.5 | Embedder on the S7 loader behind LangChain's `Embeddings` interface; dense search only | S7, M1.1 |
| M1.6 | Grounding pack: dense retrieval over terms and attributes, BFS join paths (`find_join_path`) | M1.4, M1.5 |
| M1.7 | **TDD** Gated agent: `ClaudeGateway.agent()`, the tools `plan`, `search_ontology`, `describe_table`, `validate_sql` (gate checks 1–3 only), `run_sql` and `review_answer`, their preconditions, and the post-run check with its intent, critic-off and `answerable=false` variants. Precondition tests written first | S1, M1.6 |
| M1.8 | `ontological ask` CLI against the engine API, printing steps, SQL and rows | M1.7 |
| M1.9 | Eval runner: the verifier, the control arm, result storage, and a run report | M0.8, M1.8 |
| M1.10 | Spike S3 on the smoke set; first smoke score recorded under [Spike results](#spike-results) | M1.9 |

The smoke score in M1 only needs to exist. It will be low, because BIRD and WWI need M2's Postgres source and M3's full compile.

### M2: Catalog depth and the Electron shell

Produces: every v1 source type, a correct and idempotent catalog, the golden DAL suite as a conformance gate, and an Electron app that onboards the user and shows the catalog.

| Task | Work | after |
|---|---|---|
| M2.1 | **TDD** Golden DAL suite in CI: test-only vendored connectors and service modules (§11), the Pagila and Chinook test cluster, no skips allowed | S6, M1.1 |
| M2.2 | Source executors for DuckDB files, Parquet/CSV/JSON, SQLite, and Postgres/MySQL through DuckDB's scanners; complete `get_pks`, `get_fks` and `table_type` for DuckDB (§5.5); keychain references for credentials | M1.2 |
| M2.3 | **TDD** Extract, normalize and diff, porting upstream's catalog diff tests first; re-ingest idempotence test | M2.1, M2.2 |
| M2.4 | Parse pool for query history; the month-counter fix (§1.3); a responsiveness test that times API requests during a large history ingest | M2.3 |
| M2.5 | Full profiling and date-format inference inside the executor; data-layer texts built after profiling (§7.1) | M2.2 |
| M2.6 | Staleness check on engine start (§9.1) | M2.3 |
| M2.7 | Electron shell: main process, engine supervisor that attaches to a running engine or starts one and takes a lease (§4.4), preload, CSP | M0.5 |
| M2.8 | Onboarding screen (Claude Code detection, ignored API-key variables, first source) and catalog screen | M2.7, M2.3 |
| M2.9 | WWI ingested as a Postgres source through the scanner; BIRD's four databases through SQLite attach | M2.2, M0.7 |

M2.7 and M2.8 can run in parallel with M2.1–M2.6 once the engine API from M1 holds steady.

### M3: Compile depth

Produces: the full compile on every eval database, hybrid retrieval, the retrieval benchmark, and the measured answers to split vs merged and the embedding model.

| Task | Work | after |
|---|---|---|
| M3.1 | **TDD** Per-table checkpoints in `ops.job_step`, pause until `resets_at`, resume; deterministic packing order so small-table packs stay stable between runs | M1.4 |
| M3.2 | Full split stages with packing and chunking past 40 columns; `table_synonyms`; the certified-description guard (§1.3) | M3.1 |
| M3.3 | Merged `TableSemantics` option (§6.4) | M3.2 |
| M3.4 | Semantic FK resolution in batches of about 25, with the value-overlap probe running through the executor's `probe` op | M3.2, M2.2 |
| M3.5 | SQL attribute judge with `_usage_score` fixed; bridge SQL on Opus; PII rules and the batched fallback | M3.2, M2.4 |
| M3.6 | Hybrid search: BM25 with identifier-aware tokens (S8's rebuild policy), trigram, RRF with the certified bonus; embedder priority queue (S7) | S7, S8, M1.5 |
| M3.7 | Pre-compile estimate showing split and merged (§6.5), using S2's spawn time | M3.3 |
| M3.8 | Jobs screen: progress, pause and resume, call log, cache hits | M3.1, M2.7 |
| M3.9 | Retrieval benchmark (§7.4) on dev questions: recall@k per configuration and per candidate model; choose the embedding model and recalibrate thresholds | M3.6, M2.9 |
| M3.10 | Compile-quality review on Pagila and WWI: hand-reviewed sample of terms, FKs and descriptions, split vs merged, with measured quota and time. Compile routes one step cheaper are judged the same way | M3.3, M3.4, M3.5 |

### M4: Answer depth and tuning

Produces: the full answer path, the chat screen, and defaults set from measured scores.

| Task | Work | after |
|---|---|---|
| M4.1 | **TDD** Gate checks 4–7 (§8.2), porting `test_literal_check`, `test_case_dirty_join_check` and `test_jsonb_path_check` first, plus the DuckDB JSON variant | M1.7 |
| M4.2 | Probe cap and statement timeouts; empty-result handling in `run_sql` | M4.1 |
| M4.3 | Full tool set (`get_term`, `join_path`, `column_values`), per-entity retrieval in `plan`, source choice by coverage when there is no scope, the `information` and `prediction` intents, the one restart | M4.1, M3.6 |
| M4.4 | `check_answerable` with the recalibrated threshold | M3.9 |
| M4.5 | SSE event stream with re-attach and cancel (§8.4) | M4.3 |
| M4.6 | Chat screen: steps, validated SQL, rows and CSV, ResultChart port, usage meter, source selector | M4.5, M2.7 |
| M4.7 | First tuning round (four runs, see [Tuning rounds](#tuning-rounds)), then held-out; set the defaults in §6.3 and record the scores here | M4.3, M3.9, M3.10 |

### M5: Curation

Produces: editing that changes answers, with history and undo.

| Task | Work | after |
|---|---|---|
| M5.1 | **TDD** Edit functions that write `ops.change_log` and re-embed; undo | M3.6 |
| M5.2 | Terms and Ontology screens: editing, certification, term rename, path highlight | M5.1, M2.7 |
| M5.3 | Save as analysis from chat; saved analyses retrieved as worked examples | M4.6 |
| M5.4 | Personal eval cases from confirmed answers, run with the same runner as a separate set | M5.3, M1.9 |

### M6: Interop

| Task | Work | after |
|---|---|---|
| M6.1 | `ontological-mcp` stdio shim: answers `initialize` at once, attaches to or starts an engine lazily, takes a lease (§9.2) | M0.5 |
| M6.2 | Upstream's curated MCP tools plus `validate_sql` and `run_validated_sql` | M6.1, M4.3 |
| M6.3 | Claude Code plugin with the query, manage and setup skills | M6.2 |
| M6.4 | Native YAML and Ossie import/export with synonyms and certification; import in one transaction, re-embed as a job | M5.1 |

### M7: Package

| Task | Work | after |
|---|---|---|
| M7.1 | Engine runtime build (python-build-standalone or PyInstaller one-dir) including onnxruntime, psycopg and duckdb | M6 |
| M7.2 | Bundled Postgres from S4's CI job, DuckDB extensions with autoinstall off, the embedding model | S4, M3.9 |
| M7.3 | Signing and notarization with the user's Apple Developer account | M7.1, M7.2 |
| M7.4 | Clean-machine install test with only Claude Code installed, offline start, simulated major-version restore | M7.3 |

## Test-first work

These are written test-first because the tests define the behaviour and already exist upstream or follow directly from the architecture:

- The Postgres lifecycle (M0.3) and engine leases (M0.5), where each failure case is easy to state and hard to debug after the fact.
- Gateway env sanitization and the cache key (M0.6). A stray API key would bill the user's API account silently.
- Agent preconditions and the post-run check (M1.7), which make the gated agent trustworthy.
- The golden DAL suite (M2.1) and catalog diff (M2.3), which already exist upstream.
- Gate checks 4–7 (M4.1), which already have upstream tests.
- Compile checkpoints and packing order (M3.1), and curation history and undo (M5.1).

Prompt content, retrieval tuning and UI are built against the eval and the retrieval benchmark instead.

## Spike results

Each spike appends its result here: the date, what ran, the numbers, the decision, and the ARCHITECTURE.md section it changed.

### S1: Agent SDK features (2026-11-04)

**Ran:** `python3 tools/spikes/s1_agent_sdk.py` with `claude-agent-sdk` 0.2.178 and Claude Code 2.1.318, on `haiku` at `low` effort. It runs the contract tests in `engine/tests/contract/test_agent_sdk.py` (behind `ONTOLOGICAL_CONTRACT=1`) twice. The first run uses a fresh login-shell environment. The second uses the environment inherited inside a Claude Code session, which had `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT` and ten other session variables set. Each run makes 5 spawns and costs about $0.003 at API prices.

**Result:** all 12 checks pass in both environments, and the two runs' init messages, tool logs and results are the same.

| Behaviour | Result |
|---|---|
| SDK MCP `@tool` error | A tool returning `is_error` reaches the model as a readable error. In a `plan`/`run` miniature of §8.1, `run` refused an unknown `plan_id`; the model called `plan` and retried with the id it got. Haiku recovered only when the system prompt said tools may refuse and name the missing step. Without that line it gave up (one run) |
| `output_format` with tools | Works in the same session. Claude Code implements it as a `StructuredOutput` tool, which shows in the init `tools` list, and the validated object arrives in `ResultMessage.structured_output` |
| init `model` | The alias resolves to the concrete ID (`haiku` → `claude-haiku-5-5`) |
| `ResultMessage.model_usage` | Keyed by concrete model ID, with tokens, thinking tokens and `costUSD`. It can list more than the session's model: an `opus` probe also had a `claude-haiku-5-5` entry from Claude Code's own side call |
| `RateLimitEvent` | Arrives at the start of every session and parses. `utilization` is `None`, because the CLI sends it per window under `raw["unifiedWindows"]`. `rate_limit_type` was `seven_day` |
| `permission_mode="dontAsk"` + `allowed_tools` | An SDK tool outside `allowed_tools` is denied without prompting, and its handler never runs. The denial shows as a `system`/`permission_denied` message and in `ResultMessage.permission_denials`. The model is told and carries on |
| `setting_sources=[]` | The user's plugins, MCP servers and hooks don't load. Claude Code's four built-in plugins (`cc-plugin-*@builtin`) and its bundled skills still appear in the init message. With `tools=[]` there is no `Skill` tool to invoke them |
| `tools=[]` | No built-in tools. Only our MCP tools and `StructuredOutput` are in the init `tools` list |
| `fallback_model` | With an unknown model, the session answers on the fallback and emits `system`/`model_fallback` with `trigger: "model_not_found"`. The overload trigger couldn't be provoked |

**Also found:**
- `options.env` is merged over `os.environ`. It can override a key but can't remove one. An inherited bogus `ANTHROPIC_API_KEY` was used (`apiKeySource: "ANTHROPIC_API_KEY"`, auth failed); with `env={"ANTHROPIC_API_KEY": ""}` the run used the subscription login (`apiKeySource: "none"`).
- The SDK already drops `CLAUDECODE` and sets `CLAUDE_CODE_ENTRYPOINT=sdk-py` for its child. The other inherited session variables (`CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_CHILD_SESSION`, `CLAUDE_CODE_SESSION_ATTENDED`, `CLAUDE_CODE_MESSAGING_SOCKET`, `CLAUDE_CODE_MESSAGING_TOKEN`, `CLAUDE_CODE_EXECPATH`, `CLAUDE_CODE_AUTO_CONNECT_IDE`, `CLAUDE_CODE_ENABLE_TODO_TOOLS`, `CLAUDE_PID`, `CLAUDE_EFFORT`) all reach it, and the `claude` binary reads every one. None changed anything the tests can see.
- `query()` raises `ResultError` after yielding an error `ResultMessage` (for example, a failed login). The gateway has to catch it to return `None`.

**Decision:**
- Everything works, so §8.1 stays as written: `output_format` carries `AnswerModel`, and there is no `submit_answer` tool.
- No nested run misbehaved. §6.2 still names the full list of session variables found, so M0.6 strips a known set rather than "the others S1 finds". It also says how to strip them, since `options.env` can't remove a key: the engine deletes them from its own `os.environ` at startup, and each call also blanks the four auth variables in `options.env`.
- §6.3 now says to record the answering model from `AssistantMessage.model` (and the `model_fallback` message), not from `model_usage`.
- §6.5 now says to read utilization from `raw["unifiedWindows"]`.
- For M1.7: the answer agent's system prompt says that tools can refuse and name the missing step.

### S2: How long does a spawn take? (2026-11-04)

**What ran.** `tools/spikes/s2_spawn_time.py` on macOS arm64, with Claude Code 2.1.318 and `claude-agent-sdk` 0.2.178. It made 20 calls each on `haiku`, `sonnet` and `opus`, back to back, one fresh `query()` per call, so each started a new `claude` process. The call was one line, "Classify the column `customer_email` as one of: identifier, contact, measure, date.", with a one-field JSON schema, the §6.2 options and `effort="low"`. All 60 calls returned valid `structured_output`.

The script splits each call into three timings:

- **spawn**, from calling `query()` to the CLI's `init` message. This covers the SDK's `claude -v` check, starting the process and the initialize handshake.
- **the CLI's turn**, `ResultMessage.duration_ms`.
- **close**, from the `ResultMessage` until the process exits and `query()` returns.

Overhead is spawn plus close. `duration_api_ms` can't be used to work out overhead. It counts API requests the CLI sends while it is still starting up, so it often comes out longer than `duration_ms`.

| Model | `query()` p50 / p95 | spawn p50 / p95 | close p50 / p95 | overhead p50 / p95 | CLI turn p50 / p95 |
|---|---|---|---|---|---|
| haiku | 1.87 / 3.17 s | 0.17 / 0.21 s | 0.34 / 0.44 s | 0.53 / 0.61 s | 1.36 / 2.62 s |
| sonnet | 1.99 / 2.26 s | 0.17 / 0.25 s | 0.33 / 0.40 s | 0.53 / 0.58 s | 1.45 / 1.78 s |
| opus | 3.02 / 5.64 s | 0.18 / 0.23 s | 0.33 / 0.43 s | 0.55 / 0.62 s | 2.50 / 5.24 s |
| all 60 | 2.09 / 4.08 s | 0.17 / 0.23 s | 0.34 / 0.43 s | 0.53 / 0.62 s | 1.57 / 3.49 s |

The first call on each model was not much slower. Spawn took 0.35 s on the first `sonnet` and `opus` calls, against 0.17 s after that. Whole `query()` time varies with the model, mostly on `opus`, where one call took 8.2 s. Overhead hardly varies.

**Decision: no warm pool.** The rule asks whether a spawn takes more than 1.5 s at p50. It takes 0.53 s, spawn and close together. The whole `query()` is above 1.5 s at p50 (2.09 s), but that is the model's time, and a warm pool can't remove it. A pre-spawned client would save the 0.17 s spawn. The 0.34 s close could also be saved without a pool, by not waiting for the process to exit. Neither is worth the pool's complexity in M1.

**ARCHITECTURE.md changed.** §6.5's estimate said each call costs "a few seconds of process startup" and that split might add 20–30 minutes. It now gives 0.53 s of overhead per call, about 1.5–2 minutes of overhead for split's extra calls on a 200-table database. §3's per-call cost note now says the same.

### S8: How long does a `bm25s` rebuild take at 10⁵ documents? (2026-11-04)

**What ran.** `tools/spikes/s8_bm25s_rebuild.py` with `bm25s` 0.3.16, numpy 2.5.4 and Python 3.12.13, on an Apple M5 Max. It ran twice with the defaults: 10⁵ synthetic table and column documents over 10 databases, 5 repeats, and 1,000 queries at k=50. Each document is name + synonyms + description, as §7.3 indexes them. Identifiers come in snake_case, camelCase, PascalCase and UPPER_SNAKE, with glossary abbreviations and acronyms in them, and descriptions run one to three sentences with a long tail of made-up words. That gives 1.84 M tokens, 18 tokens per document at p50 (29 at p95), a 22,128-token vocabulary and a 14 MB index.

The tokenizer is the §7.3 one: split on snake_case and camelCase, lowercase, expand glossary acronyms, drop stopwords, with the same function on queries. Planted documents check it: `cust_acct_no` ranks first for "customer account number" and for `cust_acct_no`, `ZorblaxInvoiceLineID` for "zorblax invoice line id", and `QUUXTREL_SKU_QTY` for "quuxtrel SKU qty" and "quuxtrel stock keeping unit quantity".

| Step | Run 1 p50 / max | Run 2 p50 / max |
|---|---|---|
| tokenize 10⁵ texts | 0.80 / 0.87 s | 0.82 / 0.96 s |
| `BM25.index` on the tokens | 0.89 / 0.93 s | 0.76 / 1.24 s |
| **full rebuild** (tokenize + index) | **1.73 / 1.75 s** | **1.49 / 2.38 s** |
| one database's rebuild (~10⁴ docs) | 0.16 / 0.22 s | 0.17 / 0.31 s |
| query, k=50 | 0.40 / 1.19 ms | 0.36 / 0.83 ms |
| query while a full rebuild runs on a thread | 0.57 ms p50, 24 ms p95, 111 ms max | 0.76 ms p50, 18 ms p95, 83 ms max |

Run 2's maxima came while other sessions were busy on the same machine. Searches slow down during a rebuild because they share the GIL with it. They stay correct, because the new index replaces the old one by a single reference assignment. In both runs, every search during the rebuild used the old index and none saw a partly built one.

**Decision: rebuild on every change and swap atomically.** A full rebuild takes 1.5–1.7 s at p50 and at most 2.4 s, under the rule's 3 s. Per-database indexes and debounced edits aren't needed. Searches cost under a millisecond, and up to about 0.1 s while a rebuild is running, which is small next to a tool call's turn. For M3.6:
- Run at most one rebuild at a time. A change that arrives during a rebuild marks the index stale, and one more rebuild runs after the current one, so a burst of compile writes costs two rebuilds, not one per write.
- Cache each document's tokens by `content_hash`, so a rebuild tokenizes only changed documents. That removes about half of a rebuild (the tokenize row).
- The numbers come from one fast machine. A slower Mac is the main risk to the 3 s margin. If a full rebuild there goes past about 3 s even with cached tokens, fall back to the per-database indexes measured above.

**ARCHITECTURE.md changed.** §7.3 said a rebuild "should take about a second". It now gives the measured 1.5–1.7 s and the policy above.

### S9: Can a Pro plan use Opus in Claude Code? (2026-11-04)

**What ran.** A read of Anthropic's plan docs on 2026-11-04.

**Answer: yes, Opus is part of the Pro plan in Claude Code.** Two pages say so:

- [Claude Code model configuration](https://code.claude.com/docs/en/model-config), "`default` model setting": "**Pro, Max, Ultra, Team, Enterprise, and Anthropic API**: defaults to Opus 5.5".
- [claude.com/pricing](https://claude.com/pricing), plan comparison: the Opus row reads No / Yes / Yes / Yes for Free / Pro / Max / Ultra, and Claude Code is included from Pro up.

One exception: the same model configuration page, under "Extended context", says Pro needs usage credits for Opus 4.6 with 1M context. Ontological doesn't route to `[1m]` variants. The [Pro plan article](https://support.claude.com/en/articles/8325606-what-is-the-pro-plan) and [Using Claude Code with your Pro or Max plan](https://support.claude.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan) don't mention Opus. They say Pro's five-hour and weekly limits apply across all models. Some third-party posts from earlier in 2026 say Pro needs extra usage for Opus. Anthropic's current docs don't say that.

**Decision: no change.** Ontological is for the author's own use on an Ultra plan, so Pro access doesn't affect the build. `fallback_model="sonnet"` stays on `opus` routes for overloaded Opus (§6.3).

**ARCHITECTURE.md changed.** Nothing.
