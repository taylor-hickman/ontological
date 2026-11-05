/**
 * DESIGN.md §4.6. Always present, 28 px. Left: engine state, then Claude Code. Right: the main
 * background job, then the usage meter. With no engine yet, every part is in its "no data" state.
 */
export function StatusBar() {
  return (
    <footer
      data-testid="status-bar"
      className="flex h-7 shrink-0 items-center gap-[18px] overflow-hidden border-t border-border bg-chrome px-3.5 text-xs whitespace-nowrap text-text-2"
    >
      <span className="flex items-center gap-1.5">
        {/* A hollow dot: status never depends on color alone (DESIGN.md §2.1). */}
        <span aria-hidden="true" className="size-[7px] rounded-full border border-text-muted" />
        Engine not running
      </span>
      <span>Claude Code not checked</span>
      <span className="flex-1" />
      <span className="flex items-center gap-2">
        Usage
        <span
          role="meter"
          aria-label="Claude usage"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={0}
          aria-valuetext="No data yet"
          className="block h-1.5 w-[90px] overflow-hidden rounded-[3px] bg-queued"
        />
        <span>no data yet</span>
      </span>
    </footer>
  )
}
