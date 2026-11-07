import { useState } from 'react'
import type { CatalogColumn, Source, SourceCatalog, TableDetail } from '@shared/api'
import { useEngineGet } from '../api/engine'
import { ChevronRightIcon, DatabaseIcon, KeyIcon, PlusIcon, SearchIcon, TableIcon } from '../components/icons'
import { plural, shortPath } from '../format'
import { shortcut } from '../shortcut'
import { sourceFacts } from './onboarding/SourceStep'

interface CatalogProps {
  sources: Source[] | undefined
  sourcesError: string | null
  selected: string | null
  onSelect(tableId: string): void
  onAddSource(): void
}

/** DESIGN.md §5.5: a source → schema → table tree with a filter; the selected table's detail is
 * in the right rail (`TablePanel`). */
export function Catalog({ sources, sourcesError, selected, onSelect, onAddSource }: CatalogProps) {
  const [filter, setFilter] = useState('')

  if (sourcesError) {
    return <p className="m-0 text-text">Couldn’t list the sources: {sourcesError}</p>
  }
  if (sources === undefined) {
    return <div data-testid="catalog-loading" className="h-40 rounded-xl bg-surface-sunken" aria-label="Loading sources" />
  }
  if (sources.length === 0) {
    return (
      <div data-testid="catalog-empty" className="flex flex-col items-start gap-3 rounded-xl border border-dashed border-text-faint px-6 py-8">
        <p className="m-0 font-medium">No sources yet</p>
        <p className="m-0 text-[13px] text-text-muted">
          Add a database file or a server. Ontological reads its structure, which costs no Claude usage.
        </p>
        <button
          type="button"
          onClick={onAddSource}
          className="flex min-h-10 items-center gap-2 rounded-lg bg-accent-solid px-4 font-medium text-white"
        >
          <PlusIcon />
          Add a source
          <kbd className="font-sans text-[11px] opacity-80">{shortcut('O')}</kbd>
        </button>
      </div>
    )
  }

  const needle = filter.trim().toLowerCase()
  return (
    <div className="flex flex-col gap-4">
      <label className="flex min-h-9 max-w-[420px] items-center gap-2 rounded-lg border border-border bg-surface px-2.5 text-text-muted focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-accent">
        <SearchIcon />
        <input
          type="search"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          aria-label="Filter tables"
          placeholder="Filter tables and schemas"
          className="min-w-0 flex-1 bg-transparent text-text outline-none select-text placeholder:text-text-muted"
        />
      </label>
      <ul aria-label="Sources, schemas and tables" className="m-0 flex list-none flex-col gap-3 p-0">
        {sources.map((source) => (
          <SourceNode key={source.id} source={source} needle={needle} selected={selected} onSelect={onSelect} />
        ))}
      </ul>
    </div>
  )
}

