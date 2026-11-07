import { describe, expect, it } from 'vitest'
import type { Graph, JoinPath, TermChange, TermDetail, TermRow } from '@shared/api'
import {
  addSynonym,
  certifiedFields,
  changesOf,
  descriptionAuthor,
  draftOf,
  filterTerms,
  historyLine,
  pathSummary,
  sortTerms
} from '../../src/renderer/src/ontology/format'
import { edgeKey, layoutGraph, NODE_WIDTH, pathOverlay } from '../../src/renderer/src/ontology/layout'

const NOW = Date.parse('2026-11-07T15:00:00Z')

function change(overrides: Partial<TermChange>): TermChange {
  return { id: 1, edit: 1, ts: '2026-11-07T14:00:00Z', field: 'name', old: null, new: null, actor: 'user', undoes: null, ...overrides }
}

function row(overrides: Partial<TermRow>): TermRow {
  return {
    id: 'x',
    name: 'X',
    kind: 'entity',
    description: 'An x.',
    synonyms: [],
    name_certified: false,
    description_certified: false,
    certified: false,
    tables: ['x'],
    attributes: 1,
    sql_attributes: 0,
    changed_at: '2026-10-20T00:00:00Z',
    ...overrides
  }
}

function detail(overrides: Partial<TermDetail> = {}): TermDetail {
  return {
    id: 't',
    name: 'Rental',
    kind: 'entity',
    description: 'One checkout.',
    synonyms: ['hire'],
    name_certified: false,
    description_certified: false,
    created: null,
    tables: [],
    attributes: [],
    joins: [],
    sql_attributes: [],
    history: [],
    undoable: null,
    ...overrides
  }
}

describe('history lines', () => {
  it('says what each change did, as the mockup words it', () => {
    expect(historyLine(change({ field: 'name_certified', old: false, new: true }))).toBe('You certified the name')
    expect(historyLine(change({ field: 'description_certified', old: true, new: false }))).toBe(
      'You took the certification off the description'
    )
    expect(historyLine(change({ field: 'synonyms', old: ['hire'], new: ['hire', 'loan'] }))).toBe('You added synonym “loan”')
    expect(historyLine(change({ field: 'synonyms', old: ['hire', 'loan'], new: ['hire'] }))).toBe('You removed synonym “loan”')
    expect(historyLine(change({ field: 'synonyms', old: ['a'], new: ['b'] }))).toBe('You changed the synonyms')
    expect(historyLine(change({ field: 'name', old: 'Track', new: 'Song' }))).toBe('You renamed it from “Track” to “Song”')
    expect(historyLine(change({ field: 'name', old: 'Song', new: 'Track', undoes: 3 }))).toBe(
      'You renamed it from “Song” to “Track” (undo)'
    )
    expect(historyLine(change({ field: 'description', old: 'a', new: '' }))).toBe('You cleared the description')
    expect(historyLine(change({ field: 'description', old: 'a', new: 'b', actor: 'import' }))).toBe('Import changed the description')
    expect(historyLine(change({ field: 'name', old: null, new: 'Rental', actor: 'import' }))).toBe('Import created it as “Rental”')
    expect(historyLine(change({ field: 'description', old: null, new: 'b', actor: 'import' }))).toBe('Import wrote the description')
    expect(historyLine(change({ field: 'description', old: 'a', new: 'b', actor: 'compile' }))).toBe('Compile changed the description')
  })

  it('names who wrote the description', () => {
    expect(descriptionAuthor(detail())).toBe('Written by compile')
    expect(descriptionAuthor(detail({ history: [change({ field: 'description', new: 'b' })] }))).toBe('Edited by you')
  })
})

describe('the editor draft', () => {
  it('sends only what changed, with the name trimmed', () => {
    const term = detail()
    expect(changesOf(term, draftOf(term))).toEqual({})
    expect(changesOf(term, { name: ' Loan ', description: 'One checkout.', synonyms: ['hire'] })).toEqual({ name: 'Loan' })
    expect(changesOf(term, { name: 'Rental', description: '', synonyms: ['hire', 'loan'] })).toEqual({
      description: '',
      synonyms: ['hire', 'loan']
    })
    expect(changesOf(detail({ description: null }), { name: 'Rental', description: '', synonyms: ['hire'] })).toEqual({})
  })

  it('adds a synonym once, trimmed, whatever its case', () => {
    expect(addSynonym(['hire'], ' loan ')).toEqual(['hire', 'loan'])
    expect(addSynonym(['hire'], 'HIRE')).toEqual(['hire'])
    expect(addSynonym(['hire'], '  ')).toEqual(['hire'])
  })
})

