// Where the graph's nodes go, and which of them a join path lights up (DESIGN.md §4.9, §5.3).

import { forceCollide, forceLink, forceManyBody, forceSimulation, forceX, forceY, type SimulationNodeDatum } from 'd3-force'
import type { Graph, JoinPath, Layer } from '@shared/api'

/** A graph node is a pill 128 px wide and at least 44 px tall (§4.9). */
export const NODE_WIDTH = 128
export const NODE_HEIGHT = 44
const PAD = 40
/** Room above the graph for the path card (§5.3). */
const PAD_TOP = 84

export interface Placed {
  width: number
  height: number
  at: ReadonlyMap<string, { x: number; y: number }>
}

interface SimNode extends SimulationNodeDatum {
  id: string
}

/**
 * A force layout run to rest, then moved inside the padding, with room above for the path card.
 * Every node starts on a spiral in the order given, and the simulation's random source is fixed,
 * so the same graph always lands the same way.
 */
export function layoutGraph(graph: Pick<Graph, 'nodes' | 'edges'>): Placed {
  const nodes: SimNode[] = graph.nodes.map((n, i) => {
    const angle = i * 2.399963 // the golden angle
    const r = 60 * Math.sqrt(i + 1)
    return { id: n.id, x: r * Math.cos(angle), y: r * Math.sin(angle) }
  })
  const ids = new Set(nodes.map((n) => n.id))
  const links = graph.edges
    .filter((e) => ids.has(e.source) && ids.has(e.target))
    .map((e) => ({ source: e.source, target: e.target }))
  let seed = 1
  const simulation = forceSimulation(nodes)
    .randomSource(() => {
      seed = (seed * 16807) % 2147483647
      return seed / 2147483647
    })
    .force('link', forceLink<SimNode, { source: string; target: string }>(links).id((n) => n.id).distance(170).strength(0.7))
    .force('charge', forceManyBody().strength(-700))
    .force('collide', forceCollide(NODE_WIDTH * 0.62))
    .force('x', forceX(0).strength(0.04))
    .force('y', forceY(0).strength(0.06))
    .stop()
  simulation.tick(300)
  const xs = nodes.map((n) => n.x ?? 0)
  const ys = nodes.map((n) => n.y ?? 0)
  const minX = Math.min(...xs, 0)
  const minY = Math.min(...ys, 0)
  const at = new Map<string, { x: number; y: number }>()
  for (const n of nodes) {
    at.set(n.id, { x: Math.round((n.x ?? 0) - minX + PAD + NODE_WIDTH / 2), y: Math.round((n.y ?? 0) - minY + PAD_TOP + NODE_HEIGHT / 2) })
  }
  const width = nodes.length ? Math.max(...xs) - minX + 2 * PAD + NODE_WIDTH : 2 * PAD
  const height = nodes.length ? Math.max(...ys) - minY + PAD_TOP + PAD + NODE_HEIGHT : PAD_TOP + PAD
  return { width: Math.ceil(width), height: Math.ceil(height), at }
}

/** An edge's key, whichever way round it is given. */
export function edgeKey(a: string, b: string): string {
  return a < b ? `${a}|${b}` : `${b}|${a}`
}

export interface PathOverlay {
  nodes: ReadonlySet<string>
  /** Edge key → the junction table the path passes through on it, or null. */
  edges: ReadonlyMap<string, string | null>
}

export const NO_PATH: PathOverlay = { nodes: new Set(), edges: new Map() }

/**
 * What a join path highlights on a layer: on the semantic layer its Terms and the edges between
 * them, on the data layer its tables and the joins between them, junction tables included.
 */
export function pathOverlay(path: JoinPath | null, layer: Layer): PathOverlay {
  if (!path || path.terms.length === 0) return NO_PATH
  if (layer === 'semantic') {
    return {
      nodes: new Set(path.terms.map((t) => t.id)),
      edges: new Map(path.hops.map((h) => [edgeKey(h.source, h.target), h.via]))
    }
  }
  const edges = new Map<string, string | null>()
  for (const hop of path.hops) {
    for (let i = 0; i + 1 < hop.tables.length; i++) edges.set(edgeKey(hop.tables[i]!, hop.tables[i + 1]!), null)
  }
  return { nodes: new Set(path.tables), edges }
}
