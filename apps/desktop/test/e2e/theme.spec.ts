import { expect, test, type Page } from '@playwright/test'
import { clickMenuItem, freshUserData, launch } from './app'

const bodyBackground = (page: Page) => page.evaluate(() => getComputedStyle(document.body).backgroundColor)

test('the theme follows System, Light and Dark, in the page and in native UI', async () => {
  const userData = freshUserData()
  const { app, page } = await launch(userData)
  const native = () =>
    app.evaluate(({ BrowserWindow, nativeTheme }) => ({
      source: nativeTheme.themeSource,
      background: BrowserWindow.getAllWindows()[0]!.getBackgroundColor().toUpperCase()
    }))
  try {
    await clickMenuItem(app, 'theme-dark')
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
    expect(await bodyBackground(page)).toBe('rgb(17, 19, 24)')
    await expect.poll(native).toEqual({ source: 'dark', background: '#111318' })

    await clickMenuItem(app, 'theme-light')
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
    expect(await bodyBackground(page)).toBe('rgb(244, 245, 247)')
    await expect.poll(native).toEqual({ source: 'light', background: '#F4F5F7' })

    // System drops the override, so prefers-color-scheme decides.
    await clickMenuItem(app, 'theme-system')
    await expect(page.locator('html')).not.toHaveAttribute('data-theme')
    expect((await native()).source).toBe('system')

    // Settings → Appearance does the same, and the menu's radio follows.
    await clickMenuItem(app, 'settings')
    await page.getByRole('radio', { name: 'Dark' }).check()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
    const checked = await app.evaluate(({ Menu }) => Menu.getApplicationMenu()?.getMenuItemById('theme-dark')?.checked)
    expect(checked).toBe(true)
  } finally {
    await app.close()
  }

  // The choice survives a relaunch, from the first paint.
  const again = await launch(userData)
  try {
    await expect(again.page.locator('html')).toHaveAttribute('data-theme', 'dark')
    await expect(again.page.getByRole('radio', { name: 'Dark' })).toBeChecked()
  } finally {
    await again.app.close()
  }
})
