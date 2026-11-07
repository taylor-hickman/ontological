import type { EngineState } from '@shared/engine'

/** What a request needs of a running engine. */
type Running = Pick<Extract<EngineState, { status: 'running' }>, 'engineUrl' | 'token'>

export class EngineError extends Error {
  constructor(
    readonly status: number,
    message: string
  ) {
    super(message)
  }
}

/**
 * An error's `detail` as one line: FastAPI's string, or pydantic's list of errors as
 * "semantic_layer.terms.0.name: Field required; …".
 */
export function errorDetail(detail: unknown): string | null {
  if (typeof detail === 'string') return detail
  if (!Array.isArray(detail) || detail.length === 0) return null
  return detail
    .map((e: { loc?: unknown; msg?: unknown }) => {
      const loc = Array.isArray(e.loc) ? e.loc.filter((part) => part !== 'body').join('.') : ''
      const msg = typeof e.msg === 'string' ? e.msg : JSON.stringify(e)
      return loc ? `${loc}: ${msg}` : msg
    })
    .join('; ')
}

export interface EngineInit {
  method?: string
  /** Sent as JSON. */
  body?: unknown
  /** Sent as it is, in place of `body`: a file's text. */
  text?: string
}

/** A request to the engine's API with this app's token (ARCHITECTURE.md §4.3), parsed as JSON. */
export async function engineFetch<T>(engine: Running, path: string, init?: EngineInit): Promise<T> {
  const text = await (await engineSend(engine, path, init)).text()
  return (text ? JSON.parse(text) : undefined) as T
}

/** The engine's answer as it came, for a route that answers with a file. Throws as `engineFetch` does. */
export async function engineSend(engine: Running, path: string, init?: EngineInit): Promise<Response> {
  const response = await fetch(`${engine.engineUrl}${path}`, {
    method: init?.method ?? 'GET',
    headers: {
      authorization: `Bearer ${engine.token}`,
      ...(init?.text !== undefined
        ? { 'content-type': 'application/x-yaml' }
        : init?.body !== undefined
          ? { 'content-type': 'application/json' }
          : {})
    },
    body: init?.text ?? (init?.body === undefined ? undefined : JSON.stringify(init.body))
  })
  if (!response.ok) {
    let detail = response.statusText
    try {
      detail = errorDetail(((await response.json()) as { detail?: unknown }).detail) ?? detail
    } catch {
      // not JSON: keep the status text
    }
    throw new EngineError(response.status, detail)
  }
  return response
}