describe('the glossary', () => {
  const rows = [
    row({ id: 'a', name: 'Album', certified: true, name_certified: true, description_certified: true, synonyms: ['record'] }),
    row({ id: 'b', name: 'artist', description: null, changed_at: '2026-11-06T00:00:00Z', attributes: 4 }),
    row({ id: 'c', name: 'Customer', name_certified: true, tables: ['customers'] })
  ]

  it('filters by chip and by name, synonym or table', () => {
    const ids = (r: TermRow[]) => r.map((x) => x.id)
    expect(ids(filterTerms(rows, new Set(['uncertified']), '', NOW))).toEqual(['b', 'c'])
    expect(ids(filterTerms(rows, new Set(['no-description']), '', NOW))).toEqual(['b'])
    expect(ids(filterTerms(rows, new Set(['week']), '', NOW))).toEqual(['b'])
    expect(ids(filterTerms(rows, new Set(), 'RECORD', NOW))).toEqual(['a'])
    expect(ids(filterTerms(rows, new Set(), 'customers', NOW))).toEqual(['c'])
  })

  it('sorts by any column, ties by name', () => {
    expect(sortTerms(rows, 'name', true).map((r) => r.name)).toEqual(['Album', 'artist', 'Customer'])
    expect(sortTerms(rows, 'name', false).map((r) => r.name)).toEqual(['Customer', 'artist', 'Album'])
    expect(sortTerms(rows, 'certified', false).map((r) => r.id)).toEqual(['a', 'c', 'b'])
    expect(sortTerms(rows, 'attributes', false).map((r) => r.id)).toEqual(['b', 'a', 'c'])
    expect(sortTerms(rows, 'changed', false)[0]!.id).toBe('b')
  })

  it('names the certified fields', () => {
    expect(certifiedFields(rows[0]!)).toBe('Name, description')
    expect(certifiedFields(rows[2]!)).toBe('Name')
    expect(certifiedFields(rows[1]!)).toBe('None')
  })
})

const PATH: JoinPath = {
  terms: [
    { id: 'cu', name: 'Customer' },
    { id: 're', name: 'Rental' },
    { id: 'fi', name: 'Film' },
    { id: 'ca', name: 'Category' }
  ],
  hops: [
    { source: 'cu', target: 're', via: null, join: 'rental.customer_id = customer.customer_id', tables: ['t_cu', 't_re'] },
    { source: 're', target: 'fi', via: null, join: '…', tables: ['t_re', 't_fi'] },
    { source: 'fi', target: 'ca', via: 'film_category', join: '…', tables: ['t_fi', 't_fc', 't_ca'] }
  ],
  tables: ['t_cu', 't_re', 't_fi', 't_fc', 't_ca']
}

describe('the join path', () => {
  it('sums up as the path card says it', () => {
    expect(pathSummary(PATH)).toEqual({
      hops: '3 hops',
      chain: 'Customer → Rental → Film → Category',
      via: 'via film_category'
    })
  })

  it('lights up Terms on the semantic layer and tables on the data layer', () => {
    const semantic = pathOverlay(PATH, 'semantic')
    expect([...semantic.nodes]).toEqual(['cu', 're', 'fi', 'ca'])
    expect(semantic.edges.get(edgeKey('ca', 'fi'))).toBe('film_category')
    expect(semantic.edges.get(edgeKey('re', 'cu'))).toBeNull()
    const data = pathOverlay(PATH, 'data')
    expect(data.nodes.has('t_fc')).toBe(true)
    expect([...data.edges.keys()]).toContain(edgeKey('t_fc', 't_ca'))
    expect(pathOverlay(null, 'semantic').nodes.size).toBe(0)
  })
})

describe('the layout', () => {
  const graph: Pick<Graph, 'nodes' | 'edges'> = {
    nodes: ['a', 'b', 'c', 'd', 'e'].map((id) => ({ id, name: id, certified: false, terms: [] })),
    edges: [
      { source: 'a', target: 'b', via: null, join: '' },
      { source: 'b', target: 'c', via: null, join: '' },
      { source: 'c', target: 'd', via: null, join: '' },
      { source: 'a', target: 'x', via: null, join: '' }
    ]
  }

  it('places every node inside the canvas, apart, the same way each time', () => {
    const one = layoutGraph(graph)
    const two = layoutGraph(graph)
    expect([...one.at.entries()]).toEqual([...two.at.entries()])
    const points = [...one.at.values()]
    for (const p of points) {
      expect(p.x).toBeGreaterThanOrEqual(NODE_WIDTH / 2)
      expect(p.x).toBeLessThanOrEqual(one.width - NODE_WIDTH / 2)
      expect(p.y).toBeLessThanOrEqual(one.height)
    }
    for (let i = 0; i < points.length; i++) {
      for (let j = i + 1; j < points.length; j++) {
        const d = Math.hypot(points[i]!.x - points[j]!.x, points[i]!.y - points[j]!.y)
        expect(d).toBeGreaterThan(60)
      }
    }
  })

  it('lays out an empty graph', () => {
    expect(layoutGraph({ nodes: [], edges: [] }).at.size).toBe(0)
  })
})
