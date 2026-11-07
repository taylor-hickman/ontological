// The Jobs screen's words (DESIGN.md §5.4, §8): what each job, table and call says. Pure, so the
// unit tests cover them without a window.

import type { CallRow, JobState, JobSummary, OptionEstimate, TableCalls, TableProgress } from './types'

const UNFINISHED: readonly JobState[] = ['running', 'paused', 'held', 'stopped']

/** Clock times, not durations, for anything scheduled (§8). */
export function clockTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

/** A log line's time to the second, without the day period, as the mockup's "3:31:58". */
export function logTime(iso: string): string {
  return new Intl.DateTimeFormat([], { hour: 'numeric', minute: '2-digit', second: '2-digit' })
    .formatToParts(new Date(iso))
    .filter((p) => p.type !== 'dayPeriod')
    .map((p) => p.value)
    .join('')
    .trim()
}

/** A countdown, only for banners (§4.6): "in 38 min", "in 2 h 5 min". */
export function countdown(iso: string, now: number): string {
  const minutes = Math.max(0, Math.ceil((new Date(iso).getTime() - now) / 60_000))
  if (minutes < 1) return 'in under a minute'
  if (minutes < 60) return `in ${minutes} min`
  const h = Math.floor(minutes / 60)
  const m = minutes % 60
  return m === 0 ? `in ${h} h` : `in ${h} h ${m} min`
}

