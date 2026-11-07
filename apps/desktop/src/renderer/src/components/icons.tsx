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

export function CheckIcon({ size = 13 }: IconProps) {
  return (
    <Svg size={size} strokeWidth={2.6}>
      <path d="M5 12.5l4.5 4.5L19 7" />
    </Svg>
  )
}

/** The round "look here" mark on amber notes. */
export function NoteIcon({ size = 16 }: IconProps) {
  return (
    <Svg size={size} strokeWidth={2}>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 8v5M12 16.5v.5" />
    </Svg>
  )
}

export function UploadIcon({ size = 28 }: IconProps) {
  return (
    <Svg size={size} strokeWidth={1.5}>
      <path d="M12 15V4M7 9l5-5 5 5" />
      <path d="M4 15v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4" />
    </Svg>
  )
}

export function FileIcon({ size = 18 }: IconProps) {
  return (
    <Svg size={size} strokeWidth={1.8}>
      <path d="M6 3h8l4 4v14H6z" />
      <path d="M14 3v4h4" />
    </Svg>
  )
}

export function LockIcon({ size = 13 }: IconProps) {
  return (
    <Svg size={size} strokeWidth={1.8}>
      <rect x="5" y="11" width="14" height="9" rx="2" />
      <path d="M8 11V8a4 4 0 0 1 8 0v3" />
    </Svg>
  )
}

export function ServerIcon({ size = 20 }: IconProps) {
  return <Svg size={size}>{DATABASE}</Svg>
}

export function ArrowRightIcon({ size = 14 }: IconProps) {
  return (
    <Svg size={size} strokeWidth={2}>
      <path d="M5 12h14M13 6l6 6-6 6" />
    </Svg>
  )
}

export function ChevronRightIcon({ size = 14 }: IconProps) {
  return (
    <Svg size={size} strokeWidth={2}>
      <path d="M9 6l6 6-6 6" />
    </Svg>
  )
}

export function TableIcon({ size = 14 }: IconProps) {
  return (
    <Svg size={size}>
      <rect x="4" y="5" width="16" height="14" rx="2" />
      <path d="M4 10h16M10 10v9" />
    </Svg>
  )
}

export function KeyIcon({ size = 12 }: IconProps) {
  return (
    <Svg size={size} strokeWidth={1.8}>
      <circle cx="8" cy="15" r="4" />
      <path d="M11 12l8-8M16 7l2 2" />
    </Svg>
  )
}
