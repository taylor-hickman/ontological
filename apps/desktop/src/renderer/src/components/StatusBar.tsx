import type { ClaudeStatus } from '@shared/api'
import type { EngineState } from '@shared/engine'
import { useEngineGet } from '../api/engine'
import { useEngine } from '../useEngine'

const ENGINE_LABEL: Record<EngineState['status'], string> = {
  starting: 'Starting engine…',
  running: 'Engine running',
  restarting: 'Engine stopped. Restarting…',
  stopped: 'Engine stopped',
  failed: 'Engine failed to start'
}

/** A filled dot only while the engine runs; status never depends on color alone (DESIGN.md §2.1). */
function EngineDot({ state }: { state: EngineState }) {
  const base = 'size-[7px] rounded-full'
  if (state.status === 'running') return <span aria-hidden="true" className={`${base} bg-verified`} />
  if (state.status === 'starting' || state.status === 'restarting') {
    return <span aria-hidden="true" className={`${base} border border-text-muted motion-safe:animate-pulse`} />
  }
  if (state.status === 'failed') return <span aria-hidden="true" className={`${base} bg-attention`} />
  return <span aria-hidden="true" className={`${base} border border-text-muted`} />
}

/**
 * DESIGN.md §4.6. Always present, 28 px. Left: engine state, then Claude Code. Right: the main
 * background job, then the usage meter. Until the engine reports them, those parts are in their
 * "no data" state.
 */
export function StatusBar() {
  const engine = useEngine()
  const claude = useEngineGet<ClaudeStatus>('/claude')
  return (
    <footer
      data-testid="status-bar"
      className="flex h-7 shrink-0 items-center gap-[18px] overflow-hidden border-t border-border bg-chrome px-3.5 text-xs whitespace-nowrap text-text-2"
    >
      <span
        data-testid="engine-status"
        data-status={engine.status}
        title={engine.status === 'failed' ? engine.error : undefined}
        className="flex items-center gap-1.5"
      >
        <EngineDot state={engine} />
        {ENGINE_LABEL[engine.status]}
      </span>
      <span data-testid="claude-status" data-state={claude.data?.state}>
        {claudeLabel(claude.data)}
      </span>
      <span className="flex-1" />
      <span className="flex items-center gap-2">
        Usage
        <span
          role="meter"
          aria-label="Claude usage"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={0}
          aria-valuetext="No data yet"
          className="block h-1.5 w-[90px] overflow-hidden rounded-[3px] bg-queued"
        />
        <span>no data yet</span>
      </span>
    </footer>
  )
}

/** "Claude Code 2.1.319 · logged in"; ARCHITECTURE.md §6.5's states in plain words. */
function claudeLabel(claude: ClaudeStatus | undefined): string {
  if (claude === undefined) return 'Claude Code not checked'
  if (claude.state === 'not_installed') return 'Claude Code not found'
  const version = `Claude Code ${claude.version}`
  switch (claude.state) {
    case 'ready':
      return `${version} · logged in`
    case 'needs_login':
      return `${version} · needs login`
    case 'outdated':
      return `${version} · needs ${claude.min_version} or later`
    default:
      return `${version} · login not checked`
  }
}
