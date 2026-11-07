import { useState } from 'react'
import type { TermRow } from '@shared/api'
import { CheckIcon, SearchIcon } from '../components/icons'
import { whenShort } from '../jobs/format'
import { certifiedFields, type Filter, FILTERS, filterTerms, kindLabel, type SortKey, sortTerms } from './format'
import { NoSources } from './OntologyScreen'
import type { OntologyState } from './useOntology'

const COLUMNS: { key: SortKey; label: string; numeric?: boolean }[] = [
  { key: 'name', label: 'Name' },
  { key: 'kind', label: 'Term type' },
  { key: 'certified', label: 'Certified' },
  { key: 'synonyms', label: 'Synonyms' },
  { key: 'attributes', label: 'Attributes', numeric: true },
  { key: 'changed', label: 'Last change' }
]

/** DESIGN.md §5.5: the glossary as a sortable table with filter chips; the selected Term opens in
 * the term editor in the rail. */
export function TermsMain({ state, onAddSource }: { state: OntologyState; onAddSource(): void }) {
  const [sort, setSort] = useState<{ key: SortKey; ascending: boolean }>({ key: 'name', ascending: true })
  const [filters, setFilters] = useState<ReadonlySet<Filter>>(new Set())
  const [needle, setNeedle] = useState('')
  const glossary = state.glossary.data?.source_id === state.source?.id ? state.glossary.data : undefined

  if (state.sources.length === 0) return <NoSources onAddSource={onAddSource} />
  if (state.glossary.error) return <p className="m-0 text-text">Couldn’t read the glossary: {state.glossary.error}</p>
  if (!glossary) return <div className="h-60 rounded-xl bg-surface-sunken" aria-label="Loading terms" />

  const now = Date.now()
  const rows = sortTerms(filterTerms(glossary.terms, filters, needle, now), sort.key, sort.ascending)
  const toggle = (f: Filter) => {
    const next = new Set(filters)
    if (next.has(f)) next.delete(f)
    else next.add(f)
    setFilters(next)
  }

  return (
    <div className="flex flex-col gap-4" data-testid="terms">
      <div className="flex flex-wrap items-center gap-2">
        <label className="flex min-h-9 min-w-[240px] items-center gap-2 rounded-lg border border-border bg-surface px-2.5 text-text-muted focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-accent">
          <SearchIcon />
          <input
            type="search"
            value={needle}
            onChange={(e) => setNeedle(e.target.value)}
            aria-label="Filter terms"
            placeholder="Filter by name, synonym or table"
            className="min-w-0 flex-1 bg-transparent text-text outline-none select-text placeholder:text-text-muted"
          />
        </label>
        {FILTERS.map((f) => (
          <button
            key={f.id}
            type="button"
            aria-pressed={filters.has(f.id)}
            onClick={() => toggle(f.id)}
            className={`min-h-9 rounded-full border px-3 text-[13px] ${
              filters.has(f.id) ? 'border-accent-border bg-accent-soft text-text' : 'border-border bg-surface text-text-2'
            }`}
          >
            {f.label}
          </button>
        ))}
        <span className="ml-auto text-[13px] text-text-muted tabular-nums">
          {glossary.certified} of {glossary.total} certified
        </span>
      </div>
      {glossary.total === 0 ? (
        <p className="m-0 text-[13px] text-text-muted">No terms yet. Compile {glossary.database_name} on Jobs to write them; that uses Claude quota.</p>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-border bg-surface">
          <table className="w-full border-collapse text-[13px]" data-testid="glossary">
            <thead className="bg-surface-sunken">
              <tr>
                {COLUMNS.map((c) => (
                  <th
                    key={c.key}
                    scope="col"
                    aria-sort={sort.key === c.key ? (sort.ascending ? 'ascending' : 'descending') : 'none'}
                    className={`border-b border-border-subtle px-3 font-semibold text-text-2 ${c.numeric ? 'text-right' : 'text-left'}`}
                  >
                    <button
                      type="button"
                      onClick={() => setSort({ key: c.key, ascending: sort.key === c.key ? !sort.ascending : true })}
                      className="min-h-10 text-xs"
                    >
                      {c.label}
                      {sort.key === c.key && <span aria-hidden="true">{sort.ascending ? ' ↑' : ' ↓'}</span>}
                    </button>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <Row key={row.id} row={row} selected={row.id === state.termId} onSelect={() => state.selectTerm(row.id)} now={now} />
              ))}
              {rows.length === 0 && (
                <tr>
                  <td colSpan={COLUMNS.length} className="px-3 py-4 text-text-muted">
                    No terms match.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function Row({ row, selected, onSelect, now }: { row: TermRow; selected: boolean; onSelect(): void; now: number }) {
  return (
    <tr data-testid="glossary-row" className={`border-b border-border-subtle last:border-b-0 ${selected ? '[&>td]:bg-accent-soft' : ''}`}>
      <td className="px-3">
        <button type="button" onClick={onSelect} aria-pressed={selected} className="flex min-h-11 items-center gap-1.5 text-left font-medium">
          {row.certified && (
            <span className="text-verified" role="img" aria-label="certified">
              <CheckIcon size={12} />
            </span>
          )}
          {row.name}
        </button>
      </td>
      <td className="px-3 text-text-2">{kindLabel(row.kind)}</td>
      <td className={`px-3 ${row.name_certified || row.description_certified ? 'text-verified-text' : 'text-attention'}`}>
        {certifiedFields(row)}
      </td>
      <td className="max-w-[220px] truncate px-3 text-text-2" title={row.synonyms.join(', ')}>
        {row.synonyms.join(', ') || '—'}
      </td>
      <td className="px-3 text-right text-text-2 tabular-nums">
        {row.attributes}
        {row.sql_attributes > 0 && <span className="text-text-muted"> + {row.sql_attributes} SQL</span>}
      </td>
      <td className="px-3 text-text-muted">{row.changed_at ? whenShort(row.changed_at, now) : '—'}</td>
    </tr>
  )
}
