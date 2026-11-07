import { useMemo, useRef, useState } from 'react'
import type { GraphEdge, GraphNode, Layer } from '@shared/api'
import { CheckIcon, CloseIcon, PlusIcon } from '../components/icons'
import { shortcut } from '../shortcut'
import { pathSummary } from './format'
import { edgeKey, layoutGraph, NODE_HEIGHT, NODE_WIDTH, pathOverlay, type PathOverlay } from './layout'
import type { OntologyState } from './useOntology'

const ZOOMS = [0.5, 0.67, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 2]

/** The header's tools on Ontology (DESIGN.md §5.3): the source when there are several, the
 * Semantic / Data layer switch and the path picker. */
export function OntologyTools({ state }: { state: OntologyState }) {
  const terms = state.glossary.data?.terms ?? []
  const layers: { id: Layer; label: string }[] = [
    { id: 'semantic', label: 'Semantic' },
    { id: 'data', label: 'Data' }
  ]
  const move = (e: React.KeyboardEvent) => {
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(e.key)) return
    e.preventDefault()
    const next = state.layer === 'semantic' ? 'data' : 'semantic'
    state.setLayer(next)
    ;(e.currentTarget.querySelector(`[data-layer="${next}"]`) as HTMLElement | null)?.focus()
  }
  return (
    <>
      <SourcePicker state={state} />
      <div role="radiogroup" aria-label="Layer" onKeyDown={move} className="no-drag flex rounded-lg bg-queued p-[3px]">
        {layers.map((l) => (
          <button
            key={l.id}
            type="button"
            role="radio"
            data-layer={l.id}
            aria-checked={state.layer === l.id}
            tabIndex={state.layer === l.id ? 0 : -1}
            onClick={() => state.setLayer(l.id)}
            className={`min-h-7 rounded-md px-3 ${
              state.layer === l.id ? 'bg-surface font-medium text-text shadow-[0_1px_2px_rgba(20,24,33,0.08)]' : 'text-text-2'
            }`}
          >
            {l.label}
          </button>
        ))}
      </div>
      <div className="no-drag flex min-h-8 items-center gap-1.5 rounded-lg border border-border bg-surface pr-1.5 pl-2.5" data-testid="path-picker">
        <span className="text-xs text-text-muted">Path</span>
        <TermSelect label="Path from" value={state.ends.from} terms={terms} onChange={(from) => state.setEnds({ ...state.ends, from })} />
        <span className="text-text-muted" aria-hidden="true">
          →
        </span>
        <TermSelect label="Path to" value={state.ends.to} terms={terms} onChange={(to) => state.setEnds({ ...state.ends, to })} />
        <button
          type="button"
          aria-label="Clear path"
          onClick={() => state.setEnds({ from: null, to: null })}
          disabled={!state.ends.from && !state.ends.to}
          className="flex size-[26px] items-center justify-center rounded-md text-text-muted hover:text-text disabled:opacity-40"
        >
          <CloseIcon />
        </button>
      </div>
    </>
  )
}

export function SourcePicker({ state }: { state: OntologyState }) {
  if (state.sources.length < 2) return null
  return (
    <label className="no-drag flex min-h-8 items-center gap-1.5 rounded-lg border border-border bg-surface px-2.5">
      <span className="text-xs text-text-muted">Source</span>
      <select
        value={state.source?.id ?? ''}
        onChange={(e) => state.setSource(e.target.value)}
        className="bg-transparent text-[13px] font-medium text-text outline-none"
      >
        {state.sources.map((s) => (
          <option key={s.id} value={s.id}>
            {s.name}
          </option>
        ))}
      </select>
    </label>
  )
}

