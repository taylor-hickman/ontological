import { expect, test } from '@playwright/test'
import { SCREENS } from '../../src/shared/shell'
import { clickMenuItem, freshUserData, launch } from './app'

test('opens a 960×640-minimum window with the DESIGN.md shell', async () => {
  const { app, page } = await launch(freshUserData())
  try {
    const win = await app.evaluate(({ BrowserWindow, nativeTheme }) => {
      const w = BrowserWindow.getAllWindows()[0]!
      return {
        visible: w.isVisible(),
        min: w.getMinimumSize(),
        background: w.getBackgroundColor(),
        dark: nativeTheme.shouldUseDarkColors,
        title: w.getTitle()
      }
    })
    expect(win.visible).toBe(true)
    expect(win.min).toEqual([960, 640])
    // The window background is --bg for the theme in use, so it never flashes white.
    expect(win.background.toUpperCase()).toBe(win.dark ? '#111318' : '#F4F5F7')
    expect(win.title).toBe('Ontological')

    const box = async (selector: string) => (await page.locator(selector).boundingBox())!
    expect((await box('nav[aria-label="Main"]')).width).toBe(232)
    expect((await box('[data-testid="sidebar-top-bar"]')).height).toBe(52)
    expect((await box('[data-testid="header"]')).height).toBe(52)
    expect((await box('[data-testid="status-bar"]')).height).toBe(28)

    // Drag regions: the top bars drag, the controls inside them don't (DESIGN.md §3.2).
    const region = (selector: string) =>
      page.locator(selector).evaluate((el) => getComputedStyle(el).getPropertyValue('-webkit-app-region'))
    expect(await region('[data-testid="sidebar-top-bar"]')).toBe('drag')
    expect(await region('[data-testid="header"]')).toBe('drag')
    expect(await region('[data-testid="header"] label')).toBe('no-drag')
    expect(await region('[data-testid="main-column"]')).not.toBe('drag')

    await expect(page.getByRole('meter', { name: 'Claude usage' })).toHaveAttribute('aria-valuetext', 'No data yet')
    // Fonts are bundled, not fetched.
    expect(await page.evaluate(() => document.fonts.check('14px Geist'))).toBe(true)
  } finally {
    await app.close()
  }
})

test('isolates the renderer: no Node, only the typed bridge', async () => {
  const { app, page } = await launch(freshUserData())
  try {
    const globals = await page.evaluate(() => ({
      require: typeof (window as unknown as { require?: unknown }).require,
      process: typeof (window as unknown as { process?: unknown }).process,
      bridge: Object.keys(window.ontological).sort()
    }))
    expect(globals.require).toBe('undefined')
    expect(globals.process).toBe('undefined')
    expect(globals.bridge).toEqual(
      ['closeSetup', 'engine', 'initial', 'notify', 'onCommand', 'onEngine', 'openFile', 'pathForFile', 'platform', 'reconnectEngine', 'saveFile', 'setScreen', 'setTheme', 'showContextMenu'].sort()
    )
  } finally {
    await app.close()
  }
})

test('main rejects malformed IPC arguments', async () => {
  const { app, page } = await launch(freshUserData())
  try {
    const errors = await page.evaluate(async () => {
      const api = window.ontological as unknown as Record<string, (...args: unknown[]) => Promise<unknown>>
      const attempt = (p: Promise<unknown>) => p.then(() => 'resolved', (e: Error) => e.message)
      return {
        save: await attempt(api.saveFile!('../../etc/passwd')),
        open: await attempt(api.openFile!([{ name: 'x', extensions: ['../x'] }])),
        menu: await attempt(api.showContextMenu!('copy')),
        notify: await attempt(api.notify!('', 'body')),
        theme: await attempt(api.setTheme!('sepia'))
      }
    })
    expect(errors.save).toContain('name must be a file name')
    expect(errors.open).toContain('must be letters and digits')
    expect(errors.menu).toContain('items must be an array')
    expect(errors.notify).toContain('title must not be empty')
    expect(errors.theme).toContain('theme must be system, light or dark')
  } finally {
    await app.close()
  }
})

test('⌘1–⌘6 and ⌘, switch screens through menu accelerators', async () => {
  const { app, page } = await launch(freshUserData())
  try {
    for (const screen of SCREENS) {
      const accelerator = await app.evaluate(
        ({ Menu }, id) => Menu.getApplicationMenu()?.getMenuItemById(id)?.accelerator,
        `screen-${screen.id}`
      )
      expect(accelerator).toBe(`CmdOrCtrl+${screen.key}`)
      await clickMenuItem(app, `screen-${screen.id}`)
      await expect(page.locator('h1')).toHaveText(screen.label)
      await expect(page.getByRole('button', { name: new RegExp(`^${screen.label}`) })).toHaveAttribute(
        'aria-current',
        'page'
      )
    }
    await clickMenuItem(app, 'settings')
    await expect(page.locator('h1')).toHaveText('Settings')
    await expect(page.getByRole('radiogroup', { name: 'Theme' })).toBeVisible()

    // New question goes back to Ask; clicking the sidebar works too.
    await clickMenuItem(app, 'new-question')
    await expect(page.locator('h1')).toHaveText('Ask')
    await page.getByRole('button', { name: /^Catalog/ }).click()
    await expect(page.locator('h1')).toHaveText('Catalog')
  } finally {
    await app.close()
  }
})

test('⌘K focuses search and ⌘\\ toggles the right rail, which is a sheet below 1100 px', async () => {
  const { app, page } = await launch(freshUserData())
  try {
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.setSize(1280, 820))
    await clickMenuItem(app, 'search')
    await expect(page.getByRole('searchbox')).toBeFocused()

    const rail = page.getByTestId('rail')
    await expect(rail).toBeVisible()
    await expect(rail).toHaveAttribute('aria-label', 'Provenance')
    await clickMenuItem(app, 'toggle-rail')
    await expect(rail).toHaveCount(0)
    await clickMenuItem(app, 'toggle-rail')
    await expect(rail).toBeVisible()

    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.setSize(1000, 700))
    await expect(rail).toHaveCount(0)
    await clickMenuItem(app, 'toggle-rail')
    await expect(rail).toHaveAttribute('data-sheet', 'true')
    await page.keyboard.press('Escape')
    await expect(rail).toHaveCount(0)
  } finally {
    await app.close()
  }
})
