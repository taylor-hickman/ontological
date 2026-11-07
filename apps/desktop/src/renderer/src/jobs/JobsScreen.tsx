import { useEffect, useState } from 'react'
import {
  callResult,
  clockTime,
  currentJob,
  earlierLine,
  estimateLine,
  estimateRange,
  isUnfinished,
  jobBanner,
  logTime,
  jobTitle,
  modelName,
  newTablePrompts,
  OPTION_NAME,
  OPTION_SHAPE,
  optionsShown,
  segments,
  stageLegend,
  stateWord,
  tileLabel,
  whenShort,
  type SegmentTone
} from './format'
import type { CallRow, Estimate, JobDetail, JobSummary, TableCalls } from './types'
import { useEstimate, useJobAction, useJobDetail } from './useJobs'

export interface JobsScreenState {
  loaded: boolean
  jobs: JobSummary[]
  /** The job the card shows: the one picked from Earlier, else the current one. */
  shown: JobSummary | null
  select(jobId: number): void
  detail: JobDetail | null
  calls: CallRow[]
  /** What the shown compile has left, with split and merged; null for a finished one. */
  estimate: Estimate | null
  act(path: string, body?: unknown): Promise<void>
  busy: boolean
  error: string | null
  /** Jobs the person asked to pause that haven't stopped yet: they finish their current table. */
  holding: ReadonlySet<number>
  hold(jobId: number): void
  now: number
}

/** Everything the Jobs screen and its rail show, polled from the engine while the screen is open. */
export function useJobsScreen(jobs: JobSummary[], loaded: boolean, open: boolean, refreshJobs: () => void): JobsScreenState {
  const [picked, setPicked] = useState<number | null>(null)
  const [holding, setHolding] = useState<ReadonlySet<number>>(new Set())
  const [now, setNow] = useState(() => Date.now())
  const shown = jobs.find((j) => j.id === picked) ?? currentJob(jobs)
  const { detail, calls, refresh } = useJobDetail(open && shown ? shown.id : null, shown ? isUnfinished(shown) : false)
  const left = open && shown && shown.kind === 'compile' && isUnfinished(shown) ? shown.database_name : null
  const { estimate, refresh: refreshEstimate } = useEstimate(left, shown?.state === 'running')
  const { act, busy, error } = useJobAction(() => {
    refreshJobs()
    refresh()
    refreshEstimate()
  })

  // A countdown in the banner moves on by the minute.
  useEffect(() => {
    if (!open) return
    const timer = setInterval(() => setNow(Date.now()), 30_000)
    return () => clearInterval(timer)
  }, [open])

  // A held job has stopped holding once it is no longer running.
  useEffect(() => {
    setHolding((current) => {
      const still = [...current].filter((id) => jobs.find((j) => j.id === id)?.state === 'running')
      return still.length === current.size ? current : new Set(still)
    })
  }, [jobs])

  return {
    loaded,
    jobs,
    shown,
    select: setPicked,
    detail,
    calls,
    estimate,
    act,
    busy,
    error,
    holding,
    hold: (jobId) => {
      setHolding((current) => new Set([...current, jobId]))
      void act(`/jobs/${jobId}/hold`)
    },
    now
  }
}

// --- the main column ----------------------------------------------------------------------------

const BUTTON_SECONDARY =
  'min-h-9 rounded-lg border border-border bg-surface px-3 text-text-2 hover:text-text disabled:opacity-60'

function Banner({ state, job }: { state: JobsScreenState; job: JobSummary }) {
  const banner = jobBanner(job, state.now)
  if (!banner) return null
  const run = (): void => {
    if (banner.action === 'hold') state.hold(job.id)
    else if (banner.action === 'resume') void state.act(`/jobs/${job.id}/resume`)
  }
  return (
    <div
      role="status"
      data-testid="job-banner"
      data-state={job.state}
      className="flex flex-wrap items-center gap-3.5 rounded-xl border border-attention-border bg-attention-soft px-4 py-3.5 text-attention-text"
    >
      <span aria-hidden="true" className="grid size-9 shrink-0 place-items-center rounded-full bg-surface text-attention">
        <PauseGlyph />
      </span>
      <div className="flex flex-[1_1_320px] flex-col gap-0.5">
        <span className="font-semibold">{banner.title}</span>
        <span className="text-[13px]">{banner.detail}</span>
      </div>
      {banner.action && (
        <button
          type="button"
          onClick={run}
          disabled={state.busy}
          className="min-h-9 rounded-lg border border-attention-border bg-surface px-3 text-attention-text disabled:opacity-60"
        >
          {banner.actionLabel}
        </button>
      )}
    </div>
  )
}

function PauseGlyph() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
      <path d="M9 6v12M15 6v12" />
    </svg>
  )
}

