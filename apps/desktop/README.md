# Desktop app

Ontological's Electron app: electron-vite, React 19, TypeScript and Tailwind ([ARCHITECTURE.md §9.1](../../docs/ARCHITECTURE.md#91-electron-ui)). It is the shell from [DESIGN.md §3](../../docs/DESIGN.md#3-window-and-shell): the window, sidebar, header, status bar, menus, theme and the typed preload bridge, plus the engine supervisor ([ARCHITECTURE.md §4.4](../../docs/ARCHITECTURE.md#44-engine-lifetime)). Each screen is an empty frame for now.

## Commands

Run these from `apps/desktop/`, with Node 24 and pnpm 11. The end-to-end tests also need the engine's virtualenv: run `uv sync` in `engine/` first.

| Command | What it does |
|---|---|
| `pnpm install` | Installs dependencies and downloads Electron |
| `pnpm dev` | Runs the app with hot reload |
| `pnpm start` | Builds and runs the app as it ships |
| `pnpm typecheck` | Type-checks main, preload, renderer and tests |
| `pnpm test:unit` | Vitest: window state, IPC argument checks, the menu template, the engine supervisor, the CSP and the `app://` protocol |
| `pnpm test:e2e` | Builds, then drives the app with Playwright |
| `pnpm test` | All of the above |

## Layout

- `src/main/`: the window and its saved state (`window-state.ts`), the menu bar (`menu.ts`), the IPC handlers with their argument checks (`validate.ts`), the renderer's `app://ontological` protocol (`app-protocol.ts`) and its CSP (`csp.ts`), and the engine supervisor (`engine/`).
- `src/preload/`: the bridge the renderer sees as `window.ontological`. Its type is `OntologicalApi` in `src/shared/ipc.ts`.
- `src/shared/`: the screens, themes and IPC contract both sides build against.
- `src/renderer/`: the React app. Colours are the DESIGN.md §2.1 tokens in `styles.css`; Geist and Geist Mono are bundled in `assets/fonts/`.

## The engine

At launch, main attaches to the engine `engine.json` names, or starts one, and takes a lease; quitting releases it and leaves the engine to idle out. The preload hands the renderer `engine()` and `onEngine()`, whose state carries `{engineUrl, token}` while the engine runs, and the status bar shows it.

From the repo, the app runs the engine with `uv run --project ../../engine ontological-engine`, which needs the bundled Postgres: set `ONTOLOGICAL_PG_BIN` to a `bin/` folder from `tools/spikes/s4_postgres` before `pnpm dev`. Without it the status bar says the engine failed to start, and its tooltip says why. `ONTOLOGICAL_ENGINE_CMD`, a JSON array, replaces the command; the end-to-end tests use it to run the engine with a fake store (`engine/tests/engine/serve_stub.py`). The engine's log is `logs/engine.log` in the userData folder, which Help → Open logs folder opens.

Every shortcut is a menu accelerator in `src/main/menu.ts`; the renderer has no global key listeners. The window's size, position, open screen and theme are kept in `shell-state.json` in the app's userData folder. Set `ONTOLOGICAL_USER_DATA` to use another folder, as the end-to-end tests do.
