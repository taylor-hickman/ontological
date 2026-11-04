# Ontological: Design

This is the UI design for the Electron app described in [ARCHITECTURE.md §9.1](ARCHITECTURE.md#91-electron-ui). It sets the visual tokens, the window and shell, the components, the rules for each screen, and the states every screen has to handle.

The mockups are in [design/mockups/](design/mockups/), one standalone HTML file per screen: [Ask](design/mockups/ask.html), [Ontology](design/mockups/ontology.html), [Jobs](design/mockups/jobs.html) and [Onboarding](design/mockups/onboarding.html). Open them in a browser; the sidebar links between them work. They are exported from the design canvas at <https://claude.ai/artifact/ExT8CdtKgUVKwi9YmquTat> (private to the owner), which is where they get edited. Where this file and the mockups disagree, this file wins.

---

## 1. Principles

1. **Show the proof.** Every answer shows how it was checked: the plan, the gate, the run and the review (§8.1). Something unverified looks unverified. The UI never shows SQL as validated unless `validate_sql` passed it.
2. **Cost before action.** Anything that spends Claude quota says so before it runs, with an estimate. Anything free says it is free ("reads table structure, no Claude usage").
3. **Quota is always visible.** The status bar shows the rate-limit status and reset time on every screen. A paused job says when it resumes, as a clock time.
4. **Say what leaves the machine.** Source data stays local. The app says plainly that prompts go to Claude through the user's own Claude Code login, and nothing else.
5. **Curation changes answers.** When a user edits or certifies something, the UI says the next answer will use it. That is the reason to curate.
6. **Keyboard first, mouse friendly.** Every screen and main action has a shortcut that is also a menu item. Every click area is at least 44 px.

---

## 2. Tokens

Colors are CSS custom properties on `:root`, redefined under `@media (prefers-color-scheme: dark)` and under `[data-theme="dark"]` / `[data-theme="light"]` for the Settings override. Components use the tokens, never raw hex.

### 2.1 Color

| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` | `#F4F5F7` | `#111318` | Window background, main column |
| `--chrome` | `#EBEDF1` | `#16191F` | Sidebar, status bar |
| `--surface` | `#FFFFFF` | `#1B1F26` | Cards, inputs, side panels |
| `--surface-sunken` | `#F8F9FA` | `#161A20` | Card headers, code backgrounds, tab strips |
| `--rail` | `#FBFBFC` | `#181B21` | Right-hand provenance and log rails |
| `--border` | `#DCE0E6` | `#2A2F38` | Card and control borders |
| `--border-subtle` | `#E6E9EE` | `#232830` | Dividers inside cards |
| `--text` | `#151821` | `#ECEEF2` | Body text, headings |
| `--text-2` | `#3D4350` | `#C3C8D1` | Secondary text, inactive nav |
| `--text-muted` | `#5F6675` | `#9AA1AE` | Captions, timestamps, shortcut hints |
| `--text-faint` | `#A3A9B4` | `#5C6270` | Decoration only (separators, dashed borders). Never text |
| `--accent` | `#3446D4` | `#9AA6F7` | Links, selection, chart primary, focus ring |
| `--accent-solid` | `#3446D4` | `#4757E0` | Primary button fill, with white text |
| `--accent-hover` | `#2433A8` | `#B9C1FA` | Link hover |
| `--accent-soft` | `#E8EBFC` | `#232A4D` | Term chips, selected backgrounds |
| `--accent-border` | `#C9CFF5` | `#353F78` | Borders on accent-soft fills |
| `--accent-muted` | `#8A96EA` | `#5A66C8` | Secondary chart bars, join-path connectors |
| `--verified` | `#0A7259` | `#4CC49E` | Check icons, certified marks |
| `--verified-text` | `#0A5C48` | `#6FD6B4` | Text on `--verified-soft` |
| `--verified-soft` | `#E3F4EE` | `#12322A` | Verification chain steps, "Ready" badges |
| `--verified-border` | `#A9D9C8` | `#1F5A49` | Chain connectors, certify button border |
| `--attention` | `#9A4A00` | `#F2A65A` | Quota, paused, uncertified, ignored-variable notes |
| `--attention-text` | `#5C2D00` | `#FFD3A3` | Text on `--attention-soft` |
| `--attention-soft` | `#FDF0E2` | `#33240F` | Banners, assumption notes |
| `--attention-border` | `#F2D2AE` | `#5A3B14` | Banner borders |
| `--meter-warn` | `#C26A12` | `#D98A3A` | Usage meter at `allowed_warning`, checkpointed stages |
| `--queued` | `#E3E6EB` | `#2A2F38` | Queued stages, empty meter track |

Every text pairing above passes 4.5:1 (checked: muted on chrome is 4.9:1 light and 6.8:1 dark; white on `--accent-solid` is 7.1:1 light and 5.7:1 dark).

**Meaning rules.** These are what keep the screens readable at a glance.
- Green means verified or certified, and nothing else. Never use it for "success" in general, such as a saved toast.
- Amber means the user should look: quota, a pause, an assumption, something uncertified, or an ignored setting. It never means an error by itself.
- The accent means "you can act on this" or "this is selected."
- Errors use `--text` with an icon and a sentence, not red. The product has few true errors, and red next to green fails for red-green colour blindness. If a red token turns out to be needed, it must differ from `--verified` in lightness.
- Status never depends on color alone. Each state also has an icon, a border style or a word.

### 2.2 Type

- **Geist** for UI and **Geist Mono** for SQL, identifiers, table and column names, and times in logs. Both are under the SIL Open Font License. **Bundle the woff2 files** with the renderer and list them in `THIRD_PARTY_NOTICES.md`. The renderer's CSP (§4.1) blocks Google Fonts, and the app must work offline.
- Fallbacks: `system-ui, sans-serif` and `ui-monospace, monospace`.

| Role | Size / weight | Notes |
|---|---|---|
| Question (Ask) | 21 px / 500 | The user's question reads as the heading of the turn |
| Screen heading in a panel | 22 px / 600 | Term name, onboarding step title |
| Card title | 15–18 px / 600 | |
| Body | 14 px / 400, line-height 1.45 | Answer prose is 15 px with line-height 1.6 |
| Caption, meta | 12–13 px | `--text-muted` |
| Section label | 12 px / 600, uppercase, 0.06em tracking | "JOIN PATH", "RECENT" |
| Code | 12.5 px mono, line-height 1.65 | Identifiers inline at 12 px |

Use tabular numerals for figures that line up (`font-variant-numeric: tabular-nums`).

### 2.3 Space, radius, elevation

- Spacing steps: 4, 6, 8, 10, 12, 14, 16, 20, 24, 28, 32. Groups use flex or grid `gap`, not margins.
- Radius: 4 (kbd hints), 6–8 (controls, small cards), 10 (inner cards, tiles), 12–14 (top-level cards, composer), 999 (pills, chips, graph nodes).
- Elevation: the default is a flat card with a border. Shadows are only for things that float: the composer (`0 6px 20px rgba(20,24,33,.06)`), the active onboarding step, and graph overlays. Dark mode drops shadows and relies on borders.

---

## 3. Window and shell

### 3.1 BrowserWindow

- `titleBarStyle: 'hiddenInset'` on macOS, with `trafficLightPosition` aligned to the vertical centre of the 52 px top bar. Windows and Linux use `titleBarOverlay` with `--chrome` colors when those platforms ship.
- `backgroundColor` set from `--bg` for the current theme, and `show: false` until `ready-to-show`, so the window never flashes white.
- `minWidth: 960`, `minHeight: 640`. Remember size, position and the open screen between launches.
- Security settings follow §4.1: `contextIsolation: true`, `sandbox: true`, `nodeIntegration: false`, and the CSP.

### 3.2 Drag regions

- The top 52 px of the sidebar and the screen header are `-webkit-app-region: drag`.
- Every control inside them is `no-drag`: buttons, the search field, the layer switch and the path picker.
- Never mark a scrolling area or a text selection area as draggable.

### 3.3 Layout

```
┌────────────┬──────────────────────────────────────────────┐
│ ● ● ●      │ Header: breadcrumb · title    [⌘K search] [+] │  52 px, draggable
│ Ontological├──────────────────────────────┬───────────────┤
│ [source ▾] │                              │               │
│ Ask     ⌘1 │  Main column                 │  Right rail   │
│ Ontology ⌘2│  (max 860 px of reading      │  (300–360 px, │
│ Terms   ⌘3 │   width on Ask)              │   provenance, │
│ Catalog ⌘4 │                              │   editor or   │
│ Analyses⌘5 │                              │   log)        │
│ Jobs    ⌘6 │                              │               │
│ Recent…    │                              │               │
│ Settings ⌘,│                              │               │
├────────────┴──────────────────────────────┴───────────────┤
│ ● Engine running · Claude Code 2.1.x · logged in   … Usage ▮▮▯ 71% · resets 4:10 PM │  28 px
└───────────────────────────────────────────────────────────┘
```

- **Sidebar**, 232 px. Window controls, the wordmark, the source switcher, screen navigation with shortcuts, then context for the screen: Recent conversations on Ask, the certification meter on Ontology. Jobs shows a badge while a job is running or paused (`31/48`, `Paused`).
- **Header.** Breadcrumb and title on the left, search (⌘K) and the screen's one primary action on the right.
- **Right rail.** Optional on each screen. It holds detail for whatever is selected or answered. ⌘\ toggles it. Below 1100 px of window width it becomes a sheet that slides over the main column.
- **Status bar.** Always present. Section 4.6 covers it.
- Layout uses flex-wrap, so the rails stack under the main column in a narrow window instead of clipping.

### 3.4 Menus, dialogs and the OS

- **Every shortcut is a menu accelerator.** Then it shows in the menu bar and works wherever focus is. No global `keydown` listeners in the renderer.
- Menu bar: **Ontological** (About, Settings ⌘,, Quit), **File** (New question ⌘N, Add source… ⌘O, Import…, Export…), **Edit** (standard roles), **View** (the six screens ⌘1–⌘6, Toggle right rail ⌘\, Theme), **Window**, **Help** (Claude Code login help, Open logs folder).
- File pickers, save dialogs and context menus are native, opened by the main process.
- **Notifications.** Send a system notification only when the window is not focused and a long job finishes, pauses for quota, or resumes. Nothing else.
- **Dock badge.** Shows a dot while a compile is paused for quota.
- **Closing and quitting.** On macOS, closing the window keeps the app running, as usual. Quitting does not stop the engine (§4.4). If a job is running or paused when the user quits, the quit goes ahead, and the next launch's Jobs screen shows where the job got to. Don't block quit with a dialog.

**Architecture change this needs.** §10 says the preload exposes `{engineUrl, token}` only. Native dialogs, context menus and notifications need a small typed IPC addition: `openFile(filters)`, `saveFile(name)`, `showContextMenu(items)` and `notify(title, body)`. Each handler in main validates its arguments. Update ARCHITECTURE.md §10 when M2.7 lands.

### 3.5 Theme

Follow the system through `prefers-color-scheme` by default. Settings → Appearance offers System, Light and Dark; it sets `nativeTheme.themeSource` in main and `data-theme` on the root, so native menus and dialogs match.

---

## 4. Components

### 4.1 Verification chain

The signature element. It sits at the top of every answer.

- Four pill steps joined by short connectors: **Plan** (intent or subtype), **Validate** (repair count), **Run** (time), **Review** (critic result).
- It mirrors the engine's post-run check (§8.1). A step that ran is `--verified-soft` with a filled check. A step that is not required for this answer (`information` intent, critic off) is left out, not greyed. A step that was required and missing is an outlined amber pill with the reason. In that case the answer header says "Unverified" and the SQL block loses its "Validated" label.
- While streaming, the current step shows a spinner and later steps are outlined in `--queued`. Each step expands to its `step` events from the SSE stream (§8.4).
- To the right: the model that actually ran, the effort level and the elapsed time ("Opus 5.5 · medium · 19 s"). If the Opus fallback to Sonnet fired, say so here.

### 4.2 Term link

Terms named in answer prose (from `terms_used`) get a 1.5 px dotted `--accent` underline and stay in `--text` color. Hover shows the definition and certification. Click opens the term on the Ontology screen. Only real ontology terms get the underline; this is not a general link style.

### 4.3 Assumption note

One amber line per entry in `assumptions[]`: a bold "Assumed" label, the sentence, and a "Change" link that puts a follow-up into the composer. This is how the critic's interpretation choices become visible.

### 4.4 Result card and validated SQL

- **Result card.** A tab strip (Chart, Rows · n) and a CSV action. Charts follow upstream's ResultChart spec (§9.1) and use `--accent` for the leading series and `--accent-muted` for the rest. A single-row result shows as a KPI. Every chart has an `aria-label` that states the values.
- **SQL block.** Header: shield icon and "Validated SQL" in `--verified-text`, then dialect, "read-only" and "LIMIT applied", then Copy and "Open in editor". The body is mono with syntax colors: keywords in `--accent`, literals in `--attention`. The block is always visible, never behind a tab, because the SQL is the thing being trusted.

### 4.5 Provenance rail (Ask)

- **Join path.** The tables in join order, as stacked boxes: table name in mono, its term as a chip, the join column on the connector between boxes. Junction tables have a dashed border and the word "junction". Certified terms carry a check.
- **Gate checks.** One line per §8.2 check that ran, each with a check icon and a short count ("4 of 4", "none used").
- **Repairs.** One sentence per repair the session made, in plain words.
- **Step timeline.** A link to the full tool log, with its call count.

### 4.6 Status bar and usage meter

Left side: engine state dot and label, then Claude Code version and login state. Right side: the most important background job as a link to Jobs, then the usage meter.

The meter follows `RateLimitEvent.status` (§6.5), not a percentage of its own.

| Status | Bar | Label |
|---|---|---|
| `allowed` | `--text-muted` fill | "Usage 34%" |
| `allowed_warning` | `--meter-warn` fill | "71% · resets 4:10 PM", and the job link says "slowed to 1 at a time" |
| `rejected` | full, `--attention` | "Limit reached · resets 4:10 PM" |
| unknown (no event yet) | empty track | "Usage: no data yet" |

The meter is `role="meter"` with `aria-valuenow`. Reset times are clock times in the user's locale. Show a countdown only inside banners ("in 38 min").

### 4.7 Banner

A full-width amber card for a state that blocks or pauses work: a round icon, a bold first line saying what happened, a second line saying what happens next, and at most one action. `role="status"`. Example: "Paused: your Claude usage limit is reached. Compile picks up by itself at 4:10 PM, in 38 min. The 31 finished tables are saved." Action: "Don't resume."

### 4.8 Table tile (Jobs)

A mono table name over a four-segment bar, one segment per split stage: keys, descriptions, terms, synonyms. Merged compiles show one segment. Segment colors: `--accent` done, `--meter-warn` checkpointed mid-table, `--queued` waiting. Tiles sit in an auto-fill grid, minimum 150 px wide. Each bar has an `aria-label` such as "2 of 4 stages done".

### 4.9 Graph node (Ontology)

A pill 128 px wide and at least 44 px tall, which makes it a real button.

| State | Look |
|---|---|
| Certified | 1 px solid border, green check before the name |
| Not reviewed | 1 px dashed `--text-faint` border, `--text-muted` name |
| On the highlighted path | 2 px `--accent` border, weight 600 |
| Selected | `--accent-solid` fill, white text, 6 px soft accent halo |

Edges are 1.5 px `--border`. Path edges are 3 px `--accent`, and an edge through a junction table is dashed, labelled with the junction table's name. A legend card sits bottom-left and the zoom controls (−, %, +, Fit) bottom-right.

### 4.10 Composer

A raised card at the bottom of the Ask column: a two-line textarea, a source pill ("Source: auto"), a "Check answerable" pill that runs §8.3's pre-check, the "⌘↵ to send" hint, and a send button. While a run is going, the send button becomes Stop (Esc), which cancels the run (§8.4).

### 4.11 Certification badge and certify button

- Badge: a `--verified-soft` pill with a check and the certified field ("Name certified").
- Certify button: the same green as a bordered button, next to the field it certifies.
- Machine-written, uncertified fields have an amber caption naming who wrote them ("Written by Sonnet · not certified").

---

## 5. Screens

| Screen | Mocked | Main column | Right rail |
|---|---|---|---|
| Onboarding | yes | Setup steps | none (a dark step rail on the left instead) |
| Ask | yes | Conversation and composer | Provenance |
| Ontology | yes | Graph | Term editor |
| Terms | no | Glossary table | Term editor (the same component) |
| Catalog | no | Source → schema → table tree, search | Table or column detail |
| Analyses | no | Saved analyses list | Analysis detail with SQL |
| Jobs | yes | Current job, estimate | New tables found, call log, history |
| Settings | no | Sectioned form | none |

### 5.1 Onboarding

- A dark left rail (`#151821` in both themes) with the wordmark, a one-line pitch, four numbered steps and the data-locality sentence at the bottom.
- The main column shows finished steps as compact summaries and the current step as a raised card with a 2 px accent border.
- Step 1, Claude Code: Installed (version), Logged in, Test call (Haiku `ping` with its time). If an API-key variable is set, show the amber note once, naming the variable. If `claude` is missing, link to the install page. If the login is missing, show the exact steps: open a terminal, run `claude`, type `/login`. Ontological never runs that flow itself (§3).
- Step 2, Add a source: a drop zone for files, server tiles for Postgres and MySQL, and the Keychain sentence. After a pick, show the file with table count and size and "structure read in 0.8 s".
- Step 3, Choose scope: pick schemas or tables, then the split vs merged estimate (§6.5) before anything spends quota.
- Step 4, Compile and ask: start the compile and go straight to Ask. Questions work on whatever has compiled so far.

### 5.2 Ask

- The question is the heading of each turn, with "You · <source> (auto-picked)" above it.
- The answer card, in order: verification chain, prose with term links, result card, SQL block, assumption notes, then actions. The actions are thumbs up and down and "Save as analysis", with the line "Saved analyses become worked examples for future questions."
- Thumbs up opens "Save as analysis" (§8.3).
- The reading column is at most 860 px wide. The provenance rail follows whichever answer is in view.

### 5.3 Ontology

- Header: a Semantic / Data layer switch, the path picker (two term chips with a clear button), and search.
- A path summary card sits top-left on the graph ("4 hops · Customer → Rental → Inventory → Film → Category · via film_category").
- Selecting a node opens the term editor: name and certification, description with a certify button, synonyms as chips with "+ Add", attributes (join columns link to their term), SQL attributes with source and use count, and history from `ops.change_log`.
- Editor footer: "Saving re-embeds <Term>, so the next answer uses your wording." then Undo and Save.
- The graph has a list-view fallback, reachable by keyboard, with the same nodes in a table.

### 5.4 Jobs

- The banner at the top when a job is paused or slowed.
- The job card: title, call shape, start time, any concurrency change and why, then counts (tables, Claude calls, cache hits), the stage legend and the tile grid.
- The estimate card for the remaining work, with split and merged as a radio pair. Switching applies to the remaining tables.
- In the rail: new-tables prompts from the staleness check (§9.1), saying the refresh was free and compiling is not. Then the call log (time, task, table, model, result in seconds or "cached" or "limit reached") and earlier runs.

### 5.5 Not yet mocked

- **Terms.** A sortable table: name, term type, certified fields, synonyms, attribute count, last change. Filter chips: Uncertified, Changed this week, No description. The term editor reuses §5.3's.
- **Catalog.** A tree on the left, detail on the right: columns with type, nullability, sample values, PII flag and inferred date format. Tags and rules as chips.
- **Analyses.** A list of saved question + SQL pairs with source and date. Detail shows the SQL block and "Ask again."
- **Settings.** Sections from §9.1, each a short form. The Models section shows the route table (§6.3) with model and effort per task, and a note on relative cost. The MCP section shows the `claude mcp add` snippet with a copy button.

---

## 6. States

Every screen designs these, not just the happy path.

| State | Where | Treatment |
|---|---|---|
| Engine starting | Everywhere | Status dot pulses, "Starting engine…". Screens show skeletons, not spinners |
| Engine crashed | Everywhere | Banner: "The engine stopped. Restarting…" (only if Electron started it, §4.4), else "Reconnect" |
| `needs_login` | Status bar, Ask, Jobs | Banner with the terminal steps. Jobs pause |
| `claude` not found | Onboarding, status bar | Link to install. Everything that needs Claude is disabled with that reason |
| Answer streaming | Ask | The chain fills in step by step. Prose appears only at the end, since it comes with the final output |
| Unverified answer | Ask | Header reads "Unverified: <reason>". Amber chain step. SQL label "Not validated". Save as analysis is disabled |
| `answerable=false` | Ask | No chain past Plan. Lists `missing[]` as terms the ontology lacks, and links each to Terms to add it |
| Prediction question | Ask | "Ontological doesn't make predictions." Suggests the nearest descriptive question |
| Empty result | Ask | "No rows." Lists the hints from empty-result handling (§8.1) as follow-ups the user can send |
| Usage `allowed_warning` | Status bar, Jobs | Amber meter. Job says "slowed to 1 at a time". Chat keeps working |
| Usage `rejected` | Status bar, Ask, Jobs | Ask: "Usage limit reached, resets at 4:10 PM", composer disabled until then. Jobs: §4.7 banner |
| No sources | Ask, Ontology, Catalog | An empty state with "Add a source" (⌘O) |
| Source compiled partly | Ask, Ontology | Note in the source switcher: "31 of 48 tables compiled". Answers may use only compiled tables |

---

## 7. Keyboard

| Shortcut | Action |
|---|---|
| ⌘1 – ⌘6 | Ask, Ontology, Terms, Catalog, Analyses, Jobs |
| ⌘, | Settings |
| ⌘K | Search terms, tables and analyses |
| ⌘N | New question |
| ⌘O | Add source |
| ⌘↵ | Send question |
| Esc | Stop a running answer, close a sheet or popover |
| ⌘\ | Toggle the right rail |
| ⌘C on a focused SQL block | Copy SQL |
| ↑ in an empty composer | Edit the last question |

Shortcut hints in the sidebar use `--text-muted` at 11 px. Windows and Linux swap ⌘ for Ctrl.

---

## 8. Writing

- Plain words. Say what happened and what happens next ("Compile picks up by itself at 4:10 PM").
- Name the cost. "No Claude usage" or "uses Claude quota" wherever the user starts something.
- Clock times, not durations, for anything scheduled. Durations for anything that took time ("142 ms", "19 s").
- Sentence case for every label and heading.
- Name models as the user knows them ("Sonnet 5.5"). Name tasks by route name only in logs (`table_terms`).
- No exclamation marks and no emoji.

---

## 9. Accessibility

- Real elements: `<button>`, `<a href>`, `<input>` with `<label>`, `role="tab"` / `role="radio"` groups with arrow-key movement.
- A focus ring on everything: 2 px `--accent`, 2 px offset, via `:focus-visible`.
- Click areas at least 44 px. Icon-only buttons have `aria-label`.
- Charts and meters carry text equivalents. The graph has the list fallback (§5.3).
- Respect `prefers-reduced-motion`: no pulsing dots or animated chain fills. Use static states instead.
- Text contrast at least 4.5:1, already checked for the tokens in §2.1.

---

## 10. Open questions

1. **Graph scale.** sigma.js handles thousands of nodes, but a 200-table warehouse will need clustering by schema or term group. Decide after M3's WWI compile.
2. **Ask with two sources in one conversation.** Answers come from one source (§8.1). Should a follow-up that hits a different source start a new conversation or mark the switch inline?
3. **Monaco or CodeMirror** for "Open in editor". CodeMirror 6 is smaller and themes from the same tokens.
4. **Windows and Linux chrome.** `titleBarOverlay` is planned but unmocked. macOS ships first (§10).
5. **Dark mode values** are defined and contrast-checked but not yet mocked on the canvas.
