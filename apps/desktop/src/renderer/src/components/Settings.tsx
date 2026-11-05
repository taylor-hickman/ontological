import { THEME_SOURCES, type ThemeSource } from '@shared/shell'

const LABELS: Record<ThemeSource, string> = { system: 'System', light: 'Light', dark: 'Dark' }

/** Settings has one section so far: Appearance (DESIGN.md §3.5). The rest arrive with the engine. */
export function Settings({ theme, onThemeChange }: { theme: ThemeSource; onThemeChange(theme: ThemeSource): void }) {
  return (
    <div className="flex max-w-[640px] flex-col gap-6">
      <fieldset className="m-0 flex flex-col gap-3 rounded-xl border border-border bg-surface p-5">
        <legend className="float-left mb-1 p-0 text-[15px] font-semibold">Appearance</legend>
        <p className="clear-both m-0 text-[13px] text-text-muted">
          System follows your computer's setting. Native menus and dialogs match.
        </p>
        <div role="radiogroup" aria-label="Theme" className="flex flex-wrap gap-2">
          {THEME_SOURCES.map((t) => (
            <label
              key={t}
              className={`flex min-h-11 cursor-default items-center gap-2 rounded-lg border px-3.5 ${
                theme === t ? 'border-accent-border bg-accent-soft text-text' : 'border-border text-text-2'
              } has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-accent`}
            >
              <input
                type="radio"
                name="theme"
                value={t}
                checked={theme === t}
                onChange={() => onThemeChange(t)}
                className="accent-[var(--accent-solid)] focus-visible:outline-none"
              />
              {LABELS[t]}
            </label>
          ))}
        </div>
      </fieldset>
    </div>
  )
}
