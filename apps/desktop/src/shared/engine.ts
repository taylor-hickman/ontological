// What the renderer knows about the engine (ARCHITECTURE.md §4.4). Main's engine supervisor owns
// this state and pushes every change to the renderer.

export type EngineState =
  /** Looking for a live engine, or waiting for one to start. */
  | { status: 'starting' }
  /** Serving, with this app's lease. `startedByApp` is false when the app attached to an engine already running. */
  | { status: 'running'; engineUrl: string; token: string; version: string; startedByApp: boolean }
  /** The engine this app started crashed, and the supervisor is starting another. */
  | { status: 'restarting' }
  /** The engine this app attached to went away. The app doesn't restart an engine it didn't start. */
  | { status: 'stopped' }
  /** No engine could be started; `error` says why. */
  | { status: 'failed'; error: string }
