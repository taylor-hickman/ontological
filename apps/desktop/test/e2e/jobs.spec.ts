import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { expect, test } from '@playwright/test'
import { clickMenuItem, freshUserData, launch } from './app'

// The Jobs screen (DESIGN.md §5.4) against the stub engine, which serves the jobs in
// <appData>/jobs.json and logs each action to <appData>/jobs.log (engine/tests/engine/fake_jobs.py).

const inAnHour = new Date(Date.now() + 60 * 60_000).toISOString()
const earlier = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString()

const summary = {
  error: null,
  finished_at: null,
  new_tables: [],
  dismissed: false,
  paused_until: null,
  tables: 0,
  tables_done: 0,
  calls: 0,
  cache_hits: 0,
  table_calls: 'split'
}

function option(table_calls: 'split' | 'merged', calls: number, usd: number, seconds: number) {
  return {
    table_calls,
    calls,
    calls_high: calls + 1,
    calls_by_model: { sonnet: calls },
    usd,
    usd_low: usd * 0.7,
    usd_high: usd * 1.5,
    seconds,
    seconds_low: seconds * 0.7,
    seconds_high: seconds * 1.5
  }
}

function estimate(database_name: string, tables: number) {
  return {
    database_name,
    tables,
    split: option('split', 7 * tables, 0.012 * tables, 50 * tables),
    merged: option('merged', 2 * tables, 0.02 * tables, 40 * tables),
    spawn_overhead_s: 0.53
  }
}

const SCENARIO = {
  jobs: [
    {
      ...summary,
      id: 3,
      kind: 'ingest',
      database_name: 'pagila',
      state: 'done',
      created_at: earlier(30),
      finished_at: earlier(30),
      new_tables: ['public.review', 'public.badge', 'public.tag']
    },
    {
      ...summary,
      id: 2,
      kind: 'compile',
      database_name: 'wwi',
      state: 'paused',
      paused_until: inAnHour,
      created_at: earlier(60),
      tables: 4,
      tables_done: 2,
      calls: 9,
      cache_hits: 3
    },
    {
      ...summary,
      id: 1,
      kind: 'compile',
      database_name: 'chinook',
      state: 'done',
      created_at: earlier(3 * 24 * 60),
      finished_at: earlier(3 * 24 * 60 - 5),
      tables: 11,
      tables_done: 11,
      calls: 30
    }
  ],
  details: {
    '2': {
      tables: [
        { name: 'cities', state: 'done', stages_done: 4, skipped: null },
        { name: 'countries', state: 'skipped', stages_done: 0, skipped: 'no non-FK columns' },
        { name: 'cities_archive', state: 'pending', stages_done: 2, skipped: null },
        { name: 'vehicle_temperatures', state: 'pending', table_calls: 'merged', stages_done: 0, skipped: null }
      ]
    }
  },
  estimates: { wwi: estimate('wwi', 2), pagila: estimate('pagila', 3) },
  calls: {
    '2': [
      {
        id: 12,
        at: earlier(1),
        task: 'table_terms',
        table_name: 'cities_archive',
        model: 'claude-sonnet-5-5',
        outcome: 'limited',
        cached: false,
        attempt: 1,
        duration_ms: 800
      },
      {
        id: 11,
        at: earlier(2),
        task: 'column_descriptions',
        table_name: 'cities_archive',
        model: 'claude-haiku-5-5',
        outcome: 'ok',
        cached: false,
        attempt: 1,
        duration_ms: 6100
      },
      {
        id: 10,
        at: earlier(3),
        task: 'table_keys',
        table_name: 'cities_archive',
        model: 'sonnet',
        outcome: 'ok',
        cached: true,
        attempt: 1,
        duration_ms: null
      }
    ]
  }
}

/** Everything jobs.log holds, the engine's own re-embed at start included. */
function logged(userData: string): string[] {
  try {
    return readFileSync(join(userData, 'jobs.log'), 'utf8').trim().split('\n').filter(Boolean)
  } catch {
    return []
  }
}

/** What the app asked for: the log without the re-embed the engine starts itself. */
function actions(userData: string): string[] {
  return logged(userData).filter((line) => line !== 'reembed stale')
}