function TermSelect({
  label,
  value,
  terms,
  onChange
}: {
  label: string
  value: string | null
  terms: { id: string; name: string }[]
  onChange(id: string | null): void
}) {
  return (
    <select
      aria-label={label}
      value={value ?? ''}
      onChange={(e) => onChange(e.target.value || null)}
      className={`max-w-[150px] rounded-full px-2 py-px text-[13px] font-medium outline-none ${
        value ? 'bg-accent-soft text-accent-hover' : 'bg-transparent text-text-muted'
      }`}
    >
      <option value="">Pick a term</option>
      {terms.map((t) => (
        <option key={t.id} value={t.id}>
          {t.name}
        </option>
      ))}
    </select>
  )
}

/** The certification meter in the sidebar on Ontology (DESIGN.md §3.3). */
export function CertificationMeter({ certified, total }: { certified: number; total: number }) {
  return (
    <div className="flex flex-col gap-1.5" data-testid="certification-meter">
      <span className="px-2.5 pt-[22px] text-[11px] font-semibold tracking-[0.06em] text-text-muted uppercase">Certification</span>
      <div className="flex flex-col gap-1.5 px-2.5">
        <span
          role="meter"
          aria-label="Terms certified"
          aria-valuenow={certified}
          aria-valuemin={0}
          aria-valuemax={total}
          className="block h-1.5 overflow-hidden rounded-[3px] bg-queued"
        >
          <span className="block h-1.5 bg-verified" style={{ width: total ? `${(100 * certified) / total}%` : 0 }} />
        </span>
        <span className="text-xs text-text-2">
          {certified} of {total} terms certified
        </span>
      </div>
    </div>
  )
}

interface OntologyMainProps {
  state: OntologyState
  onAddSource(): void
  onOpenJobs(): void
}

/** The graph (DESIGN.md §4.9, §5.3), or its list fallback. */
export function OntologyMain({ state, onAddSource, onOpenJobs }: OntologyMainProps) {
  const [view, setView] = useState<'graph' | 'list'>('graph')
  const graph = state.graph.data?.layer === state.layer ? state.graph.data : undefined

  if (state.sources.length === 0) return <NoSources onAddSource={onAddSource} />
  if (state.graph.error) return <p className="m-0 p-9 text-text">Couldn’t read the ontology: {state.graph.error}</p>
  if (!graph) return <div className="m-9 h-80 rounded-xl bg-surface-sunken" aria-label="Loading the graph" data-testid="graph-loading" />
  if (graph.nodes.length === 0) {
    return (
      <div className="m-9 flex flex-col items-start gap-2 rounded-xl border border-dashed border-text-faint px-6 py-8">
        <p className="m-0 font-medium">No terms yet</p>
        <p className="m-0 text-[13px] text-text-muted">
          Compile {state.source?.name ?? 'the source'} to write its terms. Compiling uses Claude quota.
        </p>
        <button type="button" onClick={onOpenJobs} className="min-h-10 text-accent hover:text-accent-hover">
          Open Jobs
        </button>
      </div>
    )
  }
  const overlay = pathOverlay(state.path, state.layer)
  const selected = (node: GraphNode) =>
    state.layer === 'semantic' ? node.id === state.termId : node.terms.some((t) => t.id === state.termId)
  const select = (node: GraphNode, shift: boolean) => {
    const term = state.layer === 'semantic' ? node.id : node.terms[0]?.id
    if (!term) return
    if (shift) {
      // Shift-click sets the path's ends: the first, then the second.
      state.setEnds(state.ends.from && !state.ends.to ? { ...state.ends, to: term } : { from: term, to: null })
      return
    }
    state.selectTerm(term)
  }

  return (
    <div className="relative flex min-h-[520px] flex-1 flex-col">
      {view === 'graph' ? (
        <Canvas nodes={graph.nodes} edges={graph.edges} overlay={overlay} selected={selected} onSelect={select} />
      ) : (
        <NodeList nodes={graph.nodes} edges={graph.edges} overlay={overlay} selected={selected} onSelect={select} layer={state.layer} />
      )}
      {state.path && (
        <PathCard state={state} />
      )}
      <div className="pointer-events-none absolute inset-x-5 bottom-5 flex flex-wrap items-end justify-between gap-3">
        {view === 'graph' ? <Legend /> : <span />}
        <button
          type="button"
          onClick={() => setView(view === 'graph' ? 'list' : 'graph')}
          className="pointer-events-auto min-h-9 rounded-[10px] border border-border bg-surface px-3 text-[13px] text-text-2"
        >
          {view === 'graph' ? 'Show as list' : 'Show as graph'}
        </button>
      </div>
    </div>
  )
}

