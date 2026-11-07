import { join, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { appProtocolHandler, resolveAppFile } from '../../src/main/app-protocol'
import { contentSecurityPolicy } from '../../src/main/csp'
import { engineCommand } from '../../src/main/engine/command'

const root = resolve('/app/out/renderer')

describe('resolveAppFile', () => {
  it('maps app://ontological/ paths into the renderer folder', () => {
    expect(resolveAppFile(root, 'app://ontological/index.html')).toBe(join(root, 'index.html'))
    expect(resolveAppFile(root, 'app://ontological/')).toBe(join(root, 'index.html'))
    expect(resolveAppFile(root, 'app://ontological/assets/a%20b.js?x=1')).toBe(join(root, 'assets/a b.js'))
  })

  it('stays inside the folder when the URL parser collapses dot segments', () => {
    expect(resolveAppFile(root, 'app://ontological/../main/index.js')).toBe(join(root, 'main/index.js'))
    expect(resolveAppFile(root, 'app://ontological/%2e%2e/main/index.js')).toBe(join(root, 'main/index.js'))
  })

  it.each([
    'app://ontological/assets/..%2F..%2Fmain%2Findex.js',
    'app://ontological/..%2Findex.html',
    'app://other/index.html',
    'file:///app/out/renderer/index.html',
    'app://ontological/%E0%A4%A',
    'not a url'
  ])('refuses %s', (url) => {
    expect(resolveAppFile(root, url)).toBeNull()
  })
})

describe('appProtocolHandler', () => {
  const handler = appProtocolHandler({
    root,
    fetchFile: async (url) => new Response(`body of ${url}`, { headers: { 'Content-Type': 'text/plain' } }),
    policy: async () => contentSecurityPolicy(5123)
  })

  it('sends HTML with the CSP header', async () => {
    const res = await handler(new Request('app://ontological/index.html'))
    expect(res.headers.get('Content-Security-Policy')).toContain('connect-src \'self\' http://127.0.0.1:5123')
    expect(res.headers.get('Content-Type')).toBe('text/plain')
    expect(await res.text()).toContain('index.html')
  })

  it('sends other files as they are', async () => {
    const res = await handler(new Request('app://ontological/assets/app.js'))
    expect(res.headers.get('Content-Security-Policy')).toBeNull()
  })

  it('answers 404 outside the renderer folder', async () => {
    const res = await handler(new Request('app://ontological/assets/..%2F..%2Fmain%2Findex.js'))
    expect(res.status).toBe(404)
  })
})

describe('contentSecurityPolicy', () => {
  it('allows only the app itself until the engine’s port is known', () => {
    const csp = contentSecurityPolicy(null)
    expect(csp).toContain("connect-src 'self';")
    expect(csp).toContain("script-src 'self';")
    expect(csp).not.toContain('127.0.0.1')
  })

  it('adds the engine’s origin to connect-src and nowhere else', () => {
    const csp = contentSecurityPolicy(5123)
    expect(csp.match(/127\.0\.0\.1/g)).toHaveLength(1)
    expect(csp).toContain("connect-src 'self' http://127.0.0.1:5123;")
  })
})

describe('engineCommand', () => {
  it('runs the repo’s engine through uv when the app runs from the repo', () => {
    expect(engineCommand({ env: {}, isPackaged: false, appPath: '/repo/apps/desktop', resourcesPath: '/r' })).toEqual([
      'uv',
      'run',
      '--quiet',
      '--project',
      resolve('/repo/engine'),
      'ontological-engine'
    ])
  })

  it('takes ONTOLOGICAL_ENGINE_CMD as a JSON array', () => {
    const env = { ONTOLOGICAL_ENGINE_CMD: '["python", "stub.py", "--idle-timeout", "2"]' }
    expect(engineCommand({ env, isPackaged: false, appPath: '/x', resourcesPath: '/r' })).toEqual([
      'python',
      'stub.py',
      '--idle-timeout',
      '2'
    ])
  })

  it('points a packaged app’s engine at the folders it bundles', () => {
    const env = { ONTOLOGICAL_ENGINE_CMD: '["engine"]' }
    const resources = '/Applications/Ontological.app/Contents/Resources'
    expect(engineCommand({ env, isPackaged: true, appPath: '/x', resourcesPath: resources })).toEqual([
      'engine',
      '--pg-bin',
      join(resources, 'postgres', 'bin'),
      '--model-dir',
      join(resources, 'model'),
      '--duckdb-extensions',
      join(resources, 'duckdb-extensions')
    ])
  })

  it.each(['python stub.py', '[]', '[1]', '{}'])('refuses ONTOLOGICAL_ENGINE_CMD=%s', (value) => {
    expect(() => engineCommand({ env: { ONTOLOGICAL_ENGINE_CMD: value }, isPackaged: false, appPath: '/x', resourcesPath: '/r' })).toThrow(
      'JSON array'
    )
  })

  it('says a packaged build has no engine yet', () => {
    expect(() => engineCommand({ env: {}, isPackaged: true, appPath: '/x', resourcesPath: '/r' })).toThrow('M7.1')
  })
})
