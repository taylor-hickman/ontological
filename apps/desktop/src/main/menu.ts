// The menu bar (DESIGN.md §3.4). Every shortcut in the app is an accelerator here, so it shows
// in the menu bar and works wherever focus is; the renderer has no global keydown listeners.

import type { MenuItemConstructorOptions } from 'electron'
import type { ShellCommand } from '@shared/ipc'
import { SCREENS, THEME_SOURCES, type ThemeSource } from '@shared/shell'

export interface MenuActions {
  send(command: ShellCommand): void
  setTheme(theme: ThemeSource): void
  openLoginHelp(): void
  openLogsFolder(): void
}

export interface MenuOptions {
  isMac: boolean
  isDev: boolean
  theme: ThemeSource
  actions: MenuActions
}

const THEME_LABELS: Record<ThemeSource, string> = { system: 'System', light: 'Light', dark: 'Dark' }

export function buildMenuTemplate({ isMac, isDev, theme, actions }: MenuOptions): MenuItemConstructorOptions[] {
  const { send } = actions
  const settings: MenuItemConstructorOptions = {
    id: 'settings',
    label: 'Settings…',
    accelerator: 'CmdOrCtrl+,',
    click: () => send({ type: 'navigate', screen: 'settings' })
  }

  const appMenu: MenuItemConstructorOptions = {
    label: 'Ontological',
    submenu: isMac
      ? [
          { role: 'about', label: 'About Ontological' },
          { type: 'separator' },
          settings,
          { type: 'separator' },
          { role: 'services' },
          { type: 'separator' },
          { role: 'hide', label: 'Hide Ontological' },
          { role: 'hideOthers' },
          { role: 'unhide' },
          { type: 'separator' },
          { role: 'quit', label: 'Quit Ontological' }
        ]
      : [{ role: 'about', label: 'About Ontological' }, { type: 'separator' }, settings, { type: 'separator' }, { role: 'quit' }]
  }

  // Add source, Import and Export need the engine, which doesn't exist yet. They are listed,
  // disabled, so the menu already has its final shape.
  const fileMenu: MenuItemConstructorOptions = {
    label: 'File',
    submenu: [
      { id: 'new-question', label: 'New question', accelerator: 'CmdOrCtrl+N', click: () => send({ type: 'new-question' }) },
      { id: 'add-source', label: 'Add source…', accelerator: 'CmdOrCtrl+O', enabled: false },
      { type: 'separator' },
      { id: 'import', label: 'Import…', enabled: false },
      { id: 'export', label: 'Export…', enabled: false },
      ...(isMac ? [] : [{ type: 'separator' } as const, { role: 'close' } as const])
    ]
  }

  const viewMenu: MenuItemConstructorOptions = {
    label: 'View',
    submenu: [
      ...SCREENS.map(
        (s): MenuItemConstructorOptions => ({
          id: `screen-${s.id}`,
          label: s.label,
          accelerator: `CmdOrCtrl+${s.key}`,
          click: () => send({ type: 'navigate', screen: s.id })
        })
      ),
      { type: 'separator' },
      { id: 'search', label: 'Search', accelerator: 'CmdOrCtrl+K', click: () => send({ type: 'focus-search' }) },
      { id: 'toggle-rail', label: 'Toggle right rail', accelerator: 'CmdOrCtrl+\\', click: () => send({ type: 'toggle-rail' }) },
      { type: 'separator' },
      {
        label: 'Theme',
        submenu: THEME_SOURCES.map(
          (t): MenuItemConstructorOptions => ({
            id: `theme-${t}`,
            label: THEME_LABELS[t],
            type: 'radio',
            checked: t === theme,
            click: () => actions.setTheme(t)
          })
        )
      },
      { type: 'separator' },
      { role: 'togglefullscreen' },
      ...(isDev ? [{ type: 'separator' } as const, { role: 'reload' } as const, { role: 'toggleDevTools' } as const] : [])
    ]
  }

  const helpMenu: MenuItemConstructorOptions = {
    role: 'help',
    submenu: [
      { id: 'login-help', label: 'Claude Code login help', click: actions.openLoginHelp },
      { id: 'open-logs', label: 'Open logs folder', click: actions.openLogsFolder }
    ]
  }

  return [appMenu, fileMenu, { role: 'editMenu' }, viewMenu, { role: 'windowMenu' }, helpMenu]
}
