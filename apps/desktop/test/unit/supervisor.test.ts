import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { EngineState } from '@shared/engine'
import {
  EngineSupervisor,
  type EngineInfo,
  type EngineIo,
  type EngineLine,
  type HttpResult,
  type Spawned
} from '../../src/main/engine/supervisor'

/** One engine process the fake knows about. */
interface FakeEngine {
  info: EngineInfo
  alive: boolean
  closing: boolean
  leases: Set<string>
  /** Set for an engine this app spawned and that served; resolves its exit. */
  exit?: (code: number | null) => void
}

/**
 * Engines in memory: engine.json, pids, the HTTP routes the supervisor uses, and spawn. A spawn
 * either serves a new engine, attaches to the live one (`started: false`), or fails, as
 * `nextSpawn` says.
 */
class FakeIo implements EngineIo {
  engines: FakeEngine[] = []
  engineJson: EngineInfo | null = null
  spawns = 0
  nextSpawn: 'serve' | 'attach' | { error: string } | 'die' = 'serve'
  requests: string[] = []
  private nextPid = 1000
  private nextLease = 0

  addEngine(alive = true): FakeEngine {
    const pid = this.nextPid++
    const engine: FakeEngine = {
      info: { pid, port: 40000 + pid, token: `token-${pid}`, version: '0.1.0' },
      alive,
      closing: false,
      leases: new Set()
    }
    this.engines.push(engine)
    this.engineJson = engine.info
    return engine
  }

  byPid(pid: number): FakeEngine | undefined {
    return this.engines.find((e) => e.info.pid === pid)
  }

  /** The engine's process dies, as a SIGKILL would leave it: engine.json stays. */
  kill(engine: FakeEngine): void {
    engine.alive = false
    engine.exit?.(null)
  }

  readEngineJson(): EngineInfo | null {
    return this.engineJson
  }

  pidAlive(pid: number): boolean {
    return this.byPid(pid)?.alive ?? false
  }

  async request(info: EngineInfo, method: string, path: string): Promise<HttpResult> {
    this.requests.push(`${method} ${path} @${info.pid}`)
    const engine = this.byPid(info.pid)
    if (!engine?.alive || engine.info.port !== info.port) throw new Error('ECONNREFUSED')
    if (method === 'GET' && path === '/health') return { status: 200, body: { pid: engine.info.pid, version: '0.1.0' } }
    if (method === 'POST' && path === '/leases') {
      if (engine.closing) return { status: 503, body: null }
      const id = `lease-${this.nextLease++}`
      engine.leases.add(id)
      return { status: 201, body: { lease: id, renew_every_s: 30, expires_after_s: 90 } }
    }
    const id = path.slice('/leases/'.length)
    if (!engine.leases.has(id)) return { status: 404, body: null }
    if (method === 'PUT') return { status: 200, body: { lease: id, renew_every_s: 30, expires_after_s: 90 } }
    engine.leases.delete(id)
    return { status: 204, body: null }
  }

  spawn(): Spawned {
    this.spawns++
    const mode = this.nextSpawn
    let exit!: (code: number | null) => void
    const exited = new Promise<number | null>((resolve) => (exit = resolve))
    let line: EngineLine | null
    if (mode === 'serve') {
      const engine = this.addEngine()
      engine.exit = exit
      line = { pid: engine.info.pid, port: engine.info.port, version: '0.1.0', started: true }
    } else if (mode === 'attach') {
      const live = this.engines.find((e) => e.alive && e.info.pid === this.engineJson?.pid)
      if (!live) throw new Error('nextSpawn is attach, but no engine is live')
      line = { pid: live.info.pid, port: live.info.port, version: '0.1.0', started: false }
      exit(0)
    } else if (mode === 'die') {
      line = null
      exit(1)
    } else {
      line = mode
      exit(1)
    }
    return { line: Promise.resolve(line), exit: exited }
  }

  async sleep(): Promise<void> {}
}

