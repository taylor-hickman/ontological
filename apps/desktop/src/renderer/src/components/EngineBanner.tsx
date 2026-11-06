import { useState } from 'react'
import type { EngineState } from '@shared/engine'
import { useEngine } from '../useEngine'
import { RestartIcon, StopIcon } from './icons'

/**
 * DESIGN.md §6, Engine crashed, drawn as a §4.7 banner. An engine this app started is restarted
 * by the supervisor, so the banner only says so. One the app attached to isn't (ARCHITECTURE.md
 * §4.4), so the banner offers Reconnect, which attaches to a live engine or starts one.
 */
export function EngineBanner() {
  const engine = useEngine()
  if (engine.status !== 'restarting' && engine.status !== 'stopped') return null
  return <Banner key={engine.status} status={engine.status} />
}

function Banner({ status }: { status: Extract<EngineState['status'], 'restarting' | 'stopped'> }) {
  const [reconnecting, setReconnecting] = useState(false)
  const restarting = status === 'restarting'

  const reconnect = (): void => {
    setReconnecting(true)
    // The banner goes once the state leaves `stopped`; this only re-enables it if nothing changed.
    void window.ontological.reconnectEngine().finally(() => setReconnecting(false))
  }

  return (
    <div
      role="status"
      data-testid="engine-banner"
      data-status={status}
      className="flex flex-wrap items-center gap-3.5 rounded-xl border border-attention-border bg-attention-soft px-4 py-3.5 text-attention-text"
    >
      <span aria-hidden="true" className="grid size-9 shrink-0 place-items-center rounded-full bg-surface text-attention">
        {restarting ? <RestartIcon /> : <StopIcon />}
      </span>
      <div className="flex flex-[1_1_320px] flex-col gap-0.5">
        <span className="font-semibold">{restarting ? 'The engine stopped. Restarting…' : 'The engine stopped'}</span>
        <span className="text-[13px]">
          {restarting
            ? 'Ontological started it, so it is starting a new one. Screens come back when it’s running.'
            : 'Another app started it, so Ontological doesn’t restart it. Reconnect attaches to a running engine or starts a new one.'}
        </span>
      </div>
      {!restarting && (
        <button
          type="button"
          onClick={reconnect}
          disabled={reconnecting}
          className="min-h-9 rounded-lg border border-attention-border bg-surface px-3 text-attention-text disabled:opacity-60"
        >
          Reconnect
        </button>
      )}
    </div>
  )
}
