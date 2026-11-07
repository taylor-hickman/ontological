// Small text helpers for counts and sizes (DESIGN.md §8: plain words, tabular figures).

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`
}

export function bytes(n: number | null): string | null {
  if (n === null) return null
  if (n < 1024) return `${n} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = n / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value >= 10 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`
}

/** "~/data/pagila.duckdb" for a path under the home folder. */
export function shortPath(path: string): string {
  const home = /^\/Users\/[^/]+|^\/home\/[^/]+/.exec(path)?.[0]
  return home ? `~${path.slice(home.length)}` : path
}
