// The renderer's content security policy (ARCHITECTURE.md §4.1). Everything loads from the app
// itself; fonts are bundled, so there is no Google Fonts. The renderer may connect to the
// engine's origin and nothing else on the machine.
//
// Main sends it as a response header, not a <meta> tag, because the engine's port is only known
// at run time and changes when the engine restarts: a document's policy is fixed once it loads,
// so main reloads the window when the engine's port moves.

export function contentSecurityPolicy(enginePort: number | null): string {
  const connect = ["'self'"]
  if (enginePort !== null) connect.push(`http://127.0.0.1:${enginePort}`)
  return [
    "default-src 'none'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "font-src 'self'",
    "img-src 'self' data:",
    `connect-src ${connect.join(' ')}`,
    "base-uri 'none'",
    "form-action 'none'"
  ].join('; ')
}
