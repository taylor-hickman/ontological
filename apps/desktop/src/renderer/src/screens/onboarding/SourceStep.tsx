import { useState } from 'react'
import type { Added, AddSourceRequest, Source } from '@shared/api'
import { useEngineRequest } from '../../api/engine'
import { FileIcon, LockIcon, ServerIcon, UploadIcon } from '../../components/icons'
import { bytes, plural, shortPath } from '../../format'
import { Problem, StepCard, StepHeading } from './Card'

export const SOURCE_FILTERS = [
  { name: 'Databases and data files', extensions: ['duckdb', 'ddb', 'sqlite', 'sqlite3', 'db', 'parquet', 'csv', 'tsv', 'json', 'jsonl', 'ndjson'] }
]

type Server = 'postgres' | 'mysql'

const SERVER_LABEL: Record<Server, string> = { postgres: 'PostgreSQL', mysql: 'MySQL' }

/** "22 tables · 1 schema · 46 MB · structure read in 0.8 s" */
export function sourceFacts(source: Source, seconds?: number): string {
  return [
    plural(source.tables, 'table'),
    plural(source.schemas, 'schema'),
    bytes(source.size_bytes),
    seconds === undefined ? null : `structure read in ${seconds} s`
  ]
    .filter(Boolean)
    .join(' · ')
}

interface SourceStepProps {
  current: boolean
  sources: Source[]
  /** How long each source added in this session took to read, by id. */
  readTimes: Record<string, number>
  onAdded(added: Added): void
  onRemoved(id: string): void
}

/** DESIGN.md §5.1 step 2: a drop zone for files, server tiles, the Keychain sentence, and each
 * source once its structure is read. */
