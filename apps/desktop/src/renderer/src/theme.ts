import type { ThemeSource } from '@shared/shell'

/**
 * DESIGN.md §3.5. System leaves `data-theme` off so `prefers-color-scheme` decides; Light and
 * Dark pin it. Main sets `nativeTheme.themeSource` too, so native menus and dialogs match.
 */
export function applyTheme(theme: ThemeSource): void {
  const root = document.documentElement
  if (theme === 'system') delete root.dataset.theme
  else root.dataset.theme = theme
}
