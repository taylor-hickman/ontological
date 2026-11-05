import type { ScreenId } from '@shared/shell'

export interface ScreenFrame {
  title: string
  /** The main column's accessible name, as the mockups give it. */
  main: string
  /** What fills the main column once the engine exists. */
  placeholder: string
  /** The right rail's accessible name, or null for a screen without one (DESIGN.md §5). */
  rail: string | null
}

export const FRAMES: Record<ScreenId, ScreenFrame> = {
  ask: {
    title: 'Ask',
    main: 'Conversation',
    placeholder: 'Questions and their checked answers appear here.',
    rail: 'Provenance'
  },
  ontology: {
    title: 'Ontology',
    main: 'Ontology graph',
    placeholder: 'The graph of terms and how they join appears here.',
    rail: 'Term editor'
  },
  terms: { title: 'Terms', main: 'Glossary', placeholder: 'The glossary of terms appears here.', rail: 'Term editor' },
  catalog: {
    title: 'Catalog',
    main: 'Catalog',
    placeholder: 'Sources, schemas and tables appear here.',
    rail: 'Table detail'
  },
  analyses: {
    title: 'Analyses',
    main: 'Saved analyses',
    placeholder: 'Saved questions and their SQL appear here.',
    rail: 'Analysis detail'
  },
  jobs: {
    title: 'Jobs',
    main: 'Current job',
    placeholder: 'Ingest and compile runs appear here.',
    rail: 'Call log and history'
  },
  settings: { title: 'Settings', main: 'Settings', placeholder: '', rail: null }
}