function SourceNode({
  source,
  needle,
  selected,
  onSelect
}: {
  source: Source
  needle: string
  selected: string | null
  onSelect(id: string): void
}) {
  const [open, setOpen] = useState(true)
  const catalog = useEngineGet<SourceCatalog>(open ? `/sources/${source.id}/catalog` : null, source.pulled)
  const schemas = (catalog.data?.schemas ?? [])
    .map((schema) => ({
      ...schema,
      tables: needle && !schema.name.toLowerCase().includes(needle)
        ? schema.tables.filter((t) => t.name.toLowerCase().includes(needle))
        : schema.tables
    }))
    .filter((schema) => schema.tables.length > 0 || !needle)
  return (
    <li data-testid="catalog-source" className="rounded-xl border border-border bg-surface">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="flex min-h-12 w-full items-center gap-2.5 rounded-xl px-3.5 py-2 text-left"
      >
        <span className={`text-text-muted transition-transform ${open ? 'rotate-90' : ''}`}>
          <ChevronRightIcon />
        </span>
        <span className="text-text-2">
          <DatabaseIcon />
        </span>
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="font-semibold">{source.name}</span>
          <span className="truncate font-mono text-xs text-text-muted" title={source.label}>
            {shortPath(source.label)}
          </span>
          <span className="text-xs text-text-2">{sourceFacts(source)}</span>
        </span>
      </button>
      {open && (
        <ul className="m-0 flex list-none flex-col gap-1 border-t border-border-subtle px-3.5 py-2.5">
          {catalog.error && <li className="text-[13px]">Couldn’t read the tables: {catalog.error}</li>}
          {!catalog.data && !catalog.error && <li className="h-8 rounded-md bg-surface-sunken" aria-label="Loading tables" />}
          {catalog.data && schemas.length === 0 && <li className="text-[13px] text-text-muted">No tables match.</li>}
          {schemas.map((schema) => (
            <li key={schema.id} className="flex flex-col">
              <span className="flex min-h-8 items-center gap-2 text-xs font-semibold tracking-[0.06em] text-text-muted uppercase">
                {schema.name}
                <span className="font-normal tracking-normal normal-case tabular-nums">{plural(schema.tables.length, 'table')}</span>
              </span>
              <ul className="m-0 grid list-none grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-1 p-0">
                {schema.tables.map((table) => (
                  <li key={table.id}>
                    <button
                      type="button"
                      data-testid="catalog-table"
                      onClick={() => onSelect(table.id)}
                      aria-current={selected === table.id || undefined}
                      className={`flex min-h-11 w-full items-center gap-2 rounded-lg px-2.5 text-left ${
                        selected === table.id ? 'bg-accent-soft text-text' : 'hover:bg-surface-sunken'
                      }`}
                    >
                      <span className="text-text-muted">
                        <TableIcon />
                      </span>
                      <span className="min-w-0 flex-1 truncate font-mono text-[13px]">{table.name}</span>
                      <span className="text-xs text-text-muted tabular-nums">{plural(table.columns, 'col', 'cols')}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

/** The right rail on Catalog: the selected table's columns with type, nullability, keys, sample
 * values, PII flag and inferred format; tags as chips. */
export function TablePanel({ tableId }: { tableId: string | null }) {
  const detail = useEngineGet<TableDetail>(tableId ? `/tables/${tableId}` : null)
  if (tableId === null) return <p className="m-0 text-[13px] text-text-muted">Select a table to see its columns.</p>
  if (detail.error) return <p className="m-0 text-[13px]">Couldn’t read the table: {detail.error}</p>
  const table = detail.data?.id === tableId ? detail.data : undefined
  if (!table) return <div className="h-40 rounded-lg bg-surface-sunken" aria-label="Loading table" />
  return (
    <div data-testid="table-detail" className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <span className="text-xs text-text-muted">
          {table.source} · {table.schema_name}
        </span>
        <h3 className="m-0 font-mono text-[15px] font-semibold break-all">{table.name}</h3>
        {table.description && <p className="m-0 text-[13px] text-text-2">{table.description}</p>}
        {table.tags.length > 0 && <Chips tags={table.tags} />}
      </div>
      <span className="text-xs font-semibold tracking-[0.06em] text-text-muted uppercase">
        Columns · {table.columns.length}
      </span>
      <ul className="m-0 flex list-none flex-col gap-0 p-0">
        {table.columns.map((column) => (
          <ColumnRow key={column.id} column={column} />
        ))}
      </ul>
    </div>
  )
}

function ColumnRow({ column }: { column: CatalogColumn }) {
  const facts = [
    column.data_type?.toLowerCase(),
    column.is_nullable === false ? 'not null' : column.is_nullable ? 'nullable' : null,
    column.is_unique && !column.pk ? 'unique' : null
  ].filter(Boolean)
  return (
    <li data-testid="column" className="flex flex-col gap-1 border-b border-border-subtle py-2.5 last:border-b-0">
      <span className="flex flex-wrap items-center gap-1.5">
        {column.pk && (
          <span className="text-accent" title="Primary key">
            <KeyIcon />
            <span className="sr-only">Primary key</span>
          </span>
        )}
        <span className="font-mono text-[13px] font-medium break-all">{column.name}</span>
        {column.pii && (
          <span className="rounded-full border border-attention-border bg-attention-soft px-1.5 text-[11px] font-medium text-attention-text">
            PII
          </span>
        )}
        {column.format && (
          <span className="rounded-full bg-surface-sunken px-1.5 font-mono text-[11px] text-text-2" title="Inferred format">
            {column.format}
          </span>
        )}
      </span>
      <span className="text-xs text-text-muted">{facts.join(' · ')}</span>
      {column.references.map((ref) => (
        <span key={ref} className="text-xs text-text-2">
          → <span className="font-mono">{ref}</span>
        </span>
      ))}
      {column.description && <span className="text-[13px] text-text-2">{column.description}</span>}
      {column.sample_values.length > 0 && (
        <span className="truncate font-mono text-[11px] text-text-muted" title={column.sample_values.join(', ')}>
          {column.sample_values.slice(0, 5).join(', ')}
        </span>
      )}
      {column.tags.filter((t) => t.trim().toLowerCase() !== 'pii').length > 0 && (
        <Chips tags={column.tags.filter((t) => t.trim().toLowerCase() !== 'pii')} />
      )}
    </li>
  )
}

function Chips({ tags }: { tags: string[] }) {
  return (
    <span className="flex flex-wrap gap-1">
      {tags.map((tag) => (
        <span key={tag} className="rounded-full border border-accent-border bg-accent-soft px-2 text-[11px] text-text">
          {tag}
        </span>
      ))}
    </span>
  )
}
