import type { MenuItemConstructorOptions } from 'electron'
import { describe, expect, it, vi } from 'vitest'
import { buildMenuTemplate, type MenuActions } from '../../src/main/menu'

function flatten(items: MenuItemConstructorOptions[]): MenuItemConstructorOptions[] {
  return items.flatMap((item) => [item, ...(Array.isArray(item.submenu) ? flatten(item.submenu) : [])])
}

function build(isMac = true, theme: 'system' | 'light' | 'dark' = 'system') {
  const actions: MenuActions = { send: vi.fn(), setTheme: vi.fn(), openLoginHelp: vi.fn(), openLogsFolder: vi.fn() }
  const items = flatten(buildMenuTemplate({ isMac, isDev: false, theme, actions }))
  const byId = (id: string) => {
    const item = items.find((i) => i.id === id)
    if (!item) throw new Error(`no menu item ${id}`)
    return item
  }
  return { actions, items, byId }
}

describe('buildMenuTemplate', () => {
  it('has DESIGN.md §3.4 top-level menus in order', () => {
    const template = buildMenuTemplate({
      isMac: true,
      isDev: false,
      theme: 'system',
      actions: { send: vi.fn(), setTheme: vi.fn(), openLoginHelp: vi.fn(), openLogsFolder: vi.fn() }
    })
    expect(template.map((m) => m.label ?? m.role)).toEqual(['Ontological', 'File', 'editMenu', 'View', 'windowMenu', 'help'])
  })

  it('gives every shortcut in DESIGN.md §7 a menu accelerator', () => {
    const { byId } = build()
    const expected: Record<string, string> = {
      'screen-ask': 'CmdOrCtrl+1',
      'screen-ontology': 'CmdOrCtrl+2',
      'screen-terms': 'CmdOrCtrl+3',
      'screen-catalog': 'CmdOrCtrl+4',
      'screen-analyses': 'CmdOrCtrl+5',
      'screen-jobs': 'CmdOrCtrl+6',
      settings: 'CmdOrCtrl+,',
      search: 'CmdOrCtrl+K',
      'new-question': 'CmdOrCtrl+N',
      'add-source': 'CmdOrCtrl+O',
      'toggle-rail': 'CmdOrCtrl+\\'
    }
    for (const [id, accelerator] of Object.entries(expected)) expect(byId(id).accelerator, id).toBe(accelerator)
  })

  it('sends the matching command for each item', () => {
    const { actions, byId } = build()
    const click = (id: string) => (byId(id).click as () => void)()
    click('screen-ontology')
    click('settings')
    click('toggle-rail')
    click('search')
    click('new-question')
    expect(vi.mocked(actions.send).mock.calls.map(([c]) => c)).toEqual([
      { type: 'navigate', screen: 'ontology' },
      { type: 'navigate', screen: 'settings' },
      { type: 'toggle-rail' },
      { type: 'focus-search' },
      { type: 'new-question' }
    ])
  })

  it('checks the current theme and sets a new one', () => {
    const { actions, byId } = build(true, 'dark')
    expect(byId('theme-dark').checked).toBe(true)
    expect(byId('theme-system').checked).toBe(false)
    ;(byId('theme-light').click as () => void)()
    expect(actions.setTheme).toHaveBeenCalledWith('light')
  })

  it('lists the engine actions disabled until the engine exists', () => {
    const { byId } = build()
    for (const id of ['add-source', 'import', 'export']) expect(byId(id).enabled, id).toBe(false)
  })

  it('keeps macOS-only roles off other platforms', () => {
    const { items } = build(false)
    expect(items.some((i) => i.role === 'services' || i.role === 'hide')).toBe(false)
    expect(items.find((i) => i.id === 'settings')?.accelerator).toBe('CmdOrCtrl+,')
  })
})