function PathCard({ state }: { state: OntologyState }) {
  const path = state.path!
  if (path.terms.length === 0) {
    return (
      <div role="status" className="absolute top-4 left-5 rounded-[10px] border border-border bg-surface px-3 py-2 text-[13px] shadow-[0_2px_8px_rgba(20,24,33,0.05)]">
        No join path connects these terms.
      </div>
    )
  }
  const sum = pathSummary(path)
  return (
    <div
      role="status"
      data-testid="path-summary"
      className="absolute top-4 left-5 flex max-w-[calc(100%-40px)] flex-wrap items-center gap-2 rounded-[10px] border border-border bg-surface px-3 py-2 text-[13px] shadow-[0_2px_8px_rgba(20,24,33,0.05)]"
    >
      <span className="font-semibold">{sum.hops}</span>
      <span className="text-text-2">{sum.chain}</span>
      {sum.via && <span className="text-text-muted">{sum.via}</span>}
    </div>
  )
}

interface NodesProps {
  nodes: GraphNode[]
  edges: GraphEdge[]
  overlay: PathOverlay
  selected(node: GraphNode): boolean
  onSelect(node: GraphNode, shift: boolean): void
}

function Canvas({ nodes, edges, overlay, selected, onSelect }: NodesProps) {
  const placed = useMemo(() => layoutGraph({ nodes, edges }), [nodes, edges])
  const [zoom, setZoom] = useState(1)
  const scroller = useRef<HTMLDivElement>(null)
  const step = (dir: 1 | -1) => {
    const i = ZOOMS.findIndex((z) => z >= zoom - 1e-6)
    setZoom(ZOOMS[Math.min(ZOOMS.length - 1, Math.max(0, (i === -1 ? ZOOMS.length - 1 : i) + dir))]!)
  }
  const fit = () => {
    const box = scroller.current
    if (!box) return
    setZoom(Math.max(0.3, Math.min(2, box.clientWidth / placed.width, box.clientHeight / placed.height)))
  }
  const at = (id: string) => placed.at.get(id)
  const plain = edges.filter((e) => !overlay.edges.has(edgeKey(e.source, e.target)))
  const onPath = edges.filter((e) => overlay.edges.has(edgeKey(e.source, e.target)))

  return (
    <>
      <div
        ref={scroller}
        data-testid="graph"
        className="graph-dots min-h-0 flex-1 overflow-auto"
        style={{ backgroundSize: `${20 * zoom}px ${20 * zoom}px` }}
      >
        <div style={{ width: placed.width * zoom, height: placed.height * zoom, margin: '0 auto' }}>
          <div className="relative origin-top-left" style={{ width: placed.width, height: placed.height, transform: `scale(${zoom})` }}>
            <svg width={placed.width} height={placed.height} className="absolute inset-0" aria-hidden="true">
              <g className="stroke-border" strokeWidth={1.5} fill="none">
                {plain.map((e) => {
                  const a = at(e.source)
                  const b = at(e.target)
                  if (!a || !b) return null
                  return (
                    <line key={edgeKey(e.source, e.target)} x1={a.x} y1={a.y} x2={b.x} y2={b.y} strokeDasharray={e.via ? '5 5' : undefined}>
                      <title>{e.via ? `via ${e.via}: ${e.join}` : e.join}</title>
                    </line>
                  )
                })}
              </g>
              <g className="stroke-accent" strokeWidth={3} fill="none" strokeLinecap="round">
                {onPath.map((e) => {
                  const a = at(e.source)
                  const b = at(e.target)
                  if (!a || !b) return null
                  const via = overlay.edges.get(edgeKey(e.source, e.target))
                  return (
                    <line key={edgeKey(e.source, e.target)} data-path-edge x1={a.x} y1={a.y} x2={b.x} y2={b.y} strokeDasharray={via ? '7 6' : undefined}>
                      <title>{e.join}</title>
                    </line>
                  )
                })}
              </g>
            </svg>
            {onPath.map((e) => {
              const via = overlay.edges.get(edgeKey(e.source, e.target))
              const a = at(e.source)
              const b = at(e.target)
              if (!via || !a || !b) return null
              return (
                <span
                  key={`via-${edgeKey(e.source, e.target)}`}
                  className="absolute -translate-x-1/2 -translate-y-1/2 bg-bg px-1 font-mono text-[11.5px] text-accent-hover"
                  style={{ left: (a.x + b.x) / 2, top: (a.y + b.y) / 2 }}
                >
                  {via}
                </span>
              )
            })}
            {nodes.map((n) => {
              const p = at(n.id)
              if (!p) return null
              return (
                <NodeButton
                  key={n.id}
                  node={n}
                  onPath={overlay.nodes.has(n.id)}
                  selected={selected(n)}
                  onSelect={onSelect}
                  style={{ left: p.x - NODE_WIDTH / 2, top: p.y - NODE_HEIGHT / 2 }}
                />
              )
            })}
          </div>
        </div>
      </div>
      <div className="absolute right-5 bottom-[68px] flex items-center overflow-hidden rounded-[10px] border border-border bg-surface">
        <button type="button" aria-label="Zoom out" onClick={() => step(-1)} className="size-9 text-base text-text-2">
          −
        </button>
        <span className="px-1.5 text-xs text-text-2 tabular-nums" aria-live="polite">
          {Math.round(zoom * 100)}%
        </span>
        <button type="button" aria-label="Zoom in" onClick={() => step(1)} className="size-9 text-base text-text-2">
          <span className="inline-flex">
            <PlusIcon />
          </span>
        </button>
        <button type="button" onClick={fit} className="h-9 border-l border-border px-3 text-[13px] text-text-2">
          Fit
        </button>
      </div>
    </>
  )
}

