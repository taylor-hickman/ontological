import { useState } from 'react'
import type { Added, ClaudeStatus, Source } from '@shared/api'
import { useEngineGet } from '../../api/engine'
import { ArrowRightIcon, CheckIcon } from '../../components/icons'
import { EngineBanner } from '../../components/EngineBanner'
import { plural } from '../../format'
import { StepCard, StepHeading } from './Card'
import { ClaudeStep } from './ClaudeStep'
import { ScopeStep, type Excluded } from './ScopeStep'
import { SourceStep } from './SourceStep'

export const STEPS = ['claude', 'source', 'scope', 'compile'] as const
export type Step = (typeof STEPS)[number]

interface OnboardingProps {
  /** Where to open: `source` for File → Add source. */
  start: Step
  sources: Source[]
  onSourcesChanged(): void
  /** Leaves onboarding for the app; `catalog` once a source is in. */
  onDone(): void
}

/**
 * DESIGN.md §5.1. A dark step rail on the left; the main column shows finished steps as compact
 * summaries and the current one as a raised card. Adding a source costs no Claude usage; only
 * the compile in step 4 does, and the scope and its estimate come first.
 */
export function Onboarding({ start, sources, onSourcesChanged, onDone }: OnboardingProps) {
  const [step, setStep] = useState<Step>(start)
  const [readTimes, setReadTimes] = useState<Record<string, number>>({})
  const [excluded, setExcluded] = useState<Excluded>(new Set())
  const claude = useEngineGet<ClaudeStatus>('/claude')
  const index = STEPS.indexOf(step)

  const onAdded = (added: Added): void => {
    setReadTimes((times) => ({ ...times, [added.source.id]: added.seconds }))
    onSourcesChanged()
  }

  const canContinue = step !== 'source' || sources.length > 0
  const next = (): void => {
    if (index < STEPS.length - 1) setStep(STEPS[index + 1]!)
    else onDone()
  }

  return (
    <div data-testid="onboarding" data-step={step} className="flex h-full flex-wrap overflow-auto bg-bg">
      <StepRail step={step} claude={claude.data} sources={sources} />
      <main className="flex min-w-0 flex-[999_1_560px] flex-col">
        <div className="drag h-[52px] shrink-0" />
        <div className="mx-auto box-border flex w-full max-w-[720px] flex-col gap-[22px] px-8 pt-6 pb-12">
          <EngineBanner />
          {index >= 0 && <ClaudeStep current={step === 'claude'} status={claude} />}
          {index >= 1 && (
            <SourceStep
              current={step === 'source'}
              sources={sources}
              readTimes={readTimes}
              onAdded={onAdded}
              onRemoved={onSourcesChanged}
            />
          )}
          {index >= 2 && <ScopeStep current={step === 'scope'} sources={sources} excluded={excluded} onChange={setExcluded} />}
          {index >= 3 && <CompileStep sources={sources} excluded={excluded} />}

          <div className="flex flex-wrap items-center gap-2.5">
            {index > 0 && (
              <button
                type="button"
                onClick={() => setStep(STEPS[index - 1]!)}
                className="min-h-10 rounded-lg px-3.5 text-text-2 hover:bg-nav-hover"
              >
                Back
              </button>
            )}
            <span className="flex-1" />
            {step !== 'compile' && (
              <button
                type="button"
                onClick={onDone}
                className="min-h-10 rounded-lg px-3.5 text-text-2 hover:bg-nav-hover"
              >
                {sources.length ? 'Close setup' : 'Set up later'}
              </button>
            )}
            <button
              type="button"
              onClick={next}
              disabled={!canContinue}
              className="flex min-h-10 items-center gap-2 rounded-lg bg-accent-solid px-4 font-medium text-white disabled:opacity-60"
            >
              {CONTINUE[step]}
              <ArrowRightIcon />
            </button>
          </div>
        </div>
      </main>
    </div>
  )
}

const CONTINUE: Record<Step, string> = {
  claude: 'Continue to sources',
  source: 'Continue to scope',
  scope: 'Continue',
  compile: 'Open the catalog'
}