test('shows a paused compile, its tables and calls, and holds and resumes it', async () => {
  const userData = freshUserData()
  writeFileSync(join(userData, 'jobs.json'), JSON.stringify(SCENARIO))
  const { app, page } = await launch(userData)
  try {
    // The sidebar and status bar say so on every screen
    await expect(page.getByTestId('nav-badge')).toHaveText('Paused')
    await expect(page.getByTestId('status-job')).toContainText('Compile wwi paused until')
    await page.getByTestId('status-job').click()
    await expect(page.locator('h1')).toHaveText('Jobs')

    const banner = page.getByTestId('job-banner')
    await expect(banner).toContainText('Paused: your Claude usage limit is reached')
    await expect(banner).toContainText(/picks up by itself at .*, in (59 min|1 h)\. The 2 finished tables are saved\./)

    const card = page.getByTestId('job-card')
    await expect(card.getByRole('heading')).toHaveText('Compile · wwi')
    await expect(card.locator('dd')).toHaveText(['2 / 4', '9', '3'])
    const tiles = card.getByRole('list', { name: 'Tables' }).getByRole('listitem')
    await expect(tiles).toHaveCount(4)
    await expect(tiles.nth(0).getByRole('img')).toHaveAttribute('aria-label', 'done')
    await expect(tiles.nth(1).getByRole('img')).toHaveAttribute('aria-label', 'skipped: no non-FK columns')
    // Stopped mid-table: the two stages it got through are checkpointed
    const archive = tiles.nth(2).locator('[data-tone]')
    await expect(archive).toHaveCount(4)
    expect(await archive.evaluateAll((els) => els.map((e) => e.getAttribute('data-tone')))).toEqual([
      'checkpointed',
      'checkpointed',
      'queued',
      'queued'
    ])
    await expect(tiles.nth(2).getByRole('img')).toHaveAttribute('aria-label', '2 of 4 stages done')
    // A table its job ran merged has merged's one stage, and the legend explains both
    await expect(tiles.nth(3)).toHaveAttribute('data-table-calls', 'merged')
    await expect(tiles.nth(3).locator('[data-tone]')).toHaveCount(1)
    await expect(card).toContainText('Split tables: keys · descriptions · terms · synonyms')
    await expect(card).toContainText('Merged tables: one merged call')

    // The estimate for the tables left, split and merged; picking merged switches the job
    const estimateCard = page.getByTestId('estimate-card')
    await expect(estimateCard.getByRole('heading')).toHaveText('Remaining 2 tables: estimate')
    const split = estimateCard.getByRole('radio', { name: /Split/ })
    const merged = estimateCard.getByRole('radio', { name: /Merged/ })
    await expect(split).toHaveAttribute('aria-checked', 'true')
    await expect(split).toContainText('Split · current')
    await expect(split).toContainText('≈ 14 calls · ≈ 2 min · ≈ $0.02 of usage')
    await expect(merged).toContainText('≈ 4 calls')
    await merged.click()
    await expect(merged).toHaveAttribute('aria-checked', 'true')
    await expect(card).toContainText('Merged calls · started')

    // The rail: the new-tables prompt, the call log, earlier runs
    const rail = page.getByTestId('rail')
    await expect(rail.getByTestId('new-tables')).toContainText('3 new tables in pagila')
    await expect(rail.getByTestId('new-tables')).toContainText('That refresh used no Claude quota. Compiling them will.')
    const log = rail.getByTestId('call-log').getByRole('listitem')
    await expect(log).toHaveCount(3)
    await expect(log.nth(0)).toContainText('table_terms')
    await expect(log.nth(0)).toContainText('cities_archive · Sonnet 5.5')
    await expect(log.nth(0)).toContainText('limit reached')
    await expect(log.nth(1)).toContainText('Haiku 5.5')
    await expect(log.nth(1)).toContainText('6.1 s')
    await expect(log.nth(2)).toContainText('cached')
    await expect(rail.getByRole('region', { name: 'Earlier' })).toContainText('Compile · chinook · 11 tables')

    // "Don't resume" holds the job; Resume runs it again
    await banner.getByRole('button', { name: 'Don’t resume' }).click()
    await expect(banner).toContainText('Paused by you')
    await expect(page.getByTestId('status-job')).toHaveText('Compile wwi paused by you')
    await banner.getByRole('button', { name: 'Resume' }).click()
    await expect(banner).toHaveCount(0)
    await expect(card).toHaveAttribute('data-state', 'running')
    await expect(page.getByTestId('nav-badge')).toHaveText('2/4')

    // Pause on a running job
    await card.getByRole('button', { name: 'Pause' }).click()
    await expect(banner).toContainText('Paused by you')

    // Later puts off the prompt; Compile starts the source's compile
    await rail.getByRole('button', { name: 'Later' }).click()
    await expect(rail.getByTestId('new-tables')).toHaveCount(0)
    expect(actions(userData)).toEqual(['choose 2 merged', 'hold 2', 'resume 2', 'hold 2', 'dismiss 3'])

    // An earlier run opens in the card
    await rail.getByRole('button', { name: /Compile · chinook/ }).click()
    await expect(card.getByRole('heading')).toHaveText('Compile · chinook')
    await expect(card).toContainText(/finished/)
  } finally {
    await app.close()
  }
})

