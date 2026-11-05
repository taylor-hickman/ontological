import { useEffect, useState } from 'react'
import type { EngineState } from '@shared/engine'

/** The engine's state from main's supervisor, kept current. */
export function useEngine(): EngineState {
  const [state, setState] = useState<EngineState>({ status: 'starting' })
  useEffect(() => {
    const api = window.ontological
    let live = true
    // Subscribe first: main answers the request before it sends any later change.
    const unsubscribe = api.onEngine(setState)
    void api.engine().then((current) => {
      if (live) setState(current)
    })
    return () => {
      live = false
      unsubscribe()
    }
  }, [])
  return state
}