function CompileStep({ sources, excluded }: { sources: Source[]; excluded: Excluded }) {
  const total = sources.reduce((n, s) => n + s.tables, 0) - excluded.size
  return (
    <StepCard current label="Compile and ask">
      <StepHeading
        current
        title="Compile and ask"
        intro="Compiling writes descriptions, terms and joins for each table with Claude. It runs in the background, and questions work on whatever has compiled so far."
      />
      <p className="m-0 tabular-nums">
        {plural(total, 'table')} from {plural(sources.length, 'source')} chosen.
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          disabled
          aria-describedby="compile-unavailable"
          className="min-h-10 rounded-lg bg-accent-solid px-4 font-medium text-white opacity-60"
        >
          Start compile
        </button>
        <span id="compile-unavailable" className="text-[13px] text-text-muted">
          Starting a compile from here isn’t available in this build yet. The structure is already in the catalog.
        </span>
      </div>
    </StepCard>
  )
}

const RAIL_STEPS: Record<Step, { title: string; hint: string }> = {
  claude: { title: 'Connect Claude Code', hint: 'Your own login' },
  source: { title: 'Add a source', hint: 'A file or a database, read-only' },
  scope: { title: 'Choose scope', hint: 'See the cost before anything runs' },
  compile: { title: 'Compile and ask', hint: 'Runs in the background' }
}

function StepRail({ step, claude, sources }: { step: Step; claude: ClaudeStatus | undefined; sources: Source[] }) {
  const index = STEPS.indexOf(step)
  const hint = (s: Step, i: number): string => {
    if (s === 'claude' && claude && claude.state !== 'ready') {
      return { not_installed: 'Not installed', outdated: 'Needs an update', needs_login: 'Needs login', unknown: 'Login not checked' }[
        claude.state
      ]
    }
    if (s === 'source' && i < index) return plural(sources.length, 'source')
    return i < index ? 'Done' : RAIL_STEPS[s].hint
  }
  return (
    <aside className="flex flex-[1_1_320px] flex-col bg-onboard-rail px-8 pb-8 text-onboard-text-2">
      <div className="drag h-[52px] shrink-0" />
      <div className="flex items-center gap-3 pt-9 pb-3">
        <svg width="34" height="34" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M12 5.5L5.5 17.5h13z" fill="none" stroke="var(--onboard-accent)" strokeWidth="1.4" />
          <circle cx="12" cy="5.5" r="2.6" fill="var(--onboard-accent)" />
          <circle cx="5.5" cy="17.5" r="2.6" fill="var(--onboard-accent)" />
          <circle cx="18.5" cy="17.5" r="2.6" fill="var(--onboard-text)" />
        </svg>
        <span className="text-xl font-semibold tracking-[-0.01em] text-onboard-text">Ontological</span>
      </div>
      <p className="m-0 mb-10 max-w-[300px] text-[15px] text-onboard-muted">
        Map what your data means, then ask it questions and get SQL you can check.
      </p>
      <ol aria-label="Setup steps" className="m-0 flex list-none flex-col gap-1 p-0">
        {STEPS.map((s, i) => {
          const done = i < index
          const current = i === index
          return (
            <li key={s} aria-current={current ? 'step' : undefined} className="flex gap-3.5 py-3">
              <span
                aria-hidden="true"
                className={`grid size-7 shrink-0 place-items-center rounded-full text-[13px] ${
                  done
                    ? 'bg-onboard-done text-onboard-text'
                    : current
                      ? 'bg-onboard-text font-semibold text-onboard-rail'
                      : 'border-[1.5px] border-onboard-line text-onboard-muted'
                }`}
              >
                {done ? <CheckIcon size={14} /> : i + 1}
              </span>
              <span className="flex flex-col">
                <span className={current ? 'font-semibold text-onboard-text' : done ? 'font-medium text-onboard-text' : 'font-medium'}>
                  {RAIL_STEPS[s].title}
                </span>
                <span className="text-[13px] text-onboard-muted">{hint(s, i)}</span>
              </span>
            </li>
          )
        })}
      </ol>
      <div className="min-h-10 flex-1" />
      <p className="m-0 max-w-[300px] text-[13px] text-onboard-muted">
        Your data stays on this Mac. The only thing that leaves it is the text of prompts sent to Claude through your own Claude
        Code login.
      </p>
    </aside>
  )
}
