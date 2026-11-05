// The screens and settings the main process and the renderer agree on (DESIGN.md §3).

export const SCREENS = [
  { id: 'ask', label: 'Ask', key: '1' },
  { id: 'ontology', label: 'Ontology', key: '2' },
  { id: 'terms', label: 'Terms', key: '3' },
  { id: 'catalog', label: 'Catalog', key: '4' },
  { id: 'analyses', label: 'Analyses', key: '5' },
  { id: 'jobs', label: 'Jobs', key: '6' }
] as const

export type NavScreenId = (typeof SCREENS)[number]['id']

/** The six navigation screens plus Settings, which opens with ⌘, rather than a number. */
export type ScreenId = NavScreenId | 'settings'

export const SCREEN_IDS: readonly ScreenId[] = [...SCREENS.map((s) => s.id), 'settings']

export function isScreenId(value: unknown): value is ScreenId {
  return typeof value === 'string' && (SCREEN_IDS as readonly string[]).includes(value)
}

/** Settings → Appearance and View → Theme (DESIGN.md §3.5). */
export type ThemeSource = 'system' | 'light' | 'dark'

export const THEME_SOURCES: readonly ThemeSource[] = ['system', 'light', 'dark']

export function isThemeSource(value: unknown): value is ThemeSource {
  return typeof value === 'string' && (THEME_SOURCES as readonly string[]).includes(value)
}

/** `--bg` for each theme (DESIGN.md §2.1), used as the BrowserWindow background so it never flashes white. */
export const WINDOW_BACKGROUND = { light: '#F4F5F7', dark: '#111318' } as const

/** The 52 px top bar shared by the sidebar and the screen header (DESIGN.md §3.3). */
export const TOP_BAR_HEIGHT = 52