let io: FakeIo
let states: EngineState[]
let supervisor: EngineSupervisor

function last(): EngineState {
  return states[states.length - 1]!
}

/** Lets the supervisor's promise chains run. */
async function settle(): Promise<void> {
  for (let i = 0; i < 20; i++) await Promise.resolve()
}

beforeEach(() => {
  vi.useFakeTimers()
  io = new FakeIo()
  states = []
  supervisor = new EngineSupervisor(io, (s) => states.push(s))
})

afterEach(() => {
  vi.useRealTimers()
})

describe('finding or starting an engine', () => {
  it('attaches to the live engine engine.json names, without spawning', async () => {
    const live = io.addEngine()
    await supervisor.start()
    expect(io.spawns).toBe(0)
    expect(last()).toEqual({
      status: 'running',
      engineUrl: `http://127.0.0.1:${live.info.port}`,
      token: live.info.token,
      version: '0.1.0',
      startedByApp: false
    })
    expect(live.leases.size).toBe(1)
  })

  it('starts an engine when engine.json is missing', async () => {
    await supervisor.start()
    expect(io.spawns).toBe(1)
    expect(states[0]).toEqual({ status: 'starting' })
    expect(last()).toMatchObject({ status: 'running', startedByApp: true })
    expect(io.engines[0]!.leases.size).toBe(1)
  })

  it('starts an engine when engine.json names a dead pid', async () => {
    io.addEngine(false)
    await supervisor.start()
    expect(io.spawns).toBe(1)
    expect(last()).toMatchObject({ status: 'running', startedByApp: true })
  })

  it('treats an engine that answered started: false as attached', async () => {
    // engine.json named no live engine when the app looked, but another client's engine won the race.
    const theirs = io.addEngine()
    io.nextSpawn = 'attach'
    const read = io.readEngineJson.bind(io)
    let reads = 0
    io.readEngineJson = () => (reads++ === 0 ? null : read())
    await supervisor.start()
    expect(io.spawns).toBe(1)
    expect(last()).toMatchObject({ status: 'running', startedByApp: false, token: theirs.info.token })
  })

  it('fails with the engine’s own error', async () => {
    io.nextSpawn = { error: 'RuntimeError: no Postgres binaries' }
    await supervisor.start()
    expect(last()).toEqual({ status: 'failed', error: 'RuntimeError: no Postgres binaries' })
  })

  it('fails when the engine exits without a line', async () => {
    io.nextSpawn = 'die'
    await supervisor.start()
    expect(last()).toMatchObject({ status: 'failed', error: expect.stringContaining('exited with 1') })
  })

  it('starts a new engine when the live one refuses a lease with 503', async () => {
    const closing = io.addEngine()
    closing.closing = true
    await supervisor.start()
    // The fast path found the closing engine; the supervisor then spawns rather than looking again.
    expect(io.spawns).toBe(1)
    expect(last()).toMatchObject({ status: 'running', startedByApp: true })
    expect(io.engines[1]!.leases.size).toBe(1)
  })
})

describe('the lease', () => {
  it('renews every renew_every_s', async () => {
    const live = io.addEngine()
    await supervisor.start()
    const [lease] = [...live.leases]
    await vi.advanceTimersByTimeAsync(30_000)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(io.requests.filter((r) => r.startsWith('PUT'))).toEqual([
      `PUT /leases/${lease} @${live.info.pid}`,
      `PUT /leases/${lease} @${live.info.pid}`
    ])
  })

  it('takes a new lease when a renewal gets 404', async () => {
    const live = io.addEngine()
    await supervisor.start()
    live.leases.clear() // expired, say after the machine slept
    await vi.advanceTimersByTimeAsync(30_000)
    expect(live.leases.size).toBe(1)
    expect(last()).toMatchObject({ status: 'running' })
  })

  it('starts a new engine when the new lease gets 503', async () => {
    const live = io.addEngine()
    await supervisor.start()
    live.leases.clear()
    live.closing = true
    await vi.advanceTimersByTimeAsync(30_000)
    await settle()
    expect(io.spawns).toBe(1)
    expect(last()).toMatchObject({ status: 'running', startedByApp: true, token: io.engines[1]!.info.token })
  })

  it('releases the lease on quit and leaves the engine running', async () => {
    await supervisor.start()
    const engine = io.engines[0]!
    await supervisor.quit()
    expect(engine.leases.size).toBe(0)
    expect(engine.alive).toBe(true)
    // No renewals after quit.
    const before = io.requests.length
    await vi.advanceTimersByTimeAsync(120_000)
    expect(io.requests.length).toBe(before)
  })
})

