import { useEffect, useMemo, useState } from 'react'
import type { EngineState } from '@shared/engine'
import { useEngine } from '../useEngine'

type Running = Pick<Extract<EngineState, { status: 'running' }>, 'engineUrl' | 'token'>

/** An engine answer that wasn't 2xx: `detail` is the engine's sentence for it. */
export class EngineError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string
  ) {
    super(detail)
  }
}

export type EngineRequest = <T>(path: string, init?: { method?: string; body?: unknown }) => Promise<T>

function requester(engine: Running): EngineRequest {
  return async <T,>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> => {
    const res = await fetch(`${engine.engineUrl}${path}`, {
      method: init.method ?? 'GET',
      headers: {
        Authorization: `Bearer ${engine.token}`,
        ...(init.body === undefined ? {} : { 'Content-Type': 'application/json' })
      },
      body: init.body === undefined ? undefined : JSON.stringify(init.body)
    })
    if (!res.ok) {
      let detail = `The engine answered ${res.status}.`
      try {
        const body = (await res.json()) as { detail?: unknown }
        if (typeof body.detail === 'string') detail = body.detail
      } catch {
        // not JSON: keep the status sentence
      }
      throw new EngineError(res.status, detail)
    }
    return (res.status === 204 ? undefined : await res.json()) as T
  }
}

/** A request function while the engine runs, else null. A new one only when the engine moves. */
export function useEngineRequest(): EngineRequest | null {
  const engine = useEngine()
  const url = engine.status === 'running' ? engine.engineUrl : null
  const token = engine.status === 'running' ? engine.token : null
  return useMemo(
    () => (url !== null && token !== null ? requester({ engineUrl: url, token }) : null),
    [url, token]
  )
}

export interface Loaded<T> {
  data: T | undefined
  error: string | null
  loading: boolean
  reload(): void
}

/** GETs `path` while the engine runs, again whenever `path`, the engine or `key` changes. A null
 * path loads nothing. */
export function useEngineGet<T>(path: string | null, key: unknown = 0): Loaded<T> {
  const request = useEngineRequest()
  const [data, setData] = useState<T | undefined>(undefined)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [tick, setTick] = useState(0)
  useEffect(() => {
    if (request === null || path === null) return
    let live = true
    setLoading(true)
    request<T>(path)
      .then((got) => {
        if (!live) return
        setData(got)
        setError(null)
      })
      .catch((e: unknown) => {
        if (live) setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (live) setLoading(false)
      })
    return () => {
      live = false
    }
  }, [request, path, key, tick])
  return { data, error, loading, reload: () => setTick((n) => n + 1) }
}
