import { useEffect, useState } from 'react'
import type { Saved, TermChanges, TermDetail } from '@shared/api'
import { useEngineGet, useEngineRequest } from '../api/engine'
import { CheckIcon, CloseIcon, NoteIcon } from '../components/icons'
import { plural } from '../format'
import { addSynonym, changesOf, descriptionAuthor, type Draft, draftOf, historyLine, saveNote, whenLabel } from './format'

interface TermEditorProps {
  termId: string | null
  /** Open another Term, from an attribute's join. */
  onOpenTerm(id: string): void
  /** An edit or undo landed: the glossary and the graph read again. */
  onChanged(): void
}

const SECTION = 'text-xs font-semibold text-text-2'

/**
 * The term editor (DESIGN.md §5.3, §4.11), the right rail on Ontology and Terms: rename, the
 * description and synonyms, certification of each field, the attributes and SQL attributes, and
 * the history with Undo. Save re-embeds what changed, so the next answer uses it (§1 rule 5).
 */
export function TermEditor({ termId, onOpenTerm, onChanged }: TermEditorProps) {
  const request = useEngineRequest()
  const [key, setKey] = useState(0)
  const loaded = useEngineGet<TermDetail>(termId ? `/terms/${termId}` : null, key)
  const [term, setTerm] = useState<TermDetail | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [renaming, setRenaming] = useState(false)
  const [adding, setAdding] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  // A new Term, or a fresh read of this one, replaces the draft.
  useEffect(() => {
    const got = loaded.data
    if (!got || got.id !== termId) return
    setTerm(got)
    setDraft(draftOf(got))
  }, [loaded.data, termId])
  useEffect(() => {
    setRenaming(false)
    setAdding(null)
    setNote(null)
    setError(null)
  }, [termId])

  if (termId === null) return <p className="m-0 text-[13px] text-text-muted">Select a term to edit it.</p>
  if (loaded.error && !term) return <p className="m-0 text-[13px]">Couldn’t read the term: {loaded.error}</p>
  if (!term || !draft || term.id !== termId) {
    return <div className="h-48 rounded-lg bg-surface-sunken" aria-label="Loading term" />
  }

  const changes = changesOf(term, draft)
  const dirty = Object.keys(changes).length > 0

  const send = async (path: string, init: { method: string; body?: unknown }, done: (saved: Saved) => string) => {
    if (!request) return
    setBusy(true)
    setError(null)
    setNote(null)
    try {
      const saved = await request<Saved>(path, init)
      if (saved.term) {
        setTerm(saved.term)
        setDraft(draftOf(saved.term))
      }
      setRenaming(false)
      setNote(done(saved))
      if (saved.edit !== null) onChanged()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }
  const save = (body: TermChanges, done: (saved: Saved) => string) =>
    void send(`/terms/${term.id}`, { method: 'PATCH', body }, done)
  const saved = (saved: Saved) =>
    saved.edit === null ? 'Nothing to save.' : `Saved. The next answer uses ${saved.term?.name ?? 'it'} as you wrote it.`
  const certify = (field: 'name_certified' | 'description_certified', value: boolean) =>
    save({ [field]: value }, (s) =>
      s.edit === null ? 'Nothing changed.' : value ? 'Certified. Answers show it as checked.' : 'Certification taken off.'
    )
  const undo = () =>
    term.undoable !== null &&
    void send(`/edits/${term.undoable}/undo`, { method: 'POST' }, () => 'Undone. The next answer uses the earlier wording.')

  const represents = term.tables.map((t) => t.name).join(', ')
  const joinRows = term.joins.filter((j) => !term.attributes.some((a) => a.table === j.table && a.column === j.column && a.joins_to))

  return (
    <div data-testid="term-editor" className="-mx-[22px] -mb-6 flex min-h-0 flex-1 flex-col">
      <div className="flex flex-col gap-1.5 border-b border-border-subtle px-[22px] pb-4">
        <span className="text-xs text-text-muted">
          {term.kind === 'association' ? 'Association' : 'Term'}
          {represents && (
            <>
              {' · represents '}
              <span className="font-mono">{represents}</span>
            </>
          )}
        </span>
        {renaming ? (
          <label className="flex flex-col gap-1">
            <span className="sr-only">Name</span>
            <input
              autoFocus
              value={draft.name}
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && dirty) save(changes, saved)
                if (e.key === 'Escape') {
                  setDraft({ ...draft, name: term.name })
                  setRenaming(false)
                }
              }}
              className="min-h-11 rounded-lg border border-border bg-surface px-3 text-[22px] font-semibold tracking-[-0.01em] text-text select-text"
            />
          </label>
        ) : (
          <div className="flex flex-wrap items-center gap-2.5">
            <h3 className="m-0 text-[22px] font-semibold tracking-[-0.01em] break-words" data-testid="term-name">
              {term.name}
            </h3>
            {term.name_certified && <Badge>Name certified</Badge>}
          </div>
        )}
        <div className="flex flex-wrap gap-2">
          {!renaming && (
            <button type="button" onClick={() => setRenaming(true)} className={QUIET}>
              Rename
            </button>
          )}
          <CertifyButton
            certified={term.name_certified}
            what="name"
            disabled={busy || draft.name.trim() !== term.name}
            onClick={(v) => certify('name_certified', v)}
          />
        </div>
      </div>

      <div className="flex flex-col gap-5 px-[22px] py-[18px]">
        <div className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <label htmlFor="term-description" className={SECTION}>
              Description
            </label>
            {term.description_certified ? (
              <Badge>Description certified</Badge>
            ) : (
              <span className="text-xs text-attention">{descriptionAuthor(term)} · not certified</span>
            )}
          </div>
          <textarea
            id="term-description"
            rows={4}
            value={draft.description}
            onChange={(e) => setDraft({ ...draft, description: e.target.value })}
            className="resize-y rounded-lg border border-border bg-surface px-3 py-2.5 text-sm leading-normal text-text select-text"
          />
          <CertifyButton
            certified={term.description_certified}
            what="description"
            disabled={busy || draft.description !== (term.description ?? '') || !term.description}
            onClick={(v) => certify('description_certified', v)}
          />
        </div>

        <div className="flex flex-col gap-2">
          <span className={SECTION} id="synonyms-label">
            Synonyms
          </span>
          <ul aria-labelledby="synonyms-label" className="m-0 flex list-none flex-wrap gap-1.5 p-0">
            {draft.synonyms.map((s) => (
              <li key={s} className="flex items-center gap-0.5 rounded-full bg-surface-sunken py-0.5 pr-1 pl-2.5 text-[13px]">
                {s}
                <button
                  type="button"
                  aria-label={`Remove synonym ${s}`}
                  onClick={() => setDraft({ ...draft, synonyms: draft.synonyms.filter((x) => x !== s) })}
                  className="flex size-6 items-center justify-center rounded-full text-text-muted hover:bg-nav-hover hover:text-text"
                >
                  <CloseIcon />
                </button>
              </li>
            ))}
            <li>
              {adding === null ? (
                <button
                  type="button"
                  onClick={() => setAdding('')}
                  className="min-h-7 rounded-full border border-dashed border-text-faint px-2.5 text-[13px] text-text-2"
                >
                  + Add
                </button>
              ) : (
                <input
                  autoFocus
                  aria-label="New synonym"
                  value={adding}
                  onChange={(e) => setAdding(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      setDraft({ ...draft, synonyms: addSynonym(draft.synonyms, adding) })
                      setAdding('')
                    }
                    if (e.key === 'Escape') setAdding(null)
                  }}
                  onBlur={() => {
                    setDraft({ ...draft, synonyms: addSynonym(draft.synonyms, adding) })
                    setAdding(null)
                  }}
                  className="min-h-7 w-32 rounded-full border border-border bg-surface px-2.5 text-[13px] text-text select-text"
                />
              )}
            </li>
          </ul>
        </div>

        <div className="flex flex-col gap-2">
          <span className={SECTION}>Attributes · {term.attributes.length + joinRows.length}</span>
          {term.attributes.length + joinRows.length > 0 && (
            <ul className="m-0 list-none rounded-lg border border-border-subtle p-0 text-[13px]" data-testid="term-attributes">
              {term.attributes.map((a) => (
                <li key={a.id} className="grid grid-cols-[minmax(0,1fr)_auto] gap-2 border-b border-border-subtle px-3 py-2 last:border-b-0">
                  <span className="truncate font-mono" title={a.description ?? a.name}>
                    {a.column}
                    {a.certified && (
                      <span className="ml-1 inline-flex text-verified" aria-label="certified" role="img">
                        <CheckIcon size={11} />
                      </span>
                    )}
                  </span>
                  {a.joins_to ? (
                    <TermLink to={a.joins_to} onOpen={onOpenTerm} />
                  ) : (
                    <span className="text-text-muted">{a.datatype?.toLowerCase() ?? ''}</span>
                  )}
                </li>
              ))}
              {joinRows.map((j) => (
                <li key={`${j.table}.${j.column}`} className="grid grid-cols-[minmax(0,1fr)_auto] gap-2 border-b border-border-subtle px-3 py-2 last:border-b-0">
                  <span className="truncate font-mono">{j.column}</span>
                  <TermLink to={j.to} onOpen={onOpenTerm} />
                </li>
              ))}
            </ul>
          )}
        </div>

        {term.sql_attributes.length > 0 && (
          <div className="flex flex-col gap-2">
            <span className={SECTION}>SQL attributes · {term.sql_attributes.length}</span>
            {term.sql_attributes.map((s) => (
              <div key={s.id} className="flex flex-col gap-1.5 rounded-lg border border-border-subtle px-3 py-2.5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-medium">{s.name}</span>
                  <span className="text-xs text-text-muted">
                    {sqlSource(s.source)} · used {plural(s.uses, 'time')}
                  </span>
                </div>
                {s.expression && (
                  <code className="overflow-x-auto rounded-md bg-surface-sunken px-2 py-1.5 font-mono text-xs whitespace-nowrap text-text-2 select-text">
                    {s.expression}
                  </code>
                )}
              </div>
            ))}
          </div>
        )}

        <div className="flex flex-col gap-2">
          <span className={SECTION}>History</span>
          <ol className="m-0 flex list-none flex-col gap-1.5 p-0 text-[13px] text-text-2" data-testid="term-history">
            {term.history.map((c) => (
              <li key={c.id} className="flex justify-between gap-2">
                <span>{historyLine(c)}</span>
                <span className="shrink-0 text-text-muted">{whenLabel(c.ts, Date.now())}</span>
              </li>
            ))}
            {term.created && (
              <li className="flex justify-between gap-2">
                <span>Compiled</span>
                <span className="shrink-0 text-text-muted">{whenLabel(term.created, Date.now())}</span>
              </li>
            )}
            {term.history.length === 0 && !term.created && <li className="text-text-muted">No edits yet.</li>}
          </ol>
        </div>
      </div>

      <div className="sticky bottom-0 mt-auto flex flex-col gap-2 border-t border-border-subtle bg-rail px-[22px] py-3.5">
        {(note || error) && (
          <p role="status" className="m-0 flex items-start gap-1.5 text-[13px] text-text">
            {error && (
              <span className="mt-0.5 text-text-muted">
                <NoteIcon size={14} />
              </span>
            )}
            {error ? `Not saved: ${error}` : note}
          </p>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <span className="basis-full text-xs text-text-muted">{saveNote(draft.name.trim() || term.name)}</span>
          <span className="flex-1" />
          <button
            type="button"
            onClick={undo}
            disabled={busy || term.undoable === null}
            className="min-h-9 rounded-lg border border-border bg-surface px-3 text-text-2 disabled:opacity-50"
          >
            Undo
          </button>
          <button
            type="button"
            onClick={() => save(changes, saved)}
            disabled={busy || !dirty}
            className="min-h-9 rounded-lg bg-text px-3.5 font-medium text-bg disabled:opacity-50"
          >
            Save
          </button>
        </div>
      </div>
    </div>
  )
}

