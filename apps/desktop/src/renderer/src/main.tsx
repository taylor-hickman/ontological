import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import { applyTheme } from './theme'
import './styles.css'

// Before the first render, so the window never paints the wrong theme.
applyTheme(window.ontological.initial.theme)

const root = document.getElementById('root')
if (!root) throw new Error('#root is missing from index.html')
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>
)
