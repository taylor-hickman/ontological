import { useState } from 'react'
import type { Glossary, Graph, JoinPath, Layer, Source } from '@shared/api'
import { type Loaded, useEngineGet } from '../api/engine'

export interface PathEnds {
  from: string | null
  to: string | null
}

/** What the Ontology and Terms screens share: the source, the selected Term, the layer, the
 * join path's ends, and the reads an edit refreshes. */
export interface OntologyState {
  source: Source | undefined
  sources: Source[]
  setSource(id: string): void
  glossary: Loaded<Glossary>
  graph: Loaded<Graph>
  path: JoinPath | null
  layer: Layer
  setLayer(layer: Layer): void
  ends: PathEnds
  setEnds(ends: PathEnds): void
  termId: string | null
  selectTerm(id: string | null): void
  /** An edit or undo landed: read the glossary, the graph and the path again. */
  changed(): void
}

export function useOntology(sources: Source[] | undefined, active: 'ontology' | 'terms' | null): OntologyState {
  const [sourceId, setSourceId] = useState<string | null>(null)
  const [layer, setLayer] = useState<Layer>('semantic')
  const [ends, setEndsState] = useState<PathEnds>({ from: null, to: null })
  const [termId, setTermId] = useState<string | null>(null)
  const [key, setKey] = useState(0)
  const list = sources ?? []
  const source = list.find((s) => s.id === sourceId) ?? list[0]
  const base = source && active ? `/sources/${source.id}` : null
  const glossary = useEngineGet<Glossary>(base && `${base}/terms`, key)
  const graph = useEngineGet<Graph>(base && active === 'ontology' ? `${base}/graph?layer=${layer}` : null, key)
  const pathQuery =
    base && active === 'ontology' && ends.from && ends.to
      ? `${base}/path?from_term=${encodeURIComponent(ends.from)}&to_term=${encodeURIComponent(ends.to)}`
      : null
  const path = useEngineGet<JoinPath>(pathQuery, key)
  return {
    source,
    sources: list,
    setSource: (id) => {
      setSourceId(id)
      setTermId(null)
      setEndsState({ from: null, to: null })
    },
    glossary,
    graph,
    path: pathQuery && path.data ? path.data : null,
    layer,
    setLayer,
    ends,
    setEnds: setEndsState,
    termId,
    selectTerm: setTermId,
    changed: () => setKey((n) => n + 1)
  }
}