const QUIET = 'min-h-8 rounded-lg border border-border bg-surface px-3 text-[13px] text-text-2 hover:text-text'

function Badge({ children }: { children: React.ReactNode }) {
  return (
    <span className="flex items-center gap-1 rounded-full bg-verified-soft px-2 py-0.5 text-xs font-medium text-verified-text">
      <CheckIcon size={11} />
      {children}
    </span>
  )
}

/** Certify when it isn't, "Remove certification" when it is (§4.11). */
function CertifyButton({
  certified,
  what,
  disabled,
  onClick
}: {
  certified: boolean
  what: 'name' | 'description'
  disabled: boolean
  onClick(value: boolean): void
}) {
  if (certified) {
    return (
      <button type="button" onClick={() => onClick(false)} disabled={disabled} className={`self-start ${QUIET} disabled:opacity-50`}>
        Remove {what} certification
      </button>
    )
  }
  return (
    <button
      type="button"
      onClick={() => onClick(true)}
      disabled={disabled}
      title={disabled ? `Save the ${what} before certifying it` : undefined}
      className="flex min-h-8 items-center gap-1.5 self-start rounded-lg border border-verified-border bg-verified-soft px-3 font-medium text-verified-text disabled:opacity-50"
    >
      <CheckIcon size={13} />
      Certify {what}
    </button>
  )
}

function TermLink({ to, onOpen }: { to: { id: string; name: string }; onOpen(id: string): void }) {
  return (
    <button type="button" onClick={() => onOpen(to.id)} className="text-[13px] text-accent hover:text-accent-hover">
      → {to.name}
    </button>
  )
}

function sqlSource(source: string | null): string {
  switch (source) {
    case 'sql':
      return 'from query history'
    case 'bridgeTable':
      return 'bridge table'
    case 'table':
      return 'from the table'
    case 'manual':
      return 'written by hand'
    default:
      return 'SQL'
  }
}
