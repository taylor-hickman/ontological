// The typed bridge the preload exposes as `window.ontological` (DESIGN.md §3.4).
// Main validates every argument it receives, so these types are a contract, not a guarantee.

import type { ScreenId, ThemeSource } from './shell'

/** A native file-dialog filter, as Electron takes it: `{ name: 'DuckDB', extensions: ['duckdb'] }`. */
export interface FileFilter {
  name: string
  extensions: string[]
}

export type ContextMenuItem =
  | { type?: 'item'; id: string; label: string; enabled?: boolean }
  | { type: 'separator' }

/** What the menu bar asks the renderer to do. Every shortcut is a menu accelerator, so these are the only key handling. */
export type ShellCommand =
  | { type: 'navigate'; screen: ScreenId }
  | { type: 'new-question' }
  | { type: 'focus-search' }
  | { type: 'toggle-rail' }
  | { type: 'theme'; theme: ThemeSource }

/** Read synchronously by the preload, so the first paint already has the right screen and theme. */
export interface InitialShellState {
  screen: ScreenId
  theme: ThemeSource
}

export interface OntologicalApi {
  platform: string
  initial: InitialShellState
  /** Remembers the open screen for the next launch. */
  setScreen(screen: ScreenId): void
  setTheme(theme: ThemeSource): Promise<void>
  /** Subscribes to menu commands; returns the unsubscribe function. */
  onCommand(listener: (command: ShellCommand) => void): () => void
  /** A native open dialog. Resolves to the picked path, or null if cancelled. */
  openFile(filters: FileFilter[]): Promise<string | null>
  /** A native save dialog with `name` as the suggested file name. Resolves to the path, or null. */
  saveFile(name: string): Promise<string | null>
  /** A native context menu at the pointer. Resolves to the picked item's id, or null if dismissed. */
  showContextMenu(items: ContextMenuItem[]): Promise<string | null>
  /** A system notification, sent only while the window is not focused. Resolves to whether it was shown. */
  notify(title: string, body: string): Promise<boolean>
}

export const IPC = {
  setScreen: 'shell:set-screen',
  setTheme: 'shell:set-theme',
  command: 'shell:command',
  openFile: 'dialog:open-file',
  saveFile: 'dialog:save-file',
  showContextMenu: 'menu:context',
  notify: 'notify'
} as const

/** The argv prefix main uses to hand the preload its initial state. */
export const INITIAL_STATE_ARG = '--ontological-initial='
