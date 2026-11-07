import { useEffect, useRef, useState } from 'react'
import type { ExportRequest, Imported, ModelFormat, Source } from '@shared/api'
import { engineFetch, engineSend } from '../engineApi'
import { jobTitle, stateWord } from '../jobs/format'
import type { JobSummary } from '../jobs/types'
import { useEngine } from '../useEngine'
import { FORMAT_NAMES, exportFileName, lastJobId, startedReembeds, summaryRows } from './format'

const FIELDSET = 'm-0 flex flex-col gap-3 rounded-xl border border-border bg-surface p-5'
const LEGEND = 'float-left mb-1 p-0 text-[15px] font-semibold'
const NOTE = 'clear-both m-0 text-[13px] text-text-muted'
const BUTTON =
  'min-h-9 rounded-lg border border-border bg-surface px-3 text-text-2 hover:text-text disabled:opacity-60'
const HEADING = 'm-0 text-[13px] font-semibold text-text-2'

const YAML_FILTERS = [{ name: 'Model YAML', extensions: ['yaml', 'yml'] }]

/** What File → Import… or Export… asks of the section, until it acts on it. */
export type InterchangeRequest = 'import' | 'export'

interface Done {
  file: string
  imported: Imported
  /** The highest job id before the import, so its re-embeds are the ones after. */
  afterId: number
}

/**
 * Settings → Import/Export (ARCHITECTURE.md §9.3). Export writes a source's model, or every
 * source's, as Ontological's YAML or Apache Ossie's, through a save dialog. Import reads a YAML
 * file of either, applies it in one transaction, and shows what it created and the re-embed job
 * it started for each database it wrote. Neither uses Claude.
 */
