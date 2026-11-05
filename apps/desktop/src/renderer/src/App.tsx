import { useEffect, useRef, useState } from 'react'
import type { ScreenId, ThemeSource } from '@shared/shell'
import { Header } from './components/Header'
import { Rail } from './components/Rail'
import { ScreenBody } from './components/ScreenBody'
import { Sidebar } from './components/Sidebar'
import { StatusBar } from './components/StatusBar'
import { FRAMES } from './screens'
import { applyTheme } from './theme'
import { useMediaQuery } from './useMediaQuery'

/** Below this window width the right rail becomes a sheet over the main column (DESIGN.md §3.3). */
const SHEET_QUERY = '(max-width: 1099px)'

export function App(): React.JSX.Element {
  const api = window.ontological
  const [screen, setScreen] = useState<ScreenId>(api.initial.screen)
  const [theme, setTheme] = useState<ThemeSource>(api.initial.theme)
  const [railOpen, setRailOpen] = useState(true)
  const [sheetOpen, setSheetOpen] = useState(false)
  const narrow = useMediaQuery(SHEET_QUERY)
  const searchRef = useRef<HTMLInputElement>(null)
  const narrowRef = useRef(narrow)
  narrowRef.current = narrow

  const changeTheme = (next: ThemeSource): void => {
    setTheme(next)
    applyTheme(next)
  }

  const navigate = (next: ScreenId): void => {
    setScreen(next)
    setSheetOpen(false)
    api.setScreen(next)
  }

  useEffect(
    () =>
      api.onCommand((command) => {
        switch (command.type) {
          case 'navigate':
            navigate(command.screen)
            break
          case 'new-question':
            navigate('ask')
            break
          case 'focus-search':
            searchRef.current?.focus()
            break
          case 'toggle-rail':
            if (narrowRef.current) setSheetOpen((open) => !open)
            else setRailOpen((open) => !open)
            break
          case 'theme':
            changeTheme(command.theme)
            break
        }
      }),
    // Subscribe once: navigate and changeTheme only call state setters and the stable api.
    []
  )

  const frame = FRAMES[screen]
  const showRail = frame.rail !== null && (narrow ? sheetOpen : railOpen)

  return (
    <div className="flex h-full flex-col" data-screen={screen}>
      <div className="flex min-h-0 flex-1">
        <Sidebar screen={screen} onNavigate={navigate} />
        <div className="flex min-w-0 flex-1 flex-col">
          <Header title={frame.title} screen={screen} searchRef={searchRef} />
          <div className="relative flex min-h-0 flex-1 flex-wrap overflow-auto">
            <ScreenBody
              screen={screen}
              frame={frame}
              theme={theme}
              onThemeChange={(next) => {
                changeTheme(next)
                void api.setTheme(next)
              }}
            />
            {showRail && frame.rail !== null && (
              <Rail label={frame.rail} sheet={narrow} onClose={() => setSheetOpen(false)} />
            )}
          </div>
        </div>
      </div>
      <StatusBar />
    </div>
  )
}
