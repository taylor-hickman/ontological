import { useState } from 'react'
import { THEME_SOURCES, type ThemeSource } from '@shared/shell'
import { useEngineGet, useEngineRequest } from '../api/engine'
import type { Embeddings as EmbeddingsInfo } from '../jobs/types'

const LABELS: Record<ThemeSource, string> = { system: 'System', light: 'Light', dark: 'Dark' }

const FIELDSET = 'm-0 flex flex-col gap-3 rounded-xl border border-border bg-surface p-5'
const LEGEND = 'float-left mb-1 p-0 text-[15px] font-semibold'
const NOTE = 'clear-both m-0 text-[13px] text-text-muted'
const BUTTON =
  'min-h-9 rounded-lg border border-border bg-surface px-3 text-text-2 hover:text-text disabled:opacity-60'

/** Settings so far: Appearance and Embeddings (DESIGN.md §3.5, ARCHITECTURE.md §9.1). The rest arrive with the engine. */
export function Settings({
  theme,
  onThemeChange,
  onOpenJobs
}: {
  theme: ThemeSource
  onThemeChange(theme: ThemeSource): void
  onOpenJobs(): void
}) {
  return (
    <div className="flex max-w-[640px] flex-col gap-6">
      <fieldset className={FIELDSET}>
        <legend className={LEGEND}>Appearance</legend>
        <p className={NOTE}>System follows your computer's setting. Native menus and dialogs match.</p>
        <div role="radiogroup" aria-label="Theme" className="flex flex-wrap gap-2">
          {THEME_SOURCES.map((t) => (
            <label
              key={t}
              className={`flex min-h-11 cursor-default items-center gap-2 rounded-lg border px-3.5 ${
                theme === t ? 'border-accent-border bg-accent-soft text-text' : 'border-border text-text-2'
              } has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-accent`}
            >
              <input
                type="radio"
                name="theme"
                value={t}
                checked={theme === t}
                onChange={() => onThemeChange(t)}
                className="accent-[var(--accent-solid)] focus-visible:outline-none"
              />
              {LABELS[t]}
            </label>
          ))}
        </div>
      </fieldset>
      <Embeddings onOpenJobs={onOpenJobs} />
    </div>
  )
}

/** "chinook", "chinook and wwi", "chinook, pagila and wwi". */
function names(list: readonly string[]): string {
  return list.length <= 1 ? (list[0] ?? '') : `${list.slice(0, -1).join(', ')} and ${list.at(-1)}`
}

/**
 * The engine's embedding model, the databases with vectors from another, and Re-embed, which
 * starts a re-embed job for each of them. The engine does the same when it starts.
 */
function Embeddings({ onOpenJobs }: { onOpenJobs(): void }) {
  const request = useEngineRequest()
  const info = useEngineGet<EmbeddingsInfo>('/embeddings')
  const [busy, setBusy] = useState(false)
  const [started, setStarted] = useState<string[] | null>(null)
  const [failed, setFailed] = useState<string | null>(null)

  async function reembed(): Promise<void> {
    if (request === null) return
    setBusy(true)
    setFailed(null)
    try {
      const { databases } = await request<{ databases: string[] }>('/jobs/reembed', { method: 'POST' })
      setStarted(databases)
      info.reload()
    } catch (e) {
      setFailed(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const model = info.data?.model
  const stale = info.data?.stale ?? []
  return (
    <fieldset className={FIELDSET} aria-label="Embeddings">
      <legend className={LEGEND}>Embeddings</legend>
      <p className={NOTE}>
        Search finds Terms through vectors from a local model. When the model changes, each database embedded with the
        old one is re-embedded as a job on the Jobs screen. The engine starts it by itself; Re-embed starts it now. It
        uses no Claude usage.
      </p>
      {info.data === undefined ? (
        <p className="m-0 text-[13px] text-text-muted">{info.error ?? 'Waiting for the engine.'}</p>
      ) : model === null ? (
        <p className="m-0 text-[13px] text-text-2">This engine runs without an embedding model, so it can't re-embed.</p>
      ) : (
        <>
          <dl className="m-0 flex flex-col gap-1 text-[13px]">
            <div className="flex flex-wrap gap-2">
              <dt className="text-text-muted">Model</dt>
              <dd className="m-0 font-mono text-text-2" data-testid="embedding-model">
                {model}
              </dd>
            </div>
            <div className="flex flex-wrap gap-2">
              <dt className="text-text-muted">From another model</dt>
              <dd className="m-0 text-text-2" data-testid="stale-databases">
                {stale.length === 0 ? 'none' : names(stale)}
              </dd>
            </div>
          </dl>
          <div className="flex flex-wrap items-center gap-3">
            <button type="button" className={BUTTON} disabled={busy || request === null} onClick={() => void reembed()}>
              {busy ? 'Re-embedding…' : 'Re-embed'}
            </button>
            <span role="status" className="text-[13px] text-text-2">
              {failed !== null
                ? `Couldn't re-embed: ${failed}`
                : started === null
                  ? ''
                  : started.length === 0
                    ? 'Nothing to re-embed: every database’s vectors are from this model.'
                    : `Re-embedding ${names(started)}.`}
            </span>
            {started !== null && started.length > 0 && (
              <button type="button" className={BUTTON} onClick={onOpenJobs}>
                Open Jobs
              </button>
            )}
          </div>
        </>
      )}
    </fieldset>
  )
}
