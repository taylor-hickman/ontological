// What the engine's /jobs routes return (engine/ontological/jobs/view.py).

export type JobKind = 'compile' | 'ingest'
export type JobState = 'running' | 'paused' | 'held' | 'stopped' | 'done'
export type StepState = 'pending' | 'done' | 'skipped'
export type Outcome = 'ok' | 'invalid' | 'limited' | 'error'

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
}

export interface TableProgress {
  name: string
  state: StepState
  stages_done: number
  skipped: string | null
}

export interface JobDetail {
  job: JobSummary
  /** The route names of a table's stages, in order. */
  stages: string[]
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
