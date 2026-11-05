// The engine supervisor (ARCHITECTURE.md §4.4). It finds a live engine through engine.json or
// starts one, holds a lease on it, and restarts it after a crash only if this app started it.
//
// It never takes engine.lock: the engine takes it itself while it decides whether to serve, and
// prints `started: false` with the live engine when another client won the race. Reading
// engine.json without the lock can find an engine that is shutting down; that engine refuses
// the lease with 503, and the supervisor then starts a new one.

import type { EngineState } from '@shared/engine'

/** What engine.json holds. */
export interface EngineInfo {
  pid: number
  port: number
  token: string
  version: string
}

/** The one JSON line the engine prints once it serves or attaches, or once it fails. */
export type EngineLine = { pid: number; port: number; version: string; started: boolean } | { error: string }

/** An engine process this app spawned. */
export interface Spawned {
  /** Its line, or null if it exited without printing one. */
  line: Promise<EngineLine | null>
  /** Its exit code, or null if a signal ended it. */
  exit: Promise<number | null>
}

export interface HttpResult {
  status: number
  body: unknown
}

/** Everything the supervisor does to the outside world, so tests can fake it. */
export interface EngineIo {
  /** engine.json, or null if it is missing or damaged. Says nothing about whether that engine runs. */
  readEngineJson(): EngineInfo | null
  pidAlive(pid: number): boolean
  /** A request with the engine's bearer token. Rejects if the engine doesn't answer. */
  request(info: EngineInfo, method: string, path: string, body?: unknown, timeoutMs?: number): Promise<HttpResult>
  /** Runs the engine in the foreground, as this app's child. */
  spawn(): Spawned
  sleep(ms: number): Promise<void>
}

interface Lease {
  lease: string
  renew_every_s: number
}

interface Held {
  info: EngineInfo
  lease: string
  renewEveryS: number
  /** The engine process, when this app started it; null when it attached. */
  owned: Spawned | null
}

const ATTEMPTS = 5
const RETRY_MS = 250
const RELEASE_TIMEOUT_MS = 1000
/** More crashes than this within CRASH_WINDOW_MS and the supervisor stops restarting. */
const MAX_CRASHES = 3
const CRASH_WINDOW_MS = 60_000

export class EngineSupervisor {
  private state: EngineState = { status: 'starting' }
  private held: Held | null = null
  private timer: ReturnType<typeof setTimeout> | undefined
  private quitting = false
  private crashes: number[] = []
  /** Bumped whenever the held engine changes, so a request that outlived its engine is ignored. */
  private generation = 0

  constructor(
    private readonly io: EngineIo,
    private readonly onState: (state: EngineState) => void
  ) {}

  get current(): EngineState {
    return this.state
  }

  /** Resolves once the app holds a lease, or has given up. */
  async start(): Promise<void> {
    this.set({ status: 'starting' })
    await this.connect(false)
  }

  /** Releases the lease and leaves the engine running: it idles out once no client holds a lease. */
  async quit(): Promise<void> {
    this.quitting = true
    clearTimeout(this.timer)
    const held = this.held
    if (!held) return
    await this.io
      .request(held.info, 'DELETE', `/leases/${held.lease}`, undefined, RELEASE_TIMEOUT_MS)
      .catch(() => undefined)
  }

  private set(state: EngineState): void {
    this.state = state
    this.onState(state)
  }

  /** Attach to the live engine or start one, then take a lease. `skipLive` is set after a 503. */
  private async connect(skipLive: boolean): Promise<void> {
    const generation = ++this.generation
    for (let attempt = 0; attempt < ATTEMPTS; attempt++) {
      if (attempt > 0) await this.io.sleep(RETRY_MS)
      if (this.quitting || generation !== this.generation) return

      let found = skipLive ? null : await this.findLive()
      if (!found) {
        try {
          found = await this.startEngine()
        } catch (err) {
          if (generation === this.generation) this.set({ status: 'failed', error: message(err) })
          return
        }
      }

      const lease = await this.takeLease(found.info).catch(() => null)
      if (this.quitting || generation !== this.generation) return
      if (lease === 'closing') {
        // The engine is shutting down. Looking again would find it still answering until it has.
        skipLive = true
        continue
      }
      if (lease === null) {
        skipLive = false
        continue
      }
      this.hold({ ...found, lease: lease.lease, renewEveryS: lease.renew_every_s })
      return
    }
    this.set({ status: 'failed', error: 'The engine didn’t give this app a lease; see logs/engine.log' })
  }

