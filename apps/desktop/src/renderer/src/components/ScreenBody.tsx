import type { ScreenId, ThemeSource } from '@shared/shell'
import type { ComponentProps } from 'react'
import type { InterchangeRequest } from '../interchange/ImportExport'
import { JobsMain, type JobsScreenState } from '../jobs/JobsScreen'
import type { ScreenFrame } from '../screens'
import { OntologyMain } from '../ontology/OntologyScreen'
import { TermsMain } from '../ontology/TermsScreen'
import type { OntologyState } from '../ontology/useOntology'
import { Catalog } from '../screens/Catalog'
import { EngineBanner } from './EngineBanner'
import { Settings } from './Settings'

interface ScreenBodyProps {
  screen: ScreenId
  frame: ScreenFrame
  theme: ThemeSource
  jobs: JobsScreenState
  onThemeChange(theme: ThemeSource): void
  catalog: ComponentProps<typeof Catalog>
  ontology: OntologyState
  onAddSource(): void
  onOpenJobs(): void
  interchange: InterchangeRequest | null
  onInterchangeHandled(): void
}

/** The main column. Until a screen is built, it is an empty frame that says what will go here. */
export function ScreenBody({
  screen,
  frame,
  theme,
  jobs,
  onThemeChange,
  catalog,
  ontology,
  onAddSource,
  onOpenJobs,
  interchange,
  onInterchangeHandled
}: ScreenBodyProps) {
  return (
    <section
      aria-label={frame.main}
      data-testid="main-column"
      className={`flex min-w-0 flex-[999_1_520px] flex-col ${screen === 'ontology' ? '' : 'gap-5 px-9 pt-7 pb-6'} ${screen === 'ask' ? 'max-w-[860px]' : ''}`}
    >
      {screen === 'ontology' ? (
        <div className="px-9 pt-5 empty:hidden">
          <EngineBanner />
        </div>
      ) : (
        <EngineBanner />
      )}
      {screen === 'settings' ? (
        <Settings
          theme={theme}
          onThemeChange={onThemeChange}
          onOpenJobs={onOpenJobs}
          sources={catalog.sources}
          jobs={jobs.jobs}
          interchange={interchange}
          onInterchangeHandled={onInterchangeHandled}
        />
      ) : screen === 'catalog' ? (
        <Catalog {...catalog} />
      ) : screen === 'ontology' ? (
        <OntologyMain state={ontology} onAddSource={onAddSource} onOpenJobs={onOpenJobs} />
      ) : screen === 'terms' ? (
        <TermsMain state={ontology} onAddSource={onAddSource} />
      ) : screen === 'jobs' ? (
        <JobsMain state={jobs} />
      ) : (
        <div className="flex flex-col gap-1 rounded-xl border border-dashed border-text-faint px-6 py-8">
          <p className="m-0 font-medium">{frame.placeholder}</p>
          <p className="m-0 text-[13px] text-text-muted">Nothing to show until the engine is running.</p>
        </div>
      )}
    </section>
  )
}
