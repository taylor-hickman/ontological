// The engine routes the renderer reads (engine/ontological/api/setup.py). The engine's pydantic
// models are the source of truth; these mirror them.

/** DESIGN.md §5.1 step 1. `needs_login` means: open a terminal, run `claude`, type `/login`. */
export type ClaudeState = 'ready' | 'not_installed' | 'outdated' | 'needs_login' | 'unknown'

export interface ClaudeStatus {
  state: ClaudeState
  cli_path: string | null
  version: string | null
  min_version: string
  logged_in: boolean | null
  /** Auth variables set in the shell, which every call blanks (ARCHITECTURE.md §3 rule 3). */
  ignored_variables: string[]
}

export interface PingResult {
  ok: boolean
  seconds: number | null
  state: string
  error: string | null
}

export type SourceKind = 'sqlite' | 'duckdb' | 'files' | 'postgres' | 'mysql' | 'unknown'

export interface Source {
  id: string
  name: string
  kind: SourceKind
  label: string
  size_bytes: number | null
  schemas: number
  tables: number
  columns: number
  pulled: string | null
}

export interface Added {
  source: Source
  seconds: number
  skipped: number
}

export type AddSourceRequest =
  | { kind: 'file'; paths: string[] }
  | { kind: 'postgres' | 'mysql'; host: string; port: number | null; database: string; user: string; password: string | null }

export interface CatalogTable {
  id: string
  name: string
  table_type: string | null
  columns: number
  pk: string[]
  description: string | null
}

export interface CatalogSchema {
  id: string
  name: string
  tables: CatalogTable[]
}

export interface SourceCatalog {
  source: Source
  schemas: CatalogSchema[]
}

export interface CatalogColumn {
  id: string
  name: string
  data_type: string | null
  is_nullable: boolean | null
  is_unique: boolean | null
  format: string | null
  sample_values: string[]
  description: string | null
  pk: boolean
  references: string[]
  tags: string[]
  pii: boolean
}

export interface TableDetail {
  id: string
  source_id: string
  source: string
  schema_name: string
  name: string
  table_type: string | null
  description: string | null
  columns: CatalogColumn[]
  tags: string[]
}
