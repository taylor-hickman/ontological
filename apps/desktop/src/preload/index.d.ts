import type { OntologicalApi } from '@shared/ipc'

declare global {
  interface Window {
    ontological: OntologicalApi
  }
}

export {}
