// The engine supervisor against the real engine process (ARCHITECTURE.md §4.4), with the engine's
// fake store standing in for Postgres.

import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { expect, test, type Page } from '@playwright/test'
import type { EngineState } from '../../src/shared/engine'
import { pidAlive, readEngineJson } from '../../src/main/engine/io'
import { freshUserData, launch, startHeadless } from './app'

type Running = Extract<EngineState, { status: 'running' }>

const started: number[] = []

test.afterEach(() => {
  // Engines idle out by themselves; this only tidies up after a failed test.
  for (const pid of started.splice(0)) {
    if (pidAlive(pid)) process.kill(pid, 'SIGTERM')
  }
})

async function waitForStatus(page: Page, status: EngineState['status']): Promise<void> {
  await expect(page.getByTestId('engine-status')).toHaveAttribute('data-status', status, { timeout: 20_000 })
}

async function running(page: Page): Promise<Running> {
  await waitForStatus(page, 'running')
  const state = await page.evaluate(() => window.ontological.engine())
  if (state.status !== 'running') throw new Error(`engine is ${state.status}`)
  return state
}

/** GET /health from the renderer, with the token the preload handed it. */
function healthFromRenderer(page: Page, state: Running): Promise<{ status: number; pid: number }> {
  return page.evaluate(async ({ engineUrl, token }) => {
    const res = await fetch(`${engineUrl}/health`, { headers: { Authorization: `Bearer ${token}` } })
    const body = (await res.json()) as { pid: number }
    return { status: res.status, pid: body.pid }
  }, state)
}

function engineExits(pid: number, timeout = 15_000): Promise<void> {
  return expect.poll(() => pidAlive(pid), { timeout, message: `engine ${pid} exits` }).toBe(false)
}

test('starts an engine when none is running, and the renderer reaches its API', async () => {
  const userData = freshUserData()
  const { app, page } = await launch(userData)
  try {
    const state = await running(page)
    expect(state.startedByApp).toBe(true)
    const info = readEngineJson(userData)!
    started.push(info.pid)
    expect(state.engineUrl).toBe(`http://127.0.0.1:${info.port}`)
    expect(state.token).toBe(info.token)
    await expect(page.getByTestId('engine-status')).toHaveText('Engine running')

    // The preload's URL and token reach the API through the CSP and the engine's CORS.
    expect(await healthFromRenderer(page, state)).toEqual({ status: 200, pid: info.pid })
  } finally {
    await app.close()
  }
})

test('the CSP allows the engine’s origin and still blocks inline scripts and other ports', async () => {
  const userData = freshUserData()
  const { app, page } = await launch(userData)
  try {
    const state = await running(page)
    started.push(readEngineJson(userData)!.pid)
    const port = Number(new URL(state.engineUrl).port)
    const result = await page.evaluate(async (otherPort) => {
      const violations: string[] = []
      document.addEventListener('securitypolicyviolation', (e) => violations.push(e.effectiveDirective))
      const script = document.createElement('script')
      script.textContent = 'window.__inlineRan = true'
      document.head.appendChild(script)
      const other = await fetch(`http://127.0.0.1:${otherPort}/`).then(
        () => 'reached',
        () => 'blocked'
      )
      await new Promise((resolve) => setTimeout(resolve, 100))
      return { inlineRan: '__inlineRan' in window, other, violations }
    }, port === 65535 ? port - 1 : port + 1)
    expect(result.inlineRan).toBe(false)
    expect(result.other).toBe('blocked')
    expect(result.violations).toEqual(expect.arrayContaining(['script-src-elem', 'connect-src']))
  } finally {
    await app.close()
  }
})

test('attaches to a headless engine already running, and quitting releases its lease', async () => {
  const userData = freshUserData()
  // A lease renewed every 5 s outlives a quit by 15 s, so an exit within that time means the app
  // released its lease rather than letting it lapse.
  const engineArgs = ['--idle-timeout', '2', '--lease-renew', '5']
  const pid = startHeadless(userData, engineArgs)
  started.push(pid)
  const { app, page } = await launch(userData, engineArgs)
  let closed = false
  try {
    const state = await running(page)
    expect(state.startedByApp).toBe(false)
    expect(readEngineJson(userData)!.pid).toBe(pid)
    expect(await healthFromRenderer(page, state)).toEqual({ status: 200, pid })

    // The lease keeps the engine up past its idle timeout.
    await page.waitForTimeout(3000)
    expect(pidAlive(pid)).toBe(true)

    await app.close()
    closed = true
    const quitAt = Date.now()
    await engineExits(pid, 12_000)
    expect(Date.now() - quitAt).toBeLessThan(12_000)
  } finally {
    if (!closed) await app.close()
  }
})