function NodeButton({
  node,
  onPath,
  selected,
  onSelect,
  style
}: {
  node: GraphNode
  onPath: boolean
  selected: boolean
  onSelect(node: GraphNode, shift: boolean): void
  style: React.CSSProperties
}) {
  const look = selected
    ? 'border-2 border-accent-solid bg-accent-solid font-semibold text-white shadow-[0_0_0_6px_color-mix(in_srgb,var(--accent)_22%,transparent)]'
    : onPath
      ? `border-2 border-accent bg-surface font-semibold text-text ${node.certified ? '' : 'border-dashed'}`
      : node.certified
        ? 'border border-text-faint bg-surface text-text-2'
        : 'border border-dashed border-text-faint bg-surface text-text-muted'
  const state = [node.certified ? 'certified' : 'not reviewed', onPath ? 'on the path' : null].filter(Boolean).join(', ')
  return (
    <button
      type="button"
      data-testid="graph-node"
      data-on-path={onPath || undefined}
      aria-pressed={selected}
      aria-label={`${node.name}, ${state}`}
      title={node.terms.length ? `Represents ${node.terms.map((t) => t.name).join(', ')}` : undefined}
      onClick={(e) => onSelect(node, e.shiftKey)}
      className={`absolute flex min-h-11 items-center justify-center gap-1.5 rounded-full px-3 text-[13px] ${look}`}
      style={{ ...style, width: NODE_WIDTH }}
    >
      {node.certified && (
        <span className={selected ? 'text-white' : 'text-verified'}>
          <CheckIcon size={13} />
        </span>
      )}
      <span className="truncate">{node.name}</span>
    </button>
  )
}