const TONE: Record<SegmentTone, string> = {
  done: 'bg-accent',
  checkpointed: 'bg-meter-warn',
  queued: 'bg-queued'
}

function Tiles({ detail }: { detail: JobDetail }) {
  return (
    <ul
      aria-label="Tables"
      className="m-0 grid list-none grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-2 p-0"
      data-testid="table-tiles"
    >
      {detail.tables.map((table) => {
        const stages = Math.max(1, detail.stages[table.table_calls].length)
        return (
          <li
            key={table.name}
            data-state={table.state}
            data-table-calls={table.table_calls}
            className="flex flex-col gap-1.5 rounded-lg border border-border-subtle bg-rail px-2.5 py-2"
          >
            <span className="truncate font-mono text-xs text-text" title={table.name}>
              {table.name}
            </span>
            <span
              role="img"
              aria-label={tileLabel(table, stages)}
              className="grid gap-[3px]"
              style={{ gridTemplateColumns: `repeat(${stages}, minmax(0, 1fr))` }}
            >
              {segments(table, stages, detail.job.state).map((tone, i) => (
                <span key={i} data-tone={tone} className={`h-[5px] rounded-[2px] ${TONE[tone]}`} />
              ))}
            </span>
            {table.state === 'skipped' && <span className="text-[11px] text-text-muted">skipped</span>}
          </li>
        )
      })}
    </ul>
  )
}

function Legend({ detail }: { detail: JobDetail }) {
  const options = optionsShown(detail.tables, detail.job.table_calls)
  const swatch = (tone: SegmentTone, label: string) => (
    <span className="flex items-center gap-1.5">
      <span aria-hidden="true" className={`size-3 rounded-[3px] ${TONE[tone]}`} />
      {label}
    </span>
  )
  return (
    <div className="flex flex-wrap gap-4 text-xs text-text-2">
      {swatch('done', 'Done')}
      {swatch('checkpointed', 'Stopped mid-table (checkpointed)')}
      {swatch('queued', 'Queued')}
      {options.length === 1 ? (
        <span className="text-text-muted">Each bar: {stageLegend(detail.stages[options[0]!])}</span>
      ) : (
        options.map((o) => (
          <span key={o} className="text-text-muted">
            {OPTION_NAME[o]} tables: {stageLegend(detail.stages[o])}
          </span>
        ))
      )}
    </div>
  )
}

function Count({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <dt className="text-xs text-text-muted">{label}</dt>
      <dd className="m-0 text-lg font-semibold tabular-nums">{value}</dd>
    </div>
  )
}

function JobCard({ state, job }: { state: JobsScreenState; job: JobSummary }) {
  const detail = state.detail
  const holding = state.holding.has(job.id)
  const meta =
    job.kind === 'compile'
      ? `${OPTION_NAME[job.table_calls]} calls · started ${clockTime(job.created_at)} · ${holding ? 'pausing after the current table' : stateWord(job)}`
      : `${job.state === 'stopped' ? `Failed: ${job.error ?? 'no reason given'}` : 'No Claude usage'} · ${clockTime(job.created_at)}`
  return (
    <article
      aria-label={jobTitle(job)}
      data-testid="job-card"
      data-state={job.state}
      className="flex flex-col gap-[18px] rounded-[14px] border border-border bg-surface px-[22px] py-5"
    >
      <div className="flex flex-wrap items-start gap-3">
        <div className="flex flex-[1_1_280px] flex-col gap-1">
          <h2 className="m-0 text-lg font-semibold">{jobTitle(job)}</h2>
          <span className="text-[13px] text-text-muted">{meta}</span>
        </div>
        {job.kind === 'compile' ? (
          <dl className="m-0 flex flex-wrap gap-6">
            <Count label="Tables" value={`${job.tables_done} / ${job.tables}`} />
            <Count label="Claude calls" value={String(job.calls)} />
            <Count label="Cache hits" value={String(job.cache_hits)} />
          </dl>
        ) : job.kind === 'ingest' ? (
          <dl className="m-0 flex flex-wrap gap-6">
            <Count label="New tables" value={String(job.new_tables.length)} />
          </dl>
        ) : null}
        {job.kind === 'compile' && job.state === 'running' && (
          <button
            type="button"
            className={BUTTON_SECONDARY}
            disabled={holding || state.busy}
            onClick={() => state.hold(job.id)}
          >
            {holding ? 'Pausing…' : 'Pause'}
          </button>
        )}
      </div>
      {job.kind === 'compile' && detail && (
        <>
          <Legend detail={detail} />
          {detail.tables.length > 0 ? (
            <Tiles detail={detail} />
          ) : (
            <p className="m-0 text-[13px] text-text-muted">Every table already had its terms, so there was nothing to compile.</p>
          )}
        </>
      )}
      {job.kind === 'ingest' && job.new_tables.length > 0 && (
        <ul aria-label="New tables" className="m-0 flex list-none flex-wrap gap-2 p-0">
          {job.new_tables.map((t) => (
            <li key={t} className="rounded-md border border-border-subtle bg-rail px-2 py-1 font-mono text-xs">
              {t}
            </li>
          ))}
        </ul>
      )}
    </article>
  )
}

