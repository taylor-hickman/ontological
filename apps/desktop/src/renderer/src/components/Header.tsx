import type { RefObject } from 'react'
import type { ScreenId } from '@shared/shell'
import { shortcut } from '../shortcut'
import { PlusIcon, SearchIcon } from './icons'

interface HeaderProps {
  title: string
  screen: ScreenId
  searchRef: RefObject<HTMLInputElement | null>
  onAddSource(): void
}

/** DESIGN.md §3.3: breadcrumb and title on the left, search and the screen's one primary action on the right. */
export function Header({ title, screen, searchRef, onAddSource }: HeaderProps) {
  return (
    <header
      className="drag flex min-h-[52px] shrink-0 flex-wrap items-center gap-3 border-b border-border bg-bg px-5 py-2"
      data-testid="header"
    >
      <div className="flex min-w-0 flex-[1_1_240px] items-baseline gap-2">
        <h1 className="m-0 text-sm font-semibold">{title}</h1>
      </div>
      {screen !== 'settings' && (
        <label className="no-drag flex min-h-8 min-w-[280px] items-center gap-2 rounded-lg border border-border bg-surface px-2.5 text-text-muted focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-accent">
          <SearchIcon />
          <input
            ref={searchRef}
            type="search"
            aria-label="Search terms, tables and analyses"
            placeholder="Search terms, tables, analyses"
            className="min-w-0 flex-1 bg-transparent text-text outline-none placeholder:text-text-muted"
          />
          <kbd className="rounded border border-border px-[5px] font-sans text-[11px]">{shortcut('K')}</kbd>
        </label>
      )}
      {screen === 'ask' && (
        <button
          type="button"
          className="no-drag flex min-h-8 items-center gap-1.5 rounded-lg bg-accent-solid px-3 font-medium text-white"
        >
          <PlusIcon />
          New question
        </button>
      )}
      {screen === 'catalog' && (
        <button
          type="button"
          onClick={onAddSource}
          className="no-drag flex min-h-8 items-center gap-1.5 rounded-lg bg-accent-solid px-3 font-medium text-white"
        >
          <PlusIcon />
          Add source
        </button>
      )}
    </header>
  )
}
