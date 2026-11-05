import { resolve } from 'node:path'
import { defineConfig, externalizeDepsPlugin } from 'electron-vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// src/shared holds the types both sides of the IPC boundary build against.
const shared = { '@shared': resolve('src/shared') }

export default defineConfig({
  main: {
    resolve: { alias: shared },
    plugins: [externalizeDepsPlugin()]
  },
  preload: {
    resolve: { alias: shared },
    plugins: [externalizeDepsPlugin()],
    // A sandboxed preload can't load ES modules.
    build: { rollupOptions: { output: { format: 'cjs', entryFileNames: '[name].cjs' } } }
  },
  renderer: {
    resolve: { alias: { '@': resolve('src/renderer/src'), ...shared } },
    plugins: [react(), tailwindcss()]
  }
})