test('Compile on a new-tables prompt shows the estimate and starts that source’s compile', async () => {
  const userData = freshUserData()
  const scenario = structuredClone(SCENARIO)
  scenario.jobs[1]!.state = 'held'
  scenario.jobs[1]!.database_name = 'pagila'
  scenario.jobs[1]!.paused_until = null
  writeFileSync(join(userData, 'jobs.json'), JSON.stringify(scenario))
  const { app, page } = await launch(userData)
  try {
    await clickMenuItem(app, 'screen-jobs')
    const prompt = page.getByTestId('rail').getByTestId('new-tables')
    await expect(prompt.getByRole('radio', { name: /Split/ })).toContainText('≈ 21 calls')
    await prompt.getByRole('radio', { name: /Merged/ }).click()
    await prompt.getByRole('button', { name: 'Compile 3 tables' }).click()
    await expect(page.getByTestId('job-card')).toHaveAttribute('data-state', 'running')
    expect(actions(userData)).toEqual(['compile pagila merged'])
  } finally {
    await app.close()
  }
})

test('an engine with no jobs says so', async () => {
  const { app, page } = await launch(freshUserData())
  try {
    await clickMenuItem(app, 'screen-jobs')
    await expect(page.getByTestId('jobs-empty')).toContainText('No compile has run yet.')
    await expect(page.getByTestId('nav-badge')).toHaveCount(0)
    await expect(page.getByTestId('status-job')).toHaveCount(0)
  } finally {
    await app.close()
  }
})

test('the engine re-embeds at start, Settings → Embeddings re-embeds again, and the job shows on Jobs', async () => {
  const userData = freshUserData()
  const scenario = {
    jobs: [
      { ...summary, id: 5, kind: 'reembed', database_name: 'wwi', state: 'running', created_at: earlier(1) },
      { ...summary, id: 4, kind: 'compile', database_name: 'chinook', state: 'done', created_at: earlier(90), tables: 11, tables_done: 11 }
    ],
    embeddings: { model: 'snowflake-arctic-embed-m-v1.5', stale: ['chinook', 'wwi'] }
  }
  writeFileSync(join(userData, 'jobs.json'), JSON.stringify(scenario))
  const { app, page } = await launch(userData)
  try {
    await expect.poll(() => logged(userData)).toEqual(['reembed stale'])

    await clickMenuItem(app, 'settings')
    const section = page.getByRole('group', { name: 'Embeddings' })
    await expect(section.getByTestId('embedding-model')).toHaveText('snowflake-arctic-embed-m-v1.5')
    await expect(section.getByTestId('stale-databases')).toHaveText('chinook and wwi')
    await section.getByRole('button', { name: 'Re-embed' }).click()
    await expect(section.getByRole('status')).toHaveText('Re-embedding chinook and wwi.')
    expect(logged(userData)).toEqual(['reembed stale', 'reembed stale'])

    // The running re-embed is listed on Jobs, under the compile the screen opens on
    await section.getByRole('button', { name: 'Open Jobs' }).click()
    await expect(page.locator('h1')).toHaveText('Jobs')
    const earlierRuns = page.getByTestId('rail').getByRole('region', { name: 'Earlier' })
    await expect(earlierRuns).toContainText('Re-embed · wwi · running')
    await earlierRuns.getByRole('button', { name: /Re-embed · wwi/ }).click()
    const card = page.getByTestId('job-card')
    await expect(card.getByRole('heading')).toHaveText('Re-embed · wwi')
    await expect(card).toContainText('No Claude usage')
  } finally {
    await app.close()
  }
})