export function SourceStep({ current, sources, readTimes, onAdded, onRemoved }: SourceStepProps) {
  const request = useEngineRequest()
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [server, setServer] = useState<Server | null>(null)
  const [dragging, setDragging] = useState(false)

  const add = async (body: AddSourceRequest, what: string): Promise<boolean> => {
    if (request === null) return false
    setBusy(what)
    setError(null)
    try {
      onAdded(await request<Added>('/sources', { method: 'POST', body }))
      return true
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      return false
    } finally {
      setBusy(null)
    }
  }

  const addFiles = (paths: string[]): void => {
    const real = paths.filter(Boolean)
    if (real.length === 0) {
      setError('That isn’t a file on this Mac. Drop a file from Finder, or choose one.')
      return
    }
    void add({ kind: 'file', paths: real }, real.length === 1 ? shortPath(real[0]!) : plural(real.length, 'file'))
  }

  const choose = async (): Promise<void> => {
    const path = await window.ontological.openFile(SOURCE_FILTERS)
    if (path) addFiles([path])
  }

  const remove = async (id: string): Promise<void> => {
    if (request === null) return
    setError(null)
    try {
      await request<void>(`/sources/${id}`, { method: 'DELETE' })
      onRemoved(id)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  if (!current) {
    return (
      <StepCard current={false} label="Sources">
        <StepHeading current={false} title="Sources" />
        <p className="m-0 text-[13px] text-text-2">
          {sources.map((s) => s.name).join(', ')} · {plural(sources.reduce((n, s) => n + s.tables, 0), 'table')}
        </p>
      </StepCard>
    )
  }

  return (
    <StepCard current label="Add a source">
      <StepHeading
        current
        title={sources.length ? 'Add a source' : 'Add your first source'}
        intro="Ontological opens sources read-only. It reads table structure first, which costs no Claude usage."
      />

      <div
        data-testid="drop-zone"
        data-dragging={dragging || undefined}
        onDragOver={(e) => {
          e.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDragging(false)
          addFiles([...e.dataTransfer.files].map((f) => window.ontological.pathForFile(f)))
        }}
        className={`flex flex-col items-center gap-2.5 rounded-xl border-[1.5px] border-dashed px-5 py-7 text-center ${
          dragging ? 'border-accent bg-accent-soft' : 'border-text-faint bg-rail'
        }`}
      >
        <span className="text-text-muted">
          <UploadIcon />
        </span>
        <span className="font-medium">Drop a database file here</span>
        <span className="text-[13px] text-text-muted">DuckDB, SQLite, Parquet, CSV or JSON</span>
        <button
          type="button"
          onClick={() => void choose()}
          disabled={busy !== null || request === null}
          className="min-h-9 rounded-lg border border-border bg-surface px-3.5 font-medium text-text disabled:opacity-60"
        >
          Choose file…
        </button>
      </div>

      <div className="flex items-center gap-3 text-xs text-text-muted">
        <span className="h-px flex-1 bg-border-subtle" />
        or connect to a server
        <span className="h-px flex-1 bg-border-subtle" />
      </div>

      <div className="grid grid-cols-[repeat(auto-fit,minmax(200px,1fr))] gap-2.5">
        {(['postgres', 'mysql'] as const).map((kind) => (
          <button
            key={kind}
            type="button"
            aria-pressed={server === kind}
            onClick={() => setServer(server === kind ? null : kind)}
            className={`flex min-h-14 items-center gap-3 rounded-[10px] border bg-surface px-3.5 py-2.5 text-left text-text ${
              server === kind ? 'border-accent bg-accent-soft' : 'border-border'
            }`}
          >
            <span className="text-text-2">
              <ServerIcon />
            </span>
            <span className="flex flex-col">
              <span className="font-medium">{SERVER_LABEL[kind]}</span>
              <span className="text-xs text-text-muted">Host, database, user</span>
            </span>
          </button>
        ))}
      </div>
      {server && (
        <ServerForm
          key={server}
          kind={server}
          busy={busy !== null}
          onSubmit={(body) => add(body, `${body.database} on ${body.host}`)}
        />
      )}
      <span className="flex items-center gap-2 text-xs text-text-muted">
        <LockIcon />
        Passwords go in the macOS Keychain, never in Ontological’s files.
      </span>

      {busy && (
        <p role="status" className="m-0 text-[13px] text-text-2">
          Reading the structure of {busy}…
        </p>
      )}
      {error && <Problem>{error}</Problem>}

      {sources.length > 0 && (
        <ul className="m-0 flex list-none flex-col gap-2 p-0" aria-label="Added sources">
          {sources.map((source) => (
            <li
              key={source.id}
              data-testid="added-source"
              className="flex flex-wrap items-center gap-3 rounded-[10px] border border-accent-border bg-accent-soft px-3.5 py-3"
            >
              <span className="text-accent">
                {source.kind === 'postgres' || source.kind === 'mysql' ? <ServerIcon size={18} /> : <FileIcon />}
              </span>
              <span className="flex flex-[1_1_260px] flex-col">
                <span className="font-mono text-[13px] break-all">{shortPath(source.label)}</span>
                <span className="text-xs text-text-2">{sourceFacts(source, readTimes[source.id])}</span>
              </span>
              <button
                type="button"
                onClick={() => void remove(source.id)}
                aria-label={`Remove ${source.name}`}
                className="min-h-8 rounded-md px-2.5 text-text-2 hover:bg-surface"
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
    </StepCard>
  )
}

function ServerForm({
  kind,
  busy,
  onSubmit
}: {
  kind: Server
  busy: boolean
  onSubmit(body: Extract<AddSourceRequest, { kind: Server }>): Promise<boolean>
}) {
  const [host, setHost] = useState('localhost')
  const [port, setPort] = useState('')
  const [database, setDatabase] = useState('')
  const [user, setUser] = useState('')
  const [password, setPassword] = useState('')
  const ready = host.trim() && database.trim() && user.trim()

  return (
    <form
      aria-label={`${SERVER_LABEL[kind]} connection`}
      className="grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-2.5 rounded-[10px] border border-border-subtle bg-surface-sunken p-3.5"
      onSubmit={(e) => {
        e.preventDefault()
        if (!ready) return
        void onSubmit({
          kind,
          host: host.trim(),
          port: port.trim() ? Number(port) : null,
          database: database.trim(),
          user: user.trim(),
          password: password || null
        }).then((ok) => {
          if (ok) setPassword('')
        })
      }}
    >
      <Field label="Host" value={host} onChange={setHost} />
      <Field label="Port" value={port} onChange={setPort} placeholder={kind === 'postgres' ? '5432' : '3306'} inputMode="numeric" />
      <Field label="Database" value={database} onChange={setDatabase} />
      <Field label="User" value={user} onChange={setUser} />
      <Field label="Password" value={password} onChange={setPassword} type="password" />
      <div className="flex items-end">
        <button
          type="submit"
          disabled={!ready || busy}
          className="min-h-9 w-full rounded-lg bg-accent-solid px-3.5 font-medium text-white disabled:opacity-60"
        >
          Connect
        </button>
      </div>
    </form>
  )
}

function Field({
  label,
  value,
  onChange,
  type = 'text',
  placeholder,
  inputMode
}: {
  label: string
  value: string
  onChange(value: string): void
  type?: string
  placeholder?: string
  inputMode?: 'numeric'
}) {
  return (
    <label className="flex flex-col gap-1 text-xs text-text-2">
      {label}
      <input
        type={type}
        value={value}
        placeholder={placeholder}
        inputMode={inputMode}
        autoComplete="off"
        spellCheck={false}
        onChange={(e) => onChange(e.target.value)}
        className="min-h-9 rounded-lg border border-border bg-surface px-2.5 text-sm text-text select-text"
      />
    </label>
  )
}
