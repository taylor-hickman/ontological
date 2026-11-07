import { describe, expect, it } from 'vitest'
import {
  callResult,
  countdown,
  currentJob,
  earlierLine,
  jobBanner,
  jobsBadge,
  jobStatusLine,
  logTime,
  modelName,
  newTablePrompts,
  segments,
  dollars,
  estimateLine,
  estimateRange,
  minutes,
  optionsShown,
  stageLegend,
  tileLabel
} from '../../src/renderer/src/jobs/format'
import type { CallRow, JobSummary, OptionEstimate, TableProgress } from '../../src/renderer/src/jobs/types'

const NOW = Date.parse('2026-11-06T15:32:00Z')

function job(overrides: Partial<JobSummary> = {}): JobSummary {
  return {
    id: 1,
    kind: 'compile',
    database_name: 'wwi',
    state: 'running',
    paused_until: null,
    error: null,
    created_at: '2026-11-06T14:31:00Z',
    finished_at: null,
    tables: 48,
    tables_done: 31,
    calls: 142,
    cache_hits: 37,
    new_tables: [],
    dismissed: false,
    table_calls: 'split',
    ...overrides
  }
}

function call(overrides: Partial<CallRow> = {}): CallRow {
  return {
    id: 1,
    at: '2026-11-06T15:31:58Z',
    task: 'table_terms',
    table_name: 'cities_archive',
    model: 'claude-sonnet-5-5',
    outcome: 'ok',
    cached: false,
    attempt: 1,
    duration_ms: 9800,
    ...overrides
  }
}

describe('the call log', () => {
  it('names models as the user knows them', () => {
    expect(modelName('claude-sonnet-5-5')).toBe('Sonnet 5.5')
    expect(modelName('claude-haiku-5-5-20261001')).toBe('Haiku 5.5')
    expect(modelName('opus')).toBe('Opus')
    expect(modelName('something-else')).toBe('something-else')
  })

  it('says how each call ended', () => {
    expect(callResult(call())).toBe('9.8 s')
    expect(callResult(call({ cached: true, duration_ms: null }))).toBe('cached')
    expect(callResult(call({ outcome: 'limited' }))).toBe('limit reached')
    expect(callResult(call({ outcome: 'error' }))).toBe('no answer')
    expect(callResult(call({ outcome: 'invalid' }))).toBe('invalid, retried')
    expect(callResult(call({ outcome: 'invalid', attempt: 2 }))).toBe('invalid, fell back')
  })
})

describe('which job shows', () => {
  it('opens on the latest unfinished compile, else the latest compile', () => {
    const refresh = job({ id: 9, kind: 'ingest', state: 'done' })
    const done = job({ id: 8, state: 'done' })
    const paused = job({ id: 5, state: 'paused' })
    expect(currentJob([refresh, done, paused])?.id).toBe(5)
    expect(currentJob([refresh, done])?.id).toBe(8)
    expect(currentJob([refresh])).toBeNull()
  })

  it('offers a refresh’s new tables until they are dismissed', () => {
    const found = job({ id: 2, kind: 'ingest', state: 'done', new_tables: ['public.a'] })
    const none = job({ id: 3, kind: 'ingest', state: 'done' })
    const failed = job({ id: 4, kind: 'ingest', state: 'stopped', new_tables: [] })
    const dismissed = job({ id: 5, kind: 'ingest', state: 'done', new_tables: ['x.y'], dismissed: true })
    expect(newTablePrompts([found, none, failed, dismissed]).map((j) => j.id)).toEqual([2])
  })
})

describe('the sidebar badge and status bar', () => {
  it('counts tables while a compile runs and says Paused or Stopped otherwise', () => {
    expect(jobsBadge([job()])).toEqual({ text: '31/48', attention: false })
    expect(jobsBadge([job({ state: 'paused' })])).toEqual({ text: 'Paused', attention: true })
    expect(jobsBadge([job({ state: 'held' })])).toEqual({ text: 'Paused', attention: true })
    expect(jobsBadge([job({ state: 'stopped' })])).toEqual({ text: 'Stopped', attention: true })
    expect(jobsBadge([job({ state: 'done' })])).toBeNull()
    expect(jobsBadge([])).toBeNull()
  })

  it('names the job and what it is doing', () => {
    expect(jobStatusLine([job()])).toBe('Compile wwi · 31 of 48 tables')
    expect(jobStatusLine([job({ state: 'paused', paused_until: '2026-11-06T16:10:00Z' })])).toMatch(
      /^Compile wwi paused until \d{1,2}:10/
    )
    expect(jobStatusLine([job({ state: 'held' })])).toBe('Compile wwi paused by you')
    expect(jobStatusLine([job({ state: 'done' })])).toBeNull()
  })
})

