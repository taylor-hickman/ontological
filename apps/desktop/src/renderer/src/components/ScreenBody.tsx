import type { ScreenId, ThemeSource } from '@shared/shell'
import type { ScreenFrame } from '../screens'
import { EngineBanner } from './EngineBanner'
import { Settings } from './Settings'

interface ScreenBodyProps {
  screen: ScreenId
  frame: ScreenFrame
  theme: ThemeSource
  onThemeChange(theme: ThemeSource): void
}

/** The main column. Until the engine exists, each screen is an empty frame that says what will go here. */
export function ScreenBody({ screen, frame, theme, onThemeChange }: ScreenBodyProps) {
  return (
    <section
      aria-label={frame.main}
      data-testid="main-column"
      className={`flex min-w-0 flex-[999_1_520px] flex-col gap-5 px-9 pt-7 pb-6 ${screen === 'ask' ? 'max-w-[860px]' : ''}`}
    >
      <EngineBanner />
      {screen === 'settings' ? (
        <Settings theme={theme} onThemeChange={onThemeChange} />
      ) : (
        <div className="flex flex-col gap-1 rounded-xl border border-dashed border-text-faint px-6 py-8">
          <p className="m-0 font-medium">{frame.placeholder}</p>
          <p className="m-0 text-[13px] text-text-muted">Nothing to show until the engine is running.</p>
        </div>
      )}
    </section>
  )
}
