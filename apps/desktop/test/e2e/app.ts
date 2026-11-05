import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { _electron as electron, type ElectronApplication, type Page } from '@playwright/test'

export interface Launched {
  app: ElectronApplication
  page: Page
}

/**
 * The engine the tests run: the real engine process with a fake store (engine/tests/engine/
 * serve_stub.py), so no Postgres is needed. It runs on the engine project's virtualenv, which
 * `uv sync` in engine/ creates; ONTOLOGICAL_TEST_PYTHON points elsewhere.
 */
const ENGINE_DIR = resolve(import.meta.dirname, '../../../../engine')
export const PYTHON = process.env.ONTOLOGICAL_TEST_PYTHON ?? join(ENGINE_DIR, '.venv/bin/python')
export const STUB = join(ENGINE_DIR, 'tests/engine/serve_stub.py')

/**
 * Short times, so each test's engine idles out soon after its app quits: 2 s idle, and a lease
 * renewed every 0.5 s that expires after 1.5 s.
 */
export const QUICK_ENGINE = ['--idle-timeout', '2', '--lease-renew', '0.5']

export function freshUserData(): string {
  return mkdtempSync(join(tmpdir(), 'ontological-e2e-'))
}

/** Launches the built app against its own userData folder, so tests don't share state or engines. */
export async function launch(userData: string, engineArgs: string[] = QUICK_ENGINE): Promise<Launched> {
  if (!existsSync(PYTHON)) throw new Error(`No Python at ${PYTHON}: run \`uv sync\` in engine/`)
  const app = await electron.launch({
    args: ['.'],
    env: {
      ...process.env,
      ONTOLOGICAL_USER_DATA: userData,
      ONTOLOGICAL_ENGINE_CMD: JSON.stringify([PYTHON, STUB, ...engineArgs])
    }
  })
  const page = await app.firstWindow()
  await page.waitForSelector('[data-testid="status-bar"]')
  return { app, page }
}

/** Starts an engine the way the MCP shim does, detached, and returns its pid. */
export function startHeadless(userData: string, engineArgs: string[] = QUICK_ENGINE): number {
  const out = execFileSync(PYTHON, [STUB, '--app-data', userData, '--headless', ...engineArgs], { encoding: 'utf8' })
  const line = JSON.parse(out.trim()) as { pid: number; started: boolean }
  if (!line.started) throw new Error(`expected a new engine, got ${out}`)
  return line.pid
}

/** Clicks a menu item the way its accelerator would, through the application menu. */
export async function clickMenuItem(app: ElectronApplication, id: string): Promise<void> {
  await app.evaluate(({ Menu }, itemId) => {
    const item = Menu.getApplicationMenu()?.getMenuItemById(itemId)
    if (!item) throw new Error(`No menu item ${itemId}`)
    item.click()
  }, id)
}
