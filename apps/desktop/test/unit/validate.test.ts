import { describe, expect, it } from 'vitest'
import { parseContextMenuItems, parseFileFilters, parseFileName, parseNotification } from '../../src/main/validate'

describe('parseFileFilters', () => {
  it('accepts well-formed filters and copies them', () => {
    const input = [{ name: 'DuckDB', extensions: ['duckdb', 'db'], extra: 'dropped' }]
    expect(parseFileFilters(input)).toEqual([{ name: 'DuckDB', extensions: ['duckdb', 'db'] }])
    expect(parseFileFilters([{ name: 'All files', extensions: ['*'] }])).toEqual([{ name: 'All files', extensions: ['*'] }])
  })

  it.each([
    ['not an array', 'csv'],
    ['a non-object entry', ['csv']],
    ['an empty name', [{ name: ' ', extensions: ['csv'] }]],
    ['no extensions', [{ name: 'CSV', extensions: [] }]],
    ['a dotted extension', [{ name: 'CSV', extensions: ['.csv'] }]],
    ['a path in an extension', [{ name: 'CSV', extensions: ['../x'] }]],
    ['too many filters', Array.from({ length: 21 }, () => ({ name: 'CSV', extensions: ['csv'] }))]
  ])('rejects %s', (_label, input) => {
    expect(() => parseFileFilters(input)).toThrow(TypeError)
  })
})

describe('parseFileName', () => {
  it('accepts a plain file name', () => {
    expect(parseFileName('answer.csv')).toBe('answer.csv')
  })

  it.each(['../answer.csv', 'dir/answer.csv', 'C:\\answer.csv', '..', '', 42])('rejects %j', (name) => {
    expect(() => parseFileName(name)).toThrow(TypeError)
  })
})

describe('parseContextMenuItems', () => {
  it('normalises items and separators', () => {
    expect(
      parseContextMenuItems([{ id: 'copy', label: 'Copy SQL' }, { type: 'separator' }, { id: 'open', label: 'Open', enabled: false }])
    ).toEqual([{ id: 'copy', label: 'Copy SQL', enabled: true }, { type: 'separator' }, { id: 'open', label: 'Open', enabled: false }])
  })

  it.each([
    ['an empty list', []],
    ['a missing label', [{ id: 'copy' }]],
    ['an unknown type', [{ type: 'submenu', id: 'x', label: 'X' }]],
    ['a non-boolean enabled', [{ id: 'x', label: 'X', enabled: 'yes' }]],
    ['too many items', Array.from({ length: 51 }, (_, i) => ({ id: `${i}`, label: 'X' }))]
  ])('rejects %s', (_label, input) => {
    expect(() => parseContextMenuItems(input)).toThrow(TypeError)
  })
})

describe('parseNotification', () => {
  it('accepts a title with an empty body', () => {
    expect(parseNotification('Compile finished', '')).toEqual({ title: 'Compile finished', body: '' })
  })

  it('rejects a missing title or an over-long body', () => {
    expect(() => parseNotification('', 'body')).toThrow(TypeError)
    expect(() => parseNotification('Title', 'x'.repeat(501))).toThrow(TypeError)
    expect(() => parseNotification(undefined, 'body')).toThrow(TypeError)
  })
})
