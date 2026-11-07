// Argument checks for every IPC handler in main (DESIGN.md §3.4). The renderer is untrusted:
// each parser either returns a clean copy of what it was given or throws.

import type { ContextMenuItem, FileFilter } from '@shared/ipc'

const MAX_FILTERS = 20
const MAX_MENU_ITEMS = 50

function str(value: unknown, what: string, max: number, { allowEmpty = false } = {}): string {
  if (typeof value !== 'string') throw new TypeError(`${what} must be a string`)
  if (!allowEmpty && value.trim() === '') throw new TypeError(`${what} must not be empty`)
  if (value.length > max) throw new TypeError(`${what} must be at most ${max} characters`)
  return value
}

function list(value: unknown, what: string, max: number): unknown[] {
  if (!Array.isArray(value)) throw new TypeError(`${what} must be an array`)
  if (value.length > max) throw new TypeError(`${what} must have at most ${max} entries`)
  return value
}

function record(value: unknown, what: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new TypeError(`${what} must be an object`)
  }
  return value as Record<string, unknown>
}

const EXTENSION = /^(\*|[A-Za-z0-9]{1,16})$/

export function parseFileFilters(value: unknown): FileFilter[] {
  return list(value, 'filters', MAX_FILTERS).map((entry, i) => {
    const f = record(entry, `filters[${i}]`)
    const extensions = list(f.extensions, `filters[${i}].extensions`, MAX_FILTERS).map((ext, j) => {
      const e = str(ext, `filters[${i}].extensions[${j}]`, 16)
      if (!EXTENSION.test(e)) throw new TypeError(`filters[${i}].extensions[${j}] must be letters and digits, or *`)
      return e
    })
    if (extensions.length === 0) throw new TypeError(`filters[${i}].extensions must not be empty`)
    return { name: str(f.name, `filters[${i}].name`, 100), extensions }
  })
}

/** A suggested file name for a save dialog: no directories, so the renderer can't pick where it goes. */
export function parseFileName(value: unknown): string {
  const name = str(value, 'name', 255)
  if (/[/\\]/.test(name) || name === '.' || name === '..') {
    throw new TypeError('name must be a file name, not a path')
  }
  return name
}

/** The most a save writes: an export of every source is a few MB, so this is room to spare. */
export const MAX_SAVE_BYTES = 256 * 1024 * 1024

/** The bytes a save dialog writes. */
export function parseFileData(value: unknown): Uint8Array {
  if (!(value instanceof Uint8Array)) throw new TypeError('data must be bytes')
  if (value.byteLength > MAX_SAVE_BYTES) throw new TypeError(`data must be at most ${MAX_SAVE_BYTES} bytes`)
  return value
}

export function parseContextMenuItems(value: unknown): ContextMenuItem[] {
  const items = list(value, 'items', MAX_MENU_ITEMS).map((entry, i): ContextMenuItem => {
    const item = record(entry, `items[${i}]`)
    if (item.type === 'separator') return { type: 'separator' }
    if (item.type !== undefined && item.type !== 'item') throw new TypeError(`items[${i}].type is not known`)
    if (item.enabled !== undefined && typeof item.enabled !== 'boolean') {
      throw new TypeError(`items[${i}].enabled must be a boolean`)
    }
    return {
      id: str(item.id, `items[${i}].id`, 100),
      label: str(item.label, `items[${i}].label`, 200),
      enabled: item.enabled !== false
    }
  })
  if (items.length === 0) throw new TypeError('items must not be empty')
  return items
}

export function parseNotification(title: unknown, body: unknown): { title: string; body: string } {
  return { title: str(title, 'title', 120), body: str(body, 'body', 500, { allowEmpty: true }) }
}
