// The typed bridge the preload exposes as `window.ontological` (DESIGN.md §3.4).
// Main validates every argument it receives, so these types are a contract, not a guarantee.

import type { EngineState } from './engine'
import type { ScreenId, ThemeSource } from './shell'

/** A native file-dialog filter, as Electron takes it: `{ name: 'DuckDB', extensions: ['duckdb'] }`. */
export interface FileFilter {
  name: string
  extensions: string[]
}

/** A text file the user picked in an open dialog, read by main. */
export interface OpenedFile {
  path: string
  /** The file's name, without its folder. */
  name: string
  text: string
}

export type ContextMenuItem =
  | { type?: 'item'; id: string; label: string; enabled?: boolean }
  | { type: 'separator' }

/** What the menu bar asks the renderer to do. Every shortcut is a menu accelerator, so these are the only key handling. */
export type ShellCommand =
  | { type: 'navigate'; screen: ScreenId }
  | { type: 'new-question' }
  | { type: 'add-source' }
  | { type: 'focus-search' }
  | { type: 'toggle-rail' }
  | { type: 'theme'; theme: ThemeSource }
  /** File → Import… and Export…: Settings → Import/Export, with Import's open dialog already up. */
  | { type: 'interchange'; action: 'import' | 'export' }

/**
 * Read synchronously by the preload on every document load, so the first paint already has the
 * right screen and theme, and a reload keeps the ones open now rather than those from launch.
 */
export interface InitialShellState {
  screen: ScreenId
  theme: ThemeSource
  /** The user has left onboarding once, so it opens only from File → Add source. */
  setupClosed: boolean
}

export interface OntologicalApi {
  platform: string
  initial: InitialShellState
  /** Remembers the open screen for the next launch. */
  setScreen(screen: ScreenId): void
  setTheme(theme: ThemeSource): Promise<void>
  /** Remembers that the user left onboarding. */
  closeSetup(): void
  /** Subscribes to menu commands; returns the unsubscribe function. */
  onCommand(listener: (command: ShellCommand) => void): () => void
  /** A native open dialog. Resolves to the picked path, or null if cancelled. */
  openFile(filters: FileFilter[]): Promise<string | null>
  /** The path of a file dropped on the page; '' for one that isn't on disk. */
  pathForFile(file: File): string
  /**
   * A native save dialog with `name` as the suggested file name; main writes `data` where the user
   * picks. Resolves to the path written, or null if cancelled.
   */
  saveFile(name: string, data: Uint8Array): Promise<string | null>
  /**
   * A native open dialog for a UTF-8 text file, which main reads. Resolves to the file, or null if
   * cancelled; rejects for a file that is too large or isn't UTF-8.
   */
  openTextFile(filters: FileFilter[]): Promise<OpenedFile | null>
  /** A native context menu at the pointer. Resolves to the picked item's id, or null if dismissed. */
  showContextMenu(items: ContextMenuItem[]): Promise<string | null>
  /** A system notification, sent only while the window is not focused. Resolves to whether it was shown. */
  notify(title: string, body: string): Promise<boolean>
  /** The engine's state; while it runs, `engineUrl` and `token` reach its API (ARCHITECTURE.md §4.4). */
  engine(): Promise<EngineState>
  /** Subscribes to engine state changes; returns the unsubscribe function. */
  onEngine(listener: (state: EngineState) => void): () => void
  /**
   * Once an engine the app attached to has stopped: attaches to a live engine or starts one.
   * Resolves when that attempt ends; `onEngine` reports how it went. Does nothing in other states.
   */
  reconnectEngine(): Promise<void>
}

export const IPC = {
  setScreen: 'shell:set-screen',
  setTheme: 'shell:set-theme',
  closeSetup: 'shell:close-setup',
  command: 'shell:command',
  openFile: 'dialog:open-file',
  saveFile: 'dialog:save-file',
  openTextFile: 'dialog:open-text-file',
  showContextMenu: 'menu:context',
  notify: 'notify',
  engine: 'engine:get',
  engineState: 'engine:state',
  reconnectEngine: 'engine:reconnect',
  initialState: 'shell:initial-state'
} as const
