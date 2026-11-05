// The supervisor's I/O on a real machine: engine.json, pids, HTTP to 127.0.0.1, and the engine
// process (ARCHITECTURE.md §4.3, §4.4).

import { spawn } from 'node:child_process'
import { closeSync, mkdirSync, openSync, readFileSync } from 'node:fs'
import { request as httpRequest } from 'node:http'
import { join } from 'node:path'
import { createInterface } from 'node:readline'
import type { EngineInfo, EngineIo, EngineLine, HttpResult, Spawned } from './supervisor'

const REQUEST_TIMEOUT_MS = 5000

export interface EngineIoOptions {
  /** <appData>, the folder engine.json and engine.lock live in. The engine gets it as --app-data. */
  appData: string
  /** The engine's command line, without --app-data. */
  command: string[]
  env?: NodeJS.ProcessEnv
}

export function nodeEngineIo({ appData, command, env = process.env }: EngineIoOptions): EngineIo {
  return {
    readEngineJson: () => readEngineJson(appData),
    pidAlive,
    request,
    spawn: () => spawnEngine(appData, command, env),
    sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms))
  }
}

export function readEngineJson(appData: string): EngineInfo | null {
  let data: unknown
  try {
    data = JSON.parse(readFileSync(join(appData, 'engine.json'), 'utf8'))
  } catch {
    return null
  }
  if (typeof data !== 'object' || data === null) return null
  const { pid, port, token, version } = data as Record<string, unknown>
  if (
    !Number.isInteger(pid) ||
    !Number.isInteger(port) ||
    typeof token !== 'string' ||
    typeof version !== 'string'
  ) {
    return null
  }
  return { pid: pid as number, port: port as number, token, version }
}

export function pidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0)
    return true
  } catch (err) {
    return (err as NodeJS.ErrnoException).code === 'EPERM'
  }
}

/**
 * node:http straight to 127.0.0.1. Unlike Electron's `net`, it never goes through the system
 * proxy, which would see the bearer token.
 */
function request(
  info: EngineInfo,
  method: string,
  path: string,
  body?: unknown,
  timeoutMs = REQUEST_TIMEOUT_MS
): Promise<HttpResult> {
  const payload = body === undefined ? undefined : JSON.stringify(body)
  return new Promise((resolve, reject) => {
    const req = httpRequest(
      {
        host: '127.0.0.1',
        port: info.port,
        method,
        path,
        timeout: timeoutMs,
        headers: {
          Authorization: `Bearer ${info.token}`,
          ...(payload === undefined ? {} : { 'Content-Type': 'application/json' })
        }
      },
      (res) => {
        const chunks: Buffer[] = []
        res.on('data', (chunk: Buffer) => chunks.push(chunk))
        res.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8')
          let parsed: unknown = null
          try {
            parsed = text ? JSON.parse(text) : null
          } catch {
            parsed = null
          }
          resolve({ status: res.statusCode ?? 0, body: parsed })
        })
        res.on('error', reject)
      }
    )
    req.on('timeout', () => req.destroy(new Error(`${method} ${path} timed out`)))
    req.on('error', reject)
    req.end(payload)
  })
}

/**
 * Runs the engine in the foreground as this app's child, in its own session so it outlives the
 * app: quitting releases the lease and leaves the engine to idle out (§4.4). Its log goes to
 * <appData>/logs/engine.log, where a headless engine logs too; its stdout carries the one line.
 */
function spawnEngine(appData: string, command: string[], env: NodeJS.ProcessEnv): Spawned {
  const [file, ...args] = command
  if (!file) throw new Error('the engine command is empty')
  const logs = join(appData, 'logs')
  mkdirSync(logs, { recursive: true })
  const log = openSync(join(logs, 'engine.log'), 'a')
  let child
  try {
    child = spawn(file, [...args, '--app-data', appData], {
      detached: true,
      stdio: ['ignore', 'pipe', log],
      env
    })
  } finally {
    closeSync(log)
  }

  const exit = new Promise<number | null>((resolve) => {
    child.once('exit', (code) => resolve(code))
    // A command that can't run emits 'error' and never 'exit'.
    child.once('error', () => resolve(null))
  })
  const line = new Promise<EngineLine | null>((resolve) => {
    child.once('error', (err) => resolve({ error: `Couldn’t run ${file}: ${err.message}` }))
    const lines = createInterface({ input: child.stdout })
    lines.once('line', (text) => {
      lines.close()
      // Keep draining, so nothing the engine prints later can fill the pipe.
      child.stdout.resume()
      resolve(parseLine(text))
    })
    lines.once('close', () => resolve(null))
  })
  child.unref()
  return { line, exit }
}

function parseLine(text: string): EngineLine | null {
  let data: unknown
  try {
    data = JSON.parse(text)
  } catch {
    return { error: `The engine printed something other than its JSON line: ${text.slice(0, 200)}` }
  }
  if (typeof data !== 'object' || data === null) return null
  const d = data as Record<string, unknown>
  if (typeof d.error === 'string') return { error: d.error }
  if (
    Number.isInteger(d.pid) &&
    Number.isInteger(d.port) &&
    typeof d.version === 'string' &&
    typeof d.started === 'boolean'
  ) {
    return { pid: d.pid as number, port: d.port as number, version: d.version, started: d.started }
  }
  return null
}
