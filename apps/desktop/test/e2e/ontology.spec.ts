import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { expect, type Page, test } from '@playwright/test'
import { clickMenuItem, freshUserData, launch } from './app'

// The Ontology and Terms screens (DESIGN.md §5.3, §5.5) against the stub engine, which lists the
// sources in <appData>/sources.json and serves the ontology in <appData>/ontology.json, changing
// it as the real edits do and logging each edit to <appData>/ontology.log
// (engine/tests/engine/fake_ontology.py).

const SOURCE = {
  id: 'src-pagila',
  name: 'pagila',
  kind: 'duckdb',
  label: '/data/pagila.duckdb',
  size_bytes: 1000,
  schemas: 1,
  tables: 5,
  columns: 30,
  pulled: '2026-11-03T10:00:00Z'
}

const day = (n: number) => new Date(Date.now() - n * 24 * 60 * 60_000).toISOString()

function row(id: string, name: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    name,
    kind: 'entity',
    description: `A ${name.toLowerCase()}.`,
    synonyms: [],
    name_certified: false,
    description_certified: false,
    certified: false,
    tables: [name.toLowerCase()],
    attributes: 3,
    sql_attributes: 0,
    changed_at: day(20),
    ...extra
  }
}

const ROWS = [
  row('t-customer', 'Customer', { name_certified: true, description_certified: true, certified: true, synonyms: ['client'] }),
  row('t-rental', 'Rental', { synonyms: ['hire'], changed_at: day(1), sql_attributes: 1 }),
  row('t-film', 'Film', { description: null }),
  row('t-category', 'Category')
]

function detail(r: (typeof ROWS)[number]) {
  return {
    id: r.id,
    name: r.name,
    kind: 'entity',
    description: r.description,
    synonyms: r.synonyms,
    name_certified: r.name_certified,
    description_certified: r.description_certified,
    created: day(4),
    tables: [{ id: `tb-${r.name.toLowerCase()}`, name: r.name.toLowerCase(), schema_name: 'public', database_name: 'pagila' }],
    attributes: [
      { id: `${r.id}-a1`, name: 'id', column: `${r.name.toLowerCase()}_id`, table: r.name.toLowerCase(), datatype: 'INTEGER', description: null, certified: false, joins_to: null }
    ],
    joins:
      r.id === 't-rental'
        ? [
            { table: 'rental', column: 'customer_id', to: { id: 't-customer', name: 'Customer' } },
            { table: 'rental', column: 'film_id', to: { id: 't-film', name: 'Film' } }
          ]
        : [],
    sql_attributes:
      r.id === 't-rental'
        ? [{ id: 's1', name: 'Days out', description: null, expression: "date_diff('day', rental_date, return_date)", source: 'sql', certified: false, uses: 14 }]
        : [],
    history: [],
    undoable: null
  }
}

const node = (r: (typeof ROWS)[number]) => ({ id: r.id, name: r.name, certified: r.certified, terms: [] })
const SCENARIO = {
  glossaries: { [SOURCE.id]: { source_id: SOURCE.id, database_name: 'pagila', terms: ROWS, certified: 1, total: 4 } },
  terms: Object.fromEntries(ROWS.map((r) => [r.id, detail(r)])),
  graphs: {
    [SOURCE.id]: {
      semantic: {
        layer: 'semantic',
        nodes: ROWS.map(node),
        edges: [
          { source: 't-customer', target: 't-rental', via: null, join: 'rental.customer_id = customer.customer_id' },
          { source: 't-film', target: 't-rental', via: null, join: 'rental.film_id = film.film_id' },
          { source: 't-category', target: 't-film', via: 'film_category', join: 'film_category.film_id = film.film_id; …' }
        ]
      },
      data: {
        layer: 'data',
        nodes: ['customer', 'rental', 'film', 'film_category', 'category'].map((t) => ({
          id: `tb-${t}`,
          name: t,
          certified: t === 'customer',
          terms: ROWS.filter((r) => r.name.toLowerCase() === t).map((r) => ({ id: r.id, name: r.name }))
        })),
        edges: [
          { source: 'tb-customer', target: 'tb-rental', via: null, join: '' },
          { source: 'tb-film', target: 'tb-rental', via: null, join: '' },
          { source: 'tb-film', target: 'tb-film_category', via: null, join: '' },
          { source: 'tb-category', target: 'tb-film_category', via: null, join: '' }
        ]
      }
    }
  },
  paths: {
    't-customer:t-category': {
      terms: [
        { id: 't-customer', name: 'Customer' },
        { id: 't-rental', name: 'Rental' },
        { id: 't-film', name: 'Film' },
        { id: 't-category', name: 'Category' }
      ],
      hops: [
        { source: 't-customer', target: 't-rental', via: null, join: '', tables: ['tb-customer', 'tb-rental'] },
        { source: 't-rental', target: 't-film', via: null, join: '', tables: ['tb-rental', 'tb-film'] },
        { source: 't-film', target: 't-category', via: 'film_category', join: '', tables: ['tb-film', 'tb-film_category', 'tb-category'] }
      ],
      tables: ['tb-customer', 'tb-rental', 'tb-film', 'tb-film_category', 'tb-category']
    }
  }
}

