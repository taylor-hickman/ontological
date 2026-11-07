import { useEffect, useRef, useState } from 'react'
import type { Source } from '@shared/api'
import type { ScreenId, ThemeSource } from '@shared/shell'
import { useEngineGet } from './api/engine'
import { Header } from './components/Header'
import { Rail } from './components/Rail'
import { ScreenBody } from './components/ScreenBody'
import { Sidebar } from './components/Sidebar'
import { StatusBar } from './components/StatusBar'
import { JobsRail, useJobsScreen } from './jobs/JobsScreen'
import { useJobs } from './jobs/useJobs'
import { CertificationMeter, OntologyTools, SourcePicker } from './ontology/OntologyScreen'
import { TermEditor } from './ontology/TermEditor'
import { type OntologyState, useOntology } from './ontology/useOntology'
import { FRAMES } from './screens'
import { TablePanel } from './screens/Catalog'
import { Onboarding, type Step } from './screens/onboarding/Onboarding'
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
  const jobs = useJobs()
  const jobsScreen = useJobsScreen(jobs.jobs, jobs.loaded, screen === 'jobs', jobs.refresh)
  const [sourcesKey, setSourcesKey] = useState(0)
  const sources = useEngineGet<Source[]>('/sources', sourcesKey)
  // Onboarding opens by itself while there are no sources, until the user leaves it; File → Add
  // source opens it at its source step.
  const [onboarding, setOnboarding] = useState<Step | null>(null)
  const [onboardingDismissed, setOnboardingDismissed] = useState(api.initial.setupClosed)
  const [catalogTable, setCatalogTable] = useState<string | null>(null)
  const noSources = sources.data?.length === 0
  const ontologyScreen = screen === 'ontology' || screen === 'terms' ? screen : null
  const ontologyBase = useOntology(sources.data, onboarding === null ? ontologyScreen : null)

  useEffect(() => {
    if (noSources && !onboardingDismissed) setOnboarding((step) => step ?? 'claude')
  }, [noSources, onboardingDismissed])

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
          case 'add-source':
            setOnboarding('source')
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

  if (onboarding !== null) {
    return (
      <div className="flex h-full flex-col">
        <div className="min-h-0 flex-1">
          <Onboarding
            start={onboarding}
            sources={sources.data ?? []}
            onSourcesChanged={() => setSourcesKey((n) => n + 1)}
            onDone={() => {
              setOnboarding(null)
              setOnboardingDismissed(true)
              api.closeSetup()
              if (sources.data?.length) navigate('catalog')
            }}
          />
        </div>
        <StatusBar jobs={jobs.jobs} onOpenJobs={() => navigate('jobs')} />
      </div>
    )
  }

  const openRail = (): void => {
    if (narrowRef.current) setSheetOpen(true)
    else setRailOpen(true)
  }

  const selectTable = (id: string): void => {
    setCatalogTable(id)
    openRail()
  }

  // Selecting a Term opens the term editor in the rail, as selecting a table does on Catalog.
  const ontology: OntologyState = {
    ...ontologyBase,
    selectTerm: (id) => {
      ontologyBase.selectTerm(id)
      if (id !== null) openRail()
    }
  }
  const glossary = ontology.glossary.data

  return (
    <div className="flex h-full flex-col" data-screen={screen}>
      <div className="flex min-h-0 flex-1">
        <Sidebar
          screen={screen}
          sources={sources.data}
          jobs={jobs.jobs}
          onNavigate={navigate}
          onAddSource={() => setOnboarding('source')}
          context={
            screen === 'ontology' && glossary && glossary.source_id === ontology.source?.id ? (
              <CertificationMeter certified={glossary.certified} total={glossary.total} />
            ) : null
          }
        />
        <div className="flex min-w-0 flex-1 flex-col">
          <Header
            title={frame.title}
            screen={screen}
            searchRef={searchRef}
            onAddSource={() => setOnboarding('source')}
            tools={
              screen === 'ontology' && ontology.source ? (
                <OntologyTools state={ontology} />
              ) : screen === 'terms' ? (
                <SourcePicker state={ontology} />
              ) : undefined
            }
          />
          <div className="relative flex min-h-0 flex-1 flex-wrap overflow-auto">
            <ScreenBody
              screen={screen}
              frame={frame}
              theme={theme}
              jobs={jobsScreen}
              onThemeChange={(next) => {
                changeTheme(next)
                void api.setTheme(next)
              }}
              catalog={{
                sources: sources.data,
                sourcesError: sources.error,
                selected: catalogTable,
                onSelect: selectTable,
                onAddSource: () => setOnboarding('source')
              }}
              ontology={ontology}
              onAddSource={() => setOnboarding('source')}
              onOpenJobs={() => navigate('jobs')}
            />
            {showRail && frame.rail !== null && (
              <Rail label={frame.rail} sheet={narrow} onClose={() => setSheetOpen(false)}>
                {screen === 'catalog' ? (
                  <TablePanel tableId={catalogTable} />
                ) : screen === 'jobs' ? (
                  <JobsRail state={jobsScreen} />
                ) : ontologyScreen ? (
                  <TermEditor termId={ontology.termId} onOpenTerm={ontology.selectTerm} onChanged={ontology.changed} />
                ) : undefined}
              </Rail>
            )}
          </div>
        </div>
      </div>
      <StatusBar jobs={jobs.jobs} onOpenJobs={() => navigate('jobs')} />
    </div>
  )
}
