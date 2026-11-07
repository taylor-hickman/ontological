import type { Source } from '@shared/api'
import { SCREENS, type ScreenId } from '@shared/shell'
import { jobsBadge } from '../jobs/format'
import type { JobSummary } from '../jobs/types'
import { shortcut } from '../shortcut'
import { ChevronDownIcon, DatabaseIcon, Logo, ScreenIcon, SettingsIcon } from './icons'

interface SidebarProps {
  screen: ScreenId
  sources: Source[] | undefined
  onNavigate(screen: ScreenId): void
  jobs: JobSummary[]
  onAddSource(): void
  /** The screen's context under the navigation: the certification meter on Ontology. */
  context?: React.ReactNode
}

function NavItem({
  current,
  label,
  hint,
  icon,
  badge,
  onClick
}: {
  current: boolean
  label: string
  hint: string
  icon: React.ReactNode
  badge?: { text: string; attention: boolean } | null
  onClick(): void
}) {
  return (
    <button
      type="button"
      aria-current={current ? 'page' : undefined}
      onClick={onClick}
      className={`flex min-h-11 w-full items-center gap-2.5 rounded-[7px] px-2.5 text-left ${
        current ? 'bg-surface font-medium text-text shadow-[0_1px_0_rgba(20,24,33,0.06)]' : 'text-text-2 hover:bg-nav-hover hover:text-text'
      }`}
    >
      {icon}
      <span className="flex-1">{label}</span>
      {badge ? (
        <span
          data-testid="nav-badge"
          className={`rounded-full px-1.5 py-px text-[11px] font-medium tabular-nums ${
            badge.attention ? 'bg-attention-soft text-attention' : 'bg-accent-soft text-accent'
          }`}
        >
          {badge.text}
        </span>
      ) : (
        <span className="text-[11px] text-text-muted">{hint}</span>
      )}
    </button>
  )
}

/** DESIGN.md §3.3: 232 px, window controls, wordmark, source switcher, navigation, screen context, Settings. */
export function Sidebar({ screen, sources, onNavigate, onAddSource, jobs, context }: SidebarProps) {
  const first = sources?.[0]
  const badge = jobsBadge(jobs)
  return (
    <nav aria-label="Main" className="flex w-[232px] shrink-0 flex-col border-r border-border bg-chrome px-2.5 pb-3">
      {/* The traffic lights sit in this 52 px band (titleBarStyle hiddenInset). */}
      <div className="drag h-[52px] shrink-0" data-testid="sidebar-top-bar" />
      <div className="flex items-center gap-2.5 px-2 pt-1 pb-3.5">
        <Logo />
        <span className="text-[15px] font-semibold tracking-[-0.01em]">Ontological</span>
      </div>
      {/* Switching between sources waits for Ask; for now it names them, or adds the first. */}
      <button
        type="button"
        data-testid="source-switcher"
        disabled={first !== undefined}
        onClick={onAddSource}
        className="mb-3.5 flex min-h-11 w-full items-center gap-2.5 rounded-lg border border-border bg-surface px-2.5 py-1.5 text-left text-text"
      >
        <span className="text-text-muted">
          <DatabaseIcon />
        </span>
        <span className="flex flex-1 flex-col leading-[1.2]">
          <span className="font-medium">{first ? first.name : 'No sources'}</span>
          <span className="text-xs text-text-muted">
            {first ? (sources!.length > 1 ? `and ${sources!.length - 1} more` : `${first.tables} tables`) : 'Add one to start'}
          </span>
        </span>
        <span className="text-text-muted">
          <ChevronDownIcon />
        </span>
      </button>
      <div className="flex flex-col gap-0.5">
        {SCREENS.map((s) => (
          <NavItem
            key={s.id}
            current={screen === s.id}
            label={s.label}
            hint={shortcut(s.key)}
            icon={<ScreenIcon screen={s.id} />}
            badge={s.id === 'jobs' ? badge : null}
            onClick={() => onNavigate(s.id)}
          />
        ))}
      </div>
      {/* Screen context goes here: Recent on Ask, the certification meter on Ontology. */}
      <div className="min-h-0 flex-1" data-testid="sidebar-context">
        {context}
      </div>
      <NavItem
        current={screen === 'settings'}
        label="Settings"
        hint={shortcut(',')}
        icon={<SettingsIcon />}
        onClick={() => onNavigate('settings')}
      />
    </nav>
  )
}
