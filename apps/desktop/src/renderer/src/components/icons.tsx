// Line icons from the mockups (docs/design/mockups/), 24-unit viewBox, stroked in currentColor.

import type { NavScreenId } from '@shared/shell'

type IconProps = { size?: number }

function Svg({ size = 16, children, strokeWidth = 1.6 }: IconProps & { children: React.ReactNode; strokeWidth?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      aria-hidden="true"
    >
      {children}
    </svg>
  )
}

const DATABASE = (
  <>
    <ellipse cx="12" cy="6" rx="7" ry="3" />
    <path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6" />
    <path d="M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3" />
  </>
)

const SCREEN_PATHS: Record<NavScreenId, React.ReactNode> = {
  ask: <path d="M4 5h16v11H9l-5 4z" />,
  ontology: (
    <>
      <circle cx="6" cy="6" r="2.5" />
      <circle cx="18" cy="9" r="2.5" />
      <circle cx="10" cy="18" r="2.5" />
      <path d="M8.3 7l7.3 1.4M16.6 11l-5.2 5M7 8.4l2.3 7.2" />
    </>
  ),
  terms: (
    <>
      <path d="M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3z" />
      <path d="M5 17a3 3 0 0 1 3-3h11" />
    </>
  ),
  catalog: DATABASE,
  analyses: <path d="M7 4h10v16l-5-4-5 4z" />,
  jobs: <path d="M3 12h4l3-7 4 14 3-7h4" />
}

export function ScreenIcon({ screen }: { screen: NavScreenId }) {
  return <Svg>{SCREEN_PATHS[screen]}</Svg>
}

export function SettingsIcon() {
  return (
    <Svg>
      <path d="M4 7h10M18 7h2M4 17h4M12 17h8" />
      <circle cx="16" cy="7" r="2" />
      <circle cx="10" cy="17" r="2" />
    </Svg>
  )
}

export function DatabaseIcon() {
  return <Svg>{DATABASE}</Svg>
}

export function ChevronDownIcon() {
  return (
    <Svg size={14} strokeWidth={2}>
      <path d="M6 9l6 6 6-6" />
    </Svg>
  )
}

export function SearchIcon() {
  return (
    <Svg size={15} strokeWidth={1.8}>
      <circle cx="11" cy="11" r="6" />
      <path d="M20 20l-4.5-4.5" />
    </Svg>
  )
}

export function PlusIcon() {
  return (
    <Svg size={14} strokeWidth={2}>
      <path d="M12 5v14M5 12h14" />
    </Svg>
  )
}

export function CloseIcon() {
  return (
    <Svg size={16} strokeWidth={1.8}>
      <path d="M6 6l12 12M18 6L6 18" />
    </Svg>
  )
}

/** The wordmark's glyph: three nodes joined as a triangle. */
export function Logo() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M12 5.5L5.5 17.5h13z" fill="none" stroke="var(--accent)" strokeWidth="1.6" />
      <circle cx="12" cy="5.5" r="2.6" fill="var(--accent)" />
      <circle cx="5.5" cy="17.5" r="2.6" fill="var(--accent)" />
      <circle cx="18.5" cy="17.5" r="2.6" fill="var(--text)" />
    </svg>
  )
}

export function RestartIcon() {
  return (
    <Svg size={16} strokeWidth={2.2}>
      <path d="M20 12a8 8 0 1 1-2.3-5.6" />
      <path d="M20 4v5h-5" />
    </Svg>
  )
}

export function StopIcon() {
  return (
    <Svg size={16} strokeWidth={2.2}>
      <rect x="7" y="7" width="10" height="10" rx="1.5" />
    </Svg>
  )
}
