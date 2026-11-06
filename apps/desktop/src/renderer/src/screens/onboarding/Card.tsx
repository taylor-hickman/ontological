import type { ReactNode } from 'react'
import { NoteIcon } from '../../components/icons'

/** A setup step: the current one is raised with a 2 px accent border, finished ones are compact
 * (DESIGN.md §5.1). */
export function StepCard({
  current,
  label,
  children
}: {
  current: boolean
  label: string
  children: ReactNode
}) {
  return (
    <section
      aria-label={label}
      data-current={current || undefined}
      className={
        current
          ? 'flex flex-col gap-[18px] rounded-[14px] border-2 border-accent-solid bg-surface px-[22px] pt-[22px] pb-5 shadow-step'
          : 'flex flex-col gap-3.5 rounded-[14px] border border-border bg-surface px-5 py-[18px]'
      }
    >
      {children}
    </section>
  )
}

export function StepHeading({ current, title, intro }: { current: boolean; title: string; intro?: ReactNode }) {
  return current ? (
    <div className="flex flex-col gap-1">
      <h1 className="m-0 text-[22px] font-semibold tracking-[-0.01em]">{title}</h1>
      {intro && <p className="m-0 text-text-2">{intro}</p>}
    </div>
  ) : (
    <h2 className="m-0 text-[15px] font-semibold">{title}</h2>
  )
}

/** An amber note: something the user should look at, never an error by itself (DESIGN.md §2.1). */
export function Note({ children, testId }: { children: ReactNode; testId?: string }) {
  return (
    <div
      data-testid={testId}
      className="flex gap-2.5 rounded-lg bg-attention-soft px-3 py-2.5 text-[13px] text-attention-text"
    >
      <span className="mt-px shrink-0 text-attention">
        <NoteIcon />
      </span>
      <span>{children}</span>
    </div>
  )
}

/** An error: text with an icon and a sentence, not red (DESIGN.md §2.1). */
export function Problem({ children }: { children: ReactNode }) {
  return (
    <p role="alert" className="m-0 flex gap-2 text-[13px] text-text">
      <span className="mt-px shrink-0 text-text-muted">
        <NoteIcon />
      </span>
      <span>{children}</span>
    </p>
  )
}

export function Mono({ children }: { children: ReactNode }) {
  return <span className="font-mono text-xs">{children}</span>
}
