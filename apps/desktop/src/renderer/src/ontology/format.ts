// The Terms and Ontology screens' words and list logic (DESIGN.md §5.3, §5.5, §8). Pure, so the
// unit tests cover them without a window.

import type { JoinPath, TermChange, TermChanges, TermDetail, TermRow } from '@shared/api'
import { clockTime } from '../jobs/format'

const WHO: Record<TermChange['actor'], string> = { user: 'You', compile: 'Compile', import: 'Import' }

function quoted(value: unknown): string {
  return `“${String(value ?? '')}”`
}

function asList(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : []
}

/** Whether the change is the Term's creation: an import logs a Term it creates from no name. */
export function isCreation(change: TermChange): boolean {
  return change.field === 'name' && change.old === null
}

/** One history line: "You certified the name", "You added synonym “loan”". */
export function historyLine(change: TermChange): string {
  const who = WHO[change.actor]
  const undo = change.undoes !== null ? ' (undo)' : ''
  switch (change.field) {
    case 'name':
      if (isCreation(change)) return `${who} created it as ${quoted(change.new)}`
      return `${who} renamed it from ${quoted(change.old)} to ${quoted(change.new)}${undo}`
    case 'description':
      if (change.old === null && change.new) return `${who} wrote the description${undo}`
      return `${who} ${change.new ? 'changed' : 'cleared'} the description${undo}`
    case 'name_certified':
    case 'description_certified': {
      const what = change.field === 'name_certified' ? 'name' : 'description'
      return change.new
        ? `${who} certified the ${what}${undo}`
        : `${who} took the certification off the ${what}${undo}`
    }
    case 'synonyms': {
      const before = asList(change.old)
      const after = asList(change.new)
      const added = after.filter((s) => !before.includes(s))
      const removed = before.filter((s) => !after.includes(s))
      if (added.length === 1 && removed.length === 0) return `${who} added synonym ${quoted(added[0])}${undo}`
      if (removed.length === 1 && added.length === 0) return `${who} removed synonym ${quoted(removed[0])}${undo}`
      return `${who} changed the synonyms${undo}`
    }
  }
}

/** "Today 2:14 PM" for today, else "Nov 3" (the mockup's history column). */
export function whenLabel(iso: string, now: number): string {
  const at = new Date(iso)
  if (at.toDateString() === new Date(now).toDateString()) return `Today ${clockTime(iso)}`
  return at.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

/** Who wrote the description, for the amber caption on an uncertified one (§4.11). */
export function descriptionAuthor(term: TermDetail): string {
  const edited = term.history.find((c) => c.field === 'description')
  if (edited?.actor === 'user') return 'Edited by you'
  if (edited?.actor === 'import') return 'Imported'
  return 'Written by compile'
}

export interface Draft {
  name: string
  description: string
  synonyms: string[]
}

export function draftOf(term: TermDetail): Draft {
  return { name: term.name, description: term.description ?? '', synonyms: [...term.synonyms] }
}

/** What Save sends: only the fields the draft changed. */
export function changesOf(term: TermDetail, draft: Draft): TermChanges {
  const changes: TermChanges = {}
  const name = draft.name.trim()
  if (name !== term.name) changes.name = name
  if (draft.description !== (term.description ?? '')) changes.description = draft.description
  if (draft.synonyms.join('\u0000') !== term.synonyms.join('\u0000')) changes.synonyms = draft.synonyms
  return changes
}

/** A synonym the chips would add: trimmed, and not one the Term already has in any case. */
export function addSynonym(synonyms: readonly string[], typed: string): string[] {
  const s = typed.trim()
  if (!s || synonyms.some((x) => x.toLowerCase() === s.toLowerCase())) return [...synonyms]
  return [...synonyms, s]
}

export function kindLabel(kind: TermRow['kind']): string {
  return kind === 'association' ? 'Association' : 'Entity'
}

/** The certified fields as words, for the glossary: "Name, description", "Name", "None". */
export function certifiedFields(row: Pick<TermRow, 'name_certified' | 'description_certified'>): string {
  if (row.name_certified && row.description_certified) return 'Name, description'
  if (row.name_certified) return 'Name'
  if (row.description_certified) return 'Description'
  return 'None'
}

export type SortKey = 'name' | 'kind' | 'certified' | 'synonyms' | 'attributes' | 'changed'
export type Filter = 'uncertified' | 'week' | 'no-description'

export const FILTERS: readonly { id: Filter; label: string }[] = [
  { id: 'uncertified', label: 'Uncertified' },
  { id: 'week', label: 'Changed this week' },
  { id: 'no-description', label: 'No description' }
]

const WEEK_MS = 7 * 24 * 60 * 60 * 1000

/** The rows that pass every filter chip on, and whose name, synonym or table matches `needle`. */
export function filterTerms(rows: readonly TermRow[], filters: ReadonlySet<Filter>, needle: string, now: number): TermRow[] {
  const n = needle.trim().toLowerCase()
  return rows.filter((row) => {
    if (filters.has('uncertified') && row.certified) return false
    if (filters.has('no-description') && row.description?.trim()) return false
    if (filters.has('week') && (!row.changed_at || now - new Date(row.changed_at).getTime() > WEEK_MS)) return false
    if (!n) return true
    return [row.name, ...row.synonyms, ...row.tables].some((s) => s.toLowerCase().includes(n))
  })
}

function sortValue(row: TermRow, key: SortKey): string | number {
  switch (key) {
    case 'name':
      return row.name.toLowerCase()
    case 'kind':
      return row.kind
    case 'certified':
      return Number(row.name_certified) + Number(row.description_certified)
    case 'synonyms':
      return row.synonyms.length
    case 'attributes':
      return row.attributes + row.sql_attributes
    case 'changed':
      return row.changed_at ? new Date(row.changed_at).getTime() : 0
  }
}

/** Sorted by `key`, ties by name, so the order is stable. */
export function sortTerms(rows: readonly TermRow[], key: SortKey, ascending: boolean): TermRow[] {
  const sign = ascending ? 1 : -1
  return [...rows].sort((a, b) => {
    const x = sortValue(a, key)
    const y = sortValue(b, key)
    if (x < y) return -sign
    if (x > y) return sign
    return a.name.toLowerCase() < b.name.toLowerCase() ? -1 : a.name.toLowerCase() > b.name.toLowerCase() ? 1 : 0
  })
}

/** The path summary card: "4 hops", "Customer → Rental → Inventory", "via film_category". */
export function pathSummary(path: JoinPath): { hops: string; chain: string; via: string | null } {
  const n = path.hops.length
  const via = [...new Set(path.hops.map((h) => h.via).filter((v): v is string => v !== null))]
  return {
    hops: n === 1 ? '1 hop' : `${n} hops`,
    chain: path.terms.map((t) => t.name).join(' → '),
    via: via.length ? `via ${via.join(', ')}` : null
  }
}

/** The editor footer (§5.3). */
export function saveNote(name: string): string {
  return `Saving re-embeds ${name}, so the next answer uses your wording.`
}