/**
 * Split and merged side by side as a radio pair (§5.4), each with its calls, time and usage.
 * `chosen` is the option picked; `onChoose` picks another.
 */
function OptionChoice({
  estimate,
  chosen,
  onChoose,
  disabled,
  current
}: {
  estimate: Estimate
  chosen: TableCalls
  onChoose(option: TableCalls): void
  disabled: boolean
  current?: TableCalls
}) {
  return (
    <div role="radiogroup" aria-label="Call shape" className="grid grid-cols-[repeat(auto-fit,minmax(200px,1fr))] gap-2.5">
      {(['split', 'merged'] as const).map((option) => {
        const picked = option === chosen
        const figures = estimate[option]
        return (
          <button
            key={option}
            type="button"
            role="radio"
            aria-checked={picked}
            disabled={disabled}
            data-option={option}
            title={estimateRange(figures)}
            onClick={() => {
              if (!picked) onChoose(option)
            }}
            className={`flex flex-col items-start gap-1 rounded-[10px] px-3.5 py-3 text-left text-text disabled:opacity-60 ${
              picked ? 'border-2 border-accent bg-accent-soft' : 'border border-border bg-surface hover:bg-nav-hover'
            }`}
          >
            <span className="font-semibold">
              {OPTION_NAME[option]}
              {current === option ? ' · current' : ''}
            </span>
            <span className="text-[13px] text-text-2 tabular-nums">{estimateLine(figures)}</span>
            <span className="text-xs text-text-muted">{OPTION_SHAPE[option]}</span>
          </button>
        )
      })}
    </div>
  )
}

const ROUGH = 'Rough: from measured calls, before Claude has seen your tables'

/** The estimate for what an unfinished compile has left; switching applies from its next pack. */
function EstimateCard({ state, job, estimate }: { state: JobsScreenState; job: JobSummary; estimate: Estimate }) {
  if (estimate.tables === 0) return null
  return (
    <article
      aria-label="Estimate"
      data-testid="estimate-card"
      className="flex flex-col gap-3.5 rounded-[14px] border border-border bg-surface px-[22px] py-[18px]"
    >
      <div className="flex flex-wrap items-baseline gap-2.5">
        <h2 className="m-0 flex-[1_1_240px] text-[15px] font-semibold">
          Remaining {estimate.tables} {estimate.tables === 1 ? 'table' : 'tables'}: estimate
        </h2>
        <span className="text-xs text-text-muted">{ROUGH}</span>
      </div>
      <OptionChoice
        estimate={estimate}
        chosen={job.table_calls}
        current={job.table_calls}
        disabled={state.busy}
        onChoose={(option) => void state.act(`/jobs/${job.id}/table_calls`, { table_calls: option })}
      />
      <p className="m-0 text-xs text-text-muted">
        Switching applies to the tables not started yet{job.state === 'running' ? ', from the next one' : ''}.
      </p>
    </article>
  )
}

export function JobsMain({ state }: { state: JobsScreenState }) {
  if (!state.loaded) {
    return (
      <div className="flex flex-col gap-1 rounded-xl border border-dashed border-text-faint px-6 py-8" data-testid="jobs-waiting">
        <p className="m-0 font-medium">Ingest and compile runs appear here.</p>
        <p className="m-0 text-[13px] text-text-muted">Nothing to show until the engine is running.</p>
      </div>
    )
  }
  const job = state.shown
  return (
    <>
      {state.error && (
        <p role="alert" className="m-0 text-[13px] text-text">
          That didn’t work: {state.error}
        </p>
      )}
      {job ? (
        <>
          <Banner state={state} job={job} />
          <JobCard state={state} job={job} />
          {state.estimate && <EstimateCard state={state} job={job} estimate={state.estimate} />}
        </>
      ) : (
        <div className="flex flex-col gap-1 rounded-xl border border-dashed border-text-faint px-6 py-8" data-testid="jobs-empty">
          <p className="m-0 font-medium">No compile has run yet.</p>
          <p className="m-0 text-[13px] text-text-muted">
            Compiles and metadata refreshes show here, with each table’s progress and every Claude call.
          </p>
        </div>
      )}
    </>
  )
}