test('quitting leaves an engine the app started running until it idles out', async () => {
  const userData = freshUserData()
  // Long enough an idle timeout to outlast the app's own shutdown on a slow CI machine.
  const { app, page } = await launch(userData, ['--idle-timeout', '8', '--lease-renew', '5'])
  await running(page)
  const pid = readEngineJson(userData)!.pid
  started.push(pid)
  const closing = Date.now()
  await app.close()
  const log = (): string => readFileSync(join(userData, 'logs/engine.log'), 'utf8')
  expect(pidAlive(pid), `app.close() took ${Date.now() - closing} ms; engine.log:\n${log()}`).toBe(true)
  await engineExits(pid, 25_000)
  // It stopped because it was idle, not because the app took it down.
  expect(log()).toContain('idle for 8s: stopping')
})

test('restarts an engine it started when that engine crashes', async () => {
  const userData = freshUserData()
  const { app, page } = await launch(userData)
  try {
    await running(page)
    await expect(page.getByTestId('engine-banner')).toHaveCount(0)
    const first = readEngineJson(userData)!.pid
    started.push(first)
    // The next engine takes 3 s to start its store, long enough to see the banner.
    writeFileSync(join(userData, 'slow-start'), '3')
    process.kill(first, 'SIGKILL')

    const banner = page.getByTestId('engine-banner')
    await expect(banner).toHaveAttribute('data-status', 'restarting', { timeout: 10_000 })
    await expect(banner).toContainText('The engine stopped. Restarting…')
    await expect(banner.getByRole('button')).toHaveCount(0)

    await expect
      .poll(() => readEngineJson(userData)?.pid, { timeout: 20_000, message: 'a new engine writes engine.json' })
      .not.toBe(first)
    const state = await running(page)
    const second = readEngineJson(userData)!
    started.push(second.pid)
    expect(state.startedByApp).toBe(true)
    expect(state.token).toBe(second.token)
    await expect(page.getByTestId('engine-banner')).toHaveCount(0)
    // The new engine has a new port; the window reloaded with it in its CSP.
    expect(await healthFromRenderer(page, state)).toEqual({ status: 200, pid: second.pid })
  } finally {
    await app.close()
  }
})

test('doesn’t restart an engine it attached to when that engine crashes', async () => {
  const userData = freshUserData()
  const pid = startHeadless(userData)
  started.push(pid)
  const { app, page } = await launch(userData)
  try {
    expect((await running(page)).startedByApp).toBe(false)
    process.kill(pid, 'SIGKILL')
    await waitForStatus(page, 'stopped')
    await expect(page.getByTestId('engine-status')).toHaveText('Engine stopped')
    // No engine took its place.
    await page.waitForTimeout(1500)
    expect(readEngineJson(userData)!.pid).toBe(pid)
    expect(await page.evaluate(() => window.ontological.engine())).toEqual({ status: 'stopped' })
  } finally {
    await app.close()
  }
})

test('Reconnect starts an engine after an attached one has gone, without relaunching', async () => {
  const userData = freshUserData()
  const pid = startHeadless(userData)
  started.push(pid)
  const { app, page } = await launch(userData)
  try {
    expect((await running(page)).startedByApp).toBe(false)
    process.kill(pid, 'SIGKILL')

    const banner = page.getByTestId('engine-banner')
    await expect(banner).toHaveAttribute('data-status', 'stopped', { timeout: 20_000 })
    await expect(banner).toContainText('The engine stopped')
    await banner.getByRole('button', { name: 'Reconnect' }).click()

    const state = await running(page)
    const next = readEngineJson(userData)!
    started.push(next.pid)
    expect(next.pid).not.toBe(pid)
    expect(state.startedByApp).toBe(true)
    await expect(page.getByTestId('engine-banner')).toHaveCount(0)
    expect(await healthFromRenderer(page, state)).toEqual({ status: 200, pid: next.pid })
  } finally {
    await app.close()
  }
})
