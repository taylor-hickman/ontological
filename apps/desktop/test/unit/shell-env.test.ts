import { chmodSync, mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { describe, expect, it, vi } from 'vitest'
import { engineEnv, mergeShellEnv, parseEnv } from '../../src/main/shell-env'

const LAUNCHD_PATH = '/usr/bin:/bin:/usr/sbin:/sbin'

describe('parseEnv', () => {
  it('reads env -0 after the mark, skipping whatever rc files printed', () => {
    const out = 'Welcome back!\n__ONTOLOGICAL_ENV__PATH=/a:/b\0NOTE=two\nlines\0EMPTY=\0'
    expect(parseEnv(out)).toEqual({ PATH: '/a:/b', NOTE: 'two\nlines', EMPTY: '' })
  })

  it('reads nothing without the mark', () => {
    expect(parseEnv('PATH=/a\0')).toEqual({})
  })
})

describe('mergeShellEnv', () => {
  it('puts the shell’s PATH first and keeps the app’s other entries once', () => {
    const env = mergeShellEnv({ PATH: LAUNCHD_PATH, HOME: '/h' }, { PATH: '/h/.local/bin:/usr/bin:/bin' })
    expect(env.PATH).toBe('/h/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin')
    expect(env.HOME).toBe('/h')
  })

  it('takes the auth variables the shell sets, and nothing else from it', () => {
    const env = mergeShellEnv(
      { PATH: LAUNCHD_PATH },
      { ANTHROPIC_API_KEY: 'sk-shell', CLAUDE_CODE_OAUTH_TOKEN: '', SECRET_THING: 'x', CLAUDECODE: '1' }
    )
    expect(env.ANTHROPIC_API_KEY).toBe('sk-shell')
    expect(env.CLAUDE_CODE_OAUTH_TOKEN).toBeUndefined()
    expect(env.SECRET_THING).toBeUndefined()
    expect(env.CLAUDECODE).toBeUndefined()
  })

  it('keeps a variable the app already has', () => {
    const env = mergeShellEnv({ ANTHROPIC_API_KEY: 'sk-app' }, { ANTHROPIC_API_KEY: 'sk-shell' })
    expect(env.ANTHROPIC_API_KEY).toBe('sk-app')
  })
})

describe('engineEnv', () => {
  it('runs the user’s shell as a login shell', async () => {
    const run = vi.fn(async () => '__ONTOLOGICAL_ENV__PATH=/opt/homebrew/bin\0')
    const env = await engineEnv({ SHELL: '/bin/fish', PATH: LAUNCHD_PATH }, { run })
    expect(run).toHaveBeenCalledWith('/bin/fish', ['-ilc', expect.stringContaining('/usr/bin/env -0')], 5000)
    expect(env.PATH).toBe(`/opt/homebrew/bin:${LAUNCHD_PATH}`)
  })

  it('falls back to the app’s environment when the shell fails or hangs', async () => {
    const onError = vi.fn()
    const base = { SHELL: '/bin/zsh', PATH: LAUNCHD_PATH }
    const env = await engineEnv(base, { run: () => Promise.reject(new Error('timed out')), onError })
    expect(env).toEqual(base)
    expect(onError).toHaveBeenCalledOnce()
  })

  it('reads a real shell whose rc files print and export', async () => {
    // A stand-in for zsh -ilc: its "rc" prints a banner and exports what a user's would.
    const dir = mkdtempSync(join(tmpdir(), 'ontological-shell-'))
    const shell = join(dir, 'shell')
    writeFileSync(
      shell,
      '#!/bin/sh\necho "rc banner"\nexport PATH="/fake/bin:$PATH"\nexport ANTHROPIC_API_KEY=sk-rc\nexec /bin/sh -c "$2"\n'
    )
    chmodSync(shell, 0o755)
    const env = await engineEnv({ SHELL: shell, PATH: LAUNCHD_PATH })
    expect(env.PATH?.split(':')[0]).toBe('/fake/bin')
    expect(env.ANTHROPIC_API_KEY).toBe('sk-rc')
  })
})
