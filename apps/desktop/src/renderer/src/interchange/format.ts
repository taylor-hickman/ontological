// What Settings → Import/Export shows, as plain functions (engine/ontological/api/interchange.py).

import type { ImportSummary, ModelFormat } from '@shared/api'
import type { JobSummary } from '../jobs/types'

/**
 * The name the save dialog suggests: "chinook-model.yaml", "chinook-ossie.yaml" or
 * "chinook-ossie.zip", and "ontological-model.yaml" or "ossie-model.yaml" for every source.
 */
export function exportFileName(database: string | null, format: ModelFormat, withOntology: boolean): string {
  const name = database?.replace(/[/\\:*?"<>|]/g, '-').replace(/^\.+/, '') || null
  if (format === 'auto_ontology') return `${name ?? 'ontological'}-model.yaml`
  return `${name === null ? 'ossie-model' : `${name}-ossie`}.${withOntology ? 'zip' : 'yaml'}`
}

/** The kinds an import counts, in the order the document nests them. */
const KINDS: readonly [string, string][] = [
  ['databases', 'Databases'],
  ['schemas', 'Schemas'],
  ['tables', 'Tables'],
  ['columns', 'Columns'],
  ['terms', 'Terms'],
  ['column_attributes', 'Column attributes'],
  ['sql_attributes', 'SQL attributes'],
  ['custom_analyses', 'Custom analyses']
]

export interface SummaryRow {
  label: string
  created: number
  /** Already in the store, so adopted as they are. */
  existing: number
}

/** A row for each kind the file held any of. */
export function summaryRows(summary: ImportSummary): SummaryRow[] {
  return KINDS.map(([key, label]) => ({
    label,
    created: summary.created[key] ?? 0,
    existing: summary.skipped[key] ?? 0
  })).filter((row) => row.created > 0 || row.existing > 0)
}

export const FORMAT_NAMES: Record<ModelFormat, string> = {
  auto_ontology: 'Ontological YAML',
  ossie: 'Apache Ossie YAML'
}

/** The highest job id now, so the jobs an import starts are the ones after it. */
export function lastJobId(jobs: readonly JobSummary[]): number {
  return jobs.reduce((max, j) => Math.max(max, j.id), 0)
}

/**
 * For each database an import re-embeds, its re-embed job: the newest one started after `afterId`,
 * or null until the jobs list shows it.
 */
export function startedReembeds(
  jobs: readonly JobSummary[],
  databases: readonly string[],
  afterId: number
): { database: string; job: JobSummary | null }[] {
  return databases.map((database) => ({
    database,
    job:
      jobs
        .filter((j) => j.kind === 'reembed' && j.database_name === database && j.id > afterId)
        .reduce<JobSummary | null>((newest, j) => (newest === null || j.id > newest.id ? j : newest), null)
  }))
}