// --- the rail -----------------------------------------------------------------------------------

const SECTION = 'm-0 text-xs font-semibold tracking-[0.06em] text-text-muted uppercase'

function NewTables({ state, refresh }: { state: JobsScreenState; refresh: JobSummary }) {
  const n = refresh.new_tables.length
  const { estimate } = useEstimate(refresh.database_name, false)
  const [chosen, setChosen] = useState<TableCalls>('split')
  return (
    <div
      data-testid="new-tables"
      className="flex flex-col gap-2 rounded-xl border border-accent-border bg-accent-soft px-3.5 py-3"
    >
      <span className="font-semibold">
        {n} new {n === 1 ? 'table' : 'tables'} in {refresh.database_name}
      </span>
      <span className="text-[13px] text-text-2">
        Found when metadata refreshed at {clockTime(refresh.created_at)}. That refresh used no Claude quota. Compiling{' '}
        {n === 1 ? 'it' : 'them'} will.
      </span>
      {estimate && estimate.tables > 0 && (
        <OptionChoice estimate={estimate} chosen={chosen} onChoose={setChosen} disabled={state.busy} />
      )}
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          disabled={state.busy}
          onClick={() => void state.act('/jobs/compile', { database_name: refresh.database_name, table_calls: chosen })}
          className="min-h-9 rounded-lg bg-accent-solid px-3 font-medium text-white disabled:opacity-60"
        >
          Compile {n === 1 ? 'it' : `${n} tables`}
        </button>
        <button
          type="button"
          disabled={state.busy}
          onClick={() => void state.act(`/jobs/${refresh.id}/dismiss`)}
          className={BUTTON_SECONDARY}
        >
          Later
        </button>
      </div>
    </div>
  )
}

function CallLog({ state }: { state: JobsScreenState }) {
  const job = state.shown
  if (!job || job.kind !== 'compile') return null
  const total = job.calls + job.cache_hits
  return (
    <section aria-label="Claude calls" className="flex flex-col gap-2.5">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className={SECTION}>Claude calls</h2>
        {total > state.calls.length && (
          <span className="text-xs text-text-muted">
            Latest {state.calls.length} of {total}
          </span>
        )}
      </div>
      {state.calls.length === 0 ? (
        <p className="m-0 text-[13px] text-text-muted">No calls yet.</p>
      ) : (
        <ol className="m-0 flex list-none flex-col p-0" data-testid="call-log">
          {state.calls.map((call) => (
            <li key={call.id} className="flex items-start gap-2.5 border-b border-border-subtle py-2 last:border-b-0">
              <span className="w-[56px] shrink-0 font-mono text-xs text-text-muted tabular-nums" title={new Date(call.at).toLocaleString()}>
                {logTime(call.at)}
              </span>
              <span className="flex min-w-0 flex-1 flex-col">
                <span className="truncate font-mono text-xs text-text">{call.task}</span>
                <span className="text-xs break-words text-text-muted">
                  {call.table_name ?? 'no table'} · {modelName(call.model)}
                </span>
              </span>
              <span
                data-outcome={call.cached ? 'cached' : call.outcome}
                className={`shrink-0 text-xs tabular-nums ${call.outcome === 'limited' ? 'font-medium text-attention' : 'text-text-2'}`}
              >
                {callResult(call)}
              </span>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

function Earlier({ state }: { state: JobsScreenState }) {
  const others = state.jobs.filter((j) => j.id !== state.shown?.id)
  if (others.length === 0) return null
  return (
    <section aria-label="Earlier" className="flex flex-col gap-1">
      <h2 className={SECTION}>Earlier</h2>
      <ul className="m-0 flex list-none flex-col p-0">
        {others.map((job) => (
          <li key={job.id}>
            <button
              type="button"
              onClick={() => state.select(job.id)}
              className="flex min-h-11 w-full items-center justify-between gap-2 rounded-lg px-2 text-left text-[13px] text-text-2 hover:bg-nav-hover hover:text-text"
            >
              <span className="min-w-0 py-1.5">
                {earlierLine(job)}
                {isUnfinished(job) && <span className="text-attention"> · {stateWord(job)}</span>}
              </span>
              <span className="shrink-0 text-xs text-text-muted">{whenShort(job.created_at, state.now)}</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  )
}

export function JobsRail({ state }: { state: JobsScreenState }) {
  if (!state.loaded) return <p className="m-0 text-[13px] text-text-muted">Nothing selected.</p>
  const prompts = newTablePrompts(state.jobs)
  return (
    <>
      {prompts.map((refresh) => (
        <NewTables key={refresh.id} state={state} refresh={refresh} />
      ))}
      <CallLog state={state} />
      <Earlier state={state} />
    </>
  )
}
