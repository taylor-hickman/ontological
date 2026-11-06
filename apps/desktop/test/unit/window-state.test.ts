import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { DEFAULT_STATE, MIN_SIZE, StateFile, fitBounds, parseState } from '../../src/main/window-state'

const display = { x: 0, y: 0, width: 1728, height: 1079 }

describe('parseState', () => {
  it('defaults everything for missing or broken input', () => {
    expect(parseState(undefined)).toEqual(DEFAULT_STATE)
    expect(parseState('nonsense')).toEqual(DEFAULT_STATE)
  })

  it('keeps valid fields and drops invalid ones', () => {
    expect(
      parseState({
        bounds: { x: 10, y: 20, width: 1000.4, height: 700 },
        maximized: true,
        screen: 'jobs',
        theme: 'dark',
        setupClosed: true
      })
    ).toEqual({ bounds: { x: 10, y: 20, width: 1000, height: 700 }, maximized: true, screen: 'jobs', theme: 'dark', setupClosed: true })
    expect(parseState({ bounds: { width: 'wide' }, screen: 'admin', theme: 'sepia', setupClosed: 'yes' })).toEqual(DEFAULT_STATE)
  })

  it('accepts Settings as the open screen', () => {
    expect(parseState({ screen: 'settings' }).screen).toBe('settings')
  })
})

describe('fitBounds', () => {
  it('keeps bounds that are on a display', () => {
    expect(fitBounds({ x: 100, y: 50, width: 1200, height: 800 }, [display])).toEqual({ x: 100, y: 50, width: 1200, height: 800 })
  })

  it('raises the size to the minimum', () => {
    expect(fitBounds({ width: 400, height: 300 }, [display])).toEqual(MIN_SIZE)
  })

  it('shrinks a window bigger than any display', () => {
    expect(fitBounds({ width: 4000, height: 3000 }, [display])).toEqual({ width: 1728, height: 1079 })
  })

  it('drops the position when the display that held the window is gone', () => {
    expect(fitBounds({ x: 3000, y: 200, width: 1200, height: 800 }, [display])).toEqual({ width: 1200, height: 800 })
  })

  it('keeps a position on a second display', () => {
    const second = { x: 1728, y: 0, width: 2560, height: 1415 }
    expect(fitBounds({ x: 2000, y: 100, width: 1200, height: 800 }, [display, second])).toEqual({
      x: 2000,
      y: 100,
      width: 1200,
      height: 800
    })
  })
})

describe('StateFile', () => {
  it('round-trips the state and survives a corrupt file', () => {
    const dir = mkdtempSync(join(tmpdir(), 'ontological-state-'))
    const file = new StateFile(join(dir, 'nested', 'shell-state.json'))
    expect(file.load()).toEqual(DEFAULT_STATE)
    const state = { ...DEFAULT_STATE, screen: 'catalog' as const, theme: 'light' as const }
    file.save(state)
    expect(file.load()).toEqual(state)
    writeFileSync(join(dir, 'nested', 'shell-state.json'), '{"bounds":')
    expect(file.load()).toEqual(DEFAULT_STATE)
  })
})
