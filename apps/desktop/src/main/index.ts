import { join } from 'node:path'
import { mkdirSync } from 'node:fs'
import {
  BrowserWindow,
  Menu,
  Notification,
  app,
  dialog,
  ipcMain,
  nativeTheme,
  screen,
  session,
  shell,
  type IpcMainEvent,
  type IpcMainInvokeEvent,
  type MenuItemConstructorOptions
} from 'electron'
import { INITIAL_STATE_ARG, IPC, type InitialShellState, type ShellCommand } from '@shared/ipc'
import { WINDOW_BACKGROUND, isScreenId, isThemeSource, type ThemeSource } from '@shared/shell'
import { buildMenuTemplate } from './menu'
import { parseContextMenuItems, parseFileFilters, parseFileName, parseNotification } from './validate'
import { MIN_SIZE, StateFile, fitBounds, type ShellState } from './window-state'

const isMac = process.platform === 'darwin'
const isDev = !app.isPackaged && Boolean(process.env.ELECTRON_RENDERER_URL)

// Tests point this at a temporary folder, so each run starts from a clean state.
if (process.env.ONTOLOGICAL_USER_DATA) app.setPath('userData', process.env.ONTOLOGICAL_USER_DATA)

/**
 * Centres the traffic lights on the 52 px top bar (DESIGN.md §3.1). Measured on macOS 26, where
 * the buttons are 14 pt circles: y 19 puts their centre at 26 pt.
 */
const TRAFFIC_LIGHTS = { x: 20, y: 19 }
const LOGIN_HELP_URL = 'https://code.claude.com/docs/en/authentication'

const stateFile = new StateFile(join(app.getPath('userData'), 'shell-state.json'))
let state: ShellState = stateFile.load()
let mainWindow: BrowserWindow | null = null
let saveTimer: NodeJS.Timeout | undefined

function saveState(): void {
  clearTimeout(saveTimer)
  saveTimer = undefined
  try {
    stateFile.save(state)
  } catch (err) {
    console.error('Could not save the window state:', err)
  }
}

function saveStateSoon(): void {
  clearTimeout(saveTimer)
  saveTimer = setTimeout(saveState, 400)
}

function currentBackground(): string {
  return nativeTheme.shouldUseDarkColors ? WINDOW_BACKGROUND.dark : WINDOW_BACKGROUND.light
}

function send(command: ShellCommand): void {
  if (!mainWindow) {
    // On macOS the menu outlives the window. A new window reads its first screen from the
    // initial state, so a navigate goes there instead.
    if (command.type === 'navigate') state.screen = command.screen
    createWindow()
    return
  }
  mainWindow.webContents.send(IPC.command, command)
}

function setTheme(theme: ThemeSource): void {
  state.theme = theme
  nativeTheme.themeSource = theme
  saveState()
  installMenu()
  send({ type: 'theme', theme })
}

function installMenu(): void {
  const template = buildMenuTemplate({
    isMac,
    isDev,
    theme: state.theme,
    actions: {
      send,
      setTheme,
      openLoginHelp: () => void shell.openExternal(LOGIN_HELP_URL),
      openLogsFolder: () => {
        const logs = app.getPath('logs')
        mkdirSync(logs, { recursive: true })
        void shell.openPath(logs)
      }
    }
  })
  Menu.setApplicationMenu(Menu.buildFromTemplate(template))
}

function createWindow(): void {
  const bounds = fitBounds(
    state.bounds,
    screen.getAllDisplays().map((d) => d.workArea)
  )
  const initial: InitialShellState = { screen: state.screen, theme: state.theme }

  const win = new BrowserWindow({
    ...bounds,
    minWidth: MIN_SIZE.width,
    minHeight: MIN_SIZE.height,
    title: 'Ontological',
    show: false,
    backgroundColor: currentBackground(),
    ...(isMac ? { titleBarStyle: 'hiddenInset' as const, trafficLightPosition: TRAFFIC_LIGHTS } : {}),
    webPreferences: {
      preload: join(import.meta.dirname, '../preload/index.cjs'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      additionalArguments: [INITIAL_STATE_ARG + JSON.stringify(initial)]
    }
  })
  mainWindow = win
  if (state.maximized) win.maximize()

  win.once('ready-to-show', () => win.show())

  const recordBounds = (): void => {
    if (win.isDestroyed()) return
    state.maximized = win.isMaximized()
    if (!state.maximized && !win.isFullScreen()) state.bounds = win.getNormalBounds()
    saveStateSoon()
  }
  win.on('resize', recordBounds)
  win.on('move', recordBounds)
  win.on('maximize', recordBounds)
  win.on('unmaximize', recordBounds)
  win.on('close', () => {
    recordBounds()
    saveState()
  })
  win.on('closed', () => {
    if (mainWindow === win) mainWindow = null
  })

  // The renderer never navigates away or opens windows. Web links go to the browser.
  win.webContents.on('will-navigate', (event) => event.preventDefault())
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('https://')) void shell.openExternal(url)
    return { action: 'deny' }
  })

  if (isDev && process.env.ELECTRON_RENDERER_URL) {
    void win.loadURL(process.env.ELECTRON_RENDERER_URL)
  } else {
    void win.loadFile(join(import.meta.dirname, '../renderer/index.html'))
  }
}

