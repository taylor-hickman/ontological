import { describe, expect, it } from 'vitest'
import { errorDetail } from '../../src/renderer/src/engineApi'
import { exportFileName, lastJobId, startedReembeds, summaryRows } from '../../src/renderer/src/interchange/format'
import type { JobSummary } from '../../src/renderer/src/jobs/types'

function job(id: number, database_name: string, kind: JobSummary['kind'] = 'reembed'): JobSummary {
  return {
    id,
    kind,
    database_name,
    state: 'running',
    paused_until: null,
    error: null,
    created_at: '2026-11-07T10:00:00Z',
    finished_at: null,
    tables: 0,
    tables_done: 0,
    calls: 0,
    cache_hits: 0,
    new_tables: [],
    dismissed: false,
    table_calls: 'split'
  }
}

describe('exportFileName', () => {
  it.each([
    ['chinook', 'auto_ontology', false, 'chinook-model.yaml'],
    ['chinook', 'ossie', false, 'chinook-ossie.yaml'],
    ['chinook', 'ossie', true, 'chinook-ossie.zip'],
    [null, 'auto_ontology', false, 'ontological-model.yaml'],
    [null, 'ossie', false, 'ossie-model.yaml'],
    [null, 'ossie', true, 'ossie-model.zip'],
    // The save dialog takes a file name, not a path.
    ['../data/shop', 'auto_ontology', false, '-data-shop-model.yaml'],
    ['...', 'ossie', false, 'ossie-model.yaml']
  ] as const)('%s, %s, ontology %s → %s', (database, format, withOntology, name) => {
    expect(exportFileName(database, format, withOntology)).toBe(name)
  })
})

describe('summaryRows', () => {
  it('lists the kinds the file held, in document order, created and already there', () => {
    const rows = summaryRows({
      created: { databases: 1, schemas: 1, tables: 0, columns: 0, terms: 3, column_attributes: 7, sql_attributes: 0, custom_analyses: 0 },
      skipped: { databases: 0, schemas: 0, tables: 11, columns: 64, terms: 0, column_attributes: 0, sql_attributes: 0, custom_analyses: 0 },
      replace: true,
      format: 'ossie'
    })
    expect(rows).toEqual([
      { label: 'Databases', created: 1, existing: 0 },
      { label: 'Schemas', created: 1, existing: 0 },
      { label: 'Tables', created: 0, existing: 11 },
      { label: 'Columns', created: 0, existing: 64 },
      { label: 'Terms', created: 3, existing: 0 },
      { label: 'Column attributes', created: 7, existing: 0 }
    ])
  })

  it('treats a missing kind as none', () => {
    expect(summaryRows({ created: { terms: 2 }, skipped: {}, replace: false, format: 'auto_ontology' })).toEqual([
      { label: 'Terms', created: 2, existing: 0 }
    ])
  })
})

describe('startedReembeds', () => {
  it('finds each database’s newest re-embed after the import began, or null until it shows', () => {
    const jobs = [job(9, 'chinook'), job(8, 'chinook', 'compile'), job(7, 'chinook'), job(3, 'wwi')]
    expect(lastJobId([])).toBe(0)
    expect(startedReembeds(jobs, ['chinook', 'wwi'], 6)).toEqual([
      { database: 'chinook', job: jobs[0] },
      // wwi's re-embed is from before the import: not this one's.
      { database: 'wwi', job: null }
    ])
  })
})

describe('errorDetail', () => {
  it('passes a string through', () => {
    expect(errorDetail('a Term named Album already exists')).toBe('a Term named Album already exists')
  })

  it('joins pydantic’s errors, by where they are', () => {
    expect(
      errorDetail([
        { loc: ['body', 'semantic_layer', 'terms', 0, 'name'], msg: 'Field required', type: 'missing' },
        { loc: [], msg: 'Value error, bad document' }
      ])
    ).toBe('semantic_layer.terms.0.name: Field required; Value error, bad document')
  })

  it('has nothing to say for anything else', () => {
    expect(errorDetail(undefined)).toBeNull()
    expect(errorDetail([])).toBeNull()
  })
})
