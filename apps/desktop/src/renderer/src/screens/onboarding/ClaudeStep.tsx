import { useState } from 'react'
import type { ClaudeStatus, PingResult } from '@shared/api'
import { useEngineRequest, type Loaded } from '../../api/engine'
import { CheckIcon } from '../../components/icons'
import { Mono, Note, Problem, StepCard, StepHeading } from './Card'

export const INSTALL_URL = 'https://code.claude.com/docs/en/setup'

/** DESIGN.md §5.1 step 1: Installed (version), Logged in, Test call. Ontological never runs the
 * login flow (ARCHITECTURE.md §3, §6.5); it says which terminal steps do. */
export function ClaudeStep({ current, status }: { current: boolean; status: Loaded<ClaudeStatus> }) {
  const request = useEngineRequest()
  const [ping, setPing] = useState<PingResult | null>(null)
  const [pinging, setPinging] = useState(false)
  const claude = status.data

  const testCall = (): void => {
    if (request === null) return
    setPinging(true)
    request<PingResult>('/claude/test', { method: 'POST' })
      .then((got) => {
        setPing(got)
        status.reload()
      })
      .catch((e: unknown) => setPing({ ok: false, seconds: null, state: 'unknown', error: String(e) }))
      .finally(() => setPinging(false))
  }

  return (
    <StepCard current={current} label="Claude Code">
      <div className="flex items-center justify-between gap-2.5">
        <StepHeading
          current={current}
          title="Claude Code"
          intro="Ontological sends prompts to Claude through your own Claude Code login. Checking it uses no Claude usage."
        />
        {claude?.state === 'ready' && (
          <span className="rounded-full bg-verified-soft px-[9px] py-0.5 text-xs font-medium text-verified-text">Ready</span>
        )}
      </div>

      {claude === undefined ? (
        status.error ? (
          <Problem>Couldn’t check Claude Code: {status.error}</Problem>
        ) : (
          <div data-testid="claude-loading" className="h-[60px] rounded-lg bg-surface-sunken" aria-label="Checking Claude Code" />
        )
      ) : (
        <>
          <ul className="m-0 grid list-none grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-2.5 p-0" data-testid="claude-checks">
            <Check
              ok={claude.version !== null && claude.state !== 'outdated'}
              title={claude.version === null ? 'Not installed' : claude.state === 'outdated' ? 'Needs an update' : 'Installed'}
              detail={claude.version === null ? 'claude isn’t on your PATH' : <Mono>v{claude.version}</Mono>}
            />
            <Check
              ok={claude.logged_in === true}
              title={claude.logged_in === true ? 'Logged in' : claude.logged_in === false ? 'Not logged in' : 'Login unknown'}
              detail={claude.logged_in === true ? 'with your subscription' : claude.logged_in === false ? 'see the steps below' : 'run the test call'}
            />
            <Check
              ok={ping?.ok === true}
              title="Test call"
              detail={
                ping?.ok ? (
                  `Haiku replied in ${ping.seconds} s`
                ) : (
                  <button
                    type="button"
                    disabled={pinging || claude.version === null || request === null}
                    onClick={testCall}
                    className="min-h-7 self-start rounded-md border border-border bg-surface px-2 text-xs text-text disabled:opacity-60"
                  >
                    {pinging ? 'Calling Haiku…' : ping ? 'Try again' : 'Run test call'}
                  </button>
                )
              }
            />
          </ul>
          {!ping?.ok && claude.version !== null && (
            <p className="m-0 text-xs text-text-muted">The test call sends one short message to Haiku: a tiny amount of Claude usage.</p>
          )}
          {ping && !ping.ok && ping.error && <Problem>The test call didn’t get an answer: {ping.error}</Problem>}

          <Remedy claude={claude} onRecheck={status.reload} checking={status.loading} />

          {claude.ignored_variables.map((name) => (
            <Note key={name} testId="ignored-variable">
              <Mono>{name}</Mono> is set in your shell. Ontological ignores it, so every call uses your subscription and never
              bills API credits.
            </Note>
          ))}
        </>
      )}
    </StepCard>
  )
}

function Check({ ok, title, detail }: { ok: boolean; title: string; detail: React.ReactNode }) {
  return (
    <li data-ok={ok || undefined} className="flex flex-col gap-0.5 rounded-lg bg-surface-sunken px-3 py-2.5">
      <span className="flex items-center gap-1.5 font-medium">
        {ok ? (
          <span className="text-verified">
            <CheckIcon />
          </span>
        ) : (
          <span aria-hidden="true" className="size-[13px] rounded-full border-[1.5px] border-text-faint" />
        )}
        {title}
      </span>
      <span className="flex flex-col text-xs text-text-muted">{detail}</span>
    </li>
  )
}

/** What to do when Claude Code isn't ready: install it, update it, or log in from a terminal. */
function Remedy({ claude, onRecheck, checking }: { claude: ClaudeStatus; onRecheck(): void; checking: boolean }) {
  if (claude.state === 'ready') return null
  const recheck = (
    <button
      type="button"
      onClick={onRecheck}
      disabled={checking}
      className="min-h-9 self-start rounded-lg border border-border bg-surface px-3 font-medium text-text disabled:opacity-60"
    >
      {checking ? 'Checking…' : 'Check again'}
    </button>
  )
  if (claude.state === 'not_installed') {
    return (
      <div data-testid="claude-remedy" data-state={claude.state} className="flex flex-col gap-2.5">
        <p className="m-0">
          Ontological needs Claude Code, signed in with your own subscription.{' '}
          <a href={INSTALL_URL} target="_blank" rel="noreferrer" className="text-accent hover:text-accent-hover">
            Install Claude Code
          </a>
          , then check again.
        </p>
        {recheck}
      </div>
    )
  }
  if (claude.state === 'outdated') {
    return (
      <div data-testid="claude-remedy" data-state={claude.state} className="flex flex-col gap-2.5">
        <p className="m-0">
          Ontological needs Claude Code {claude.min_version} or later. Run <Mono>claude update</Mono> in a terminal, then check
          again.
        </p>
        {recheck}
      </div>
    )
  }
  if (claude.state === 'needs_login') {
    return (
      <div data-testid="claude-remedy" data-state={claude.state} className="flex flex-col gap-2.5">
        <p className="m-0">Log in to Claude Code from a terminal. Ontological never asks for your login itself.</p>
        <ol className="m-0 flex list-none flex-col gap-1.5 rounded-lg border border-border-subtle bg-surface-sunken p-3">
          <li>1. Open a terminal.</li>
          <li>
            2. Run <Mono>claude</Mono>.
          </li>
          <li>
            3. Type <Mono>/login</Mono> and follow its steps.
          </li>
        </ol>
        {recheck}
      </div>
    )
  }
  return (
    <div data-testid="claude-remedy" data-state={claude.state} className="flex flex-col gap-2.5">
      <p className="m-0">Claude Code didn’t say whether you’re logged in. The test call will tell.</p>
      {recheck}
    </div>
  )
}