describe('the banner', () => {
  it('says when a paused compile picks up, how soon, and what is saved', () => {
    const banner = jobBanner(job({ state: 'paused', paused_until: '2026-11-06T16:10:00Z' }), NOW)
    expect(banner?.title).toBe('Paused: your Claude usage limit is reached')
    expect(banner?.detail).toMatch(/^Compile picks up by itself at \d{1,2}:10.*, in 38 min\. The 31 finished tables are saved\.$/)
    expect(banner?.action).toBe('hold')
    expect(banner?.actionLabel).toBe('Don’t resume')
  })

  it('offers Resume for a held or stopped compile, with the login steps when that is why', () => {
    expect(jobBanner(job({ state: 'held' }), NOW)?.action).toBe('resume')
    const login = jobBanner(job({ state: 'stopped', error: 'table_terms: Claude didn’t answer (error; not logged in: …)' }), NOW)
    expect(login?.title).toBe('Stopped: Claude Code isn’t logged in')
    expect(login?.detail).toContain('run claude and type /login')
    expect(jobBanner(job(), NOW)).toBeNull()
    expect(jobBanner(job({ state: 'done' }), NOW)).toBeNull()
    expect(jobBanner(job({ kind: 'ingest', state: 'stopped' }), NOW)).toBeNull()
  })

  it('counts down in minutes, then hours', () => {
    expect(countdown('2026-11-06T15:32:20Z', NOW)).toBe('in 1 min')
    expect(countdown('2026-11-06T15:31:00Z', NOW)).toBe('in under a minute')
    expect(countdown('2026-11-06T17:37:00Z', NOW)).toBe('in 2 h 5 min')
    expect(countdown('2026-11-06T17:32:00Z', NOW)).toBe('in 2 h')
  })
})

describe('table tiles', () => {
  const table = (overrides: Partial<TableProgress>): TableProgress => ({
    name: 'cities',
    state: 'pending',
    table_calls: 'split',
    stages_done: 0,
    skipped: null,
    ...overrides
  })

  it('fills a done table, and a pending one up to the stages it got through', () => {
    expect(segments(table({ state: 'done', stages_done: 3 }), 3, 'running')).toEqual(['done', 'done', 'done'])
    expect(segments(table({ stages_done: 1 }), 3, 'running')).toEqual(['done', 'queued', 'queued'])
    expect(segments(table({}), 3, 'paused')).toEqual(['queued', 'queued', 'queued'])
  })

  it('shows the stages a table got through before its job stopped as checkpointed', () => {
    for (const state of ['paused', 'held', 'stopped'] as const) {
      expect(segments(table({ stages_done: 2 }), 3, state)).toEqual(['checkpointed', 'checkpointed', 'queued'])
    }
  })

  it('labels every bar in words', () => {
    expect(tileLabel(table({ state: 'done' }), 3)).toBe('done')
    expect(tileLabel(table({ stages_done: 2 }), 3)).toBe('2 of 3 stages done')
    expect(tileLabel(table({}), 3)).toBe('queued')
    expect(tileLabel(table({ state: 'skipped', skipped: 'no non-FK columns' }), 3)).toBe('skipped: no non-FK columns')
    expect(stageLegend(['table_keys', 'column_descriptions', 'table_terms'])).toBe('keys · descriptions · terms')
  })
})

describe('merged runs', () => {
  const table = (name: string, table_calls: 'split' | 'merged'): TableProgress => ({
    name,
    state: 'pending',
    table_calls,
    stages_done: 0,
    skipped: null
  })

  it('names merged\'s one stage, and lists each option the tables ran with, split first', () => {
    expect(stageLegend(['table_semantics'])).toBe('one merged call')
    expect(optionsShown([table('a', 'merged'), table('b', 'split')], 'merged')).toEqual(['split', 'merged'])
    expect(optionsShown([table('a', 'merged')], 'split')).toEqual(['merged'])
    expect(optionsShown([], 'merged')).toEqual(['merged'])
  })
})

describe('the estimate', () => {
  const option = (overrides: Partial<OptionEstimate> = {}): OptionEstimate => ({
    table_calls: 'split',
    calls: 70,
    calls_high: 77,
    calls_by_model: { haiku: 30, sonnet: 40 },
    usd: 0.42,
    usd_low: 0.29,
    usd_high: 0.63,
    seconds: 900,
    seconds_low: 630,
    seconds_high: 1350,
    ...overrides
  })

  it('says calls, time and usage on one line, and the range behind them', () => {
    expect(estimateLine(option())).toBe('≈ 70 calls · ≈ 15 min · ≈ $0.42 of usage')
    expect(estimateRange(option())).toBe('70–77 calls, 11 min to 23 min, $0.29 to $0.63 at API prices')
    expect(estimateLine(option({ calls: 0, usd: 0, seconds: 0 }))).toBe('No Claude calls')
    expect(estimateLine(option({ calls: 1, usd: 0.004, seconds: 20 }))).toBe('≈ 1 call · ≈ under a minute · ≈ under $0.01 of usage')
  })

  it('rounds time to minutes and hours, and usage to cents', () => {
    expect(minutes(29)).toBe('under a minute')
    expect(minutes(4800)).toBe('1 h 20 min')
    expect(minutes(7200)).toBe('2 h')
    expect(dollars(1.234)).toBe('$1.23')
  })
})

describe('earlier runs', () => {
  it('sums up each run in a line', () => {
    expect(earlierLine(job({ state: 'done', tables: 22, database_name: 'pagila' }))).toBe('Compile · pagila · 22 tables')
    expect(earlierLine(job({ state: 'held' }))).toBe('Compile · wwi · 31 of 48 tables')
    expect(earlierLine(job({ kind: 'ingest', state: 'done', new_tables: ['a.b', 'a.c'] }))).toBe('Ingest metadata · wwi · 2 new tables')
    expect(earlierLine(job({ kind: 'ingest', state: 'stopped' }))).toBe('Ingest metadata · wwi · failed')
  })
})

describe('log times', () => {
  it('keep the seconds and drop the day period', () => {
    expect(logTime('2026-11-06T15:31:58Z')).toMatch(/^\d{1,2}:31:58$/)
  })
})