  private async findLive(): Promise<{ info: EngineInfo; owned: null } | null> {
    const info = this.io.readEngineJson()
    if (!info || !this.io.pidAlive(info.pid)) return null
    return (await this.answers(info)) ? { info, owned: null } : null
  }

  /** Whether the engine answers /health with its own pid: after a reboot the pid and port can be anyone's. */
  private async answers(info: EngineInfo): Promise<boolean> {
    try {
      const res = await this.io.request(info, 'GET', '/health')
      return res.status === 200 && isRecord(res.body) && res.body.pid === info.pid
    } catch {
      return false
    }
  }

  private async startEngine(): Promise<{ info: EngineInfo; owned: Spawned | null }> {
    let child: Spawned
    try {
      child = this.io.spawn()
    } catch (err) {
      throw new Error(`Couldn’t run the engine: ${message(err)}`)
    }
    const line = await child.line
    if (line === null) {
      const code = await child.exit
      throw new Error(`The engine exited with ${code ?? 'a signal'} before it served; see logs/engine.log`)
    }
    if ('error' in line) throw new Error(line.error)
    // The line has no token; engine.json does. The engine writes it before it prints the line.
    const info = this.io.readEngineJson()
    if (!info || info.pid !== line.pid || info.port !== line.port) {
      throw new Error('engine.json doesn’t name the engine that started')
    }
    return { info, owned: line.started ? child : null }
  }

  private async takeLease(info: EngineInfo): Promise<Lease | 'closing'> {
    const res = await this.io.request(info, 'POST', '/leases', { client: 'electron' })
    if (res.status === 503) return 'closing'
    if (res.status === 201 && isLease(res.body)) return res.body
    throw new Error(`POST /leases answered ${res.status}`)
  }

  private hold(held: Held): void {
    this.held = held
    this.set({
      status: 'running',
      engineUrl: `http://127.0.0.1:${held.info.port}`,
      token: held.info.token,
      version: held.info.version,
      startedByApp: held.owned !== null
    })
    this.schedule(held.renewEveryS)
    const owned = held.owned
    if (owned) void owned.exit.then(() => this.onExit(owned))
  }

  private release(): void {
    clearTimeout(this.timer)
    this.held = null
    this.generation++
  }

  private schedule(seconds: number): void {
    clearTimeout(this.timer)
    this.timer = setTimeout(() => void this.renew(), seconds * 1000)
  }

  private async renew(): Promise<void> {
    const held = this.held
    if (!held || this.quitting) return
    const generation = this.generation
    const stale = (): boolean => generation !== this.generation || this.quitting

    const res = await this.io.request(held.info, 'PUT', `/leases/${held.lease}`).catch(() => null)
    if (stale()) return
    if (res?.status === 200) {
      this.schedule(isLease(res.body) ? res.body.renew_every_s : held.renewEveryS)
      return
    }
    if (res?.status === 404) {
      // The lease expired, say while the machine slept. Take a new one.
      const lease = await this.takeLease(held.info).catch(() => null)
      if (stale()) return
      if (lease === 'closing') {
        this.release()
        this.set({ status: 'starting' })
        void this.connect(true)
        return
      }
      if (lease) {
        held.lease = lease.lease
        held.renewEveryS = lease.renew_every_s
      }
    } else if (res === null && !held.owned && !(await this.answers(held.info))) {
      if (stale()) return
      // An engine this app attached to has gone. Its own client restarts it, not this app.
      this.release()
      this.set({ status: 'stopped' })
      return
    }
    // A missed renewal is fine: the lease survives two. An engine this app started that has
    // crashed is handled by onExit.
    this.schedule(held.renewEveryS)
  }

  private onExit(child: Spawned): void {
    if (this.quitting || this.held?.owned !== child) return
    this.release()
    const now = Date.now()
    this.crashes = this.crashes.filter((t) => now - t < CRASH_WINDOW_MS)
    if (this.crashes.length >= MAX_CRASHES) {
      this.set({ status: 'failed', error: 'The engine keeps stopping; see logs/engine.log' })
      return
    }
    this.crashes.push(now)
    this.set({ status: 'restarting' })
    void this.connect(false)
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isLease(value: unknown): value is Lease {
  return (
    isRecord(value) &&
    typeof value.lease === 'string' &&
    typeof value.renew_every_s === 'number' &&
    value.renew_every_s > 0
  )
}

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}
