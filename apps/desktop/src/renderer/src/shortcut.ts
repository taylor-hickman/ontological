/** The hint for a ⌘ shortcut. Windows and Linux swap ⌘ for Ctrl (DESIGN.md §7). */
export function shortcut(key: string): string {
  return window.ontological.platform === 'darwin' ? `⌘${key}` : `Ctrl+${key}`
}