function edits(userData: string): string[] {
  try {
    return readFileSync(join(userData, 'ontology.log'), 'utf8').trim().split('\n').filter(Boolean)
  } catch {
    return []
  }
}

async function start(): Promise<{ userData: string; page: Page; close(): Promise<void> }> {
  const userData = freshUserData()
  writeFileSync(join(userData, 'sources.json'), JSON.stringify([SOURCE]))
  writeFileSync(join(userData, 'ontology.json'), JSON.stringify(SCENARIO))
  const { app, page } = await launch(userData)
  await page.setViewportSize({ width: 1400, height: 900 })
  // The window reloads once the engine is up on its port; a menu command sent before that reload
  // is lost, so wait for the engine and retry until the screen is open.
  await expect(page.getByTestId('engine-status')).toHaveText('Engine running')
  await expect(async () => {
    await clickMenuItem(app, 'screen-ontology')
    await expect(page.locator('h1')).toHaveText('Ontology', { timeout: 1000 })
  }).toPass()
  await expect(page.getByTestId('graph-node').first()).toBeVisible()
  return { userData, page, close: () => app.close() }
}

test('the graph highlights a join path, and a term is renamed, certified and undone', async () => {
  const { userData, page, close } = await start()
  try {
    await expect(page.locator('h1')).toHaveText('Ontology')
    const nodes = page.getByTestId('graph-node')
    await expect(nodes).toHaveCount(4)
    await expect(page.getByRole('button', { name: 'Customer, certified' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Film, not reviewed' })).toBeVisible()
    const meter = page.getByTestId('certification-meter')
    await expect(meter).toContainText('1 of 4 terms certified')
    await expect(meter.getByRole('meter')).toHaveAttribute('aria-valuenow', '1')

    // The path picker: the path card and the nodes and edges on it
    await page.getByLabel('Path from').selectOption({ label: 'Customer' })
    await page.getByLabel('Path to').selectOption({ label: 'Category' })
    await expect(page.getByTestId('path-summary')).toHaveText('3 hopsCustomer → Rental → Film → Categoryvia film_category')
    await expect(page.locator('[data-testid="graph-node"][data-on-path]')).toHaveCount(4)
    await expect(page.locator('line[data-path-edge]')).toHaveCount(3)
    await expect(page.locator('line[data-path-edge][stroke-dasharray]')).toHaveCount(1)
    // The data layer lights up the tables, the junction included
    await page.getByRole('radio', { name: 'Data' }).click()
    await expect(page.getByRole('button', { name: 'film_category, not reviewed, on the path' })).toBeVisible()
    await expect(page.locator('line[data-path-edge]')).toHaveCount(4)
    await page.getByRole('radio', { name: 'Semantic' }).click()
    await page.getByRole('button', { name: 'Clear path' }).click()
    await expect(page.getByTestId('path-summary')).toHaveCount(0)

    // The list fallback has the same nodes
    await page.getByRole('button', { name: 'Show as list' }).click()
    await expect(page.getByTestId('graph-list').locator('tbody tr')).toHaveCount(4)
    await expect(page.getByTestId('graph-list')).toContainText('Film (via film_category)')
    await page.getByRole('button', { name: 'Show as graph' }).click()

    // Select Rental: the term editor opens in the rail
    await page.getByRole('button', { name: 'Rental, not reviewed' }).click()
    const editor = page.getByTestId('term-editor')
    await expect(editor.getByTestId('term-name')).toHaveText('Rental')
    await expect(editor).toContainText('Written by compile · not certified')
    await expect(editor).toContainText('from query history · used 14 times')
    await expect(editor.getByTestId('term-attributes')).toContainText('customer_id→ Customer')

    // Rename it and add a synonym, then save
    await editor.getByRole('button', { name: 'Rename' }).click()
    await editor.getByRole('textbox', { name: 'Name' }).fill('Loan')
    await editor.getByRole('button', { name: '+ Add' }).click()
    await editor.getByRole('textbox', { name: 'New synonym' }).fill('checkout')
    await editor.getByRole('textbox', { name: 'New synonym' }).press('Enter')
    await expect(editor).toContainText('Saving re-embeds Loan, so the next answer uses your wording.')
    await editor.getByRole('button', { name: 'Save' }).click()
    await expect(editor.getByRole('status')).toHaveText('Saved. The next answer uses Loan as you wrote it.')
    await expect(editor.getByTestId('term-name')).toHaveText('Loan')
    const history = editor.getByTestId('term-history').getByRole('listitem')
    await expect(history.nth(0)).toContainText('You added synonym “checkout”')
    await expect(history.nth(1)).toContainText('You renamed it from “Rental” to “Loan”')
    await expect(page.getByRole('button', { name: 'Loan, not reviewed' })).toBeVisible()
    expect(edits(userData)).toEqual(['edit t-rental {"name": "Loan", "synonyms": ["hire", "checkout"]}'])

    // A rename onto a taken name is refused, and says why
    await editor.getByRole('button', { name: 'Rename' }).click()
    await editor.getByRole('textbox', { name: 'Name' }).fill('Film')
    await editor.getByRole('button', { name: 'Save' }).click()
    await expect(editor.getByRole('status')).toHaveText("Not saved: a Term named 'Film' exists")
    await editor.getByRole('textbox', { name: 'Name' }).fill('Loan')

    // Certify both fields: the node and the meter follow
    await editor.getByRole('button', { name: 'Certify name' }).click()
    await expect(editor).toContainText('Name certified')
    await editor.getByRole('button', { name: 'Certify description' }).click()
    await expect(editor).toContainText('Description certified')
    await expect(page.getByRole('button', { name: 'Loan, certified' })).toBeVisible()
    await expect(meter).toContainText('2 of 4 terms certified')

    // Undo takes the last certification off again
    await editor.getByRole('button', { name: 'Undo' }).click()
    await expect(editor.getByRole('status')).toHaveText('Undone. The next answer uses the earlier wording.')
    await expect(editor).toContainText('not certified')
    await expect(history.nth(0)).toContainText('You took the certification off the description (undo)')
    await expect(meter).toContainText('1 of 4 terms certified')

    // An attribute's join opens the Term it leads to
    await editor.getByRole('button', { name: '→ Customer' }).click()
    await expect(editor.getByTestId('term-name')).toHaveText('Customer')
  } finally {
    await close()
  }
})

test('the glossary sorts and filters, and opens a term in the editor', async () => {
  const { page, close } = await start()
  try {
    await page.getByRole('button', { name: 'Terms' }).first().click()
    await expect(page.locator('h1')).toHaveText('Terms')
    const rows = page.getByTestId('glossary-row')
    await expect(rows).toHaveCount(4)
    await expect(rows.nth(0)).toContainText('Category')
    await expect(rows.nth(1)).toContainText('Customer')
    await expect(rows.nth(1)).toContainText('Name, description')
    await expect(rows.nth(1)).toContainText('client')

    // Sort by name, descending
    await page.getByRole('button', { name: 'Name', exact: true }).click()
    await expect(rows.nth(0)).toContainText('Rental')
    await expect(page.getByRole('columnheader', { name: /Name/ })).toHaveAttribute('aria-sort', 'descending')

    // The chips
    await page.getByRole('button', { name: 'Uncertified' }).click()
    await expect(rows).toHaveCount(3)
    await page.getByRole('button', { name: 'No description' }).click()
    await expect(rows).toHaveCount(1)
    await expect(rows.nth(0)).toContainText('Film')
    await page.getByRole('button', { name: 'No description' }).click()
    await page.getByRole('button', { name: 'Uncertified' }).click()
    await page.getByRole('button', { name: 'Changed this week' }).click()
    await expect(rows).toHaveCount(1)
    await expect(rows.nth(0)).toContainText('Rental')
    await page.getByRole('button', { name: 'Changed this week' }).click()
    await page.getByRole('searchbox', { name: 'Filter terms' }).fill('client')
    await expect(rows).toHaveCount(1)
    await page.getByRole('searchbox', { name: 'Filter terms' }).fill('')

    // A row opens the term editor
    await rows.filter({ hasText: 'Film' }).getByRole('button').click()
    const editor = page.getByTestId('term-editor')
    await expect(editor.getByTestId('term-name')).toHaveText('Film')
    // A Term without a description can't be certified until it has one
    await expect(editor.getByRole('button', { name: 'Certify description' })).toBeDisabled()
    await editor.getByRole('textbox', { name: 'Description' }).fill('A film in the catalogue.')
    await editor.getByRole('button', { name: 'Save' }).click()
    await expect(editor.getByRole('button', { name: 'Certify description' })).toBeEnabled()
    await expect(rows.filter({ hasText: 'Film' })).toHaveCount(1)
    await page.getByRole('button', { name: 'No description' }).click()
    await expect(rows).toHaveCount(0)
  } finally {
    await close()
  }
})
