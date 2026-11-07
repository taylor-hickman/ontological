import type { ReactNode } from 'react'
import { CloseIcon } from './icons'

interface RailProps {
  label: string
  /** Below 1100 px the rail is a sheet over the main column (DESIGN.md §3.3). */
  sheet: boolean
  onClose(): void
  /** The rail's content; without it, the rail says nothing is selected. */
  children?: ReactNode
}

export function Rail({ label, sheet, onClose, children }: RailProps) {
  return (
    <aside
      aria-label={label}
      data-testid="rail"
      data-sheet={sheet || undefined}
      onKeyDown={sheet ? (e) => e.key === 'Escape' && onClose() : undefined}
      className={`flex min-w-0 flex-col gap-6 border-l border-border bg-rail px-[22px] py-6 ${
        sheet ? 'absolute inset-y-0 right-0 z-10 w-[340px] max-w-full overflow-y-auto shadow-[-8px_0_24px_rgba(20,24,33,0.08)]' : 'max-h-full flex-[1_1_300px] max-w-[360px] overflow-y-auto'
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <h2 className="m-0 text-xs font-semibold tracking-[0.06em] text-text-muted uppercase">{label}</h2>
        {sheet && (
          <button
            type="button"
            aria-label={`Close ${label.toLowerCase()}`}
            onClick={onClose}
            autoFocus
            className="-my-3 -mr-2 flex size-11 items-center justify-center rounded-lg text-text-muted hover:bg-nav-hover hover:text-text"
          >
            <CloseIcon />
          </button>
        )}
      </div>
      {children ?? <p className="m-0 text-[13px] text-text-muted">Nothing selected.</p>}
    </aside>
  )
}
