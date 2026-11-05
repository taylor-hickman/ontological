// The renderer loads from app://ontological/, not file://, so main can send its CSP as a header
// with the engine's current port (csp.ts). Electron's security checklist prefers a custom
// protocol to file:// anyway. The engine allows this origin through CORS.

import { resolve, sep } from 'node:path'
import { pathToFileURL } from 'node:url'

export const APP_SCHEME = 'app'
export const APP_HOST = 'ontological'
export const APP_ORIGIN = `${APP_SCHEME}://${APP_HOST}`

/** The file under `root` that an app:// URL names, or null for another host or a path outside it. */
export function resolveAppFile(root: string, url: string): string | null {
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    return null
  }
  if (parsed.protocol !== `${APP_SCHEME}:` || parsed.host !== APP_HOST) return null
  let pathname: string
  try {
    pathname = decodeURIComponent(parsed.pathname)
  } catch {
    return null
  }
  const file = resolve(root, `.${pathname === '/' ? '/index.html' : pathname}`)
  return file.startsWith(root + sep) ? file : null
}

export interface AppProtocolOptions {
  /** The built renderer, out/renderer. */
  root: string
  /** Reads a file:// URL; Electron's `net.fetch`, which sets the content type. */
  fetchFile: (fileUrl: string) => Promise<Response>
  /** The CSP for a document being loaded now. */
  policy: () => Promise<string>
}

export function appProtocolHandler({ root, fetchFile, policy }: AppProtocolOptions) {
  return async (request: Request): Promise<Response> => {
    const file = resolveAppFile(root, request.url)
    if (!file) return new Response('Not found', { status: 404 })
    const res = await fetchFile(pathToFileURL(file).toString())
    if (!file.endsWith('.html')) return res
    const headers = new Headers(res.headers)
    headers.set('Content-Security-Policy', await policy())
    return new Response(res.body, { status: res.status, headers })
  }
}