describe('when the engine dies', () => {
  it('restarts an engine it started', async () => {
    await supervisor.start()
    const first = io.engines[0]!
    io.kill(first)
    await settle()
    expect(states).toContainEqual({ status: 'restarting' })
    expect(io.spawns).toBe(2)
    expect(last()).toMatchObject({ status: 'running', startedByApp: true, token: io.engines[1]!.info.token })
  })

  it('doesn’t restart an engine it attached to', async () => {
    const live = io.addEngine()
    await supervisor.start()
    io.kill(live)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(io.spawns).toBe(0)
    expect(last()).toEqual({ status: 'stopped' })
    // And it stops renewing.
    const before = io.requests.length
    await vi.advanceTimersByTimeAsync(120_000)
    expect(io.requests.length).toBe(before)
  })

  it('keeps renewing an attached engine that missed one request but is alive', async () => {
    const live = io.addEngine()
    await supervisor.start()
    const realRequest = io.request.bind(io)
    let failNext = true
    io.request = async (info, method, path) => {
      if (failNext && method === 'PUT') {
        failNext = false
        throw new Error('ETIMEDOUT')
      }
      return realRequest(info, method, path)
    }
    await vi.advanceTimersByTimeAsync(30_000)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(last()).toMatchObject({ status: 'running' })
    expect(live.leases.size).toBe(1)
  })

  it('doesn’t restart an engine that exits after quit', async () => {
    await supervisor.start()
    await supervisor.quit()
    io.kill(io.engines[0]!)
    await settle()
    expect(io.spawns).toBe(1)
  })

  it('reconnects to an attached engine’s successor by starting one', async () => {
    const live = io.addEngine()
    await supervisor.start()
    io.kill(live)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(last()).toEqual({ status: 'stopped' })

    states = []
    await supervisor.reconnect()
    expect(states[0]).toEqual({ status: 'starting' })
    expect(io.spawns).toBe(1)
    const next = io.engines[1]!
    expect(last()).toMatchObject({ status: 'running', startedByApp: true, token: next.info.token })
    expect(next.leases.size).toBe(1)
    // An engine started by Reconnect is the app's own, so a crash restarts it.
    io.kill(next)
    await settle()
    expect(io.spawns).toBe(2)
  })

  it('reconnects to an engine another client started meanwhile', async () => {
    const live = io.addEngine()
    await supervisor.start()
    io.kill(live)
    await vi.advanceTimersByTimeAsync(30_000)
    const other = io.addEngine()
    await supervisor.reconnect()
    expect(io.spawns).toBe(0)
    expect(last()).toMatchObject({ status: 'running', startedByApp: false, token: other.info.token })
  })

  it('reconnects only from stopped', async () => {
    await supervisor.start()
    states = []
    await supervisor.reconnect()
    expect(states).toEqual([])
    expect(io.spawns).toBe(1)
  })

  it('gives up after three crashes in a minute', async () => {
    await supervisor.start()
    for (let i = 0; i < 3; i++) {
      io.kill(io.engines[i]!)
      await settle()
    }
    expect(io.spawns).toBe(4)
    io.kill(io.engines[3]!)
    await settle()
    expect(io.spawns).toBe(4)
    expect(last()).toMatchObject({ status: 'failed', error: expect.stringContaining('keeps stopping') })
  })
})