export function ImportExport({
  sources,
  jobs,
  request,
  onRequestHandled,
  onOpenJobs
}: {
  sources: Source[] | undefined
  jobs: JobSummary[]
  request: InterchangeRequest | null
  /** Clears `request`, so the section acts on it once, not again each time Settings opens. */
  onRequestHandled(): void
  onOpenJobs(): void
}) {
  const engine = useEngine()
  const running = engine.status === 'running' ? engine : null
  const sectionRef = useRef<HTMLFieldSetElement>(null)
  const jobsRef = useRef(jobs)
  jobsRef.current = jobs

  const [database, setDatabase] = useState('')
  const [format, setFormat] = useState<ModelFormat>('auto_ontology')
  const [withOntology, setWithOntology] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [exported, setExported] = useState<string | null>(null)
  const [exportError, setExportError] = useState<string | null>(null)

  const [replace, setReplace] = useState(true)
  const [importing, setImporting] = useState(false)
  const [done, setDone] = useState<Done | null>(null)
  const [importError, setImportError] = useState<string | null>(null)

  // A source that went away leaves the picker on every source.
  const picked = sources?.some((s) => s.name === database) ? database : ''
  const ossie = format === 'ossie'

  async function exportModel(): Promise<void> {
    if (running === null) return
    setExporting(true)
    setExported(null)
    setExportError(null)
    try {
      const body: ExportRequest = {
        databases: picked === '' ? [] : [picked],
        format,
        include_ontology: ossie && withOntology
      }
      const response = await engineSend(running, '/model/export', { method: 'POST', body })
      const data = new Uint8Array(await response.arrayBuffer())
      const path = await window.ontological.saveFile(exportFileName(picked || null, format, ossie && withOntology), data)
      if (path !== null) setExported(path)
    } catch (e) {
      setExportError(e instanceof Error ? e.message : String(e))
    } finally {
      setExporting(false)
    }
  }

  async function importModel(): Promise<void> {
    if (running === null) {
      setImportError("The engine isn't running, so nothing can be imported.")
      return
    }
    setImportError(null)
    let file
    try {
      file = await window.ontological.openTextFile(YAML_FILTERS)
    } catch (e) {
      setImportError(e instanceof Error ? e.message : String(e))
      return
    }
    if (file === null) return
    setImporting(true)
    setDone(null)
    try {
      const afterId = lastJobId(jobsRef.current)
      const imported = await engineFetch<Imported>(running, `/model/import?replace=${replace}`, {
        method: 'POST',
        text: file.text
      })
      setDone({ file: file.name, imported, afterId })
    } catch (e) {
      setImportError(`Couldn't import ${file.name}: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setImporting(false)
    }
  }

  // File → Import… and Export… land here, and Import opens its dialog at once. The request waits
  // while the engine is starting, as it is on the section's first render, then is used up as it's
  // acted on; App drops one left when the user leaves Settings. The action waits a tick, and a
  // cleanup cancels it, so StrictMode's second mount in development doesn't open a second dialog.
  const settling = engine.status === 'starting' || engine.status === 'restarting'
  useEffect(() => {
    if (request === null || settling) return
    const timer = setTimeout(() => {
      onRequestHandled()
      sectionRef.current?.scrollIntoView({ block: 'nearest' })
      if (request === 'export') sectionRef.current?.querySelector<HTMLElement>('select')?.focus()
      else void importModel()
    }, 0)
    return () => clearTimeout(timer)
    // Only a new request, or the engine settling, acts: the callbacks are the latest render's.
  }, [request, settling])

  const noSources = sources !== undefined && sources.length === 0
  return (
    <fieldset ref={sectionRef} className={FIELDSET} aria-label="Import/Export">
      <legend className={LEGEND}>Import/Export</legend>
      <p className={NOTE}>
        Moves a source's catalog and semantic model, with its Terms' synonyms and certification, to or from a YAML file:
        Ontological's own, or Apache Ossie's for other tools. Neither uses Claude usage.
      </p>

      <section aria-label="Export" className="flex flex-col gap-2">
        <h3 className={HEADING}>Export</h3>
        <div className="flex flex-wrap items-center gap-3 text-[13px]">
          <label className="flex items-center gap-2 text-text-2">
            Source
            <select
              value={picked}
              onChange={(e) => setDatabase(e.target.value)}
              disabled={noSources}
              className="min-h-9 rounded-lg border border-border bg-surface px-2 text-text"
            >
              <option value="">Every source</option>
              {(sources ?? []).map((s) => (
                <option key={s.id} value={s.name}>
                  {s.name}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div role="radiogroup" aria-label="Format" className="flex flex-wrap gap-x-4 gap-y-1 text-[13px] text-text-2">
          {(['auto_ontology', 'ossie'] as const).map((f) => (
            <label key={f} className="flex min-h-8 items-center gap-2">
              <input
                type="radio"
                name="export-format"
                value={f}
                checked={format === f}
                onChange={() => setFormat(f)}
                className="accent-[var(--accent-solid)]"
              />
              {FORMAT_NAMES[f]}
            </label>
          ))}
        </div>
        <label className={`flex min-h-8 items-center gap-2 text-[13px] ${ossie ? 'text-text-2' : 'text-text-faint'}`}>
          <input
            type="checkbox"
            checked={ossie && withOntology}
            disabled={!ossie}
            onChange={(e) => setWithOntology(e.target.checked)}
            className="accent-[var(--accent-solid)]"
          />
          With the ontology: a zip of the semantic model, the ontology and the mapping between them
        </label>
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            className={BUTTON}
            disabled={exporting || running === null || noSources}
            onClick={() => void exportModel()}
          >
            {exporting ? 'Exporting…' : 'Export…'}
          </button>
          <span role="status" aria-label="Export status" className="min-w-0 text-[13px] break-all text-text-2">
            {noSources
              ? 'Add a source to export its model.'
              : exportError !== null
                ? `Couldn't export: ${exportError}`
                : exported !== null
                  ? `Saved to ${exported}`
                  : ''}
          </span>
        </div>
      </section>

      <section aria-label="Import" className="flex flex-col gap-2 border-t border-border pt-3">
        <h3 className={HEADING}>Import</h3>
        <p className="m-0 text-[13px] text-text-muted">
          Reads either format in one transaction, then re-embeds each database it wrote as a job on the Jobs screen.
          What the store already holds is kept as it is.
        </p>
        <label className="flex min-h-8 items-center gap-2 text-[13px] text-text-2">
          <input
            type="checkbox"
            checked={replace}
            onChange={(e) => setReplace(e.target.checked)}
            className="accent-[var(--accent-solid)]"
          />
          Replace: remove the Terms and attributes of those databases that the file leaves out
        </label>
        <div className="flex flex-wrap items-center gap-3">
          <button type="button" className={BUTTON} disabled={importing || running === null} onClick={() => void importModel()}>
            {importing ? 'Importing…' : 'Import…'}
          </button>
          <span role="status" aria-label="Import status" className="min-w-0 text-[13px] text-text-2">
            {importError ?? (done !== null ? `Imported ${done.file} as ${FORMAT_NAMES[done.imported.summary.format]}.` : '')}
          </span>
        </div>
        {done !== null && <ImportResult done={done} jobs={jobs} onOpenJobs={onOpenJobs} />}
      </section>
    </fieldset>
  )
}

function ImportResult({ done, jobs, onOpenJobs }: { done: Done; jobs: JobSummary[]; onOpenJobs(): void }) {
  const rows = summaryRows(done.imported.summary)
  const reembeds = startedReembeds(jobs, done.imported.reembedding, done.afterId)
  return (
    <div className="flex flex-col gap-3">
      {rows.length === 0 ? (
        <p className="m-0 text-[13px] text-text-2">The file held nothing to import.</p>
      ) : (
        <table aria-label="Imported" className="w-full max-w-[420px] border-collapse text-[13px] tabular-nums">
          <thead>
            <tr className="text-left text-text-muted">
              <th className="py-1 pr-4 font-normal">Kind</th>
              <th className="py-1 pr-4 text-right font-normal">Created</th>
              <th className="py-1 text-right font-normal">Already there</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.label} className="border-t border-border text-text-2">
                <th scope="row" className="py-1 pr-4 text-left font-normal">
                  {row.label}
                </th>
                <td className="py-1 pr-4 text-right">{row.created.toLocaleString()}</td>
                <td className="py-1 text-right">{row.existing.toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="flex flex-col gap-1 text-[13px]">
        {done.imported.reembed_error !== null ? (
          <p className="m-0 text-text-2">Nothing was re-embedded: {done.imported.reembed_error}.</p>
        ) : reembeds.length === 0 ? (
          <p className="m-0 text-text-2">It wrote no database, so nothing needs re-embedding.</p>
        ) : (
          <ul aria-label="Re-embed jobs" className="m-0 flex list-none flex-col gap-1 p-0">
            {reembeds.map(({ database, job }) => (
              <li key={database} className="text-text-2">
                {job === null
                  ? `Re-embed · ${database} · starting`
                  : `${jobTitle(job)} · ${stateWord(job)}${job.state === 'stopped' && job.error ? `: ${job.error}` : ''}`}
              </li>
            ))}
          </ul>
        )}
        {reembeds.length > 0 && (
          <div>
            <button type="button" className={BUTTON} onClick={onOpenJobs}>
              Open Jobs
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