/** Today's runs by time, earlier ones by date: "9:02 AM", "Nov 3". */
export function whenShort(iso: string, now: number): string {
  const at = new Date(iso)
  if (at.toDateString() === new Date(now).toDateString()) return clockTime(iso)
  return at.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

const FAMILIES: Record<string, string> = { haiku: 'Haiku', sonnet: 'Sonnet', opus: 'Opus', fable: 'Fable' }

/** Models as the user knows them (§8): `claude-sonnet-5-5` → "Sonnet 5.5"; an alias → "Sonnet". */
export function modelName(model: string): string {
  const id = /^claude-([a-z]+)-(\d+)-(\d+)/.exec(model)
  if (id) return `${FAMILIES[id[1]!] ?? id[1]} ${id[2]}.${id[3]}`
  return FAMILIES[model] ?? model
}

/** How a call ended, in the call log: its time in seconds, "cached" or "limit reached". */
export function callResult(call: CallRow): string {
  if (call.cached) return 'cached'
  switch (call.outcome) {
    case 'limited':
      return 'limit reached'
    case 'error':
      return 'no answer'
    case 'invalid':
      return call.attempt >= 2 ? 'invalid, fell back' : 'invalid, retried'
    case 'ok':
      return call.duration_ms === null ? 'answered' : `${(call.duration_ms / 1000).toFixed(1)} s`
  }
}

export function isUnfinished(job: JobSummary): boolean {
  return UNFINISHED.includes(job.state)
}

/**
 * The job the screen opens on: the latest unfinished compile, else the latest compile. Jobs come
 * newest first.
 */
export function currentJob(jobs: readonly JobSummary[]): JobSummary | null {
  const compiles = jobs.filter((j) => j.kind === 'compile')
  return compiles.find(isUnfinished) ?? compiles[0] ?? null
}

/** Refreshes that found tables the person hasn't been asked about yet (§5.4, §9.1). */
export function newTablePrompts(jobs: readonly JobSummary[]): JobSummary[] {
  return jobs.filter((j) => j.kind === 'ingest' && j.state === 'done' && j.new_tables.length > 0 && !j.dismissed)
}

/** The sidebar's Jobs badge (§3.3): "31/48" while a compile runs, "Paused", "Stopped". */
export function jobsBadge(jobs: readonly JobSummary[]): { text: string; attention: boolean } | null {
  const job = currentJob(jobs)
  if (!job || !isUnfinished(job)) return null
  if (job.state === 'running') return { text: `${job.tables_done}/${job.tables}`, attention: false }
  if (job.state === 'stopped') return { text: 'Stopped', attention: true }
  return { text: 'Paused', attention: true }
}

/** The status bar's job link (§4.6). */
export function jobStatusLine(jobs: readonly JobSummary[]): string | null {
  const job = currentJob(jobs)
  if (!job || !isUnfinished(job)) return null
  const name = `Compile ${job.database_name}`
  switch (job.state) {
    case 'running':
      return `${name} · ${job.tables_done} of ${job.tables} tables`
    case 'paused':
      return job.paused_until ? `${name} paused until ${clockTime(job.paused_until)}` : `${name} paused`
    case 'held':
      return `${name} paused by you`
    default:
      return `${name} stopped`
  }
}

const KIND_NAME: Record<JobSummary['kind'], string> = {
  compile: 'Compile',
  ingest: 'Ingest metadata',
  reembed: 'Re-embed'
}

export function jobTitle(job: JobSummary): string {
  return `${KIND_NAME[job.kind]} · ${job.database_name}`
}

/** A job's state as a word, so it never depends on color (§2.1). */
export function stateWord(job: JobSummary): string {
  switch (job.state) {
    case 'running':
      return 'running'
    case 'paused':
      return 'paused for the usage limit'
    case 'held':
      return 'paused by you'
    case 'stopped':
      return 'stopped'
    case 'done':
      return job.finished_at ? `finished ${clockTime(job.finished_at)}` : 'finished'
  }
}

/** One line for an earlier run: "Compile · pagila · 22 tables". */
export function earlierLine(job: JobSummary): string {
  if (job.kind === 'reembed') return job.state === 'stopped' ? `${jobTitle(job)} · failed` : jobTitle(job)
  if (job.kind === 'ingest') {
    const found = job.new_tables.length
    if (job.state === 'stopped') return `${jobTitle(job)} · failed`
    return found > 0 ? `${jobTitle(job)} · ${found} new ${found === 1 ? 'table' : 'tables'}` : jobTitle(job)
  }
  const tables = job.state === 'done' ? `${job.tables} tables` : `${job.tables_done} of ${job.tables} tables`
  return `${jobTitle(job)} · ${tables}`
}

export interface Banner {
  title: string
  detail: string
  action: 'hold' | 'resume' | null
  actionLabel: string
}

function finished(job: JobSummary): string {
  const n = job.tables_done
  return n === 0 ? 'Nothing is lost.' : `The ${n} finished ${n === 1 ? 'table is' : 'tables are'} saved.`
}

/**
 * §4.7's banner for a compile that is paused or stopped, or a re-embed that stopped; null while it
 * runs or once it's done.
 */
export function jobBanner(job: JobSummary, now: number): Banner | null {
  if (job.kind === 'reembed') {
    return job.state === 'stopped'
      ? {
          title: 'Stopped: re-embedding didn’t finish',
          detail: `${job.error ?? 'The re-embed stopped.'} Search may miss what changed in ${job.database_name} until it runs. Retrying uses no Claude usage.`,
          action: 'resume',
          actionLabel: 'Retry'
        }
      : null
  }
  if (job.kind !== 'compile') return null
  switch (job.state) {
    case 'paused': {
      const when = job.paused_until
        ? `Compile picks up by itself at ${clockTime(job.paused_until)}, ${countdown(job.paused_until, now)}.`
        : 'Compile picks up by itself.'
      return {
        title: 'Paused: your Claude usage limit is reached',
        detail: `${when} ${finished(job)}`,
        action: 'hold',
        actionLabel: 'Don’t resume'
      }
    }
    case 'held':
      return {
        title: 'Paused by you',
        detail: `Compile of ${job.database_name} waits until you resume it. ${finished(job)} Resuming uses Claude quota.`,
        action: 'resume',
        actionLabel: 'Resume'
      }
    case 'stopped': {
      const login = /not logged in|needs_login/i.test(job.error ?? '')
      return login
        ? {
            title: 'Stopped: Claude Code isn’t logged in',
            detail: `Open a terminal, run claude and type /login, then resume. ${finished(job)}`,
            action: 'resume',
            actionLabel: 'Resume'
          }
        : {
            title: 'Stopped: Claude didn’t answer',
            detail: `${job.error ?? 'The compile stopped.'} ${finished(job)} Resume picks up at the next table.`,
            action: 'resume',
            actionLabel: 'Resume'
          }
    }
    default:
      return null
  }
}

export type SegmentTone = 'done' | 'checkpointed' | 'queued'

/**
 * A tile's segments (§4.8): `--accent` done, `--meter-warn` for the stages a table got through
 * before its job stopped mid-table, `--queued` waiting.
 */
export function segments(table: TableProgress, stages: number, jobState: JobState): SegmentTone[] {
  const done = table.state === 'done' ? stages : table.state === 'skipped' ? 0 : table.stages_done
  const stoppedMidTable = table.state === 'pending' && jobState !== 'running'
  return Array.from({ length: stages }, (_, i) => (i < done ? (stoppedMidTable ? 'checkpointed' : 'done') : 'queued'))
}

/** A tile bar's text equivalent (§4.8, §9): "2 of 3 stages done". */
export function tileLabel(table: TableProgress, stages: number): string {
  if (table.state === 'done') return 'done'
  if (table.state === 'skipped') return `skipped: ${table.skipped ?? 'nothing to compile'}`
  if (table.stages_done === 0) return 'queued'
  return `${table.stages_done} of ${stages} stages done`
}

/** "keys · descriptions · terms", from the stages' route names. */
export function stageLegend(stages: readonly string[]): string {
  const names: Record<string, string> = {
    table_keys: 'keys',
    column_descriptions: 'descriptions',
    table_terms: 'terms',
    table_synonyms: 'synonyms',
    table_semantics: 'one merged call'
  }
  return stages.map((s) => names[s] ?? s).join(' · ')
}

/** The options a compile's tables ran with, split first: what the legend explains. */
export function optionsShown(tables: readonly TableProgress[], current: TableCalls): TableCalls[] {
  const used = new Set<TableCalls>(tables.map((t) => t.table_calls))
  if (used.size === 0) used.add(current)
  return (['split', 'merged'] as const).filter((o) => used.has(o))
}

export const OPTION_NAME: Record<TableCalls, string> = { split: 'Split', merged: 'Merged' }

/** What each option's calls are, in a few words, as the mockup's "bulk on Haiku". */
export const OPTION_SHAPE: Record<TableCalls, string> = {
  split: 'four calls a table, packed; bulk on Haiku',
  merged: 'one Sonnet call per table'
}

/** A duration estimate (§8): "under a minute", "4 min", "1 h 20 min". */
export function minutes(seconds: number): string {
  const m = Math.round(seconds / 60)
  if (m < 1) return 'under a minute'
  if (m < 60) return `${m} min`
  const h = Math.floor(m / 60)
  const rest = m % 60
  return rest === 0 ? `${h} h` : `${h} h ${rest} min`
}

/** Usage at API prices: "$0.42", "under $0.01". */
export function dollars(usd: number): string {
  return usd < 0.01 ? 'under $0.01' : `$${usd.toFixed(2)}`
}

/** An option's estimate on one line: "≈ 70 calls · ≈ 15 min · ≈ $0.42 of usage". */
export function estimateLine(option: OptionEstimate): string {
  if (option.calls === 0) return 'No Claude calls'
  const calls = `≈ ${option.calls} ${option.calls === 1 ? 'call' : 'calls'}`
  return `${calls} · ≈ ${minutes(option.seconds)} · ≈ ${dollars(option.usd)} of usage`
}

/** The range behind an estimate: "16–18 calls, 1 to 2 min, $0.11 to $0.23 at API prices". */
export function estimateRange(option: OptionEstimate): string {
  const calls = option.calls_high > option.calls ? `${option.calls}–${option.calls_high}` : String(option.calls)
  const low = minutes(option.seconds_low)
  const high = minutes(option.seconds_high)
  const time = low === high ? high : `${low} to ${high}`
  return `${calls} calls, ${time}, ${dollars(option.usd_low)} to ${dollars(option.usd_high)} at API prices`
}
