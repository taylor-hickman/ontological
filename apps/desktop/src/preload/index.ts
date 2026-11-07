// The only bridge between the renderer and main (DESIGN.md §3.4). The renderer gets these
// functions and nothing else: no ipcRenderer, no Node.

import { contextBridge, ipcRenderer, webUtils, type IpcRendererEvent } from 'electron'
import type { EngineState } from '@shared/engine'
import { IPC, type InitialShellState, type OntologicalApi, type ShellCommand } from '@shared/ipc'

/** Main's screen and theme as they are now, so a reload doesn't fall back to the launch-time ones. */
function readInitialState(): InitialShellState {
  const fallback: InitialShellState = { screen: 'ask', theme: 'system', setupClosed: false }
  try {
    const state = ipcRenderer.sendSync(IPC.initialState) as InitialShellState | null
    return state ? { ...fallback, ...state } : fallback
  } catch {
    return fallback
  }
}

const api: OntologicalApi = {
  platform: process.platform,
  initial: readInitialState(),
  setScreen: (screen) => ipcRenderer.send(IPC.setScreen, screen),
  setTheme: (theme) => ipcRenderer.invoke(IPC.setTheme, theme),
  closeSetup: () => ipcRenderer.send(IPC.closeSetup),
  onCommand: (listener) => {
    const handler = (_event: IpcRendererEvent, command: ShellCommand): void => listener(command)
    ipcRenderer.on(IPC.command, handler)
    return () => ipcRenderer.removeListener(IPC.command, handler)
  },
  openFile: (filters) => ipcRenderer.invoke(IPC.openFile, filters),
  pathForFile: (file) => webUtils.getPathForFile(file),
  saveFile: (name, data) => ipcRenderer.invoke(IPC.saveFile, name, data),
  openTextFile: (filters) => ipcRenderer.invoke(IPC.openTextFile, filters),
  showContextMenu: (items) => ipcRenderer.invoke(IPC.showContextMenu, items),
  notify: (title, body) => ipcRenderer.invoke(IPC.notify, title, body),
  engine: () => ipcRenderer.invoke(IPC.engine),
  onEngine: (listener) => {
    const handler = (_event: IpcRendererEvent, state: EngineState): void => listener(state)
    ipcRenderer.on(IPC.engineState, handler)
    return () => ipcRenderer.removeListener(IPC.engineState, handler)
  },
  reconnectEngine: () => ipcRenderer.invoke(IPC.reconnectEngine)
}

contextBridge.exposeInMainWorld('ontological', api)
