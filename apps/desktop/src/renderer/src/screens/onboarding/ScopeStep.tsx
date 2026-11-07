import { useEffect, useRef } from 'react'
import type { CatalogSchema, Source, SourceCatalog } from '@shared/api'
import { useEngineGet } from '../../api/engine'
import { plural } from '../../format'
import { Problem, StepCard, StepHeading } from './Card'

/** The tables left out of the compile, by table id. Everything else is in scope. */
export type Excluded = ReadonlySet<string>

interface ScopeStepProps {
  current: boolean
  sources: Source[]
  excluded: Excluded
  onChange(excluded: Excluded): void
}

/** DESIGN.md §5.1 step 3: pick schemas or tables, then see the split vs merged estimate before
 * anything spends quota. The estimate (§6.5) arrives with M3.7; its slot is here. */
export function ScopeStep({ current, sources, excluded, onChange }: ScopeStepProps) {
  const total = sources.reduce((n, s) => n + s.tables, 0)
  const chosen = total - excluded.size

  if (!current) {
    return (
      <StepCard current={false} label="Scope">
        <StepHeading current={false} title="Scope" />
        <p className="m-0 text-[13px] text-text-2">
          {chosen} of {plural(total, 'table')} chosen to compile
        </p>
      </StepCard>
    )
  }

  return (
    <StepCard current label="Choose scope">
      <StepHeading
        current
        title="Choose scope"
        intro="Pick the schemas and tables to compile. Compiling is the step that uses Claude, so a smaller scope costs less."
      />
      <div className="flex flex-col gap-3">
        {sources.map((source) => (
          <SourceScope key={source.id} source={source} excluded={excluded} onChange={onChange} />
        ))}
      </div>
      <p className="m-0 text-[13px] text-text-2 tabular-nums" data-testid="scope-count">
        {chosen} of {plural(total, 'table')} chosen
      </p>
      <div
        data-testid="estimate-slot"
        className="flex flex-col gap-1 rounded-[10px] border border-dashed border-text-faint px-4 py-3.5"
      >
        <span className="text-xs font-semibold tracking-[0.06em] text-text-muted uppercase">Estimate</span>
        <span>The calls, Claude usage and time for a split and a merged compile show here before anything runs.</span>
        <span className="text-[13px] text-text-muted">Not available in this build yet.</span>
      </div>
    </StepCard>
  )
}

function SourceScope({ source, excluded, onChange }: { source: Source; excluded: Excluded; onChange(e: Excluded): void }) {
  const catalog = useEngineGet<SourceCatalog>(`/sources/${source.id}/catalog`)
  if (catalog.error) return <Problem>Couldn’t read {source.name}’s tables: {catalog.error}</Problem>
  if (!catalog.data) return <div className="h-10 rounded-lg bg-surface-sunken" aria-label={`Loading ${source.name}`} />
  return (
    <fieldset className="m-0 flex flex-col gap-1.5 rounded-[10px] border border-border-subtle p-3">
      <legend className="px-1 font-mono text-[13px]">{source.name}</legend>
      {catalog.data.schemas.map((schema) => (
        <SchemaScope key={schema.id} schema={schema} excluded={excluded} onChange={onChange} />
      ))}
    </fieldset>
  )
}

function SchemaScope({ schema, excluded, onChange }: { schema: CatalogSchema; excluded: Excluded; onChange(e: Excluded): void }) {
  const box = useRef<HTMLInputElement>(null)
  const ids = schema.tables.map((t) => t.id)
  const out = ids.filter((id) => excluded.has(id)).length
  const all = out === 0
  const none = out === ids.length
  useEffect(() => {
    if (box.current) box.current.indeterminate = !all && !none
  }, [all, none])

  const set = (tableIds: string[], include: boolean): void => {
    const next = new Set(excluded)
    for (const id of tableIds) {
      if (include) next.delete(id)
      else next.add(id)
    }
    onChange(next)
  }

  return (
    <div className="flex flex-col">
      <label className="flex min-h-9 items-center gap-2 rounded-md px-1">
        <input
          ref={box}
          type="checkbox"
          checked={all}
          aria-label={`Schema ${schema.name}`}
          onChange={() => set(ids, !all)}
          className="size-4 accent-accent-solid"
        />
        <span className="font-medium">{schema.name}</span>
        <span className="text-xs text-text-muted tabular-nums">
          {ids.length - out} of {plural(ids.length, 'table')}
        </span>
      </label>
      <ul className="m-0 grid list-none grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-x-3 py-1 pl-7">
        {schema.tables.map((table) => (
          <li key={table.id}>
            <label className="flex min-h-8 items-center gap-2">
              <input
                type="checkbox"
                checked={!excluded.has(table.id)}
                onChange={(e) => set([table.id], e.target.checked)}
                className="size-4 accent-accent-solid"
              />
              <span className="truncate font-mono text-xs">{table.name}</span>
            </label>
          </li>
        ))}
      </ul>
    </div>
  )
}
