import { execFileSync } from 'node:child_process'
import { chmodSync, existsSync, mkdtempSync, writeFileSync } from 'node:fs'
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

/**
 * A new userData folder. Unless `onboarding` is set, it is one where the user already left
 * onboarding, so the app opens on its shell even with no sources.
 */
export function freshUserData({ onboarding = false }: { onboarding?: boolean } = {}): string {
  const dir = mkdtempSync(join(tmpdir(), 'ontological-e2e-'))
  if (!onboarding) writeFileSync(join(dir, 'shell-state.json'), JSON.stringify({ setupClosed: true }))
  return dir
}

/**
 * A stand-in for the login shell main reads its environment from (src/main/shell-env.ts), so the
 * tests never pick up the developer's own rc files. `exports` are what its "rc" exports.
 */
export function fakeShell(exports: Record<string, string> = {}): string {
  const path = join(mkdtempSync(join(tmpdir(), 'ontological-shell-')), 'shell')
  const lines = Object.entries(exports).map(([k, v]) => `export ${k}="${v}"`)
  writeFileSync(path, ['#!/bin/sh', ...lines, 'exec /bin/sh -c "$2"', ''].join('\n'))
  chmodSync(path, 0o755)
  return path
}

/** Launches the built app against its own userData folder, so tests don't share state or engines. */
export async function launch(
  userData: string,
  engineArgs: string[] = QUICK_ENGINE,
  env: Record<string, string | undefined> = {}
): Promise<Launched> {
  if (!existsSync(PYTHON)) throw new Error(`No Python at ${PYTHON}: run \`uv sync\` in engine/`)
  // An `undefined` in `env` removes that variable.
  const merged = Object.fromEntries(
    Object.entries({
      ...process.env,
      ONTOLOGICAL_USER_DATA: userData,
      ONTOLOGICAL_ENGINE_CMD: JSON.stringify([PYTHON, STUB, ...engineArgs]),
      SHELL: fakeShell(),
      ...env
    }).filter((entry): entry is [string, string] => entry[1] !== undefined)
  )
  const app = await electron.launch({ args: ['.'], env: merged })
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