function Legend() {
  return (
    <div className="pointer-events-auto flex flex-wrap gap-3.5 rounded-[10px] border border-border bg-surface px-3 py-2 text-xs text-text-2">
      <span className="flex items-center gap-1.5">
        <span aria-hidden="true" className="h-[3px] w-[18px] rounded-sm bg-accent" />
        On path
      </span>
      <span className="flex items-center gap-1.5">
        <span aria-hidden="true" className="h-2.5 w-3.5 rounded-md border-[1.5px] border-text-2" />
        Certified
      </span>
      <span className="flex items-center gap-1.5">
        <span aria-hidden="true" className="h-2.5 w-3.5 rounded-md border-[1.5px] border-dashed border-text-muted" />
        Not yet reviewed
      </span>
      <span className="flex items-center gap-1.5">
        <span aria-hidden="true" className="h-0 w-[18px] border-t-2 border-dashed border-accent" />
        Through a junction
      </span>
      <span className="text-text-muted">Shift-click two to see the path</span>
    </div>
  )
}

/** The graph's list fallback, reachable by keyboard (§5.3, §9). */
function NodeList({ nodes, edges, overlay, selected, onSelect, layer }: NodesProps & { layer: Layer }) {
  const names = new Map(nodes.map((n) => [n.id, n.name]))
  const neighbours = (id: string) =>
    edges
      .flatMap((e) => (e.source === id ? [e] : e.target === id ? [{ ...e, target: e.source }] : []))
      .map((e) => (e.via ? `${names.get(e.target)} (via ${e.via})` : names.get(e.target)))
      .filter(Boolean)
      .join(', ')
  return (
    <div className="min-h-0 flex-1 overflow-auto px-9 pt-20 pb-20">
      <table className="w-full border-collapse text-[13px]" data-testid="graph-list">
        <thead>
          <tr className="text-left text-xs text-text-muted">
            <th className="py-2 pr-3 font-semibold">{layer === 'semantic' ? 'Term' : 'Table'}</th>
            <th className="py-2 pr-3 font-semibold">Status</th>
            <th className="py-2 pr-3 font-semibold">Joins</th>
          </tr>
        </thead>
        <tbody>
          {nodes.map((n) => (
            <tr key={n.id} className={`border-t border-border-subtle ${selected(n) ? 'bg-accent-soft' : ''}`}>
              <td className="py-1 pr-3">
                <button
                  type="button"
                  onClick={(e) => onSelect(n, e.shiftKey)}
                  aria-pressed={selected(n)}
                  className={`min-h-11 text-left ${layer === 'data' ? 'font-mono' : ''} ${overlay.nodes.has(n.id) ? 'font-semibold text-accent' : 'text-text'}`}
                >
                  {n.name}
                  {overlay.nodes.has(n.id) && <span className="sr-only"> (on the path)</span>}
                </button>
              </td>
              <td className="py-1 pr-3 text-text-2">{n.certified ? 'Certified' : 'Not reviewed'}</td>
              <td className="py-1 pr-3 text-text-2">{neighbours(n.id) || 'None'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function NoSources({ onAddSource }: { onAddSource(): void }) {
  return (
    <div className="m-9 flex flex-col items-start gap-3 rounded-xl border border-dashed border-text-faint px-6 py-8" data-testid="ontology-empty">
      <p className="m-0 font-medium">No sources yet</p>
      <p className="m-0 text-[13px] text-text-muted">Add a source and compile it to see its terms.</p>
      <button
        type="button"
        onClick={onAddSource}
        className="flex min-h-10 items-center gap-2 rounded-lg bg-accent-solid px-4 font-medium text-white"
      >
        <PlusIcon />
        Add a source
        <kbd className="font-sans text-[11px] opacity-80">{shortcut('O')}</kbd>
      </button>
    </div>
  )
}
