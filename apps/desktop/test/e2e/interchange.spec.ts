import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { expect, test, type ElectronApplication, type Page } from '@playwright/test'
import { clickMenuItem, freshUserData, launch } from './app'

// Settings → Import/Export (ARCHITECTURE.md §9.3) against the stub engine, whose model import
// and export are engine/tests/engine/fake_interchange.py: each is logged to
// <appData>/interchange.log, and an import's re-embeds become running jobs (fake_jobs.py).

const SOURCE = {
  id: 'src-chinook',
  name: 'chinook',
  kind: 'sqlite',
  label: '/data/chinook.sqlite',
  size_bytes: 1000,
  schemas: 1,
  tables: 11,
  columns: 64,
  pulled: '2026-11-03T10:00:00Z'
}

function withSource(): string {
  const userData = freshUserData()
  writeFileSync(join(userData, 'sources.json'), JSON.stringify([SOURCE]))
  return userData
}

function logged(userData: string, file: string): string[] {
  const path = join(userData, file)
  return existsSync(path) ? readFileSync(path, 'utf8').split('\n').filter(Boolean) : []
}

/** The native save dialog answers with `path`; the name it suggested is kept in `globalThis.suggested`. */
async function saveTo(app: ElectronApplication, path: string): Promise<void> {
  await app.evaluate(({ dialog }, picked) => {
    dialog.showSaveDialog = (async (_win: unknown, options: { defaultPath?: string }) => {
      ;(globalThis as { suggested?: string }).suggested = options.defaultPath
      return { canceled: false, filePath: picked }
    }) as unknown as typeof dialog.showSaveDialog
  }, path)
}

/** The native open dialog answers with `path`, as if the user picked it, and counts in `globalThis.opened`. */
async function pickFile(app: ElectronApplication, path: string): Promise<void> {
  await app.evaluate(({ dialog }, picked) => {
    const counted = globalThis as { opened?: number }
    counted.opened = 0
    dialog.showOpenDialog = (async () => {
      counted.opened = (counted.opened ?? 0) + 1
      return { canceled: false, filePaths: [picked] }
    }) as typeof dialog.showOpenDialog
  }, path)
}

const timesOpened = (app: ElectronApplication) => app.evaluate(() => (globalThis as { opened?: number }).opened)

async function openSection(app: ElectronApplication, page: Page) {
  await clickMenuItem(app, 'settings')
  return page.getByRole('group', { name: 'Import/Export' })
}

const folder = () => mkdtempSync(join(tmpdir(), 'ontological-interchange-'))

test('exports a source as Ontological YAML, and as Ossie with the ontology zip, through the save dialog', async () => {
  const userData = withSource()
  const out = folder()
  const { app, page } = await launch(userData)
  try {
    const section = await openSection(app, page)
    await section.getByLabel('Source').selectOption('chinook')
    // The ontology is an Ossie document, so the box waits for that format.
    await expect(section.getByRole('checkbox', { name: /With the ontology/ })).toBeDisabled()

    const yaml = join(out, 'picked.yaml')
    await saveTo(app, yaml)
    await section.getByRole('button', { name: 'Export…' }).click()
    await expect(section.getByRole('status', { name: 'Export status' })).toHaveText(`Saved to ${yaml}`)
    expect(await app.evaluate(() => (globalThis as { suggested?: string }).suggested)).toBe('chinook-model.yaml')
    expect(readFileSync(yaml, 'utf8')).toContain('format: auto_ontology')

    await section.getByRole('radio', { name: 'Apache Ossie YAML' }).check()
    await section.getByRole('checkbox', { name: /With the ontology/ }).check()
    const zip = join(out, 'picked.zip')
    await saveTo(app, zip)
    await section.getByRole('button', { name: 'Export…' }).click()
    await expect(section.getByRole('status', { name: 'Export status' })).toHaveText(`Saved to ${zip}`)
    expect(await app.evaluate(() => (globalThis as { suggested?: string }).suggested)).toBe('chinook-ossie.zip')
    expect(readFileSync(zip).subarray(0, 2).toString()).toBe('PK')

    expect(logged(userData, 'interchange.log')).toEqual(['export auto_ontology chinook', 'export ossie chinook ontology'])
  } finally {
    await app.close()
  }
})

