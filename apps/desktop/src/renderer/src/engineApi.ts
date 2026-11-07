import type { EngineState } from '@shared/engine'

type Running = Extract<EngineState, { status: 'running' }>

export class EngineError extends Error {
  constructor(
    readonly status: number,
    message: string
  ) {
    super(message)
  }
}

/** A request to the engine's API with this app's token (ARCHITECTURE.md §4.3). */
export async function engineFetch<T>(engine: Running, path: string, init?: { method?: string; body?: unknown }): Promise<T> {
  const response = await fetch(`${engine.engineUrl}${path}`, {
    method: init?.method ?? 'GET',
    headers: {
      authorization: `Bearer ${engine.token}`,
      ...(init?.body === undefined ? {} : { 'content-type': 'application/json' })
    },
    body: init?.body === undefined ? undefined : JSON.stringify(init.body)
  })
  if (!response.ok) {
    let detail = response.statusText
    try {
      detail = ((await response.json()) as { detail?: string }).detail ?? detail
    } catch {
      // not JSON: keep the status text
    }
    throw new EngineError(response.status, detail)
  }
  const text = await response.text()
  return (text ? JSON.parse(text) : undefined) as T
}
