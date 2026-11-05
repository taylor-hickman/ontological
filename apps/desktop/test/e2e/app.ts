import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { _electron as electron, type ElectronApplication, type Page } from '@playwright/test'

export interface Launched {
  app: ElectronApplication
  page: Page
}

export function freshUserData(): string {
  return mkdtempSync(join(tmpdir(), 'ontological-e2e-'))
}

/** Launches the built app against its own userData folder, so tests don't share state. */
export async function launch(userData: string): Promise<Launched> {
  const app = await electron.launch({
    args: ['.'],
    env: { ...process.env, ONTOLOGICAL_USER_DATA: userData }
  })
  const page = await app.firstWindow()
  await page.waitForSelector('[data-testid="status-bar"]')
  return { app, page }
}

/** Clicks a menu item the way its accelerator would, through the application menu. */
export async function clickMenuItem(app: ElectronApplication, id: string): Promise<void> {
  await app.evaluate(({ Menu }, itemId) => {
    const item = Menu.getApplicationMenu()?.getMenuItemById(itemId)
    if (!item) throw new Error(`No menu item ${itemId}`)
    item.click()
  }, id)
}
