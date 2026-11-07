// Onboarding and the catalog screen (DESIGN.md §5.1, §5.5) against the stub engine, whose sources
// live in memory and read SQLite files (engine/tests/engine/fake_sources.py), and a fake `claude`.

import { chmodSync, mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { DatabaseSync } from 'node:sqlite'
import { expect, test, type ElectronApplication, type Page } from '@playwright/test'
import { clickMenuItem, fakeShell, freshUserData, launch } from './app'

/** A `claude` that answers --version and `auth status --json`. */
function fakeClaude({ version = '2.1.319', loggedIn = true, dir = mkdtempSync(join(tmpdir(), 'ontological-claude-')) } = {}): string {
  const path = join(dir, 'claude')
  writeFileSync(
    path,
    '#!/bin/sh\n' +
      `if [ "$1" = "--version" ]; then echo '${version} (Claude Code)'; exit 0; fi\n` +
      `if [ "$1 $2" = "auth status" ]; then echo '{"loggedIn": ${loggedIn}}'; exit 0; fi\n` +
      'exit 2\n'
  )
  chmodSync(path, 0o755)
  return path
}

/** A small SQLite shop: customer and orders, with a foreign key between them. */
function shopDatabase(): string {
  const path = join(mkdtempSync(join(tmpdir(), 'ontological-src-')), 'shop.sqlite')
  const db = new DatabaseSync(path)
  db.exec(
    'CREATE TABLE customer (id INTEGER PRIMARY KEY, email TEXT NOT NULL);' +
      'CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customer(id), total REAL);' +
      "INSERT INTO customer VALUES (1, 'a@x.com'), (2, 'b@x.com');" +
      'INSERT INTO orders VALUES (10, 1, 9.5);'
  )
  db.close()
  return path
}

/** The native open dialog answers with `path`, as if the user picked it. */
async function pickFile(app: ElectronApplication, path: string): Promise<void> {
  await app.evaluate(({ dialog }, picked) => {
    dialog.showOpenDialog = (async () => ({ canceled: false, filePaths: [picked] })) as typeof dialog.showOpenDialog
  }, path)
}

const NO_KEYS = {
  ANTHROPIC_API_KEY: undefined,
  ANTHROPIC_AUTH_TOKEN: undefined,
  ANTHROPIC_BASE_URL: undefined,
  CLAUDE_CODE_OAUTH_TOKEN: undefined
}

async function onboarding(page: Page): Promise<void> {
  await expect(page.getByTestId('onboarding')).toBeVisible({ timeout: 20_000 })
}

test('first launch: Claude Code is checked, an ignored API key is named, and a first source is added', async () => {
  const shop = shopDatabase()
  const { app, page } = await launch(freshUserData({ onboarding: true }), undefined, {
    ...NO_KEYS,
    ONTOLOGICAL_CLAUDE_CLI: fakeClaude(),
    ANTHROPIC_API_KEY: 'sk-not-used'
  })
  try {
    await onboarding(page)
    await expect(page.getByTestId('onboarding')).toHaveAttribute('data-step', 'claude')
    const checks = page.getByTestId('claude-checks')
    await expect(checks).toContainText('Installed')
    await expect(checks).toContainText('v2.1.319')
    await expect(checks).toContainText('Logged in')
    await expect(page.getByText('Ready', { exact: true })).toBeVisible()
    await expect(page.getByTestId('claude-status')).toHaveText('Claude Code 2.1.319 · logged in')
    // Shown once, naming the variable (ARCHITECTURE.md §3 rule 3).
    await expect(page.getByTestId('ignored-variable')).toHaveCount(1)
    await expect(page.getByTestId('ignored-variable')).toContainText('ANTHROPIC_API_KEY is set in your shell')

    await page.getByRole('button', { name: 'Run test call' }).click()
    await expect(checks).toContainText('Haiku replied in 0.4 s')

    await page.getByRole('button', { name: 'Continue to sources' }).click()
    await expect(page.getByRole('heading', { name: 'Add your first source' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Continue to scope' })).toBeDisabled()

    await pickFile(app, shop)
    await page.getByRole('button', { name: 'Choose file…' }).click()
    const added = page.getByTestId('added-source')
    await expect(added).toContainText('shop.sqlite')
    await expect(added).toContainText('2 tables · 1 schema')
    await expect(added).toContainText('structure read in 0.1 s')

    await page.getByRole('button', { name: 'Continue to scope' }).click()
    await expect(page.getByRole('heading', { name: 'Choose scope' })).toBeVisible()
    await expect(page.getByTestId('scope-count')).toHaveText('2 of 2 tables chosen')
    await page.getByRole('checkbox', { name: 'orders' }).uncheck()
    await expect(page.getByTestId('scope-count')).toHaveText('1 of 2 tables chosen')
    await expect(page.getByRole('checkbox', { name: 'Schema main' })).not.toBeChecked()
    await expect(page.getByTestId('estimate-slot')).toBeVisible()

    await page.getByRole('button', { name: 'Continue', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Compile and ask' })).toBeVisible()
    await expect(page.getByText('1 table from 1 source chosen.')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Start compile' })).toBeDisabled()

    await page.getByRole('button', { name: 'Open the catalog' }).click()
    await expect(page.getByTestId('onboarding')).toHaveCount(0)
    await expect(page.locator('[data-screen="catalog"]')).toBeVisible()
    await expect(page.getByTestId('source-switcher')).toContainText('shop')
  } finally {
    await app.close()
  }
})

test('a logged-out Claude Code gets the terminal steps, never a login screen', async () => {
  const { app, page } = await launch(freshUserData({ onboarding: true }), undefined, {
    ...NO_KEYS,
    ONTOLOGICAL_CLAUDE_CLI: fakeClaude({ loggedIn: false })
  })
  try {
    await onboarding(page)
    const remedy = page.getByTestId('claude-remedy')
    await expect(remedy).toHaveAttribute('data-state', 'needs_login')
    await expect(remedy).toContainText('Open a terminal')
    await expect(remedy).toContainText('Run claude')
    await expect(remedy).toContainText('Type /login')
    await expect(page.getByTestId('ignored-variable')).toHaveCount(0)
    await expect(page.getByLabel('Setup steps')).toContainText('Needs login')
    await expect(page.getByTestId('claude-status')).toHaveText('Claude Code 2.1.319 · needs login')
  } finally {
    await app.close()
  }
})

test('a missing claude links to the install page', async () => {
  const { app, page } = await launch(freshUserData({ onboarding: true }), undefined, {
    ...NO_KEYS,
    ONTOLOGICAL_CLAUDE_CLI: join(tmpdir(), 'no-such-claude')
  })
  try {
    await onboarding(page)
    await expect(page.getByTestId('claude-remedy')).toHaveAttribute('data-state', 'not_installed')
    await expect(page.getByRole('link', { name: 'Install Claude Code' })).toHaveAttribute('href', /^https:\/\//)
    await expect(page.getByRole('button', { name: 'Run test call' })).toBeDisabled()
  } finally {
    await app.close()
  }
})

test('the catalog shows each source’s tables, and a table’s columns in the rail', async () => {
  const shop = shopDatabase()
  const { app, page } = await launch(freshUserData(), undefined, { ...NO_KEYS, ONTOLOGICAL_CLAUDE_CLI: fakeClaude() })
  try {
    await clickMenuItem(app, 'screen-catalog')
    await expect(page.getByTestId('catalog-empty')).toBeVisible({ timeout: 20_000 })

    // File → Add source opens onboarding at its source step.
    await clickMenuItem(app, 'add-source')
    await expect(page.getByTestId('onboarding')).toHaveAttribute('data-step', 'source')
    await pickFile(app, shop)
    await page.getByRole('button', { name: 'Choose file…' }).click()
    await expect(page.getByTestId('added-source')).toContainText('shop.sqlite')
    await page.getByRole('button', { name: 'Close setup' }).click()

    await expect(page.locator('[data-screen="catalog"]')).toBeVisible()
    const source = page.getByTestId('catalog-source')
    await expect(source).toContainText('shop')
    await expect(source).toContainText('2 tables · 1 schema')
    await expect(page.getByTestId('catalog-table')).toHaveCount(2)

    await page.getByLabel('Filter tables').fill('ord')
    await expect(page.getByTestId('catalog-table')).toHaveCount(1)
    await page.getByTestId('catalog-table').click()

    const detail = page.getByTestId('table-detail')
    await expect(detail).toContainText('orders')
    await expect(detail.getByTestId('column')).toHaveCount(3)
    const customerId = detail.getByTestId('column').filter({ hasText: 'customer_id' })
    await expect(customerId).toContainText('→ main.customer.id')
    await expect(detail.getByTestId('column').first()).toContainText('Primary key')
  } finally {
    await app.close()
  }
})

test('leaving setup is remembered, so a reload with no sources opens the shell', async () => {
  const { app, page } = await launch(freshUserData({ onboarding: true }), undefined, {
    ...NO_KEYS,
    ONTOLOGICAL_CLAUDE_CLI: fakeClaude()
  })
  try {
    await onboarding(page)
    await page.getByRole('button', { name: 'Set up later' }).click()
    await expect(page.getByTestId('onboarding')).toHaveCount(0)
    await expect(page.getByTestId('source-switcher')).toContainText('No sources')

    await page.reload()
    await expect(page.getByTestId('engine-status')).toHaveAttribute('data-status', 'running', { timeout: 20_000 })
    await clickMenuItem(app, 'screen-catalog')
    await expect(page.getByTestId('catalog-empty')).toBeVisible()
    await expect(page.getByTestId('onboarding')).toHaveCount(0)

    // The sidebar's source switcher adds the first source.
    await page.getByTestId('source-switcher').click()
    await expect(page.getByTestId('onboarding')).toHaveAttribute('data-step', 'source')
  } finally {
    await app.close()
  }
})

test('opened from Finder: claude and an API key are found through the login shell', async () => {
  // launchd's PATH and a HOME with nothing in it: only the login shell knows where claude is.
  const home = mkdtempSync(join(tmpdir(), 'ontological-home-'))
  const bin = mkdtempSync(join(tmpdir(), 'ontological-bin-'))
  fakeClaude({ dir: bin })
  const { app, page } = await launch(freshUserData({ onboarding: true }), undefined, {
    ...NO_KEYS,
    ONTOLOGICAL_CLAUDE_CLI: undefined,
    HOME: home,
    PATH: '/usr/bin:/bin:/usr/sbin:/sbin',
    SHELL: fakeShell({ PATH: `${bin}:$PATH`, ANTHROPIC_API_KEY: 'sk-from-zshrc' })
  })
  try {
    await onboarding(page)
    const checks = page.getByTestId('claude-checks')
    await expect(checks).toContainText('Installed')
    await expect(checks).toContainText('v2.1.319')
    await expect(page.getByTestId('ignored-variable')).toContainText('ANTHROPIC_API_KEY is set in your shell')
  } finally {
    await app.close()
  }
})
