// The user's login-shell environment, for the engine (ARCHITECTURE.md §3, §6.5).
//
// An app opened from Finder or the Dock gets launchd's environment: PATH is /usr/bin:/bin:/usr/sbin:
// /sbin, and nothing from the user's shell rc files. The engine inherits it, so it would find no
// `claude` in ~/.local/bin or /opt/homebrew/bin, and never see an ANTHROPIC_API_KEY the shell
// exports, which onboarding has to name as ignored. So main runs the login shell once, reads its
// environment, and gives the engine the shell's PATH and auth variables. Nothing else is taken.

import { execFile } from 'node:child_process'

/** The variables every Claude call blanks; onboarding names the ones the shell sets (§3 rule 3). */
export const AUTH_VARIABLES = ['ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_OAUTH_TOKEN'] as const

const MARK = '__ONTOLOGICAL_ENV__'
// Whatever an rc file prints comes before the mark; env -0 keeps values with newlines whole.
const SCRIPT = `printf '%s' '${MARK}'; /usr/bin/env -0`
export const SHELL_TIMEOUT_MS = 5000

/** The variables in `env -0` output after the mark. */
export function parseEnv(output: string): Record<string, string> {
  const at = output.lastIndexOf(MARK)
  if (at < 0) return {}
  const vars: Record<string, string> = {}
  for (const entry of output.slice(at + MARK.length).split('\0')) {
    const eq = entry.indexOf('=')
    if (eq > 0) vars[entry.slice(0, eq)] = entry.slice(eq + 1)
  }
  return vars
}

/**
 * `base` with the shell's PATH entries first, then its own that the shell lacks, and each auth
 * variable the shell sets that `base` doesn't. A variable set in `base` wins: the app was then
 * started from a terminal, or by a test, with what it should use.
 */
export function mergeShellEnv(base: NodeJS.ProcessEnv, shell: Record<string, string>): NodeJS.ProcessEnv {
  const env = { ...base }
  if (shell.PATH) {
    const parts = [...shell.PATH.split(':'), ...(base.PATH ?? '').split(':')].filter(Boolean)
    env.PATH = [...new Set(parts)].join(':')
  }
  for (const name of AUTH_VARIABLES) {
    if (shell[name] && base[name] === undefined) env[name] = shell[name]
  }
  return env
}

export type RunShell = (shell: string, args: string[], timeoutMs: number) => Promise<string>

const runShell: RunShell = (shell, args, timeoutMs) =>
  new Promise((resolve, reject) => {
    execFile(
      shell,
      args,
      { timeout: timeoutMs, maxBuffer: 4 * 1024 * 1024, encoding: 'utf8' },
      (err, stdout) => (err ? reject(err) : resolve(stdout))
    )
  })

/**
 * `base` merged with the login shell's environment. If the shell can't be read in time, `base`
 * as it is, and `onError` says why: the engine then still looks in the usual install folders.
 */
export async function engineEnv(
  base: NodeJS.ProcessEnv,
  { run = runShell, timeoutMs = SHELL_TIMEOUT_MS, onError }: { run?: RunShell; timeoutMs?: number; onError?(err: unknown): void } = {}
): Promise<NodeJS.ProcessEnv> {
  const shell = base.SHELL || '/bin/zsh'
  try {
    return mergeShellEnv(base, parseEnv(await run(shell, ['-ilc', SCRIPT], timeoutMs)))
  } catch (err) {
    onError?.(err)
    return base
  }
}
