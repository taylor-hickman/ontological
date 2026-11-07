// What the engine's /jobs routes return (engine/ontological/jobs/view.py).

/** `reembed` makes a database's vectors current, after an import or a change of embedding model. */
export type JobKind = 'compile' | 'ingest' | 'reembed'
export type JobState = 'running' | 'paused' | 'held' | 'stopped' | 'done'
export type StepState = 'pending' | 'done' | 'skipped'
export type Outcome = 'ok' | 'invalid' | 'limited' | 'error'
/** How a compile asks about its tables: split's four stages, or one merged call a table. */
export type TableCalls = 'split' | 'merged'

export interface JobSummary {
  id: number
  kind: JobKind
  database_name: string
  state: JobState
  /** ISO time; set while `paused`. */
  paused_until: string | null
  error: string | null
  created_at: string
  finished_at: string | null
  tables: number
  tables_done: number
  /** Spawns of `claude`. */
  calls: number
  cache_hits: number
  /** A refresh's new tables, `schema.table`. */
  new_tables: string[]
  dismissed: boolean
  /** A compile's option for the tables it has left. */
  table_calls: TableCalls
}

export interface TableProgress {
  name: string
  state: StepState
  /** The option the table ran with, or will. */
  table_calls: TableCalls
  /** Of `JobDetail.stages[table_calls]`. */
  stages_done: number
  skipped: string | null
}

export interface JobDetail {
  job: JobSummary
  /** Each option's stages, by route name, in order. */
  stages: Record<TableCalls, string[]>
  tables: TableProgress[]
}

export interface CallRow {
  id: number
  at: string
  task: string
  table_name: string | null
  model: string
  outcome: Outcome
  cached: boolean
  attempt: number
  duration_ms: number | null
}

/** One option's estimate (engine/ontological/compile/estimate.py, ARCHITECTURE.md §6.5). */
export interface OptionEstimate {
  table_calls: TableCalls
  calls: number
  calls_high: number
  calls_by_model: Record<string, number>
  /** Claude usage as its cost at API prices, in USD. */
  usd: number
  usd_low: number
  usd_high: number
  seconds: number
  seconds_low: number
  seconds_high: number
}

export interface Estimate {
  database_name: string
  tables: number
  split: OptionEstimate
  merged: OptionEstimate
  spawn_overhead_s: number
}

/** Settings → Embeddings (`GET /embeddings`): the engine's model, and the databases with vectors from another. */
export interface Embeddings {
  /** null: the engine runs without an embedding model. */
  model: string | null
  stale: string[]
}
