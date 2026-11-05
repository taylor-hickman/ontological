// The window's size, position, open screen and theme, kept between launches (DESIGN.md §3.1).

import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { isScreenId, isThemeSource, type ScreenId, type ThemeSource } from '@shared/shell'

export interface Rect {
  x: number
  y: number
  width: number
  height: number
}

/** A position is optional: without one, Electron centres the window. */
export type WindowBounds = Partial<Pick<Rect, 'x' | 'y'>> & Pick<Rect, 'width' | 'height'>

export interface ShellState {
  bounds: WindowBounds
  maximized: boolean
  screen: ScreenId
  theme: ThemeSource
}

export const MIN_SIZE = { width: 960, height: 640 } as const

export const DEFAULT_STATE: ShellState = {
  bounds: { width: 1280, height: 820 },
  maximized: false,
  screen: 'ask',
  theme: 'system'
}

const isFiniteNumber = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** Reads whatever was on disk, keeping each field that is valid and defaulting the rest. */
export function parseState(raw: unknown): ShellState {
  const obj = typeof raw === 'object' && raw !== null ? (raw as Record<string, unknown>) : {}
  const b = typeof obj.bounds === 'object' && obj.bounds !== null ? (obj.bounds as Record<string, unknown>) : {}
  const bounds: WindowBounds =
    isFiniteNumber(b.width) && isFiniteNumber(b.height)
      ? { width: Math.round(b.width), height: Math.round(b.height) }
      : { ...DEFAULT_STATE.bounds }
  if (isFiniteNumber(b.x) && isFiniteNumber(b.y)) {
    bounds.x = Math.round(b.x)
    bounds.y = Math.round(b.y)
  }
  return {
    bounds,
    maximized: obj.maximized === true,
    screen: isScreenId(obj.screen) ? obj.screen : DEFAULT_STATE.screen,
    theme: isThemeSource(obj.theme) ? obj.theme : DEFAULT_STATE.theme
  }
}

function overlap(a: Rect, b: Rect): number {
  const w = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x)
  const h = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y)
  return w > 0 && h > 0 ? w * h : 0
}

/**
 * Fits saved bounds to the displays there are now. The size is kept at least MIN_SIZE and at most
 * the largest work area. The position is dropped, so the window centres, when a display that held
 * it is gone and too little of the window would still be on screen to grab its top bar.
 */
export function fitBounds(saved: WindowBounds, workAreas: Rect[]): WindowBounds {
  const maxW = Math.max(MIN_SIZE.width, ...workAreas.map((a) => a.width))
  const maxH = Math.max(MIN_SIZE.height, ...workAreas.map((a) => a.height))
  const width = Math.min(Math.max(saved.width, MIN_SIZE.width), maxW)
  const height = Math.min(Math.max(saved.height, MIN_SIZE.height), maxH)
  if (saved.x === undefined || saved.y === undefined) return { width, height }

  const topBar: Rect = { x: saved.x, y: saved.y, width, height: 52 }
  const visible = workAreas.reduce((sum, area) => sum + overlap(topBar, area), 0)
  if (visible < Math.min(width, 200) * 52) return { width, height }
  return { x: saved.x, y: saved.y, width, height }
}

export class StateFile {
  constructor(private readonly path: string) {}

  load(): ShellState {
    try {
      return parseState(JSON.parse(readFileSync(this.path, 'utf8')))
    } catch {
      return parseState(undefined)
    }
  }

  /** Writes through a temporary file, so a crash mid-write leaves the old state rather than half a file. */
  save(state: ShellState): void {
    mkdirSync(dirname(this.path), { recursive: true })
    const tmp = `${this.path}.tmp`
    writeFileSync(tmp, JSON.stringify(state, null, 2))
    renameSync(tmp, this.path)
  }
}
