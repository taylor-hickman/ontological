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

// The ontology (engine/ontological/curate/ontology.py and service.py, api/curate.py).

export interface TermRef {
  id: string
  name: string
}

/** An association when a table it represents is a junction table. */
export type TermKind = 'entity' | 'association'

export interface TermRow {
  id: string
  name: string
  kind: TermKind
  description: string | null
  synonyms: string[]
  name_certified: boolean
  description_certified: boolean
  /** Name and description both certified. */
  certified: boolean
  tables: string[]
  attributes: number
  sql_attributes: number
  changed_at: string | null
}

export interface Glossary {
  source_id: string
  database_name: string
  terms: TermRow[]
  certified: number
  total: number
}

export interface TermAttribute {
  id: string
  name: string
  column: string
  table: string
  datatype: string | null
  description: string | null
  certified: boolean
  joins_to: TermRef | null
}

export interface TermJoin {
  table: string
  column: string
  to: TermRef
}

export interface SqlAttribute {
  id: string
  name: string
  description: string | null
  expression: string | null
  /** manual, sql (from query history), table, bridgeTable */
  source: string | null
  certified: boolean
  uses: number
}

export type TermField = 'name' | 'description' | 'synonyms' | 'name_certified' | 'description_certified'

export interface TermChange {
  id: number
  edit: number
  ts: string
  field: TermField
  old: unknown
  new: unknown
  actor: 'user' | 'compile' | 'import'
  undoes: number | null
}

export interface TermDetail {
  id: string
  name: string
  kind: TermKind
  description: string | null
  synonyms: string[]
  name_certified: boolean
  description_certified: boolean
  created: string | null
  tables: { id: string; name: string; schema_name: string; database_name: string }[]
  attributes: TermAttribute[]
  joins: TermJoin[]
  sql_attributes: SqlAttribute[]
  /** Newest first. */
  history: TermChange[]
  /** The edit Undo reverts. */
  undoable: number | null
}

export interface TermChanges {
  name?: string
  description?: string
  synonyms?: string[]
  name_certified?: boolean
  description_certified?: boolean
}

export interface Saved {
  edit: number | null
  reembedded: number | null
  term: TermDetail | null
}

export type Layer = 'semantic' | 'data'

export interface GraphNode {
  /** A Term's id on the semantic layer, a table's on the data layer. */
  id: string
  name: string
  certified: boolean
  /** On the data layer, the Terms the table represents. */
  terms: TermRef[]
}

export interface GraphEdge {
  source: string
  target: string
  /** The junction table a semantic edge passes through. */
  via: string | null
  join: string
}

export interface Graph {
  layer: Layer
  nodes: GraphNode[]
  edges: GraphEdge[]
}

export interface PathHop {
  source: string
  target: string
  via: string | null
  join: string
  tables: string[]
}

export interface JoinPath {
  /** In order; empty when nothing joins the two. */
  terms: TermRef[]
  hops: PathHop[]
  tables: string[]
}

// Model import and export (engine/ontological/api/interchange.py, interchange/store.py).

/** `auto_ontology` is the native YAML; `ossie` is Apache Ossie. */
export type ModelFormat = 'auto_ontology' | 'ossie'

export interface ExportRequest {
  /** Database names, a source's `name`; empty exports every one. */
  databases: string[]
  format: ModelFormat
  /** Ossie only: a zip of the semantic model, the ontology and the mapping. */
  include_ontology: boolean
}

/** What an import wrote, by kind: databases, schemas, tables, columns, terms, column_attributes, sql_attributes, custom_analyses. */
export interface ImportSummary {
  created: Record<string, number>
  /** Already in the store, so adopted as they are. */
  skipped: Record<string, number>
  replace: boolean
  /** The format the file was read as. */
  format: ModelFormat
}

export interface Imported {
  summary: ImportSummary
  /** The databases whose re-embed job started. */
  reembedding: string[]
  /** Why none started, such as an engine with no embedding model. */
  reembed_error: string | null
}
