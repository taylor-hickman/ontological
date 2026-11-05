// The only bridge between the renderer and main (DESIGN.md §3.4). The renderer gets these
// functions and nothing else: no ipcRenderer, no Node.

import { contextBridge, ipcRenderer, type IpcRendererEvent } from 'electron'
import type { EngineState } from '@shared/engine'
import { INITIAL_STATE_ARG, IPC, type InitialShellState, type OntologicalApi, type ShellCommand } from '@shared/ipc'

function readInitialState(): InitialShellState {
  const arg = process.argv.find((a) => a.startsWith(INITIAL_STATE_ARG))
  const fallback: InitialShellState = { screen: 'ask', theme: 'system' }
  if (!arg) return fallback
  try {
    return { ...fallback, ...(JSON.parse(arg.slice(INITIAL_STATE_ARG.length)) as Partial<InitialShellState>) }
  } catch {
    return fallback
  }
}

const api: OntologicalApi = {
  platform: process.platform,
  initial: readInitialState(),
  setScreen: (screen) => ipcRenderer.send(IPC.setScreen, screen),
  setTheme: (theme) => ipcRenderer.invoke(IPC.setTheme, theme),
  onCommand: (listener) => {
    const handler = (_event: IpcRendererEvent, command: ShellCommand): void => listener(command)
    ipcRenderer.on(IPC.command, handler)
    return () => ipcRenderer.removeListener(IPC.command, handler)
  },
  openFile: (filters) => ipcRenderer.invoke(IPC.openFile, filters),
  saveFile: (name) => ipcRenderer.invoke(IPC.saveFile, name),
  showContextMenu: (items) => ipcRenderer.invoke(IPC.showContextMenu, items),
  notify: (title, body) => ipcRenderer.invoke(IPC.notify, title, body),
  engine: () => ipcRenderer.invoke(IPC.engine),
  onEngine: (listener) => {
    const handler = (_event: IpcRendererEvent, state: EngineState): void => listener(state)
    ipcRenderer.on(IPC.engineState, handler)
    return () => ipcRenderer.removeListener(IPC.engineState, handler)
  }
}

contextBridge.exposeInMainWorld('ontological', api)