function isMainWindowSender(event: IpcMainEvent | IpcMainInvokeEvent): boolean {
  const win = mainWindow
  return !!win && event.sender === win.webContents && event.senderFrame === win.webContents.mainFrame
}

/** Only the main window's top frame may call into main. A rejected invoke reaches the caller. */
function fromMainWindow(event: IpcMainInvokeEvent): BrowserWindow {
  if (!isMainWindowSender(event) || !mainWindow) throw new Error('IPC from an unknown sender')
  return mainWindow
}

function registerIpc(): void {
  // A one-way message has no caller to reject to, so a bad one is dropped.
  ipcMain.on(IPC.setScreen, (event, screenId: unknown) => {
    if (!isMainWindowSender(event) || !isScreenId(screenId)) return
    state.screen = screenId
    saveStateSoon()
  })

  ipcMain.handle(IPC.setTheme, (event, theme: unknown) => {
    fromMainWindow(event)
    if (!isThemeSource(theme)) throw new TypeError('theme must be system, light or dark')
    setTheme(theme)
  })

  ipcMain.handle(IPC.openFile, async (event, filters: unknown) => {
    const win = fromMainWindow(event)
    const result = await dialog.showOpenDialog(win, { properties: ['openFile'], filters: parseFileFilters(filters) })
    return result.canceled ? null : (result.filePaths[0] ?? null)
  })

  ipcMain.handle(IPC.saveFile, async (event, name: unknown) => {
    const win = fromMainWindow(event)
    const result = await dialog.showSaveDialog(win, { defaultPath: parseFileName(name) })
    return result.canceled ? null : (result.filePath ?? null)
  })

  ipcMain.handle(IPC.showContextMenu, (event, items: unknown) => {
    const win = fromMainWindow(event)
    const parsed = parseContextMenuItems(items)
    return new Promise<string | null>((resolve) => {
      let picked: string | null = null
      const template = parsed.map(
        (item): MenuItemConstructorOptions =>
          item.type === 'separator'
            ? { type: 'separator' }
            : { label: item.label, enabled: item.enabled !== false, click: () => (picked = item.id) }
      )
      // The click handler runs before the menu's callback, so `picked` is set by then.
      Menu.buildFromTemplate(template).popup({ window: win, callback: () => resolve(picked) })
    })
  })

  ipcMain.handle(IPC.notify, (event, title: unknown, body: unknown) => {
    const win = fromMainWindow(event)
    const n = parseNotification(title, body)
    // DESIGN.md §3.4: notify only when the window is not focused.
    if (win.isFocused() || !Notification.isSupported()) return false
    new Notification({ title: n.title, body: n.body }).show()
    return true
  })
}

app.whenReady().then(() => {
  nativeTheme.themeSource = state.theme
  nativeTheme.on('updated', () => mainWindow?.setBackgroundColor(currentBackground()))

  // The renderer asks for no permissions; notifications come from main.
  session.defaultSession.setPermissionRequestHandler((_wc, _permission, callback) => callback(false))
  session.defaultSession.setPermissionCheckHandler(() => false)

  registerIpc()
  installMenu()
  createWindow()

  app.on('activate', () => {
    if (!mainWindow) createWindow()
  })
})

// On macOS, closing the window keeps the app running. Quitting never waits on a dialog.
app.on('window-all-closed', () => {
  if (!isMac) app.quit()
})

app.on('before-quit', saveState)
