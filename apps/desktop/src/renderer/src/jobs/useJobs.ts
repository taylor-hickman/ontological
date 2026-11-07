import { useCallback, useEffect, useRef, useState } from 'react'
import type { EngineState } from '@shared/engine'
import { engineFetch } from '../engineApi'
import { useEngine } from '../useEngine'
import type { CallRow, Estimate, JobDetail, JobSummary } from './types'

/** How often the screen asks the engine again. Jobs change by the table, so 2 s is plenty. */
export const POLL_MS = 2000

type Running = Extract<EngineState, { status: 'running' }>

/**
 * GET `path` now and every `everyMs` while the engine runs; `everyMs` null asks once. Returns the
 * latest answer and a `refresh` to ask again at once, as after an action.
 */
function usePoll<T>(path: string | null, everyMs: number | null): { data: T | null; error: string | null; refresh(): void } {
  const engine = useEngine()
  const running = engine.status === 'running' ? engine : null
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tick, setTick] = useState(0)
  const key = running && path ? `${running.engineUrl}${path}` : null

  useEffect(() => {
    if (!running || !path) {
      setData(null)
      return
    }
    let live = true
    const ask = (): void => {
      engineFetch<T>(running, path).then(
        (got) => {
          if (!live) return
          setData(got)
          setError(null)
        },
        (e: Error) => {
          if (live) setError(e.message)
        }
      )
    }
    ask()
    const timer = everyMs === null ? null : setInterval(ask, everyMs)
    return () => {
      live = false
      if (timer !== null) clearInterval(timer)
    }
    // `running` changes identity with each state push; the key says when it matters.
  }, [key, everyMs, tick])

  return { data, error, refresh: useCallback(() => setTick((t) => t + 1), []) }
}

/** The latest jobs, newest first, for the sidebar badge, the status bar and the screen. */
export function useJobs(): { jobs: JobSummary[]; loaded: boolean; error: string | null; refresh(): void } {
  const { data, error, refresh } = usePoll<{ jobs: JobSummary[] }>('/jobs', POLL_MS)
  return { jobs: data?.jobs ?? [], loaded: data !== null, error, refresh }
}

/** One job's tables and call log, kept current while it is unfinished. */
export function useJobDetail(
  jobId: number | null,
  live: boolean
): { detail: JobDetail | null; calls: CallRow[]; refresh(): void } {
  const every = live ? POLL_MS : null
  const detail = usePoll<JobDetail>(jobId === null ? null : `/jobs/${jobId}`, every)
  const calls = usePoll<{ calls: CallRow[] }>(jobId === null ? null : `/jobs/${jobId}/calls`, every)
  const refreshDetail = detail.refresh
  const refreshCalls = calls.refresh
  const refresh = useCallback(() => {
    refreshDetail()
    refreshCalls()
  }, [refreshDetail, refreshCalls])
  // A detail from another job, while the new one loads, is not this job's.
  const matching = detail.data && detail.data.job.id === jobId ? detail.data : null
  return { detail: matching, calls: matching ? (calls.data?.calls ?? []) : [], refresh }
}

/** How often an estimate is asked again while its compile runs: it changes by the pack. */
export const ESTIMATE_POLL_MS = 10_000

/**
 * The split and merged estimate (§6.5) for a database's next compile, or what its unfinished one
 * has left. Asks Claude nothing.
 */
export function useEstimate(databaseName: string | null, live: boolean): { estimate: Estimate | null; refresh(): void } {
  const path = databaseName === null ? null : `/jobs/estimate?database_name=${encodeURIComponent(databaseName)}`
  const { data, refresh } = usePoll<Estimate>(path, live ? ESTIMATE_POLL_MS : null)
  return { estimate: data && data.database_name === databaseName ? data : null, refresh }
}

/** POSTs a job action, then refreshes what shows it. Returns the error, if any, for the screen. */
export function useJobAction(onDone: () => void): {
  act(path: string, body?: unknown): Promise<void>
  busy: boolean
  error: string | null
} {
  const engine = useEngine()
  const engineRef = useRef<Running | null>(null)
  engineRef.current = engine.status === 'running' ? engine : null
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const act = useCallback(
    async (path: string, body?: unknown) => {
      const current = engineRef.current
      if (!current) return
      setBusy(true)
      setError(null)
      try {
        await engineFetch(current, path, { method: 'POST', body })
      } catch (e) {
        setError((e as Error).message)
      } finally {
        setBusy(false)
        onDone()
      }
    },
    [onDone]
  )
  return { act, busy, error }
}