test('File → Import… reads a file, shows what it created and the re-embed job it started', async () => {
  const userData = withSource()
  const file = join(folder(), 'chinook-ossie.yaml')
  writeFileSync(
    file,
    [
      'semantic_model: {}',
      'databases: [{name: chinook}]',
      'created: {terms: 3, column_attributes: 7}',
      'skipped: {databases: 1, tables: 11, columns: 64}',
      ''
    ].join('\n')
  )
  const { app, page } = await launch(userData)
  try {
    await pickFile(app, file)
    await clickMenuItem(app, 'import')
    await expect(page.locator('h1')).toHaveText('Settings')
    const section = page.getByRole('group', { name: 'Import/Export' })
    await expect(section.getByRole('status', { name: 'Import status' })).toHaveText(
      'Imported chinook-ossie.yaml as Apache Ossie YAML.'
    )

    const rows = section.getByRole('table', { name: 'Imported' }).getByRole('row')
    await expect(rows).toHaveText([
      'KindCreatedAlready there',
      'Databases01',
      'Tables011',
      'Columns064',
      'Terms30',
      'Column attributes70'
    ])
    await expect(section.getByRole('list', { name: 'Re-embed jobs' })).toHaveText('Re-embed · chinook · running')
    expect(logged(userData, 'interchange.log')).toEqual(['import replace=True'])
    expect(logged(userData, 'jobs.log')).toContain('reembed chinook')

    await section.getByRole('button', { name: 'Open Jobs' }).click()
    await expect(page.locator('h1')).toHaveText('Jobs')
    const earlierRuns = page.getByTestId('rail').getByRole('region', { name: 'Earlier' })
    await earlierRuns.getByRole('button', { name: /Re-embed · chinook · running/ }).click()
    await expect(page.getByTestId('job-card').getByRole('heading')).toHaveText('Re-embed · chinook')

    // Coming back to Settings doesn't ask again: File → Import… was used up by the one import.
    await clickMenuItem(app, 'settings')
    await expect(page.getByRole('group', { name: 'Import/Export' })).toBeVisible()
    await page.waitForTimeout(500)
    expect(await timesOpened(app)).toBe(1)
    expect(logged(userData, 'interchange.log')).toEqual(['import replace=True'])

    // Asking again does.
    await clickMenuItem(app, 'import')
    await expect.poll(() => logged(userData, 'interchange.log')).toEqual(['import replace=True', 'import replace=True'])
    expect(await timesOpened(app)).toBe(2)
  } finally {
    await app.close()
  }
})

test('an import the engine refuses, or a file that isn’t text, says why and writes nothing', async () => {
  const userData = withSource()
  const dir = folder()
  const refused = join(dir, 'taken.yaml')
  writeFileSync(refused, 'databases: [{name: chinook}]\nerror: a Term named Album already exists\n')
  const binary = join(dir, 'model.yaml')
  writeFileSync(binary, Buffer.from([0xff, 0xfe, 0x00, 0x41]))
  const { app, page } = await launch(userData)
  try {
    const section = await openSection(app, page)
    const status = section.getByRole('status', { name: 'Import status' })
    await section.getByRole('checkbox', { name: /^Replace/ }).uncheck()

    await pickFile(app, refused)
    await section.getByRole('button', { name: 'Import…' }).click()
    await expect(status).toHaveText("Couldn't import taken.yaml: a Term named Album already exists")
    expect(logged(userData, 'interchange.log')).toEqual(['import replace=False'])

    await pickFile(app, binary)
    await section.getByRole('button', { name: 'Import…' }).click()
    await expect(status).toContainText("model.yaml isn't UTF-8 text")
    expect(logged(userData, 'interchange.log')).toEqual(['import replace=False'])
    expect(logged(userData, 'jobs.log')).not.toContain('reembed chinook')
    await expect(section.getByRole('table', { name: 'Imported' })).toHaveCount(0)
  } finally {
    await app.close()
  }
})
